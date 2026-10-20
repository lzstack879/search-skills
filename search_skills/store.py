'''SQLite storage for companies found by this skill.'''

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The seven report columns, in the order the weekly sheet uses them.
FIELDS = (
    'company_name',
    'company_website',
    'company_perple',
    'company_email',
    'industry',
    'summary',
    'match_result',
)

DEFAULT_DB = Path(__file__).resolve().parents[1] / 'data' / 'companies.db'

SCHEMA = '''
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    company_name TEXT NOT NULL DEFAULT '',
    company_website TEXT NOT NULL DEFAULT '',
    company_perple TEXT NOT NULL DEFAULT '',
    company_email TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    match_result TEXT NOT NULL DEFAULT '',
    match_score INTEGER NOT NULL DEFAULT 0,
    query TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_companies_created ON companies(created_at);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    query TEXT NOT NULL DEFAULT '',
    searched INTEGER NOT NULL DEFAULT 0,
    fetched INTEGER NOT NULL DEFAULT 0,
    stored INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    rows INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
'''


MIGRATIONS = (
    ("domain", "ALTER TABLE companies ADD COLUMN domain TEXT NOT NULL DEFAULT ''"),
    ("email_status", "ALTER TABLE companies ADD COLUMN email_status TEXT NOT NULL DEFAULT 'pending'"),
    ("email_source", "ALTER TABLE companies ADD COLUMN email_source TEXT NOT NULL DEFAULT ''"),
    ("email_kind", "ALTER TABLE companies ADD COLUMN email_kind TEXT NOT NULL DEFAULT ''"),
    ("email_attempts", "ALTER TABLE companies ADD COLUMN email_attempts INTEGER NOT NULL DEFAULT 0"),
    ("email_checked_at", "ALTER TABLE companies ADD COLUMN email_checked_at TEXT NOT NULL DEFAULT ''"),
    ("email_evidence", "ALTER TABLE companies ADD COLUMN email_evidence TEXT NOT NULL DEFAULT ''"),
    ("email_verdict", "ALTER TABLE companies ADD COLUMN email_verdict TEXT NOT NULL DEFAULT ''"),
    ("email_grade", "ALTER TABLE companies ADD COLUMN email_grade TEXT NOT NULL DEFAULT ''"),
)

EMAIL_TERMINAL = ('found', 'rejected', 'domain_dead', 'no_contact')


def host_of(url: str) -> str:
    from urllib.parse import urlparse

    try:
        host = (urlparse(url or '').hostname or '').lower()
    except ValueError:
        return ''
    return host[4:] if host.startswith('www.') else host


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')


def dedupe_key(name: str, website: str, email: str = '') -> str:
    '''Stable identity: website when present, else the name, else the email.'''
    website = (website or '').strip().lower()
    email = (email or '').strip().lower()
    name = ' '.join((name or '').strip().lower().split())
    if website:
        return 'web:' + website
    if name:
        return 'name:' + name
    return 'email:' + email


class Store:
    '''Thin sqlite3 wrapper; one connection per call keeps it thread-safe.'''

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> 'Store':
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute('PRAGMA table_info(companies)')}
            for name, statement in MIGRATIONS:
                if name not in columns:
                    conn.execute(statement)
            rows = conn.execute(
                "SELECT id, company_website FROM companies WHERE domain = '' AND company_website != ''"
            ).fetchall()
            for row in rows:
                conn.execute(
                    'UPDATE companies SET domain = ? WHERE id = ?',
                    (host_of(row[1]), row[0]),
                )
        return self

    def upsert(self, record: dict) -> str:
        '''Insert or refresh one company. Returns inserted/updated/skipped.'''
        name = str(record.get('company_name') or '').strip()
        website = str(record.get('company_website') or '').strip()
        email = str(record.get('company_email') or '').strip()
        if not name:
            return 'skipped'
        key = dedupe_key(name, website, email)
        now = _now()
        row = {
            'dedupe_key': key,
            'company_name': name,
            'company_website': website,
            'company_perple': str(record.get('company_perple') or '').strip(),
            'company_email': email,
            'industry': str(record.get('industry') or '').strip(),
            'summary': str(record.get('summary') or '').strip(),
            'match_result': str(record.get('match_result') or '').strip(),
            'match_score': int(record.get('match_score') or 0),
            'query': str(record.get('query') or ''),
            'source_url': str(record.get('source_url') or ''),
            'raw_json': json.dumps(record.get('raw') or {}, ensure_ascii=False)[:8000],
            'domain': str(record.get('domain') or host_of(website)),
            'email_status': str(record.get('email_status') or ('found' if email else 'pending')),
            'email_source': str(record.get('email_source') or ('extract' if email else '')),
            'email_kind': str(record.get('email_kind') or ''),
            'email_attempts': int(record.get('email_attempts') or 0),
            'email_checked_at': str(record.get('email_checked_at') or ''),
            'created_at': now,
            'updated_at': now,
        }
        with self._connect() as conn:
            existing = conn.execute(
                'SELECT id FROM companies WHERE dedupe_key = ?', (key,)
            ).fetchone()
            if existing:
                conn.execute(
                    '''UPDATE companies SET company_name=:company_name,
                       company_website=:company_website, company_perple=:company_perple,
                       company_email=:company_email, industry=:industry, summary=:summary,
                       match_result=:match_result, match_score=:match_score,
                       query=:query, source_url=:source_url, raw_json=:raw_json,
                       domain=:domain, email_status=:email_status, email_source=:email_source,
                       email_kind=:email_kind, email_attempts=:email_attempts,
                       email_checked_at=:email_checked_at,
                       updated_at=:updated_at WHERE dedupe_key=:dedupe_key''',
                    row,
                )
                return 'updated'
            conn.execute(
                '''INSERT INTO companies (dedupe_key, company_name, company_website,
                   company_perple, company_email, industry, summary, match_result,
                   match_score, query, source_url, raw_json, domain, email_status,
                   email_source, email_kind, email_attempts, email_checked_at,
                   created_at, updated_at)
                   VALUES (:dedupe_key, :company_name, :company_website, :company_perple,
                   :company_email, :industry, :summary, :match_result, :match_score,
                   :query, :source_url, :raw_json, :domain, :email_status,
                   :email_source, :email_kind, :email_attempts, :email_checked_at,
                   :created_at, :updated_at)''',
                row,
            )
        return 'inserted'

    def pending_email(self, limit: int = 50, retry_days: int = 7) -> list[dict]:
        '''Leads that still need an email: fresh ones plus old no_contact retries.'''
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
                  - timedelta(days=retry_days)).isoformat(timespec='seconds')
        with self._connect() as conn:
            rows = conn.execute(
                '''SELECT * FROM companies
                   WHERE domain != '' AND (
                     email_status = 'pending'
                     OR (email_status = 'no_contact' AND email_attempts < 3
                         AND (email_checked_at = '' OR email_checked_at < ?))
                     OR (company_email != '' AND email_verdict = '')
                   )
                   ORDER BY match_score DESC, id ASC LIMIT ?''',
                (cutoff, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def save_email(self, dedupe_key: str, **fields) -> None:
        '''Write one verification outcome back onto the lead.'''
        allowed = ('company_email', 'email_status', 'email_source', 'email_kind',
                   'email_attempts', 'email_checked_at', 'company_perple', 'email_evidence',
                   'email_verdict', 'email_grade')
        payload = {k: v for k, v in fields.items() if k in allowed}
        if not payload:
            return
        payload['email_checked_at'] = payload.get('email_checked_at') or _now()
        sets = ', '.join(key + '=' + ':' + key for key in payload)
        with self._connect() as conn:
            conn.execute(
                'UPDATE companies SET ' + sets + ', updated_at=:updated_at '
                'WHERE dedupe_key=:dedupe_key',
                {**payload, 'updated_at': _now(), 'dedupe_key': dedupe_key},
            )

    def get(self, dedupe_key: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM companies WHERE dedupe_key = ?', (dedupe_key,)
            ).fetchone()
        return dict(row) if row else None

    def agent_tasks(self, limit: int = 20, min_score: int = 55) -> list[dict]:
        '''Leads the script could not crack, worth an agent's eyes.'''
        with self._connect() as conn:
            rows = conn.execute(
                '''SELECT * FROM companies
                   WHERE email_status IN ('no_contact', 'needs_agent')
                     AND company_email = '' AND domain != '' AND match_score >= ?
                   ORDER BY match_score DESC, id ASC LIMIT ?''',
                (min_score, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_needs_agent(self, keys: list[str]) -> None:
        if not keys:
            return
        with self._connect() as conn:
            conn.executemany(
                "UPDATE companies SET email_status='needs_agent', updated_at=? WHERE dedupe_key=?",
                [(_now(), key) for key in keys],
            )

    def email_stats(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                'SELECT email_status, COUNT(*) FROM companies GROUP BY email_status'
            ).fetchall()
        return {str(r[0] or 'unknown'): int(r[1]) for r in rows}

    def list_between(self, since: str, until: str, limit: int = 5000) -> list[dict]:
        '''Rows whose created_at falls in [since, until).'''
        with self._connect() as conn:
            rows = conn.execute(
                '''SELECT * FROM companies WHERE created_at >= ? AND created_at < ?
                   ORDER BY match_score DESC, company_name ASC LIMIT ?''',
                (since, until, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_recent(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM companies ORDER BY id DESC LIMIT ?', (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def log_run(
        self,
        query: str = '',
        searched: int = 0,
        fetched: int = 0,
        stored: int = 0,
        note: str = '',
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                'INSERT INTO runs (started_at, query, searched, fetched, stored, note) VALUES (?,?,?,?,?,?)',
                (_now(), query, searched, fetched, stored, note[:500]),
            )

    def report_for(self, week_start: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                'SELECT path FROM reports WHERE week_start = ?', (week_start,)
            ).fetchone()
        return str(row['path']) if row else ''

    def record_report(self, week_start: str, path: str, rows: int) -> None:
        with self._connect() as conn:
            conn.execute(
                '''INSERT INTO reports (week_start, path, rows, created_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(week_start) DO UPDATE SET path=excluded.path,
                   rows=excluded.rows, created_at=excluded.created_at''',
                (week_start, path, rows, _now()),
            )

    def dedupe_keys(self) -> set:
        '''Every website key already stored, so a scrape never re-adds a company.'''
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT dedupe_key FROM companies WHERE dedupe_key LIKE 'web:%'"
            ).fetchall()
        return {str(r[0]).lower() for r in rows}

    def stats(self) -> dict:
        with self._connect() as conn:
            total = conn.execute('SELECT COUNT(*) FROM companies').fetchone()[0]
            runs = conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
            reports = conn.execute('SELECT COUNT(*) FROM reports').fetchone()[0]
        return {'companies': total, 'runs': runs, 'reports': reports}
