'''Hybrid lead generation: 80% from exhibitions/PDFs + 20% from search.

This combines the best of both worlds:
- Exhibition PDFs: cheap, high quality, bulk volume (500-2000 companies/PDF)
- DeepSeek Search: flexible, finds long-tail companies not in exhibitions
'''

from __future__ import annotations

import asyncio
from typing import Any

from .harvest_pdf import harvest_exhibitions
from .leadgen import LeadGen
from .pipeline import ICP
from .store import Store


async def run_hybrid_leadgen(icp: ICP, store: Store, client: Any,
                              target: int = 100, pdf_ratio: float = 0.8) -> dict:
    '''Run a hybrid lead generation round: PDF harvest + search complement.

    Args:
        icp: Ideal customer profile
        store: Database for deduplication and storage
        client: DeepSeekClient for search/fetch
        target: Total number of companies to collect
        pdf_ratio: Fraction from PDF sources (0.8 = 80% PDF, 20% search)

    Returns:
        stats dict with: {pdf_count, search_count, stored, skipped, calls}
    '''
    pdf_target = int(target * pdf_ratio)
    search_target = target - pdf_target

    stats = {
        'pdf_count': 0,
        'search_count': 0,
        'stored': 0,
        'skipped': 0,
        'calls': 0,
    }

    # Phase 1: Harvest from exhibitions/PDFs (cost: free)
    print(f'\n=== Phase 1: Harvesting from exhibitions (target: {pdf_target}) ===')
    pdf_leads = harvest_exhibitions(icp, limit=pdf_target * 2)  # Over-fetch for filtering
    stats['pdf_count'] = len(pdf_leads)

    # Filter by ICP and deduplicate
    known = store.dedupe_keys()
    filtered = []
    for lead in pdf_leads:
        # Basic filtering: must have website
        if not lead.get('company_website'):
            continue
        # Deduplicate
        host = lead['company_website'].replace('https://', '').replace('http://', '').split('/')[0].lower()
        if host in known:
            stats['skipped'] += 1
            continue
        known.add(host)
        filtered.append(lead)

    pdf_final = filtered[:pdf_target]
    print(f'PDF harvest: {len(pdf_leads)} raw → {len(filtered)} filtered → {len(pdf_final)} selected')

    # Store PDF leads directly (they already have name + website)
    for lead in pdf_final:
        store.upsert({
            'company_name': lead['company_name'],
            'company_website': lead['company_website'],
            'source_url': lead['source_url'],
            'match_score': 60,  # Default score, will be refined by pipeline if needed
            'email_status': 'pending',
        })
        stats['stored'] += 1

    # Phase 2: Complement with search (cost: ~¥0.10 for 20 companies)
    if search_target > 0:
        print(f'\n=== Phase 2: Complementing with search (target: {search_target}) ===')
        # LeadGen signature: (client, store, icp, ...)
        gen = LeadGen(client, store, icp, use_llm=True)
        gen.known = known  # Pass the updated dedupe set

        # Run fewer topics since we already have bulk from PDFs
        topics = max(1, search_target // 10)  # 1 topic per 10 companies
        search_stats = await gen.round(count=topics)

        stats['search_count'] = search_stats.get('inserted', 0) + search_stats.get('updated', 0)
        stats['calls'] = search_stats.get('calls', 0)
        print(f'Search complement: {stats["search_count"]} companies from {topics} topics')

    print(f'\n=== Hybrid Summary ===')
    print(f'PDF sources: {stats["stored"]} companies (cost: ¥0)')
    print(f'Search complement: {stats["search_count"]} companies (cost: ~¥{stats["calls"] * 0.005:.2f})')
    print(f'Total stored: {stats["stored"] + stats["search_count"]}')
    print(f'Skipped (duplicates): {stats["skipped"]}')

    return stats
