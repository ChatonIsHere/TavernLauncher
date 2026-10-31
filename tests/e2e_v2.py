"""End-to-end manifest v2 check across both installers, against
tests/fixtures/v2 served at http://127.0.0.1:8765 (started here if nothing is
listening there already).

A host's client launcher (Python, target client_windows) and its server (TavernLib's
reconciler, target server) share one game folder. Checks, in both install
orders: record bytes against the spec's golden files, the two implementations'
server handshakes against each other, which folders MelonLoader would load per
process, the join verdict matrix, a client plan built from the C# server's
mods list, and damage repair.

Usage: python tests/e2e_v2.py <TavernLib's tests/Harness/bin/Debug/Harness.exe>"""
import contextlib
import functools
import http.server
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

HARNESS = sys.argv[1]
LAUNCHER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(LAUNCHER, "tests", "fixtures", "v2")
sys.path.insert(0, LAUNCHER)
from tavern_shared.mods import install, modlist, plan, repos  # noqa: E402
from conftest import install_mod_closure  # noqa: E402

class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_a):
        pass


with socket.socket() as _probe:
    _serving = _probe.connect_ex(("127.0.0.1", 8765)) == 0
if not _serving:
    _handler = functools.partial(_Quiet, directory=os.path.join(FIXTURES, "repo"))
    _server = http.server.ThreadingHTTPServer(("127.0.0.1", 8765), _handler)
    threading.Thread(target=_server.serve_forever, daemon=True).start()

BASE = "http://127.0.0.1:8765"
EXAMPLE, SPLIT, HELPER = "ExampleDev.ExampleMod", "ExampleDev.SplitMod", "ExampleDev.PcHelper"
failures = []


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)


def harness(*args):
    out = subprocess.run([HARNESS, *args], capture_output=True, encoding="utf-8", timeout=300)
    if out.returncode != 0:
        print(out.stdout, out.stderr)
        raise RuntimeError(f"harness {args[0]} exited {out.returncode}")
    return out.stdout


def harness_json(*args):
    """The JSON document the harness printed to stdout (indented or not)."""
    out = harness(*args)
    start = min((i for i in (out.find("{"), out.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise RuntimeError(f"no JSON from harness {args}")
    return json.loads(out[start:])


def select(game, role):
    """The mod folders MelonLoader would load for `role`, forward-slashed."""
    return [l.strip().replace("\\", "/") for l in harness("select", game, role).splitlines() if l.strip()]


def admitted(game, claim):
    verdict = harness("parity", game, claim).strip().splitlines()[-1]
    if not verdict.startswith(("admit", "refuse")):
        raise RuntimeError(f"unexpected parity output: {verdict!r}")
    return verdict.startswith("admit")


def py_install(game, mod_id, target):
    index = repos.fetch_indexes([BASE], force=True)
    root = next(s for s in index if s.id == mod_id)
    with contextlib.redirect_stderr(io.StringIO()):
        install_mod_closure(game, root, index, [BASE], lambda _m: None, target)


def cs_server_install(game, *mod_ids):
    harness("reconcile", game, BASE, ",".join(mod_ids))


def record(game, mod_id):
    with open(os.path.join(game, "Mods", mod_id, "manifest.json"), "rb") as f:
        return f.read()


def expected(case):
    with open(os.path.join(FIXTURES, "expected", case + ".json"), "rb") as f:
        return f.read()


def new_game():
    game = tempfile.mkdtemp(prefix="v2e2e_")
    os.makedirs(os.path.join(game, "Mods"))
    return game


games = []
for order in ("python-first", "csharp-first"):
    game = new_game()
    games.append(game)
    if order == "python-first":
        py_install(game, EXAMPLE, "client_windows"); py_install(game, SPLIT, "client_windows")
        cs_server_install(game, EXAMPLE, SPLIT)
    else:
        cs_server_install(game, EXAMPLE, SPLIT)
        py_install(game, EXAMPLE, "client_windows"); py_install(game, SPLIT, "client_windows")

    print(f"\n== shared folder, {order}")
    check(record(game, EXAMPLE) == expected("ExampleMod.client_windows+server"), f"{order}: ExampleMod record bytes")
    check(record(game, SPLIT) == expected("SplitMod.client_windows+server"), f"{order}: SplitMod record bytes")
    check(record(game, HELPER) == expected("PcHelper.client_windows"), f"{order}: PcHelper record bytes")
    for mod_id in (EXAMPLE, SPLIT):
        for t in ("client_windows", "server"):
            check(os.path.isdir(os.path.join(game, "Mods", mod_id, "." + t)), f"{order}: {mod_id}/.{t} exists")
            check(install.verify_mod_files(game, mod_id, t) is True, f"{order}: {mod_id} {t} verifies (Python)")
        check(not any(f.endswith(".dll") for f in os.listdir(os.path.join(game, "Mods", mod_id))),
              f"{order}: nothing loadable at {mod_id}/ root")

    # Server handshake: both implementations, same folder.
    py_hash, py_count, py_mods, py_untracked = install.handshake_snapshot(game, target="server")
    cs = harness_json("snap", game)
    check(cs.get("hash") == py_hash, f"{order}: mods_hash identical (py {py_hash[:12]} / cs {str(cs.get('hash'))[:12]})")
    check(cs.get("count") == py_count == 2, f"{order}: count 2 on both (PcHelper is client_windows-only)")
    norm = lambda ms: sorted(json.dumps(m, sort_keys=True) for m in ms)
    check(norm(cs.get("mods", [])) == norm(py_mods), f"{order}: mods_list entries identical")
    check({m["id"] for m in py_mods} == {EXAMPLE, SPLIT}, f"{order}: server lists only server-installed mods")

game = games[0]

print("\n== which folders MelonLoader loads")
for role, own, other in (("server", ".server", ".client_windows"), ("client_windows", ".client_windows", ".server")):
    dirs = select(game, role)
    for mod_id in (EXAMPLE, SPLIT):
        check(any(d.endswith(f"/{mod_id}/{own}") for d in dirs), f"{role}: loads {mod_id}/{own}")
        check(not any(f"/{mod_id}/{other}" in d for d in dirs), f"{role}: never loads {mod_id}/{other}")
    check(any(d.endswith(f"/{HELPER}/.client_windows") for d in dirs) == (role == "client_windows"),
          f"{role}: PcHelper/.client_windows loaded only for client_windows")
    if role == "server":
        check(any(d.endswith(f"/{SPLIT}/.server/Assets") for d in dirs), "server: zip subfolders load too")
# A disabled record loads nothing for either role.
mod_dir = os.path.join(game, "Mods", SPLIT)
os.replace(os.path.join(mod_dir, "manifest.json"), os.path.join(mod_dir, "manifest.disabled.json"))
for role in ("server", "client_windows"):
    dirs = select(game, role)
    check(not any(SPLIT in d for d in dirs), f"{role}: disabled SplitMod loads nothing")
os.replace(os.path.join(mod_dir, "manifest.disabled.json"), os.path.join(mod_dir, "manifest.json"))

print("\n== join verdicts (TavernLib)")
both = json.dumps({EXAMPLE: "1.2.0", SPLIT: "1.0.0"})
for claim, allowed, label in (
        (both, True, "claim with both exact versions"),
        (json.dumps({EXAMPLE: "1.2.0"}), False, "claim missing SplitMod"),
        (json.dumps({EXAMPLE: "1.1.0", SPLIT: "1.0.0"}), False, "claim with the wrong ExampleMod version"),
        ("none", False, "no claim (Quest standalone), parity mods with client targets"),
):
    check(admitted(game, claim) == allowed, f"parity: {label} -> {'allowed' if allowed else 'denied'}")


def hand_record(game, mod_id, targets, parity):
    """A server-installed record written straight from the spec, for verdict
    cases the fixture repo doesn't cover."""
    d = os.path.join(game, "Mods", mod_id, ".server")
    os.makedirs(d)
    with open(os.path.join(d, "X.dll"), "wb") as f:
        f.write(b"MZ")
    sha = "0" * 64
    import hashlib
    file_sha = hashlib.sha256(b"MZ").hexdigest()
    rec = {"manifest_version": 2, "id": mod_id, "name": mod_id, "version": "1.0.0",
           "author": "T", "description": "", "parity_required": parity, "dependencies": {},
           "artifacts": [{"targets": targets, "download_url": "http://127.0.0.1:8765/x",
                          "sha256": sha, "package": "dll", "library_dependencies": []}],
           "source_repo": BASE,
           "installed": {"server": {"sha256": sha, "libraries": [], "files": {"X.dll": file_sha}}}}
    with open(os.path.join(game, "Mods", mod_id, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2)


g2 = new_game(); games.append(g2)
hand_record(g2, "T.QuestOnly", ["server", "client_android"], True)
check(admitted(g2, "none") is False, "parity: no claim vs required server+client_android mod -> denied")
check(admitted(g2, "{}") is True, "parity: PC claim vs required server+client_android mod (no client_windows build) -> allowed")
g3 = new_game(); games.append(g3)
hand_record(g3, "T.Optional", ["server", "client_windows"], False)
hand_record(g3, "T.ServerOnly", ["server"], True)
check(admitted(g3, "none") is True, "parity: no claim vs optional + server-only required mods -> allowed")
check(admitted(g3, "{}") is True, "parity: empty claim vs optional + server-only required -> allowed")

print("\n== a fresh PC client plans against the C# server's mods list")
client = new_game(); games.append(client)
server_mods = harness_json("snap", game)["mods"]
index = repos.fetch_indexes([BASE], force=True)
with contextlib.redirect_stderr(io.StringIO()):
    jp = plan.plan_join(client, server_mods, index, [BASE], pinned=[], declined=[])
reasons = {e.mod_id: e.reason for e in jp.entries}
check(reasons.get(EXAMPLE) == "required" and reasons.get(SPLIT) == "required",
      f"plan: both parity mods required ({reasons})")
check(HELPER in reasons, "plan: ExampleMod's client_windows-only dependency is pulled in for client_windows")

print("\n== damage repair on the server side")
victim = os.path.join(game, "Mods", SPLIT, ".server", "A.dll")
with open(victim, "wb") as f:
    f.write(b"corrupted")
install._verify_memo.clear()     # what the Mod Manager's Refresh does; see the design doc
check(install.verify_mod_files(game, SPLIT, "server") is False, "python sees the server build as damaged")
check(install.verify_mod_files(game, SPLIT, "client_windows") is True, "python sees the client_windows build as intact")
check(harness("verify", game, SPLIT, "server").strip() == "False", "C# sees the server build as damaged")
check(harness("verify", game, SPLIT, "client_windows").strip() == "True", "C# sees the client_windows build as intact")
install._verify_memo.clear()
before = record(game, SPLIT)
with open(victim, "rb"):         # a running server holding its build open
    cs_server_install(game, EXAMPLE, SPLIT)
check(record(game, SPLIT) == before and install.verify_mod_files(game, SPLIT, "server") is False,
      "C# repair while .server is in use: refused, record untouched")
install._verify_memo.clear()
cs_server_install(game, EXAMPLE, SPLIT)
check(record(game, SPLIT) == expected("SplitMod.client_windows+server"), "after C# repair: record bytes still golden")
check(install.verify_mod_files(game, SPLIT, "server") is True, "after C# repair: server build verifies")
check(install.verify_mod_files(game, SPLIT, "client_windows") is True, "after C# repair: client_windows build untouched")

print("\n== a replaced mod (the fixture repo retires ExampleDev.OldSplit for SplitMod)")
OLD = "ExampleDev.OldSplit"
g4 = new_game(); games.append(g4)
cs_server_install(g4, OLD)
ids = sorted(r["id"] for r in install.list_installed_mods(g4))
check(ids == [SPLIT], f"C# reconcile: a modlist entry for a replaced mod installs its replacement ({ids})")
index = repos.fetch_indexes([BASE], force=True)
check([(r.old_id, r.by) for r in index.replaced] == [(OLD, SPLIT)], "python: the index carries the replacement")
with contextlib.redirect_stderr(io.StringIO()):
    imported = modlist.import_modlist({"schema": 1, "mods": [OLD]}, index, [BASE], "server")
check([m.id for m in imported.roots] == [SPLIT] and imported.replaced == [(OLD, SPLIT)],
      "python import: the same swap")

rdir = tempfile.mkdtemp(prefix="v2rep_"); games.append(rdir)
with open(os.path.join(FIXTURES, "repo", "repository.json"), encoding="utf-8") as f:
    served = json.load(f)
with open(os.path.join(FIXTURES, "repo", "manifests", "ExampleDev", "SplitMod", "1.0.0.json"), encoding="utf-8") as f:
    split = json.load(f)
root = dict(split, id="ExampleDev.Root", dependencies={OLD: "1.0.0"})
for rel, obj in (("index.json", {SPLIT: served["mods"][SPLIT]}), ("replaced.json", served["replaced"]),
                 (f"{SPLIT}/1.0.0.json", split), ("ExampleDev.Root/1.0.0.json", root)):
    os.makedirs(os.path.dirname(os.path.join(rdir, rel)), exist_ok=True)
    with open(os.path.join(rdir, rel), "w", encoding="utf-8") as f:
        json.dump(obj, f)
resolved = harness("resolve", rdir, "ExampleDev.Root@1.0.0", "server").strip().splitlines()[-1]
check(resolved == f"ok {SPLIT}@1.0.0", f"C# resolver: a dependency on a replaced mod gets its replacement ({resolved})")

print("\n== a copied mod folder is untracked, never a second install")
copy = os.path.join(game, "Mods", SPLIT + " - Copy")
shutil.copytree(os.path.join(game, "Mods", SPLIT), copy)
check(not any(" - Copy/." in d for d in select(game, "server")), "C#: the copy's build doesn't load")
py_untracked = [u["name"] for u in install.list_untracked_mods(game)]
cs_untracked = [u["name"] for u in harness_json("snap", game)["untracked"]]
check(py_untracked == cs_untracked == [SPLIT + " - Copy"], f"both list the copy as untracked ({py_untracked} / {cs_untracked})")
check(harness_json("snap", game)["hash"] == install.handshake_snapshot(game, "server")[0], "mods_hash still identical")
shutil.rmtree(copy)

print("\n== handshake order and stamps for awkward names")
g5 = new_game(); games.append(g5)
for mod_id in ("T.\ue000", "T.\U0001f600", "T.a"):
    hand_record(g5, mod_id, ["server", "client_windows"], True)
loose = os.path.join(g5, "Mods", "Loose.dll")
open(loose, "wb").close()
os.utime(loose, ns=(1_700_000_000_999_999_900,) * 2)
cs = harness_json("snap", g5)
py_hash, _count, py_mods, _u = install.handshake_snapshot(g5, "server")
check(cs["hash"] == py_hash, "mods_hash identical with astral ids and a sub-second mtime")
check([m["id"] for m in cs["mods"]] == [m["id"] for m in py_mods], "mods_list order identical")

print("\n== record bytes for awkward strings")
fmt_dir = tempfile.mkdtemp(prefix="v2fmt_"); games.append(fmt_dir)
awkward = {"a": "DEL\x7f ctl\x01 \u00e9 \U0001f600 \\ \"q\"", "n": [1, -2, {"k": None, "t": True}]}
with open(os.path.join(fmt_dir, "in.json"), "w", encoding="utf-8") as f:
    json.dump(awkward, f, ensure_ascii=False)
harness("format", os.path.join(fmt_dir, "in.json"), os.path.join(fmt_dir, "out.json"))
with open(os.path.join(fmt_dir, "out.json"), "rb") as f:
    check(f.read() == install._sidecar_bytes(awkward), "ModRecord.Format matches _sidecar_bytes byte for byte")
named = dict(split, artifacts=[dict(split["artifacts"][1], download_url="https://x/v1/My%20Mod.dll")])
with open(os.path.join(fmt_dir, "named.json"), "w", encoding="utf-8") as f:
    json.dump(named, f)
cs_name = harness("manifest", os.path.join(fmt_dir, "named.json")).split("name=")[-1].strip()
py_name = plan._manifest_from_dict(named, BASE).artifacts[0].install_name(SPLIT)
check(cs_name == py_name == "My Mod.dll", f"dll install name percent-decoded on both sides ({py_name} / {cs_name})")

print("\n== reconcile against a source that changes (scratch repo on :8766)")
scratch = tempfile.mkdtemp(prefix="v2src_"); games.append(scratch)
shutil.copytree(os.path.join(FIXTURES, "repo"), scratch, dirs_exist_ok=True)
SCRATCH = "http://127.0.0.1:8766"
_scratch_server = http.server.ThreadingHTTPServer(
    ("127.0.0.1", 8766), functools.partial(_Quiet, directory=scratch))
threading.Thread(target=_scratch_server.serve_forever, daemon=True).start()
with open(os.path.join(FIXTURES, "repo", "manifests", "ExampleDev", "PcHelper", "1.0.0.json"), encoding="utf-8") as f:
    helper = json.load(f)
art = dict(helper["artifacts"][0], targets=["server"])


def publish(mod_id, deps=None, manifest=True):
    """Lists mod_id 1.0.0 in the scratch index, a server build of PcHelper's dll."""
    if manifest:
        m = dict(helper, id=mod_id, name=mod_id, parity_required=False, dependencies=deps or {},
                 artifacts=[art])
        folder = os.path.join(scratch, "manifests", *mod_id.split(".", 1))
        os.makedirs(folder, exist_ok=True)
        for leaf in ("1.0.0.json", "latest.json"):
            with open(os.path.join(folder, leaf), "w", encoding="utf-8") as f:
                json.dump(m, f)
    served["mods"][mod_id] = {"1": dict(served["mods"][HELPER]["1"], name=mod_id, targets=["server"])}
    save_index()


def save_index():
    with open(os.path.join(scratch, "repository.json"), "w", encoding="utf-8") as f:
        json.dump(served, f)


def enabled(game_dir):
    return {r["id"]: r.get("enabled") for r in install.list_installed_mods(game_dir)}


g6 = new_game(); games.append(g6)
publish("ExampleDev.NeedsOld", {OLD: "1.0.0"})
for run in (1, 2):
    harness("reconcile", g6, SCRATCH, "ExampleDev.NeedsOld")
    check(enabled(g6).get(SPLIT) is True, f"run {run}: a dependency on a replaced mod keeps its replacement enabled")

g7 = new_game(); games.append(g7)
publish("ExampleDev.Keeper")
harness("reconcile", g7, SCRATCH, "ExampleDev.Keeper")
del served["mods"]["ExampleDev.Keeper"]
served["replaced"]["ExampleDev.Keeper"] = {"by": "ExampleDev.Ghost"}
publish("ExampleDev.Ghost", manifest=False)
harness("reconcile", g7, SCRATCH, "ExampleDev.Keeper")
check(enabled(g7) == {"ExampleDev.Keeper": True}, f"a replacement that can't be fetched keeps the listed mod ({enabled(g7)})")

os.makedirs(os.path.join(scratch, "v1"))
with open(os.path.join(scratch, "v1", "repository.json"), "w", encoding="utf-8") as f:
    json.dump({"index_version": 1, "mods": {}}, f)
check(harness("validaterepo", SCRATCH).strip().splitlines()[-1] == "ok", "addrepo accepts a v2 index")
check(harness("validaterepo", SCRATCH + "/v1").strip().splitlines()[-1].startswith("refused: That repository's index is schema v1"),
      "addrepo refuses a v1 index")
_scratch_server.shutdown()

for g in games:
    shutil.rmtree(g, ignore_errors=True)
print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'}")
sys.exit(1 if failures else 0)
