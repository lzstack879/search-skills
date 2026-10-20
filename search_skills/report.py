'''Weekly Excel report — one sheet per ISO week, named xxxx/xx/xx.'''

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .store import FIELDS, Store

REPORTS_DIR = Path(__file__).resolve().parents[1] / 'reports'

# Bilingual headers: the Chinese label is for the reader, the key is for the sheet.
HEADERS = {
    'company_name': '公司名称 company_name',
    'company_website': '公司网址 company_website',
    'company_perple': '联系人 company_perple',
    'company_email': '联系邮箱 company_email',
    'industry': '公司标签 industry',
    'summary': '公司简介 summary',
    'match_result': '匹配程度 match_result',
    'email_kind': '邮箱类型 email_kind',
    'email_verdict': '验证结果 email_verdict',
    'email_grade': '邮箱等级 email_grade',
}

WIDTHS = {
    'company_name': 30,
    'company_website': 34,
    'company_perple': 22,
    'company_email': 30,
    'industry': 20,
    'summary': 60,
    'match_result': 24,
    'email_kind': 14,
    'email_verdict': 18,
    'email_grade': 16,
}


def week_bounds(when: date | None = None) -> tuple[date, date]:
    '''Kept for callers that still ask for the ISO week containing a date.'''
    when = when or date.today()
    monday = when - timedelta(days=when.weekday())
    return monday, monday + timedelta(days=6)


def day_bounds(when: date | None = None) -> tuple[str, str]:
    '''The half-open window of one *local* day, expressed in UTC.

    created_at is stored as naive UTC, so a local day has to be shifted before
    it is compared against it -- otherwise a 00:30 local run lands on yesterday.
    '''
    when = when or date.today()
    start_local = datetime.combine(when, time.min).astimezone(timezone.utc).replace(tzinfo=None)
    end_local = (start_local + timedelta(days=1))
    return (start_local.isoformat(timespec='seconds'),
            end_local.isoformat(timespec='seconds'))


def report_path(day: date, root: Path | None = None) -> Path:
    '''reports/YYYY/MM/DD potential-customer report.xlsx (xxxx/xx/xx naming).'''
    root = root or REPORTS_DIR
    return root / str(day.year) / str(day.month).zfill(2) / (
        str(day.day).zfill(2) + '潜在客户搜索报表.xlsx'
    )


def _cell_text(value) -> str:
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def build_report(
    store: Store,
    when: date | None = None,
    root: Path | None = None,
) -> tuple[Path, int]:
    '''Write the sheet for the day containing `when`. Returns (path, rows).'''
    day = when or date.today()
    since, until = day_bounds(day)
    rows = store.list_between(since, until)

    book = Workbook()
    sheet = book.active
    sheet.title = '潜在客户 ' + day.isoformat()

    columns = list(FIELDS) + ['email_kind', 'email_verdict', 'email_grade']
    header_font = Font(bold=True, color='FFFFFF')
    header_fill = PatternFill('solid', fgColor='305496')
    for index, key in enumerate(columns, start=1):
        cell = sheet.cell(row=1, column=index, value=HEADERS[key])
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center', vertical='center')
        sheet.column_dimensions[get_column_letter(index)].width = WIDTHS[key]
    sheet.freeze_panes = 'A2'

    for offset, row in enumerate(rows, start=2):
        for index, key in enumerate(columns, start=1):
            cell = sheet.cell(row=offset, column=index, value=_cell_text(row.get(key)))
            cell.alignment = Alignment(vertical='top', wrap_text=key == 'summary')

    if rows:
        sheet.auto_filter.ref = 'A1:' + get_column_letter(len(columns)) + str(len(rows) + 1)

    path = report_path(day, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
    return path, len(rows)


def maybe_generate_daily(
    store: Store,
    when: date | None = None,
    root: Path | None = None,
    force: bool = False,
) -> tuple[Path, int, bool]:
    '''Generate today's report once. Returns (path, rows, created).'''
    day = when or date.today()
    existing = store.report_for(day.isoformat())
    if existing and not force:
        return Path(existing), 0, False
    path, rows = build_report(store, day, root)
    store.record_report(day.isoformat(), str(path), rows)
    return path, rows, True


def maybe_generate_weekly(store, when=None, root=None, force=False):
    '''Deprecated alias: the report is daily now.'''
    return maybe_generate_daily(store, when, root, force)


def describe(when: date | None = None) -> str:
    '''Human summary of the current reporting window.'''
    day = when or date.today()
    return 'day ' + day.isoformat()
