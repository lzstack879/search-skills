import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolate_usage_ledger(tmp_path, monkeypatch):
    '''Never let a test write to data/usage.json.'''
    from search_skills import client as client_module

    monkeypatch.setattr(client_module, 'USAGE_PATH', tmp_path / 'usage.json')
    yield
