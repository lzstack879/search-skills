'''Harvest companies from exhibition PDFs and B2B directories.

Much cheaper and more reliable than search: one PDF yields 500-2000 companies,
and the quality is near 100% (real exhibitors with verified websites).
'''

from __future__ import annotations

import re
from typing import Any
from io import BytesIO

import requests
import pdfplumber


def harvest_fakuma_pdf(industries: list[str]) -> list[dict[str, Any]]:
    '''Fakuma 2026 - plastics processing exhibition (Germany).

    Downloads the official exhibitor catalog PDF and extracts company names + websites.
    The PDF lists ~1500 exhibitors starting from page 21.
    '''
    url = 'https://www.fakuma-messe.de/dl/Messekatalog_Fakuma.pdf'
    try:
        resp = requests.get(url, timeout=60, headers={'User-Agent': 'Mozilla/5.0'})
        pdf = pdfplumber.open(BytesIO(resp.content))
        companies = []

        # Pattern: company name (uppercase start) followed by website
        # Example: "2E mechatronic GmbH & Co. KG    www.2e-mechatronic.de"
        pattern = r'([A-Z][A-Za-z0-9\s&\.\-]+(?:GmbH|AG|SE|KG|Ltd|Inc|Corp|BV|SA)?)\s+(www\.[a-z0-9\-\.]+)'

        for page_num in range(20, min(len(pdf.pages), 100)):  # Pages 21-100
            text = pdf.pages[page_num].extract_text()
            if not text:
                continue

            for match in re.finditer(pattern, text, re.IGNORECASE):
                name = match.group(1).strip()
                website = match.group(2).strip()

                # Clean up name
                name = re.sub(r'\s+', ' ', name)
                if len(name) < 3 or len(name) > 80:
                    continue

                # Normalize website
                if not website.startswith('http'):
                    website = 'https://' + website

                companies.append({
                    'company_name': name,
                    'company_website': website,
                    'country': 'Germany',
                    'source_url': f'{url}#page={page_num + 1}',
                    'source_type': 'exhibition_pdf',
                })

        pdf.close()
        return companies
    except Exception as e:
        print(f'Fakuma PDF harvest failed: {e}')
        return []


def harvest_motek_html(industries: list[str]) -> list[dict[str, Any]]:
    '''Motek 2025 - assembly and handling technology (Germany).

    Scrapes the HTML exhibitor list. Much faster than PDF and easier to parse.
    '''
    url = 'https://www.motek-messe.de/ausstellerliste/'
    try:
        resp = requests.get(url, timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
        resp.raise_for_status()
        html = resp.text
        companies = []

        # The page has exhibitor cards with company name and website
        # Try multiple patterns based on common structures
        import re

        # Pattern 1: Extract from links with company domains
        # Look for <a href="https://company.com">Company Name</a>
        link_pattern = r'<a[^>]*href="(https?://(?!www\.motek)[^"]+)"[^>]*>([^<]{3,80})</a>'
        for match in re.finditer(link_pattern, html, re.IGNORECASE):
            website, name = match.group(1), match.group(2)
            name = name.strip()

            # Filter out navigation links and generic text
            if len(name) < 3 or name.lower() in ['here', 'click', 'more', 'details', 'website']:
                continue
            if 'motek' in website.lower() or 'messe' in website.lower():
                continue

            companies.append({
                'company_name': name,
                'company_website': website,
                'country': 'Germany',
                'source_url': url,
                'source_type': 'exhibition_html',
            })

        # If no results, try extracting company names from text blocks
        if len(companies) < 10:
            # Pattern 2: Company name followed by website on next line
            block_pattern = r'<div[^>]*class="[^"]*exhibitor[^"]*"[^>]*>(.*?)</div>'
            for block_match in re.finditer(block_pattern, html, re.DOTALL | re.IGNORECASE):
                block = block_match.group(1)
                # Extract text and links from this block
                text = re.sub(r'<[^>]+>', ' ', block)
                text = text.strip()

                # Find website in this block
                website_match = re.search(r'(https?://[^\s<>"]+)', block)
                if website_match:
                    website = website_match.group(1)
                    # Use text before website as company name
                    name = text.split(website)[0].strip()
                    if 3 < len(name) < 80:
                        companies.append({
                            'company_name': name,
                            'company_website': website,
                            'country': 'Germany',
                            'source_url': url,
                            'source_type': 'exhibition_html',
                        })

        print(f'Motek: extracted {len(companies)} companies from HTML')
        return companies[:500]
    except Exception as e:
        print(f'Motek HTML harvest failed: {e}')
        return []


def harvest_exhibitions(icp: Any, limit: int = 200) -> list[dict[str, Any]]:
    '''Harvest from exhibitions and directories based on ICP industries.

    Returns a list of {company_name, company_website, country, source_url, source_type}.
    No match_score yet - that's computed later by the pipeline.
    '''
    results = []
    icp_text = ' '.join(icp.industries).lower()

    # Try Motek first (easiest and fastest)
    if any(kw in icp_text for kw in ['machinery', 'automation', 'manufacturing',
                                       'engineering', 'assembly', 'handling']):
        print('Harvesting Motek exhibitor list...')
        results.extend(harvest_motek_html(icp.industries))

    # Try Fakuma if plastics/packaging
    if any(kw in icp_text for kw in ['plastics', 'packaging', 'injection',
                                       'polymer', 'extrusion']):
        print('Harvesting Fakuma PDF catalog...')
        results.extend(harvest_fakuma_pdf(icp.industries))

    # Deduplicate by website host
    seen = set()
    unique = []
    for r in results:
        host = r['company_website'].replace('https://', '').replace('http://', '').split('/')[0].lower()
        if host not in seen and len(host) > 3:
            seen.add(host)
            unique.append(r)

    print(f'Harvested {len(unique)} unique companies from exhibitions')
    return unique[:limit]

