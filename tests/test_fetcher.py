'''Offline tests for the two-backend fetcher.'''

import asyncio

import pytest

from search_skills.client import FetchPage
from search_skills.fetcher import Fetcher


class FakeBackend:
    def __init__(self, content='', error=None, title='T'):
        self.content = content
        self.error = error
        self.title = title
        self.calls = []

    async def fetch(self, url, **kwargs):
        self.calls.append(url)
        if self.error:
            raise RuntimeError(self.error)
        return FetchPage(url=url, title=self.title, content=self.content)


def build(local, cloud, min_chars=10):
    fetcher = Fetcher(client=cloud, local=False, min_chars=min_chars)
    fetcher.local = local
    return fetcher


def test_local_wins_when_it_returns_enough_text():
    local = FakeBackend('x' * 40, title='local')
    cloud = FakeBackend('short')
    page = asyncio.run(build(local, cloud).fetch('https://a.com'))
    assert page.title == 'local'
    assert cloud.calls == []


def test_short_local_page_falls_back_to_cloud():
    local = FakeBackend('tiny')
    cloud = FakeBackend('y' * 200, title='cloud')
    fetcher = build(local, cloud, min_chars=50)
    page = asyncio.run(fetcher.fetch('https://a.com'))
    assert page.title == 'cloud'
    assert fetcher.stats['local_short'] == 1
    assert fetcher.stats['cloud_ok'] == 1


def test_local_error_falls_back_to_cloud():
    local = FakeBackend(error='cloudflare blocked')
    cloud = FakeBackend('z' * 100, title='cloud')
    fetcher = build(local, cloud, min_chars=50)
    page = asyncio.run(fetcher.fetch('https://a.com'))
    assert page.title == 'cloud'
    assert fetcher.stats['local_fail'] == 1


def test_cloud_failure_still_returns_the_short_local_page():
    local = FakeBackend('tiny')
    cloud = FakeBackend(error='HTTP 504')
    fetcher = build(local, cloud, min_chars=500)
    page = asyncio.run(fetcher.fetch('https://a.com'))
    assert page.content == 'tiny'
    assert fetcher.stats['cloud_fail'] == 1


def test_both_failing_raises():
    local = FakeBackend(error='blocked')
    cloud = FakeBackend(error='504')
    with pytest.raises(RuntimeError):
        asyncio.run(build(local, cloud).fetch('https://a.com'))


def test_cloud_only_mode_skips_the_local_backend():
    local = FakeBackend('x' * 500)
    cloud = FakeBackend('y' * 500, title='cloud')
    fetcher = Fetcher(client=cloud, local=False)
    page = asyncio.run(fetcher.fetch('https://a.com'))
    assert page.title == 'cloud'
    assert local.calls == []
