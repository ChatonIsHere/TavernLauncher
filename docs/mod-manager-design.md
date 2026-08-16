# Mod manager design

The community mod system spans four repos, and this file is the map: what each
piece owns, the contracts between them, and the reasoning behind the decisions
that aren't obvious from any single file. Docstrings carry the local detail;
this records the cross-cutting shape.

## The four parts

1. **CommunityMods** — the index. Submissions merge to a protected `main`; a
   GitHub App rebuilds `repository.json` (slim browse data), per-version
   manifests (`manifests/<author>/<repo>/<version>.json`, immutable), and
   `latest*.json` pointers. Install-critical facts (download URL, sha256,
   dependencies) live **only** in the per-version manifest so index and
   manifest can't drift.
2. **TavernLauncher `tavern_shared/mods/`** — the Python manager both
   launchers share: fetch, resolve, verified install, cache, join planning.
   The UI windows only render what this package computes.
3. **TavernLib** — the C# twin for what must run inside the game process:
   the join-time parity check, the handshake snapshot, and the headless
   server's boot-time reconciler (`ModReconciler`). Several shapes are
   deliberately implemented twice (see "Cross-language invariants").
4. **The wire** — the launcher's auth service (TCP 1762) answers `ping` (with
   `mods_hash`/`mods_count`) and `mods_list` (length-prefixed JSON). TavernLib
   produces the same shapes for headless servers, so a client can't tell the
   two apart.

## On-disk contracts

Any other installer of these mods must match these byte-for-byte
(TavernLib's `ModInstaller` does):

- Each mod installs into **its own folder** `Mods/<id>/`; the record is
  `Mods/<id>/manifest.json` — the fetched manifest verbatim plus install
  fields. The filename is load-bearing: recent MelonLoader only scans a
  `Mods/` subfolder containing a `manifest.json` (existence check, content
  unread), so the record doubles as the enable marker.
- **Disable = rename** the record to `manifest.disabled.json`. Files stay put;
  MelonLoader stops scanning. Uninstall = delete the folder.
- The record carries `files`: `{relative path: sha256}` of everything the
  installer placed, hashed from staging before the record was written. This is
  the damage-detection reference (see "Integrity layers"). Records written
  before the field existed — or by TavernLib's C# installer, which doesn't
  write it — simply have no damage detection: `verify_mod_files` returns
  `None` there, never `False`.
- Libraries (`library_dependencies`) install **flat** into `UserLibs/` with a
  `<filename>.meta.json` sidecar; the record's `libraries` list is how
  removal reference-counts them. Native DLLs must be libraries because
  MelonLoader only registers `UserLibs/` as a native search path.
- A `Mods/<name>/` folder that isn't ours (no usable record) is **displaced**
  into `Mods/.displaced/`, never deleted. "Ours" is one predicate everywhere:
  record has an `id` and a usable `parity_required` (`_owned_mod_record`,
  and TavernLib's `ModRecord.Read` by construction).
- The client cache lives at `%AppData%/TheModdingTavern/mod_cache/<id>/<version>/`
  (libraries under `_libraries/<filename>/<sha256>/`). Bounded: a new store
  prunes a mod's oldest entries past `_CACHE_KEEP_VERSIONS`.

## Resolution

- Versions are strict `MAJOR.MINOR.PATCH`; no pre-release forms, so ordering
  never guesses.
- Dependencies resolve **minimum-version-with-major-lock**: within the
  required major, take the highest indexed version ≥ the running minimum.
  Cross-major demands from two requirers are a named diamond conflict.
  Cycles and over-deep chains (> 5) raise with the chain spelled out.
- Every closure is resolved **for a side** ("client"/"server"). A dependency
  that can't load on that side is pruned before its constraints are recorded,
  along with everything only it needed — reported via `_warn`, never silent.
- All resolution/conflict checks happen **before any disk write**
  (`install_mod_closure`, `apply_import`).

## The join flow (client)

`plan_join` computes: required(server) ∪ recommended(server, not declined) ∪
dependency closure ∪ user pins — then diffs against disk and cache. The
failure routing is by **who asked**, because the consequences differ:

| origin | failure lands in | effect |
|---|---|---|
| server, `parity_required` | `missing` / `needs_repo` | blocks the join |
| server, recommended | (skipped, warn) | never blocks, never nags |
| user pin | `unresolved_pins` | visible row, never blocks |

`needs_repo` (required, resolvable only from a repo the client hasn't added)
**blocks**, same as `missing`. It didn't originally — it was "information to
act on" — but a required mod that won't be installed can only end one way: the
render succeeds, then the post-render parity check fails with "mods still
don't match". Offering Apply was a button that could never succeed. The
distinction that matters survives in the row: `needs_repo` names the exact
source to add; `missing` has nothing to suggest.

The diff window shows one row per mod, problems first. Only two kinds of row
are the user's call: recommendations (Install/Skip; declines persist per
server) and deactivations of mods the server doesn't run (Deactivate/Keep/
**Pin** — Pin also records an always-on pin, the durable form of Keep, so the
next join stops proposing the deactivation).

Render order: enable-by-rename where the exact version is on disk disabled →
cache restore where cached (falling back to a verified download if the cache
entry fails its restore-time check) → verified download otherwise → library
pass → deactivations (cache first, then disable) → orphan-library prune. A
failed render leaves each mod folder whole (staged, atomic swaps) but the
*set* possibly half-applied — which is why the launch path re-verifies parity
after every render and refuses to launch on a mismatch.

Post-join rejection recovery: a server-side denial makes the game write
`last_rejection.json`; the launcher validates it, resolves the named
mods+versions through the client's **own** repos only, installs, and offers a
relaunch.

## Cross-language invariants

- **`mods_hash`**: sha256 of newline-joined lines — id-sorted
  `id@version@req|opt` for managed mods, then lowercase-name-sorted
  `untracked:<name>@<mtime>` lines. Byte-identical between
  `install.handshake_snapshot` and TavernLib's `ModHandshake`. Changing the
  separator, ordering, or participating fields silently breaks every join
  against the other side's build. `parity_required` is in it (a server can
  flip required↔recommended without a version bump); untracked stamps are in
  it (a hand-dropped DLL must move the hash). The handshake entry's `sha256`
  field is **not** in it — additive, advisory.
- **The record shape** and the "is this folder ours" predicate (above).
- **`_untracked_stamp`**: mtime alone, not mtime+size (a directory has no
  portable size both languages agree on).

## Trust model

- **Version parity is anti-footgun, not anti-cheat.** The client's
  `TavernMods` JWT claim is self-asserted; `ModParity.ValidateClient` exists
  for stale pre-join checks, direct connects, and older launchers — a
  modified client can lie. Malformed/absent claims fail closed.
- **Same version, different bytes** passes version parity. Handshake entries
  now carry the record's `sha256` on both implementations, and the client
  logs an advisory when the builds diverge — it can't fix it (its repos serve
  what they serve), but the divergence stops being undiagnosable.
- **Repo hints never authorize pulls.** `source_repo` in any payload
  (handshake, rejection, modlist `repos`) is display/suggestion only.
  Pullable sources are exactly: the default repo, plus repos added through
  the live-validated `add_repo` (launcher) / the TrustedRepos allow-list
  (headless). Third-party repos get an explicit "unverified" warning at add
  time, and the hostile-input surface (filenames, ids, zip members: traversal,
  colons/ADS, symlinks, bombs) is re-checked at write time regardless of
  where the manifest came from.

## Integrity layers

1. Download: sha256 from the manifest, verified before anything is written;
   staged assembly, atomic swap; temp files on the destination volume.
2. Install record: per-file `files` map → `verify_mod_files` → the
   `damaged` status in both windows (and the launcher buttons' attention
   count). Verification is memoized per install (zip bundles can be huge);
   an install/restore of that mod invalidates its entry, the manager's
   Refresh clears the memo whole.
3. Cache: `cache_restore_mod` re-verifies the staged copy against the
   record's `files` before swapping it in; a rotten entry is discarded and
   the render falls back to a fresh download.
4. Post-render parity re-check before launch; server-side check at join.

## Untracked mods

Anything MelonLoader would load that this manager didn't install: a loose
`Mods/*.dll`, or a folder with a foreign/unusable `manifest.json`. All a
server can honestly say about one is its name and shape — no id, version, or
source — so clients can **see** them (advisory lines, part of the handshake
hash) but are never blocked over them, and no importer ever resolves one from
a modlist's `untracked` block. Surfacing-and-toggling only, by design: any
install/update path for them would be guessing identities by filename.

## Deliberate asymmetries (easy to "fix" wrongly)

- `uninstall_mod` counts **disabled** mods as still needing their libraries;
  the join flow's `_prune_orphaned_libraries` counts **enabled** only. The
  join flow can afford the tighter rule because enabling there re-runs a
  library pass, and `ensure_mod_libraries` repairs the manager-window bare
  re-enable. Uninstall has no later pass, so it must keep a disabled mod's
  libraries or break its next enable.
- A mod whose installed version *exceeds* everything indexed reads `current`,
  not `outdated`/`unknown`: nothing newer exists to offer, and flagging a
  working install over index bookkeeping would be a false alarm.
- Declining is "don't add", never "take away". A decline never uninstalls or
  deactivates something already present.
- The Setup alert flashes for the three core components; community-mod
  updates are a quiet count on the Mod Manager button. Joins force-correct
  the mods that matter per server, so an available update is information,
  not an emergency.

## Known limitations

- Parity is enforceable only as far as the client is honest (see Trust
  model). Signed attestation of loaded assemblies is out of scope.
- On a launcher-run server, the handshake and join checks read **disk**, but
  the running session loaded its mods at startup: changing mods while the
  server is up advertises a set the live session isn't running. The server
  launcher warns and asks for a restart; it cannot restart the game safely
  itself.
- The verify memo can report a stale "intact" for damage that arrives after
  a clean check (see Integrity layers) — bounded by Refresh and by every
  install/restore of the mod.
- TavernLib's C# installer doesn't write the `files` map yet, so headless
  installs have no damage detection until it does.
