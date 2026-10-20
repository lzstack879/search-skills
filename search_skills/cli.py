'''Command line entry points for the company-search skill.'''

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

from .adaptive import RoundStats, analyze_and_adjust_icp, format_adjustment_report
from .client import DeepSeekClient, DeepSeekError, usage_summary
from .icp_builder import (
    ICP_PATH,
    TEMPLATE_PATH,
    ask_questions,
    fill_gaps,
    from_answers,
    from_site,
    read_icp,
    template,
    write_icp,
)
from .emailver import EmailVerifier
from .fetcher import Fetcher
from .leadgen import STOP_PATH, LeadGen, parse_seeds_csv
from .pipeline import ICP, Pipeline
from .report import build_report, day_bounds, maybe_generate_daily
from .seedpages import SeedPageReader, fetch_json, json_rows, write_csv
from .seeds import SeedBuilder
from .store import Store

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = BASE_DIR / 'data' / 'companies.db'
DEFAULT_ICP = BASE_DIR / 'data' / 'icp.json'


def _load_dotenv() -> None:
    '''Read .env when python-dotenv is installed; otherwise rely on the shell.'''
    try:
        from dotenv import load_dotenv  # type: ignore
    except Exception:
        return
    load_dotenv(BASE_DIR / '.env')


def load_icp(path: Path | None, args) -> ICP:
    '''Precedence: explicit flags, then the JSON file, then defaults.'''
    base = {}
    target = path or DEFAULT_ICP
    try:
        base = json.loads(Path(target).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        base = {}
    icp = ICP.from_dict(base)
    if args.industry:
        icp.industries = list(args.industry)
    if args.title:
        icp.titles = list(args.title)
    if args.geo:
        icp.geography = list(args.geo)
    if args.size:
        icp.company_size = args.size
    if args.product:
        icp.product = args.product
    return icp


def _print_records(records: list[dict]) -> None:
    if not records:
        print('  (no company stored this run)')
        return
    for item in records:
        print(
            '  ' + str(item.get('match_result', ''))[:22].ljust(24)
            + str(item.get('company_name', ''))[:28].ljust(30)
            + str(item.get('company_email', ''))[:32]
        )


async def _with_fetcher(factory, cloud_only: bool = False):
    '''Run a coroutine that needs a page fetcher, then shut the browser down.'''
    fetcher = Fetcher(client=DeepSeekClient(), local=not cloud_only)
    try:
        return await factory(fetcher)
    finally:
        await fetcher.close()


def cmd_run(args) -> int:
    icp = load_icp(args.icp, args)
    store = Store(args.db)
    client = DeepSeekClient()
    pipeline = Pipeline(
        client,
        store,
        icp,
        use_llm=not args.no_llm,
        per_query=args.per_query,
        fetch_limit=args.fetch_limit,
    )
    async def _run(fetcher):
        pipeline.fetcher = fetcher
        return await pipeline.run(args.count)

    try:
        stats = asyncio.run(_with_fetcher(_run, args.cloud_fetch))
    except DeepSeekError as e:
        print('DeepSeek error: ' + str(e), file=sys.stderr)
        return 2
    print('queries   : ' + str(stats['queries']))
    print('hits      : ' + str(stats['hits']))
    print('judged    : ' + str(stats['judged']))
    print('fetched   : ' + str(stats['fetched']))
    print('inserted  : ' + str(stats['inserted']))
    print('updated   : ' + str(stats['updated']))
    print('skipped   : ' + str(stats['skipped']))
    print('')
    _print_records(stats['records'])
    usage = usage_summary()
    print('')
    print('today: search=%s fetch=%s | total: %s calls, %.4f CNY' % (
        usage['today_search'], usage['today_fetch'],
        usage['total_search'] + usage['total_fetch'], usage['total_cny'],
    ))
    return 0


def cmd_seedpages(args) -> int:
    '''Turn an exhibitor roster page into seeds.csv.'''
    store = Store(args.db).init()
    reader = SeedPageReader(DeepSeekClient(), store=store)
    urls = list(args.url or [])

    if args.from_json:
        try:
            data = fetch_json(args.from_json)
        except Exception as e:
            print('JSON fetch failed: ' + str(e)[:120], file=sys.stderr)
            return 2
        rows = json_rows(data, path=args.json_path, name_key=args.name_key,
                         domain_key=args.domain_key)
        count = write_csv(rows, args.out)
        print('json rows: ' + str(count) + ' -> ' + args.out)
        for row in rows[:15]:
            print('  %-38s %-28s' % (row['company'][:38], row['domain'][:28]))
        return 0

    async def _run(fetcher):
        reader.fetcher = fetcher
        pages = urls
        if args.query:
            pages = await reader.find_pages(args.query, args.pages)
            print('pages found: ' + str(len(pages)))
            for page in pages:
                print('  - ' + page[:100])
        rows: list[dict] = []
        for page in pages:
            try:
                part = await reader.read_page(page, limit=args.limit,
                                              wait_for=args.wait_for or '')
            except Exception as e:
                print('  %-70s FAILED: %s' % (page[:70], str(e)[:60]))
                continue
            print('  %-70s %3d companies' % (page[:70], len(part)))
            if not part and reader.last_diagnosis:
                print('        ! ' + reader.last_diagnosis)
            rows.extend(part)
        named = [r for r in rows if not r.get('domain')]
        if named and not args.resolve:
            print('  name-only rows (no domain, use --resolve): ' + str(len(named)))
        if args.resolve:
            resolved = await reader.resolve_domains(rows, limit=args.resolve_limit)
            print('  resolved ' + str(len(resolved) - len([r for r in rows if r.get('domain')]))
                  + ' domains')
            rows = resolved
        return [r for r in rows if r.get('domain')]

    try:
        rows = asyncio.run(_with_fetcher(_run, args.cloud_fetch))
    except DeepSeekError as e:
        print('DeepSeek error: ' + str(e), file=sys.stderr)
        return 2

    deduped: dict[str, dict] = {}
    for row in rows:
        deduped.setdefault(row['domain'], row)
    final = list(deduped.values())
    count = write_csv(final, args.out)
    print('')
    print('unique domains: ' + str(count) + ' -> ' + args.out)
    for row in final[:15]:
        print('  %-38s %-28s %s' % (row['company'][:38], row['domain'][:28],
                                    row.get('country', '')[:12]))
    if count > 15:
        print('  ... +' + str(count - 15) + ' more')
    return 0


def cmd_seeds(args) -> int:
    '''Ask the model for candidate companies, keep the ones that really exist.'''
    store = Store(args.db).init()
    builder = SeedBuilder(DeepSeekClient(), store)
    try:
        icp = ICP.from_dict(json.loads(Path(args.icp or DEFAULT_ICP).read_text(encoding='utf-8')))
    except (OSError, ValueError):
        icp = ICP()

    result = asyncio.run(builder.generate(
        args.count, country=args.country, industry=args.industry, icp=icp,
        exclude=[r['company_name'] for r in store.list_recent(200)],
    ))
    if result.get('signals'):
        print('需求信号：' + ' / '.join(result['signals']))
        print('')
    for seed in result['seeds']:
        print('  %-34s %-30s %-14s %-22s %s' % (
            seed['name'][:34], seed['domain'][:30], seed.get('country', '')[:14],
            seed.get('industry', '')[:22], seed.get('why_fit', '')[:40]))
    stats = result['stats']
    print('')
    print('proposed %s | parsed %s | duplicate %s | dead-domain %s | kept %s' % (
        stats['proposed'], stats['parsed'], stats['duplicate'], stats['dead'],
        len(result['seeds'])))
    if args.dry_run:
        print('dry-run: nothing saved')
        return 0
    saved = builder.save(result['seeds'])
    print('saved %s seeds -> data/companies.db (email_status=pending)' % saved)
    return 0


def cmd_emails(args) -> int:
    '''Skill B: turn pending leads into verified addresses (or no_contact).'''
    store = Store(args.db).init()
    verifier = EmailVerifier(DeepSeekClient(), store)

    if args.regrade:
        out = verifier.regrade()
        print('grades recomputed: ' + str(out['changed']))
        return 0

    if args.export_tasks:
        out = verifier.export_tasks(args.export_tasks, limit=args.limit,
                                    min_score=args.min_score)
        print('exported: ' + str(out['exported']) + ' tasks -> ' + out['path'])
        print('把 tasks 交给 Agent 研究，结果写成 results.json 再 --import-results 导回')
        return 0

    if args.import_results:
        report = verifier.import_results(args.import_results)
        for row in report['rows']:
            print('  + %-30s %-32s %-8s %s' % (
                str(row['company_name'])[:30], row['email'][:32], row['kind'],
                row['evidence'][:50]))
        for err in report['errors']:
            print('  - %-30s %s' % (str(err['dedupe_key'])[:30], err['reason']))
        print('')
        print('accepted: ' + str(report['accepted']) + ' | rejected: ' + str(report['rejected']))
        return 0

    async def _run(fetcher):
        verifier.fetcher = fetcher
        return await verifier.run(args.limit, retry_days=args.retry_days)

    try:
        report = asyncio.run(_with_fetcher(_run, args.cloud_fetch))
    except DeepSeekError as e:
        print('DeepSeek error: ' + str(e), file=sys.stderr)
        return 2

    for row in report.get('rows') or []:
        print('  %-30s %-26s %-32s %-8s %-4s %s' % (
            str(row.get('company_name'))[:30], str(row.get('domain'))[:26],
            str(row.get('email'))[:32], str(row.get('kind') or '-'),
            str(row.get('grade') or '-'), row.get('status')))
    print('')
    print('picked   : ' + str(report['picked']))
    print('found    : ' + str(report['found']) + '  (generic ' + str(report.get('generic', 0)) + ')')
    print('no_contact: ' + str(report['no_contact']))
    print('rejected : ' + str(report['rejected']))
    print('dead_domain: ' + str(report['domain_dead']))
    counts = store.email_stats()
    print('ledger   : ' + ', '.join(key + '=' + str(value) for key, value in sorted(counts.items())))
    print('calls today: ' + str(report.get('calls_today', 0)))
    return 0


def cmd_contacts(args) -> int:
    '''Skill C: turn generic mailboxes into named decision-makers via agents.'''
    from .contacts import ContactEnricher

    store = Store(args.db).init()
    enricher = ContactEnricher(store)

    if args.export_tasks:
        out = enricher.export_tasks(args.export_tasks, limit=args.limit,
                                    min_score=args.min_score)
        print('exported: %s contact tasks -> %s' % (out['exported'], out['path']))
        print('把 tasks 交给 /contacts 或其他 agent 研究，结果写成 results.json 再 --import-results 导回')
        return 0

    if args.import_results:
        report = enricher.import_results(args.import_results)
        for row in report['rows']:
            print('  + %-30s %-40s' % (row['company_name'][:30], row['contact'][:40]))
        for err in report['errors']:
            print('  - %-30s %s' % (str(err['dedupe_key'])[:30], err['reason']))
        print('')
        print('accepted: %s | rejected: %s' % (report['accepted'], report['rejected']))
        return 0

    print('contacts command: use --export-tasks or --import-results')
    return 1


def cmd_report(args) -> int:
    store = Store(args.db).init()
    when = date.fromisoformat(args.date) if args.date else date.today()
    if args.force:
        path, rows = build_report(store, when)
        store.record_report(when.isoformat(), str(path), rows)
        created = True
    else:
        path, rows, created = maybe_generate_daily(store, when)
    start, end = day_bounds(when)
    print(('created ' if created else 'exists  ') + str(path))
    if created:
        print('rows    : ' + str(rows))
    print('window  : ' + start + ' .. ' + end)
    return 0


def cmd_daily(args) -> int:
    '''Idempotent daily hook: safe to call from cron / Task Scheduler.'''
    store = Store(args.db).init()
    path, rows, created = maybe_generate_daily(store)
    if created:
        print('daily report created: ' + str(path) + ' (' + str(rows) + ' rows)')
    else:
        print('daily report already exists: ' + str(path))
    return 0


def cmd_list(args) -> int:
    store = Store(args.db).init()
    rows = store.list_recent(args.limit)
    if not rows:
        print('(empty)')
        return 0
    print('  ' + 'name'.ljust(30) + 'email'.ljust(34) + 'match')
    for row in rows:
        print(
            '  ' + str(row['company_name'])[:28].ljust(30)
            + str(row['company_email'])[:32].ljust(34)
            + str(row['match_result'])[:26]
        )
    print('')
    print('total: ' + str(store.stats()['companies']))
    return 0


def cmd_usage(args) -> int:
    usage = usage_summary(args.day)
    for key in ('day', 'today_search', 'today_fetch', 'total_search', 'total_fetch', 'total_cny'):
        print(key.ljust(14) + str(usage[key]))
    return 0


def _print_icp(icp: dict, meta: dict, path: Path) -> None:
    print('written : ' + str(path))
    print('method  : ' + str(meta.get('method', '')) + (' <- ' + str(meta.get('source'))
          if meta.get('source') else ''))
    print('industries : ' + ', '.join(icp.get('industries') or []))
    print('titles     : ' + ', '.join(icp.get('titles') or []))
    print('geography  : ' + ', '.join(icp.get('geography') or []))
    print('size       : ' + str(icp.get('company_size', '')))
    print('product    : ' + str(icp.get('product', '')))
    gaps = meta.get('gaps') or []
    coverage = meta.get('coverage')
    if coverage is not None:
        print('coverage   : ' + str(coverage) + ' | ' + ' '.join(
            '%s=%s' % (key, (meta.get('fields') or {}).get(key, {}).get('status', '?'))
            for key in ('industries', 'titles', 'geography', 'company_size', 'product')))
    if gaps:
        print('gaps       : ' + ', '.join(gaps) + '   (用 --fill 现场补，或用 --strict 拒绝落盘)')
    confidence = int(meta.get('confidence') or 0)
    if confidence:
        print('confidence : ' + str(confidence) + '/100')
    if confidence and confidence < 50:
        print('')
        print('注意：置信度偏低，建议人工核对 industry/title/size 后再用于批量搜索。')
    for item in meta.get('evidence') or []:
        print('  - ' + str(item))


def cmd_icp(args) -> int:
    out = Path(args.out) if args.out else ICP_PATH
    if args.show:
        try:
            print(json.dumps(read_icp(out), ensure_ascii=False, indent=2))
        except DeepSeekError as e:
            print(str(e), file=sys.stderr)
            return 2
        return 0
    if args.template:
        TEMPLATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        TEMPLATE_PATH.write_text(
            json.dumps(template(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
        )
        print('template written: ' + str(TEMPLATE_PATH))
        return 0
    try:
        if args.url:
            result = asyncio.run(_with_fetcher(
                lambda f: from_site(args.url, pages=args.pages, use_llm=not args.no_llm, fetcher=f),
                args.cloud_fetch,
            ))
        elif args.answers:
            payload = json.loads(Path(args.answers).read_text(encoding='utf-8'))
            result = asyncio.run(from_answers(payload, use_llm=not args.no_llm))
        else:
            if not sys.stdin.isatty():
                print('no --url / --answers: run this in a terminal to answer the 5 questions',
                      file=sys.stderr)
                return 2
            result = asyncio.run(from_answers(ask_questions(), use_llm=not args.no_llm))
    except (DeepSeekError, ValueError, OSError) as e:
        print('error: ' + str(e), file=sys.stderr)
        return 2

    meta = result.get('meta') or {}
    gaps = meta.get('gaps') or []
    if gaps and args.strict:
        print('ICP 还缺: ' + ', '.join(gaps) + '  (--strict 不写文件)', file=sys.stderr)
        return 2
    if args.fill:
        coverage = float(meta.get('coverage') or 0)
        weak = [k for k, info in (meta.get('fields') or {}).items()
                if info.get('status') == 'inferred'] if coverage < 0.6 else []
        ask_keys = gaps + weak
        if ask_keys:
            preset = {}
            for key in weak:
                value = result['icp'].get(key)
                preset[key] = ', '.join(value) if isinstance(value, list) else str(value or '')
            print('')
            print('官网只覆盖了一部分字段。需要确认: ' + ', '.join(ask_keys))
            print('直接回车 = 用方括号里的值（有则沿用，无则用默认）。')
            answers = ask_questions(ask_keys, defaults=preset)
            result = asyncio.run(fill_gaps(result, answers, use_llm=not args.no_llm))
            gaps = (result.get('meta') or {}).get('gaps') or []
    path = write_icp(result['icp'], out, result['meta'])
    _print_icp(result['icp'], result['meta'], path)
    return 0


def _print_leadgen_stats(stats: dict) -> None:
    for key in ('topics', 'pages', 'candidates', 'vendor', 'resolved', 'fetched',
                'inserted', 'updated', 'dropped', 'skipped'):
        print('  ' + key.ljust(9) + str(stats.get(key, 0)))
    for record in stats.get('records') or []:
        print('  + ' + str(record.get('match_result', ''))[:24].ljust(26)
              + str(record.get('company_name', ''))[:28].ljust(30)
              + str(record.get('company_email', ''))[:34])


def _make_leadgen(args):
    icp = load_icp(args.icp, args)
    store = Store(args.db)
    gen = LeadGen(
        DeepSeekClient(),
        store,
        icp,
        use_llm=not args.no_llm,
        per_topic=args.per_topic,
        pages_per_topic=args.pages,
        fetch_limit=args.fetch,
        daily_budget=args.budget,
    )
    return gen, store


def _append_log(line: str) -> None:
    path = BASE_DIR / 'data' / 'leadgen.log'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as handle:
            handle.write(line + '\n')
    except OSError:
        pass


def cmd_leadgen(args) -> int:
    gen, store = _make_leadgen(args)

    async def _round(fetcher):
        gen.fetcher = fetcher
        gen.pipeline.fetcher = fetcher
        if args.seeds:
            seeds = parse_seeds_csv(args.seeds)
            print('seeds loaded: ' + str(len(seeds)) + ' from ' + args.seeds)
            return await gen.seed_round(seeds, limit=args.fetch or 50)
        return await gen.round(args.topics)

    try:
        stats = asyncio.run(_with_fetcher(_round, args.cloud_fetch))
    except DeepSeekError as e:
        print('DeepSeek error: ' + str(e), file=sys.stderr)
        return 2
    _print_leadgen_stats(stats)
    usage = usage_summary()
    print('today: %s calls, %.4f CNY | total companies %s' % (
        usage['today_search'] + usage['today_fetch'], usage['total_cny'],
        store.stats()['companies'],
    ))
    return 0


def cmd_loop(args) -> int:
    gen, store = _make_leadgen(args)
    client = gen.client
    deadline = time.time() + args.hours * 3600 if args.hours else None
    round_no = 0
    round_stats_history = []
    check_interval = getattr(args, 'adaptive_check', 5)  # 每 N 轮检查一次
    enable_adaptive = not args.no_adaptive if hasattr(args, 'no_adaptive') else True

    budget_label = str(args.budget) if args.budget > 0 else 'unlimited'
    print('lead-gen loop: every %ss, budget %s calls/day, %s%s' % (
        args.interval, budget_label, 'no time limit' if not deadline else str(args.hours) + 'h',
        ', adaptive ICP ON' if enable_adaptive else '',
    ), flush=True)
    _append_log('=== loop start ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                + ' interval=' + str(args.interval) + 's budget=' + budget_label
                + (' adaptive=ON check_every=' + str(check_interval) if enable_adaptive else ' adaptive=OFF')
                + ' ===')
    try:
        while True:
            if STOP_PATH.exists():
                print('STOP file found, exiting', flush=True)
                break
            if deadline and time.time() >= deadline:
                print('time limit reached, exiting', flush=True)
                break
            if args.rounds and round_no >= args.rounds:
                print('round limit reached, exiting', flush=True)
                break
            if gen.budget_left() <= 4:
                print(datetime.now().strftime('%H:%M') + ' daily budget spent ('
                      + str(gen.calls_today()) + ' calls), sleeping 30 min', flush=True)
                time.sleep(1800)
                continue
            round_no += 1
            round_start = time.time()
            async def _round(fetcher):
                gen.fetcher = fetcher
                gen.pipeline.fetcher = fetcher
                return await gen.round(args.topics)
            try:
                stats = asyncio.run(_with_fetcher(_round, args.cloud_fetch))
                stats['round_id'] = round_no
                stats['time_cost'] = time.time() - round_start
                round_stats_history.append(RoundStats.from_dict(stats))
            except DeepSeekError as e:
                line = 'DeepSeek error: ' + str(e)
                print(line, flush=True)
                _append_log(datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' ' + line)
                time.sleep(300)
                continue
            except Exception as e:
                line = 'round failed: ' + type(e).__name__ + ': ' + str(e)
                print(line, flush=True)
                _append_log(datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' ' + line)
                time.sleep(120)
                continue
            usage = usage_summary()
            line = '%s round=%s topics=%s pages=%s names=%s resolved=%s fetched=%s new=%s upd=%s skip=%s calls=%s cny=%.3f total=%s' % (
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'), round_no,
                stats.get('topics'), stats.get('pages'), stats.get('candidates'),
                stats.get('resolved'), stats.get('fetched'), stats.get('inserted'),
                stats.get('updated'), stats.get('skipped'),
                usage['today_search'] + usage['today_fetch'], usage['total_cny'],
                store.stats()['companies'],
            )
            fetch_stats = stats.get('fetch') or {}
            if fetch_stats:
                line += ' fetch(local=%s short=%s fail=%s cloud=%s)' % (
                    fetch_stats.get('local_ok', 0), fetch_stats.get('local_short', 0),
                    fetch_stats.get('local_fail', 0), fetch_stats.get('cloud_ok', 0))
            print(line, flush=True)
            _append_log(line)
            for record in stats.get('records') or []:
                _append_log('    + ' + str(record.get('company_name', ''))[:40]
                            + ' | ' + str(record.get('company_website', ''))[:50]
                            + ' | ' + str(record.get('company_email', ''))[:40]
                            + ' | ' + str(record.get('match_result', ''))[:30])

            # Adaptive ICP check
            if enable_adaptive and round_no % check_interval == 0 and round_no >= check_interval:
                async def _analyze():
                    return await analyze_and_adjust_icp(
                        gen.icp,
                        round_stats_history[-check_interval:],
                        store,
                        client,
                        {
                            'names': len(gen.seen.names),
                            'hosts': len(gen.seen.hosts),
                            'queries': len(gen.seen.queries),
                        },
                    )
                try:
                    new_icp, reason, adjusted = asyncio.run(_analyze())
                    if adjusted:
                        report = format_adjustment_report(gen.icp, new_icp, reason, round_no)
                        print(report, flush=True)
                        _append_log(report)
                        gen.icp = new_icp
                        gen.pipeline.icp = new_icp
                        # Persist adjusted ICP
                        adaptive_path = BASE_DIR / 'data' / f'icp_adaptive_r{round_no}.json'
                        adaptive_path.write_text(
                            json.dumps({
                                'industries': new_icp.industries,
                                'titles': new_icp.titles,
                                'geography': new_icp.geography,
                                'company_size': new_icp.company_size,
                                'product': new_icp.product,
                                '_adaptive': {
                                    'round': round_no,
                                    'reason': reason,
                                    'timestamp': datetime.now().isoformat(),
                                }
                            }, ensure_ascii=False, indent=2),
                            encoding='utf-8'
                        )
                    else:
                        print(f'[Round {round_no}] ICP check: {reason}', flush=True)
                        _append_log(f'[Round {round_no}] ICP check: {reason}')
                except Exception as e:
                    line = f'ICP analysis failed at round {round_no}: {type(e).__name__}: {str(e)[:100]}'
                    print(line, flush=True)
                    _append_log(line)

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print('interrupted', flush=True)
    _append_log('=== loop stop ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' ===')
    print('stopped after %s rounds' % round_no, flush=True)
    return 0


def _add_leadgen_args(parser) -> None:
    parser.add_argument('--topics', type=int, default=4, help='how many topic queries per round')
    parser.add_argument('--per-topic', type=int, default=8, help='search results per topic')
    parser.add_argument('--pages', type=int, default=2, help='list pages fetched per topic')
    parser.add_argument('--fetch', type=int, default=4, help='company homepages fetched per round')
    parser.add_argument('--budget', type=int, default=0, help='max search+fetch calls per day (0 = unlimited)')
    parser.add_argument('--cloud-fetch', action='store_true', help='skip local rendering, DeepSeek Fetch only')
    parser.add_argument('--seeds', metavar='CSV', help='skip search, harvest these domains')
    parser.add_argument('--no-llm', action='store_true', help='heuristics only, no chat calls')
    parser.add_argument('--industry', action='append', help='ICP industry (repeatable)')
    parser.add_argument('--title', action='append', help='ICP title (repeatable)')
    parser.add_argument('--geo', action='append', help='ICP geography (repeatable)')
    parser.add_argument('--size', help='ICP company size')
    parser.add_argument('--product', help='what you sell (feeds the prompts)')
    parser.add_argument('--icp', type=Path, help='path to an ICP json file')
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='search_skills',
        description='Company search + resolution on DeepSeek Search/Fetch only.',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    run = sub.add_parser('run', help='run one search pass')
    run.add_argument('--count', type=int, default=8, help='how many queries to plan')
    run.add_argument('--industry', action='append', help='ICP industry (repeatable)')
    run.add_argument('--title', action='append', help='ICP title (repeatable)')
    run.add_argument('--geo', action='append', help='ICP geography (repeatable)')
    run.add_argument('--size', help='ICP company size, e.g. "10-200 employees"')
    run.add_argument('--product', help='what you sell (feeds the prompts)')
    run.add_argument('--icp', type=Path, help='path to an ICP json file')
    run.add_argument('--per-query', type=int, default=6, help='results per query')
    run.add_argument('--fetch-limit', type=int, default=4, help='pages to fetch per run')
    run.add_argument('--no-llm', action='store_true', help='skip chat calls, use heuristics')
    run.add_argument('--cloud-fetch', action='store_true', help='skip local rendering, DeepSeek Fetch only')
    run.add_argument('--db', type=Path, default=DEFAULT_DB)
    run.set_defaults(func=cmd_run)

    icp_cmd = sub.add_parser('icp', help='build data/icp.json from a website or an interview')
    icp_cmd.add_argument('--url', help='seller website, e.g. https://acme.com')
    icp_cmd.add_argument('--answers', type=Path, help='json file with the five ICP answers')
    icp_cmd.add_argument('--out', type=Path, help='output path (default data/icp.json)')
    icp_cmd.add_argument('--pages', type=int, default=4, help='extra pages to fetch from the site')
    icp_cmd.add_argument('--no-llm', action='store_true', help='heuristics only, no chat calls')
    icp_cmd.add_argument('--cloud-fetch', action='store_true', help='skip local rendering, DeepSeek Fetch only')
    icp_cmd.add_argument('--show', action='store_true', help='print the current ICP and exit')
    icp_cmd.add_argument('--fill', action='store_true', help='ask only the missing fields after scraping')
    icp_cmd.add_argument('--strict', action='store_true', help='refuse to write the ICP when fields are missing')
    icp_cmd.add_argument('--template', action='store_true', help='write templates/icp.template.json')
    icp_cmd.set_defaults(func=cmd_icp)

    mine = sub.add_parser('leadgen', help='one round: mine names, resolve them, store leads')
    _add_leadgen_args(mine)
    mine.set_defaults(func=cmd_leadgen)

    loop = sub.add_parser('loop', help='continuous background mining (long running)')
    _add_leadgen_args(loop)
    loop.add_argument('--interval', type=int, default=1200, help='seconds between rounds')
    loop.add_argument('--hours', type=float, default=12.0, help='stop after N hours (0 = never)')
    loop.add_argument('--rounds', type=int, default=0, help='stop after N rounds (0 = never)')
    loop.add_argument('--adaptive-check', type=int, default=5, help='check ICP every N rounds for adaptive adjustment')
    loop.add_argument('--no-adaptive', action='store_true', help='disable adaptive ICP adjustment')
    loop.set_defaults(func=cmd_loop)

    pages = sub.add_parser('seedpages', help='exhibitor/association page -> seeds.csv')
    pages.add_argument('--url', action='append', help='page URL (repeatable)')
    pages.add_argument('--query', help='search this instead of giving URLs')
    pages.add_argument('--pages', type=int, default=3, help='how many result pages to read')
    pages.add_argument('--limit', type=int, default=400, help='max companies per page')
    pages.add_argument('--wait-for', metavar='CSS', help='wait for this selector (JS roster pages)')
    pages.add_argument('--from-json', metavar='URL', help='read a fair JSON API instead of HTML')
    pages.add_argument('--json-path', default='', help='dotted path to the array, e.g. data.items')
    pages.add_argument('--name-key', default='name', help='field holding the company name')
    pages.add_argument('--domain-key', default='website', help='field holding the website')
    pages.add_argument('--resolve', action='store_true', help='search domains missing a link')
    pages.add_argument('--resolve-limit', type=int, default=20, help='max lookups for --resolve')
    pages.add_argument('--out', default='data/seeds.csv', help='output CSV path')
    pages.add_argument('--cloud-fetch', action='store_true', help='skip local rendering')
    pages.add_argument('--db', type=Path, default=DEFAULT_DB)
    pages.set_defaults(func=cmd_seedpages)

    seed = sub.add_parser('seeds', help='model-proposed seed list, verified by MX')
    seed.add_argument('--count', type=int, default=30, help='how many companies to ask for')
    seed.add_argument('--country', default='', help='focus country, e.g. Germany')
    seed.add_argument('--industry', default='', help='focus industry, e.g. industrial machinery')
    seed.add_argument('--dry-run', action='store_true', help='print but do not save')
    seed.add_argument('--icp', type=Path, help='ICP json to align with')
    seed.add_argument('--db', type=Path, default=DEFAULT_DB)
    seed.set_defaults(func=cmd_seeds)

    mail = sub.add_parser('emails', help='verify emails for pending leads')
    mail.add_argument('--limit', type=int, default=20, help='how many leads to process')
    mail.add_argument('--retry-days', type=int, default=7, help='retry no_contact after N days')
    mail.add_argument('--regrade', action='store_true', help='recompute A/B/C from stored verdicts')
    mail.add_argument('--export-tasks', metavar='PATH', help='write agent tasks for unsolved leads')
    mail.add_argument('--import-results', metavar='PATH', help='import agent answers (evidence required)')
    mail.add_argument('--min-score', type=int, default=55, help='only export leads at or above this score')
    mail.add_argument('--cloud-fetch', action='store_true', help='skip local rendering')
    mail.add_argument('--db', type=Path, default=DEFAULT_DB)
    mail.set_defaults(func=cmd_emails)

    contact = sub.add_parser('contacts', help='enrich generic mailboxes with named contacts')
    contact.add_argument('--db', type=Path, default=DEFAULT_DB)
    contact.add_argument('--export-tasks', type=str, metavar='PATH', help='export contact tasks for agents')
    contact.add_argument('--import-results', type=str, metavar='PATH', help='import agent contact results')
    contact.add_argument('--limit', type=int, default=10, help='how many to export')
    contact.add_argument('--min-score', type=int, default=60, help='minimum match_score')
    contact.set_defaults(func=cmd_contacts)

    report = sub.add_parser('report', help='build today xlsx')
    report.add_argument('--date', help='any date inside the ISO week')
    report.add_argument('--force', action='store_true', help='rebuild an existing week')
    report.add_argument('--db', type=Path, default=DEFAULT_DB)
    report.set_defaults(func=cmd_report)

    daily = sub.add_parser('daily', help='idempotent daily report hook (cron friendly)')
    daily.add_argument('--db', type=Path, default=DEFAULT_DB)
    daily.set_defaults(func=cmd_daily)

    weekly = sub.add_parser('weekly', help='deprecated alias of daily')
    weekly.add_argument('--db', type=Path, default=DEFAULT_DB)
    weekly.set_defaults(func=cmd_daily)

    listing = sub.add_parser('list', help='show the most recent companies')
    listing.add_argument('--limit', type=int, default=20)
    listing.add_argument('--db', type=Path, default=DEFAULT_DB)
    listing.set_defaults(func=cmd_list)

    usage = sub.add_parser('usage', help='search/fetch call ledger')
    usage.add_argument('--day', help='YYYY-MM-DD')
    usage.set_defaults(func=cmd_usage)
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == '__main__':
    raise SystemExit(main())
