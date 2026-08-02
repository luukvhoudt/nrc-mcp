# Spec corrections

`netradiant-mcp-spec.md` was written partly from documentation and partly from
recollection, and it says so: §13 asks that the Urban Terror entity ontology be verified
"from real sources — the gamepack defs and released map files — not from documentation
alone and not from any model's recollection, including mine."

That verification has now been done, against upstream source at
`10165e88d118c97c4cd430e396f27fa759ac8b9f` (2026-07-09), the installed
NetRadiant-custom-20240309 Urban Terror gamepack, and 26 real `.map` files.

**Several claims did not survive.** Three of them were listed in the spec as *verified*
hard-validator rules, and implementing them as written would have produced validators that
fail correct maps. They are recorded here rather than silently fixed, because the spec is a
living document and the next person to read it needs to know which parts were checked.

Severity key: **W** = wrong, implementing as written causes incorrect behaviour;
**M** = misleading; **U** = unverified, do not ship as a hard rule.

---

## 1. Urban Terror rules (§7.1) — the important ones

### W1. Team Survivor does *not* need dedicated spawn entities

> §7.1: "**Team Survivor needs its own dedicated spawn entities** — TS cannot be shared
> with other gametypes via `g_gametype`."

**Wrong.** The gamepack lists `4` as a valid value of `info_ut_spawn`'s `g_gametype`, and
there is no TS-specific classname anywhere in `urtentities.ent` or `urtobjects.ent`.

What is actually true is narrower: **TS requires each team's spawns to be in separate
groups.** The gamepack's words are "gametype 4 should be in separate groups for each team."

This matters because §8.2 gives `UT_TS_SPAWN_SHARED` as a worked validator example, with the
message "8 `info_ut_spawn` entities list gametype 4 alongside others; TS requires dedicated
spawns". That validator would fire on correct maps. Do not implement it.

The related claim in §7.1 — that in TS each group takes a *unique* `group` name and the game
picks the two furthest-apart sets — **is supported**, in both halves, and is safe to ship as
a hard rule.

### W2. Player height is 69.375 units, not 56

> §7.3: "standing (56u) and crouch headroom"

**Wrong for this game.** `measurment.def` in the gamepack states the player box as
**30.250 × 30.250 × 69.375**, crouching **48.250**, step **18.000**.

56 is the legacy Quake 3 height. It survives inside the gamepack only as the *editor
bounding box* of `info_player_deathmatch` and the `team_CTF_*` spawns, while both
Urban-Terror-native spawn classes use `32 32 70`. A `movement_check` built on 56u would
pass corridors the player cannot stand up in — which is precisely the failure §7.3 warns
about when it says "calibrate the physics constants empirically … do not trust forum
numbers, and do not trust mine."

These numbers are now in `profiles/urt43.yaml` under `movement:`, marked `verified` with
the gamepack as source. They should still be confirmed by instrumenting the engine before
anything depends on them quantitatively.

### M3. Spawn groups: the gamepack says 8, not 16

> §7.1: "Spawn groups should contain **16 points**, spaced **≥ 16 units apart**."

The only worked example in the gamepack uses **eight** per team: "take 8 spawn points…
assign all eight a group id of 1… another eight… group 2". 16 appears nowhere. Sixteen is a
plausible reading of "8 per team × 2 teams sharing a group id", but it is not stated.

The **≥16 units apart** figure is stated nowhere at all, and the geometry argues against it:
the `info_ut_spawn` editor box is 32×32×70 and the player is 30.25 wide, so spawns need
roughly **32u** merely not to overlap. Shipped as `unverified`.

### Confirmed as written

- **FFA does not use `info_ut_spawn`.** Supported three independent ways: the class is
  described as being for team modes, `g_gametype` offers only 3/4/5/6/7/8, and `notfree`
  exists to exclude a spawn from Free-for-all and Jump.
- There is also a **stronger** rule the spec missed: you need `info_player_deathmatch` or
  `info_player_start` in the map to spawn *in every gametype*, not only FFA.
- **Bomb mode needs exactly two bombsites.** `info_ut_bombsite` states: "You MUST have 2
  bomb sites per map or the map will crash upon loading into bomb mode." That is a clean,
  verified, high-value hard validator, and the spec did not have it.

### Gametype IDs (§7.1 table)

IDs 0, 1, 3, 4, 6, 7, 8, 9 are confirmed. Two problems:

- The gamepack **contradicts itself on 5**: `info_ut_spawn` calls it "Follow the Leader";
  every other table in both files calls it "Assasins" (sic).
- IDs **2, 10 and 11 appear nowhere**, so Freeze Tag and Gun Game are unconfirmed here.

### Entities the spec assumed exist but do not

- **No bomb objective/target entity.** `info_ut_bombsite` is the only bomb entity.
  `ut_weapon_bomb` is a bombbag pickup, not an objective.
- **No jump telepads or checkpoints.** Jump timing is exactly `ut_jumpstart`, `ut_jumpstop`,
  `ut_jumpcancel`; teleports are stock `trigger_teleport`/`target_teleporter`.
- **No ladder entity.** Zero hits for "ladder" in the entity defs. Ladders appear to be a
  shader property; unproven, filed unverified.
- **No window-specific or spawn-door classname.** Breakable glass is `func_breakable` with
  `breakIndex` type 0; spawn doors are `func_door` with `only` set to a team.

---

## 2. Upstream repository facts (§2)

| # | Spec claim | Reality |
| --- | --- | --- |
| W4 | "C++ **GTK** editor" (§1.1) | **Qt5.** The Makefile pkg-configs `Qt5Core/Qt5Gui/Qt5Widgets/Qt5Svg`. Only dead code still carries GTK naming. |
| W5 | `bspc` binary | The tool is **`mbspc`**; there is no `bspc` target. Build with `make binaries-q3map2 binaries-mbspc`. |
| W6 | `q3map2 -json <-unpack\|-pack>` | **`-unpack` is never parsed.** It appears in the usage text, but unpack is the *default* for `-json`. Also `-json` must be the **first** argument and the filename the **last**. |
| W7 | `-maxshaderinfo <N>` (§6.3) | **Removed upstream** — "refactor to work without -maxshaderinfo limit". Passing it is now an error. `-maxmapdrawsurfs` still exists. |
| W8 | "`generic_module.py` generates module boilerplate — use it" (§9.1) | **Dead.** Python 2 syntax (won't run), calls `svn add` in a git repo, generates only an include guard, and is referenced nowhere. |
| M9 | "no CMake" (§2) | True of the project build, but `libs/assimp/` ships ~40 `CMakeLists.txt`. |
| M10 | "`contrib/` holds 12 plugins, each a small `.cpp/.h/.def` set" | 12 directories, but only **8 are built**, and sizes run from 3 files (`sunplug`) to 93 (`bobtoolz`). `sunplug` is the right model. The `.def` is a *Windows module-definition* file, not an entity def. |
| M11 | "`docs/changelog-custom.txt` — new q3map2 flags land here first" (§10.1) | A poor flag source: freeform prose, ~45 flag lines total, and it documents flags later removed with no removal marker. Use **`tools/quake3/q3map2/help.cpp`** (267 `{ "-flag", "description" }` literals) and cross-check against `takeArg`/`takeFront` call sites. |
| — | Assimp "40+ formats" | Confirmed (51 importers), but the in-tree copy is only used with `ASSIMP_INTERNAL=yes`; the **default build links the system libassimp**, so the host's version decides the format list. |
| — | "20 model autoclip modes" | Confirmed, but they are spawnflag *bit combinations* whitelisted in `clipflags_doClip()`, not a named enum. |

Useful things found that the spec did not mention:

- **`regression_tests/q3map2/`** contains 18 deliberately pathological maps
  (`degenerate_winding`, `duplicate_plane`, `disappearing_sliver`, `tiny_structural_brush`,
  `snap_plane`, `sparkly_seam`, `piercing_triangle`, …). This is a better degenerate-case
  corpus than anything worth inventing, and it is now imported by
  `tools/import_corpus.py`.
- Building only the compilers needs **libxml2, glib-2.0, libpng, libjpeg, zlib, assimp** and
  **no Qt at all**. `mbspc` has zero external dependencies.

---

## 3. `.map` format details the spec did not specify

These were unknown rather than wrong, and each one would have broken byte-identical
round-trip. All are implemented and covered by tests.

1. **Float formatting is `snprintf("%10.10lf")`, then strip trailing zeroes, then strip a
   trailing `.`** (`libs/stream/textstream.h`, class `Decimal`). Never exponent notation.
   Consequences: `-0.0` is written as **`-0`** (and real maps contain it), and precision is
   capped at 10 decimal places.
2. **Every file this fork saves starts with a newline.** The token writer holds a pending
   `'\n'` separator and emits it before the first token.
3. **Files end in ways a boolean cannot capture.** `corpus/real/ut4_dofa_ac.map` ends
   `}\r\n\r\n\r\n`. This was the only map in the corpus the kernel initially failed to
   reproduce; both leading and trailing whitespace are now stored verbatim.
4. **Shader names are stored without the `textures/` prefix**, and an empty shader is
   spelled `NULL`. The reader re-adds the prefix. Anything resolving a shader must add it
   back explicitly.
5. **Fork-specific `//@$&` layer records** appear before the first entity
   (`//@$& layerdef "0" -1 0 0 0`) and between primitives (`//@$& layer 0`). They are
   comments, so preserving comments preserves layers for free.
6. **Punctuation must be whitespace-separated.** Both upstream readers tokenise on
   whitespace only — `(0 0 1)` fails to parse. Our writer always pads.
7. **Valve 220 detection is a raw 1024-byte look-ahead for the literal strings `" [ "` and
   `" ] "`**, decided once per file. There is no `"mapversion" "220"` key in the code at
   all. We detect per face from syntax instead, which is strictly more robust.
8. **Valve 220 rotation is sign-flipped** on both read and write by the editor, and q3map2
   ignores it entirely.
9. **A Quake 3 `patchDef3` cannot be reopened.** The editor writes one when fixed
   subdivisions are enabled, but `MapQ3API::parsePrimitive` accepts only `patchDef2`, and
   q3map2's `ParsePatch` hard-wires a 5-value header. This is an upstream bug and now a
   validator (`PATCH_DEF3_UNREADABLE`).
10. **The editor's own save is lossy**: it discards empty group entities and
    non-contributing faces, and drops all patches for the Quake 1/2/Half-Life formats. Our
    kernel is lossless, which is a real advantage worth keeping.
11. **Patch control grids nest width-major**: `width` rows of `height` points each.

---

## 4. What this changes about the plan

- §7.1's "four verified rules" are **two verified, one wrong, one unverified.** The verified
  set to ship is: TS needs separate groups per team; FFA uses
  `info_player_start`/`info_player_deathmatch`; plus the newly found bomb rule (exactly two
  bombsites) and the "every gametype needs a deathmatch spawn" rule.
- The `Confidence` distinction in §8.2 is not bureaucratic. It is the mechanism that would
  have prevented W1 from shipping as a hard failure, and it is enforced in code:
  `validate.rs` carries `Confidence` on every finding, and `profiles/urt43.yaml` marks 895
  entries — 868 verified against the gamepack, 27 explicitly not.
- §10.1's interface-hash watcher should hash **`help.cpp`** as well as `include/*.h`, since
  that is the real flag inventory and flags are removed there without notice (W7).
