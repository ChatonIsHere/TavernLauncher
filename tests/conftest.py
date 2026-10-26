"""Shared pytest wiring for the launcher's tests."""
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tavern_shared.mods import resolve


@pytest.fixture(autouse=True)
def _fresh_manifest_memo():
    """Tests stub repos._get_json per case, so a manifest memoized by one test
    must never answer for another."""
    resolve._manifest_memo.clear()
    yield
    resolve._manifest_memo.clear()


@pytest.fixture(autouse=True)
def _wired_host_config():
    """Both apps wire the mod manager's config at startup and saves are
    strict, so every test starts wired to a throwaway store."""
    from tavern_shared.mods import base
    prev = (base._load, base._save)
    base.set_config_accessors(dict, lambda cfg: None)
    yield
    base.set_config_accessors(*prev)


def stub(test, owner, name, value):
    """owner.name = value for one unittest test, restored on cleanup. Patch the
    module that owns a name, or a stub can silently miss its callers."""
    test.addCleanup(setattr, owner, name, getattr(owner, name))
    setattr(owner, name, value)


def temp_dir(test, prefix="tavern_test_"):
    """A fresh temp directory, removed when the test ends."""
    path = tempfile.mkdtemp(prefix=prefix)
    test.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path
