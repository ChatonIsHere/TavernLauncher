"""Cross-language golden records for manifest v2. tests/fixtures/v2 was
generated straight from the spec, independently of either installer: a static
repo (served at cases.json's base_url) and the exact record bytes each install
must leave. TavernLib is held to the same files.

The offline case serves the copy in tests/fixtures/v2 through the network
seams and always runs; the live one installs from the fixture server itself
and skips when nothing is listening there."""
import contextlib
import io
import json
import os
import shutil
import unittest
import urllib.request
from urllib.parse import urlparse

from conftest import stub, temp_dir
from tavern_shared.mods import install, repos

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "v2")

with open(os.path.join(FIXTURES, "cases.json"), encoding="utf-8") as _f:
    CASES = json.load(_f)
BASE = CASES["base_url"]


def _expected(case):
    with open(os.path.join(FIXTURES, "expected", case + ".json"), "rb") as f:
        return f.read()


class _Golden:
    """Each case installed through install_mod_closure, the path a launcher's
    Install button takes; a two-target case in both orders."""

    def setUp(self):
        self.addCleanup(repos._index_cache.pop, BASE, None)

    def _record(self, game, mod_id):
        with open(os.path.join(game, "Mods", mod_id, "manifest.json"), "rb") as f:
            return f.read()

    def _install(self, mod_id, order):
        game = temp_dir(self, "tavern_golden_")
        index = repos.fetch_indexes([BASE], force=True)
        root = next(s for s in index if s.id == mod_id)
        with contextlib.redirect_stderr(io.StringIO()):
            for target in order:
                install.install_mod_closure(game, root, index, [BASE], lambda _m: None, target)
        return game

    def test_every_case_matches_its_golden_bytes(self):
        for case, spec in CASES["cases"].items():
            targets = spec["targets"]
            for order in ([targets, targets[::-1]] if len(targets) > 1 else [targets]):
                with self.subTest(case=case, order=order):
                    game = self._install(spec["id"], order)
                    self.assertEqual(self._record(game, spec["id"]), _expected(case))
                    helper = os.path.join(game, "Mods", "ExampleDev.PcHelper")
                    if spec["id"] == "ExampleDev.ExampleMod" and "client_windows" in targets:
                        # Pulled in as ExampleMod's dependency, not installed directly.
                        self.assertEqual(self._record(game, "ExampleDev.PcHelper"),
                                         _expected("PcHelper.client_windows"))
                    elif spec["id"] == "ExampleDev.ExampleMod":
                        self.assertFalse(os.path.exists(helper))   # pc-only: pruned


class OfflineGolden(_Golden, unittest.TestCase):
    def setUp(self):
        super().setUp()
        repo = os.path.join(FIXTURES, "repo")

        def local(url):
            assert url.startswith(BASE + "/"), url
            return os.path.join(repo, *urlparse(url).path.lstrip("/").split("/"))

        def get_json(url, timeout=15):
            try:
                with open(local(url), "rb") as f:
                    return json.loads(f.read().decode("utf-8"))
            except FileNotFoundError:
                raise repos.ModManagerError(f"404 {url}")

        def download(url, dest, on_progress=None):
            shutil.copyfile(local(url), dest)

        stub(self, repos, "_get_json", get_json)
        stub(self, install, "_download_with_progress", download)


def _serving():
    try:
        with urllib.request.urlopen(f"{BASE}/repository.json", timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


class LiveGolden(_Golden, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _serving():
            raise unittest.SkipTest(f"no fixture repo is serving at {BASE}")


if __name__ == "__main__":
    unittest.main()
