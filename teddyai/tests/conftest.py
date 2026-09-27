"""
Shared pytest fixtures for the TeddyAI engine tests.

The project is a flat set of modules at the repository root, not a package, so
every test needs the root on sys.path. That is done here once rather than in
each test file.

WHY A TEST DIRECTORY EXISTS AT ALL (2026-09-09). Until this date the project had
no test suite: pytest was installed and there were zero test files. The only
automated check was check_identical.py, a differential harness that proves the
NQ path is bit-identical after a change by running the CURRENT code against a
dated .bak copy. That is a good regression proof and a poor unit test -- it
cannot test a function that has no .bak to compare against, which is every
function added from here on.

The convention these tests follow:
  * small, mathematically inspectable fixtures wherever the maths allows it
  * real cached chains only for the regression baseline, never for unit maths
  * no giant JSON snapshots; the baseline stores flips, totals and a HASH of
    each gamma map, so a diff says WHICH number moved
"""
import sys
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DATA = ROOT / "data"


@pytest.fixture(autouse=True)
def offline_by_default(monkeypatch):
    """Tests must explicitly stub I/O; never spend model/data quota or post."""
    import socket, subprocess
    def blocked(*args, **kwargs):
        raise RuntimeError('External network/process execution is disabled in tests; provide an explicit fixture')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket.socket, 'connect_ex', blocked)
    monkeypatch.setattr(subprocess, 'Popen', blocked)


@pytest.fixture(scope="session")
def root():
    return ROOT


@pytest.fixture(scope="session")
def baseline():
    """The frozen production numbers every regression test compares against."""
    p = FIXTURES / "baseline.json"
    if not p.exists():
        pytest.skip("tests/fixtures/baseline.json missing -- run python tests/make_baseline.py")
    return json.loads(p.read_text(encoding="utf-8"))


def chain_path(settle):
    return DATA / f"{settle}-chain.json"


def have_chain(settle):
    return chain_path(settle).exists()
