# Mod manager design

A map of the community mod manager and the decisions the code doesn't explain.
Every format rule is in TavernMods'
[`docs/REPO_STRUCTURE.md`](https://github.com/ModdingTavern/TavernMods/blob/main/docs/REPO_STRUCTURE.md)
("the spec"), which TavernLib implements too: both must produce the same bytes
and verdicts.

## Pieces

| Where | What |
|---|---|
| `tavern_shared/mods/base.py` | error type, warn channel, version grammar, host config accessors |
| `layout.py`, `manifest.py` | on-disk paths; parsing the index and manifests |
| `repos.py`, `resolve.py` | sources and merged indexes; manifest fetching and the dependency closure |
| `install.py` | verified installs, the record writer, listing, verification, the handshake snapshot |
| `cache.py` | the client's cache of builds and libraries |
| `plan.py` | join planning, the comparison rows, rendering a plan |
| `modlist.py`, `pins.py` | modlist import/export; pins and declined recommendations |
| `ui.py` | Mod Manager, Community Mods, Mod Sources, Change Version |
| `client/core/mod_diff_window.py` | the pre-join comparison, which also shows the apply |
| `client/core/launcher_window.py` | Sync Mods and Join (`_reconcile_mods`), the mods claim, rejection recovery |
| `server/core/auth_service.py` | `mods_hash`/`mods_count` on ping and the framed `mods_list` reply |
| `tavern_shared/mods_window.py` | the Setup window (patch, MelonLoader, TavernLib), with `mod_install.py`, `patch.py`, `bundled.py` and `manual_install.py` behind it |

The client launcher installs `client_windows`, the server launcher `server`
(`LauncherCommon.MOD_TARGET`). One game folder often serves both, so each
lists, verifies, exports and claims only its own target.

## Decisions

- **One write path.** `install._place_target` is the only thing that places a
  target folder; a verified download (`install_mod`) and a verified cache
  copy (`cache_restore_mod`) both end there, so the spec's version rule is
  applied once.
- **Record bytes** come from `install._sidecar_bytes`, never a text-mode
  write. `tests/test_v2_golden.py` holds them to records generated from the
  spec alone.
- **Choices the spec leaves open**, made the same way as TavernLib: a known
  target listed twice is refused even beside an unknown package; a missing
  `package` is an unknown one; absent `library_dependencies` reads as `[]`;
  the record copies an artifact's known keys in compiled order, then the
  rest; JSON escapes follow Python's `json`.
- **Pre-v2 records** read as damaged, so a reinstall moves them to the
  per-target layout; hashing their files in place would bless any corruption.
  A version above everything indexed reads current, not outdated.
- **Resolution happens before any write**, conflicts included. A per-version
  manifest naming another id or version fails that repo like a 404. Repo
  hints (`source_repo` anywhere) only reorder the client's own trusted repos;
  only `add_repo`'s live check adds a source.
- **Join failures route by who asked.** A required mod that can't be
  installed blocks (`missing`, or `needs_repo` naming the source to add); a
  recommendation that can't be resolved is skipped; an unresolvable pin is a
  visible row that never blocks. Declining is "don't add", never "take away".
- **Sync and Join are one routine**: plan, compare, apply, then re-verify
  parity before launching, since a render can leave the set half-applied.
  The verify's single read of `Mods/` feeds the parity check, the
  same-version-different-build advisory and the `TavernMods` claim.
- **Deactivate, don't uninstall.** A join disables what the server doesn't
  run and caches it first, so switching back is a rename or a file move.
  `adopt_installed_mods` caches what's installed before any plan, so a render
  never destroys the only copy.
- **Two library rules on purpose.** `uninstall_mod` keeps libraries that
  disabled mods pin; the join's prune keeps only what enabled mods pin, since
  enabling re-runs a library pass (`ensure_mod_libraries` covers the Mod
  Manager's bare enable). Both count every target, so a client join never
  prunes the host's server-build libraries.
- **Dependencies are asked about, not assumed.** A Get Mods install plans
  first (`install.plan_mod_install`) and lists the dependencies not yet
  installed; the player may take the mod alone. `missing_dependencies` reads
  only install records, so "Needs mods" costs no network, and "Install deps"
  resolves from the record (`install_dependencies`), so it works for a
  version the source no longer lists.
- **Replaced mods** (the spec's `replaced`). `fetch_indexes` returns a
  `repos.ModIndex`, a list of summaries carrying every source's
  replacements, so no signature that takes an index changed. Only the source
  in an installed record's `source_repo` can retire it (`install.replaced_by`),
  so another source can't take a mod over. An id named without a source goes
  through `replacement_of_unlisted`, used by the resolver, pins and modlist
  import. Nothing switches by itself: the Mod Manager offers it, and any
  install from these windows ends with `retire_replaced`, which uninstalls
  the old mod once the new one covers its targets and moves its pin on.
  TavernLib's reconcile does the same for a modlist entry, but disables
  rather than uninstalls, as it does any unlisted mod.
- **Alerts.** Setup flashes for its three components; community mod updates
  and replaced mods are only a count on the Mod Manager button, since joins
  correct them.
- **Server side.** The auth service caches the handshake snapshot for 5 s per
  game folder, as TavernLib does, and only writes from Setup or the Mod
  Manager invalidate it. A running server loaded its mods at startup, so the
  server launcher warns that changes need a restart.
- **Untracked mods are never touched automatically**, with the spec's one
  exception (a loose DLL an install just shadowed), which TavernLib applies
  identically so both sides report the same untracked set.
- **Setup payloads.** Downloads are retried, length-checked and
  magic-checked. When they keep failing, the copy shipped in `Patch/`
  (checked against `bundled.json`) stands in only if it improves the
  install, and Setup offers step-by-step manual instructions.
  `dependencies/refresh_bundled_payloads.py` re-cuts `Patch/` at build time
  and swaps nothing in unless every download succeeded.

## Known limitations

- Parity is anti-footgun, not anti-cheat: the `TavernMods` claim is
  self-asserted.
- On a client, writes while the game runs fail on Windows file locks. The
  prompt (`game_guard.py`) only knows games this launcher started, and can be
  overridden.
- `_force_ipv4` makes every thread's lookups IPv4-only while any download
  holds it.
- The verify memo can report a stale "intact" for damage that arrives after a
  clean check, until Refresh or the mod's next install.
- Newtonsoft accepts JSON that `json.load` refuses, so a hand-edited record
  using comments or trailing commas is ours to TavernLib and foreign here.
- Disabling is one switch for the whole folder, so on a machine that plays and
  hosts it affects both launchers' builds; uninstall's confirmation says so.
- For the same reason, switching from a replaced mod installed for both
  targets keeps the old one until the other launcher installs the
  replacement too, and it stays marked "Replaced" until then.
- TavernEssentials' in-game board joins carry no `TavernMods` claim, so a
  server that requires mods refuses them. Only TavernEssentials can fix that.
