"""End-to-end manifest v2 check across both installers, against
tests/fixtures/v2 served at http://127.0.0.1:8765 (started here if nothing is
listening there already).

A host's client launcher (Python, target pc) and its server (TavernLib's
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
from tavern_shared.mods import install, plan, repos  # noqa: E402

with socket.socket() as _probe:
    _serving = _probe.connect_ex(("127.0.0.1", 8765)) == 0
if not _serving:
    class _Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_a):
            pass
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
    out = subprocess.run([HARNESS, *args], capture_output=True, text=True, timeout=300)
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
        install.install_mod_closure(game, root, index, [BASE], lambda _m: None, target)


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
        py_install(game, EXAMPLE, "pc"); py_install(game, SPLIT, "pc")
        cs_server_install(game, EXAMPLE, SPLIT)
    else:
        cs_server_install(game, EXAMPLE, SPLIT)
        py_install(game, EXAMPLE, "pc"); py_install(game, SPLIT, "pc")

    print(f"\n== shared folder, {order}")
    check(record(game, EXAMPLE) == expected("ExampleMod.pc+server"), f"{order}: ExampleMod record bytes")
    check(record(game, SPLIT) == expected("SplitMod.pc+server"), f"{order}: SplitMod record bytes")
    check(record(game, HELPER) == expected("PcHelper.pc"), f"{order}: PcHelper record bytes")
    for mod_id in (EXAMPLE, SPLIT):
        for t in ("pc", "server"):
            check(os.path.isdir(os.path.join(game, "Mods", mod_id, "." + t)), f"{order}: {mod_id}/.{t} exists")
            check(install.verify_mod_files(game, mod_id, t) is True, f"{order}: {mod_id} {t} verifies (Python)")
        check(not any(f.endswith(".dll") for f in os.listdir(os.path.join(game, "Mods", mod_id))),
              f"{order}: nothing loadable at {mod_id}/ root")

    # Server handshake: both implementations, same folder.
    py_hash, py_count, py_mods, py_untracked = install.handshake_snapshot(game, target="server")
    cs = harness_json("snap", game)
    check(cs.get("hash") == py_hash, f"{order}: mods_hash identical (py {py_hash[:12]} / cs {str(cs.get('hash'))[:12]})")
    check(cs.get("count") == py_count == 2, f"{order}: count 2 on both (PcHelper is pc-only)")
    norm = lambda ms: sorted(json.dumps(m, sort_keys=True) for m in ms)
    check(norm(cs.get("mods", [])) == norm(py_mods), f"{order}: mods_list entries identical")
    check({m["id"] for m in py_mods} == {EXAMPLE, SPLIT}, f"{order}: server lists only server-installed mods")

game = games[0]

print("\n== which folders MelonLoader loads")
for role, own, other in (("server", ".server", ".pc"), ("pc", ".pc", ".server")):
    dirs = select(game, role)
    for mod_id in (EXAMPLE, SPLIT):
        check(any(d.endswith(f"/{mod_id}/{own}") for d in dirs), f"{role}: loads {mod_id}/{own}")
        check(not any(f"/{mod_id}/{other}" in d for d in dirs), f"{role}: never loads {mod_id}/{other}")
    check(any(d.endswith(f"/{HELPER}/.pc") for d in dirs) == (role == "pc"),
          f"{role}: PcHelper/.pc loaded only for pc")
    if role == "server":
        check(any(d.endswith(f"/{SPLIT}/.server/Assets") for d in dirs), "server: zip subfolders load too")
# A disabled record loads nothing for either role.
mod_dir = os.path.join(game, "Mods", SPLIT)
os.replace(os.path.join(mod_dir, "manifest.json"), os.path.join(mod_dir, "manifest.disabled.json"))
for role in ("server", "pc"):
    dirs = select(game, role)
    check(not any(SPLIT in d for d in dirs), f"{role}: disabled SplitMod loads nothing")
os.replace(os.path.join(mod_dir, "manifest.disabled.json"), os.path.join(mod_dir, "manifest.json"))

print("\n== join verdicts (TavernLib)")
both = json.dumps({EXAMPLE: "1.2.0", SPLIT: "1.0.0"})
for claim, allowed, label in (
        (both, True, "claim with both exact versions"),
        (json.dumps({EXAMPLE: "1.2.0"}), False, "claim missing SplitMod"),
        (json.dumps({EXAMPLE: "1.1.0", SPLIT: "1.0.0"}), False, "claim with the wrong ExampleMod version"),
        ("none", False, "no claim (Quest standalone), parity mods with pc targets"),
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
hand_record(g2, "T.QuestOnly", ["server", "quest"], True)
check(admitted(g2, "none") is False, "parity: no claim vs required server+quest mod -> denied")
check(admitted(g2, "{}") is True, "parity: PC claim vs required server+quest mod (no pc build) -> allowed")
g3 = new_game(); games.append(g3)
hand_record(g3, "T.Optional", ["server", "pc"], False)
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
check(HELPER in reasons, "plan: ExampleMod's pc-only dependency is pulled in for pc")

print("\n== damage repair on the server side")
victim = os.path.join(game, "Mods", SPLIT, ".server", "A.dll")
with open(victim, "wb") as f:
    f.write(b"corrupted")
install._verify_memo.clear()     # what the Mod Manager's Refresh does; see the design doc
check(install.verify_mod_files(game, SPLIT, "server") is False, "python sees the server build as damaged")
check(install.verify_mod_files(game, SPLIT, "pc") is True, "python sees the pc build as intact")
check(harness("verify", game, SPLIT, "server").strip() == "False", "C# sees the server build as damaged")
check(harness("verify", game, SPLIT, "pc").strip() == "True", "C# sees the pc build as intact")
install._verify_memo.clear()
cs_server_install(game, EXAMPLE, SPLIT)
check(record(game, SPLIT) == expected("SplitMod.pc+server"), "after C# repair: record bytes still golden")
check(install.verify_mod_files(game, SPLIT, "server") is True, "after C# repair: server build verifies")
check(install.verify_mod_files(game, SPLIT, "pc") is True, "after C# repair: pc build untouched")

for g in games:
    shutil.rmtree(g, ignore_errors=True)
print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'}")
sys.exit(1 if failures else 0)
