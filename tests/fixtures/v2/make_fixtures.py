"""Builds the manifest-v2 golden fixtures straight from the spec
(TavernMods docs/REPO_STRUCTURE.md), independent of
either installer, so the Python and C# implementations can both be held to the
same bytes.

Output (under argv[1], default this folder; only repo/, expected/ and cases.json
are replaced):
  repo/                       a static mod repository, served at BASE by the e2e
    repository.json           index_version 2
    manifests/<author>/<repo>/<version>.json, latest.json, latest.<major>.json
    files/                    every artifact and library the manifests point at
  expected/<case>.json        the exact record bytes the spec requires
  cases.json                  what each expected record is the result of

Mods:
  ExampleDev.ExampleMod 1.2.0  one dll artifact for client_windows+server, plus a client_android zip
                               (ignored by installers); pins a library; scenes;
                               non-ASCII description; depends on PcHelper
  ExampleDev.PcHelper 1.0.0    client_windows only, so a server install prunes it
  ExampleDev.SplitMod 1.0.0    separate client_windows dll and server zip (nested files,
                               mixed case, to pin the ordinal sort of `files`)
"""
import hashlib
import io
import json
import os
import shutil
import sys
import zipfile

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
BASE = "http://127.0.0.1:8765"
REPO = os.path.join(OUT, "repo")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_bytes(rel, data):
    path = os.path.join(REPO, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return f"{BASE}/{rel}"


def zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members:
            # Fixed timestamp so the zip, and so its sha256, is reproducible.
            z.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), data)
    return buf.getvalue()


def dump(obj):
    """The spec's record formatting: 2-space indent, ": ", ASCII-escaped
    (lowercase \\uXXXX), CRLF, no trailing newline."""
    return json.dumps(obj, indent=2, ensure_ascii=True).replace("\n", "\r\n")


for _old in ("repo", "expected"):
    shutil.rmtree(os.path.join(OUT, _old), ignore_errors=True)
os.makedirs(REPO)

# -- artifacts -----------------------------------------------------------------
lib_data = b"MZ ExampleLib native stand-in"
lib = {"name": "ExampleLib", "download_url": write_bytes("files/ExampleLib.dll", lib_data),
       "sha256": sha(lib_data), "filename": "ExampleLib.dll"}

ex_dll = b"MZ ExampleMod pc+server build"
ex_quest = zip_bytes([("ExampleMod.dll", b"MZ quest build")])
helper_dll = b"MZ PcHelper"
split_pc = b"MZ SplitMod pc build"
split_server_members = [("B.dll", b"MZ B"), ("a/x.txt", b"x"), ("A.dll", b"MZ A"),
                        ("Assets/bundle.bin", b"\x00\x01")]
split_server = zip_bytes(split_server_members)

manifests = {
    ("ExampleDev", "ExampleMod", "1.2.0"): {
        "manifest_version": 2,
        "id": "ExampleDev.ExampleMod",
        "name": "Example Mod",
        "version": "1.2.0",
        "author": "ExampleDev",
        "description": "Ünïcödé ✓ check",
        "parity_required": True,
        "scenes": ["normal", "quest"],
        "dependencies": {"ExampleDev.PcHelper": "1.0.0"},
        "artifacts": [
            {"targets": ["client_windows", "server"],
             "download_url": write_bytes("files/ExampleMod.dll", ex_dll),
             "sha256": sha(ex_dll), "package": "dll",
             "library_dependencies": [lib]},
            {"targets": ["client_android"],
             "download_url": write_bytes("files/ExampleMod-quest.zip", ex_quest),
             "sha256": sha(ex_quest), "package": "zip",
             "library_dependencies": []},
        ],
    },
    ("ExampleDev", "PcHelper", "1.0.0"): {
        "manifest_version": 2,
        "id": "ExampleDev.PcHelper",
        "name": "PC Helper",
        "version": "1.0.0",
        "author": "ExampleDev",
        "description": "Windows client only",
        "parity_required": False,
        "dependencies": {},
        "artifacts": [
            {"targets": ["client_windows"],
             "download_url": write_bytes("files/PcHelper.dll", helper_dll),
             "sha256": sha(helper_dll), "package": "dll",
             "library_dependencies": []},
        ],
    },
    ("ExampleDev", "SplitMod", "1.0.0"): {
        "manifest_version": 2,
        "id": "ExampleDev.SplitMod",
        "name": "Split Mod",
        "version": "1.0.0",
        "author": "ExampleDev",
        "description": "different client and server builds",
        "parity_required": True,
        "dependencies": {},
        "artifacts": [
            {"targets": ["server"],
             "download_url": write_bytes("files/SplitMod-server.zip", split_server),
             "sha256": sha(split_server), "package": "zip",
             "library_dependencies": []},
            {"targets": ["client_windows"],
             "download_url": write_bytes("files/SplitMod-pc.dll", split_pc),
             "sha256": sha(split_pc), "package": "dll",
             "library_dependencies": []},
        ],
    },
}

index = {"index_version": 2, "mods": {}}
for (author, repo, version), m in manifests.items():
    text = json.dumps(m, indent=2, ensure_ascii=False)
    base = f"manifests/{author}/{repo}"
    for name in (f"{version}.json", "latest.json", f"latest.{version.split('.')[0]}.json"):
        write_bytes(f"{base}/{name}", text.encode("utf-8"))
    entry = {"name": m["name"], "author": m["author"], "description": m["description"],
             "parity_required": m["parity_required"],
             "targets": sorted({t for a in m["artifacts"] for t in a["targets"]})}
    if m.get("scenes") is not None:
        entry["scenes"] = m["scenes"]
    entry["versions"] = [version]
    index["mods"][m["id"]] = {version.split(".")[0]: entry}
write_bytes("repository.json", json.dumps(index, indent=2, ensure_ascii=False).encode("utf-8"))


# -- expected records ------------------------------------------------------------
def placed_files(artifact, mod_id):
    """{path relative to the target folder: sha256}, as an install places it."""
    if artifact["package"] == "zip":
        data = {"ExampleDev.ExampleMod": ex_quest, "ExampleDev.SplitMod": split_server}[mod_id]
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return {i.filename: sha(z.read(i)) for i in z.infolist() if not i.is_dir()}
    name = artifact["download_url"].rsplit("/", 1)[1]
    blob = {"ExampleMod.dll": ex_dll, "PcHelper.dll": helper_dll, "SplitMod-pc.dll": split_pc}[name]
    return {name: sha(blob)}


def record(m, targets):
    rec = {k: m[k] for k in ("manifest_version", "id", "name", "version", "author",
                             "description", "parity_required")}
    if m.get("scenes") is not None:
        rec["scenes"] = list(m["scenes"])
    rec["dependencies"] = dict(m["dependencies"])
    rec["artifacts"] = m["artifacts"]
    rec["source_repo"] = BASE
    installed = {}
    for t in sorted(targets):
        art = next(a for a in m["artifacts"] if t in a["targets"])
        files = placed_files(art, m["id"])
        installed[t] = {"sha256": art["sha256"],
                        "libraries": [l["filename"] for l in art["library_dependencies"]],
                        "files": {k: files[k] for k in sorted(files)}}
    rec["installed"] = installed
    return rec


by_id = {m["id"]: m for m in manifests.values()}
cases = {
    "ExampleMod.client_windows": ("ExampleDev.ExampleMod", ["client_windows"]),
    "ExampleMod.server": ("ExampleDev.ExampleMod", ["server"]),
    "ExampleMod.client_windows+server": ("ExampleDev.ExampleMod", ["client_windows", "server"]),
    "PcHelper.client_windows": ("ExampleDev.PcHelper", ["client_windows"]),
    "SplitMod.client_windows+server": ("ExampleDev.SplitMod", ["server", "client_windows"]),
    "SplitMod.server": ("ExampleDev.SplitMod", ["server"]),
}
os.makedirs(os.path.join(OUT, "expected"))
for name, (mod_id, targets) in cases.items():
    with open(os.path.join(OUT, "expected", name + ".json"), "w", encoding="ascii", newline="") as f:
        f.write(dump(record(by_id[mod_id], targets)))
with open(os.path.join(OUT, "cases.json"), "w", encoding="utf-8") as f:
    json.dump({"base_url": BASE,
               "cases": {k: {"id": v[0], "targets": sorted(v[1])} for k, v in cases.items()}},
              f, indent=2)
print("fixtures written to", OUT)
