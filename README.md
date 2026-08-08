# nrc-mcp

An MCP server for designing, sculpting, optimizing and shipping levels for
[NetRadiant-custom](https://github.com/Garux/netradiant-custom), targeting Urban Terror.

`nrc-mcp` owns the `.map` file. It parses and writes it losslessly, derives geometry with exact
arithmetic, drives `q3map2`, and exposes all of it to an agent as **48 tools and 5 resources**.
A human gets the same capabilities through a CLI and a set of `mise` tasks.

It is not an editor and not a plugin. It reads and writes the same files NetRadiant does, so you can
keep the editor open and use both.

---

## Contents

- [Requirements](#requirements) · [Install](#install) · [Configure](#configure)
- [Connect an MCP client](#connect-an-mcp-client)
- [Your first session](#your-first-session)
- [Tool reference](#tool-reference) · [Resources](#resources)
- [Reading the output](#reading-the-output) — confidence, and what a warning means
- [Command line](#command-line)
- [How it works](#how-it-works)
- [Limits](#limits) — what it will refuse to do
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)

---

## Requirements

**Always needed**

- [mise](https://mise.jdx.dev) — the only build/run interface. It installs and pins everything else.
- A POSIX shell. Linux, macOS or WSL. Native Windows is untested.

That is enough for parsing, validation, rendering, sculpting, analysis and the whole test suite.
No GPU, no display, no game install, no editor.

**Needed only to compile, package or read BSPs**

- `q3map2` — from a NetRadiant-custom install, or built from source with `mise run vendor:build`.
- A game installation, for `-fs_basepath`. For Urban Terror that is the directory containing
  `q3ut4/`.

**Optional**

- NetRadiant-custom itself, if you want to see the map in the editor. Its gamepack is also the
  source the Urban Terror profile was extracted from.
- Blender, for the mesh handoff. `nrc-mcp` never launches it; it writes a brief and validates what
  comes back.

## Install

```sh
git clone <this repo> && cd nrc-mcp
mise trust                    # approve this repo's mise config
mise install                  # rust, python 3.12, uv — pinned
mise run bootstrap            # build the kernel, generate the corpus
mise run test                 # prove it works before trusting anything
```

`mise run test` should end with:

```
  syntactic: 49/49 byte-identical (3343 brushes, 10 patches)
  semantic: 6/6 compiled BSPs identical

GATE GREEN
```

Alongside 265 Rust tests, 272 Python tests and 5 scenario tapes. The `semantic` line only appears if a compiler is
configured; without one it is skipped, not failed.

**If the gate is red, stop.** Nothing downstream of the kernel is trustworthy while a map does not
survive a round-trip. See [Troubleshooting](#troubleshooting).

## Configure

Machine-specific paths go in **`mise.local.toml`**, which is gitignored. Nothing in `mise.toml`
needs editing.

```toml
[env]
# Where the game lives. The directory that CONTAINS q3ut4/, not q3ut4 itself.
URT_BASEPATH = "/mnt/c/Program Files/UrbanTerror43"
URT_GAMEDIR  = "/mnt/c/Program Files/UrbanTerror43/q3ut4"

# The compiler.
Q3MAP2 = "/mnt/c/Program Files/NetRadiant/NetRadiant-custom-20240309/q3map2.exe"

# Set to "windows" when q3map2 is a .exe reached from WSL. Omit on native Linux/macOS.
NRC_Q3MAP2_MODE = "windows"

# The gamepack .ent files — the only accepted source for the entity ontology.
URT_GAMEPACK = "/mnt/c/Program Files/NetRadiant/NetRadiant-custom-20240309/gamepacks/urt.game/q3ut4"

# Your own .map sources. Read-only: corpus:import copies out, never writes back.
URT_MAPSRC = "/mnt/c/Program Files/UrbanTerror43/q3ut4/maps"
```

| Variable | Required for | Notes |
| --- | --- | --- |
| `URT_BASEPATH` | compiling, packaging | Passed as `-fs_basepath`. Defaults to a stub in `vendor/`. |
| `URT_GAMEDIR` | shader auditing | Where `scripts/` and `textures/` are found. |
| `Q3MAP2` | compiling, BSP reports | Defaults to the source build under `vendor/`. |
| `NRC_Q3MAP2_MODE` | WSL only | `windows` makes every path translate through `wslpath`. |
| `URT_GAMEPACK` | re-extracting a profile | Not needed to *use* the shipped profile. |
| `URT_MAPSRC` | `corpus:import` | Optional. The corpus works without your maps. |
| `NRC_PROFILE` | — | Active game profile. Defaults to `urt43`. |
| `NRC_SELFDEV` | — | Set to `1` to allow the opt-in self-tuning loop. Off by default. |

Check what resolved:

```sh
mise run info
```

### If q3map2 is a Windows .exe and you are in WSL

This is a supported and tested configuration, and the one thing worth knowing is that
**`q3map2.exe` cannot read `/home/...` paths and rejects UNC paths like
`\\wsl$\Ubuntu\home\...` outright.** `tools/q3map2.py` handles it: arguments are translated with
`wslpath -w`, and a map that lives on the Linux side is staged to a Windows-side directory before
compiling, then results are copied back. You do not have to do anything except set
`NRC_Q3MAP2_MODE = "windows"`.

The consequence to expect: compiling a map under `/home` is slower than compiling one already on
`/mnt/c`, because of the staging copy.

## Connect an MCP client

The server speaks JSON-RPC on stdio.

**Claude Code**

```sh
claude mcp add nrc -- mise -C /absolute/path/to/nrc-mcp run mcp:serve
```

The `-C` is what lets the server start from anywhere; drop it if you always launch from inside the
repo.

**Any client with a JSON config** (`claude_desktop_config.json`, `.mcp.json`, …)

```json
{
  "mcpServers": {
    "nrc": {
      "command": "mise",
      "args": ["run", "mcp:serve"],
      "cwd": "/absolute/path/to/nrc-mcp"
    }
  }
}
```

`cwd` matters — it is how `mise` finds the config that supplies every path. If your client cannot set
a working directory, put it in the arguments instead:
`"args": ["-C", "/absolute/path/to/nrc-mcp", "run", "mcp:serve"]`.

**Verify without a client**

```sh
mise run mcp:tools      # print the whole surface, and the limits, and exit
mise run mcp:inspect    # the official MCP inspector, in a browser
```

`mise run mcp:serve` writes only protocol on stdout — diagnostics go to stderr — so it is safe to
point a client straight at it.

## Your first session

A complete pass, in the order that works. Every step is a tool call; the shell equivalents are shown
where one exists.

**1 — Open a map.** Everything else operates on the currently open map.

```json
{"tool": "map_open", "path": "corpus/real/ut4_woolis.map"}
```

**2 — Look at the numbers.**

```json
{"tool": "map_stats", "grid": 8}
```

Counts, bounds, a shader histogram, and grid alignment. `grid` is the authoring grid you want
alignment measured against — it changes the report, never the map.

**3 — Look at the map.** Sculpting blind fails, so do this early and often.

```json
{"tool": "render_contact_sheet"}
{"tool": "render_topdown", "overlay": "structural"}
```

The image comes back **in the response**, not as a file — there is no output path to choose. Counts,
dimensions, scale and warnings arrive as structured data alongside it, so you read an exact number
instead of reading your own render. Use the `nrc render` CLI when you want a PNG on disk.

Orthographic views are **wireframe** on purpose: a filled top-down of a sealed map shows you the
underside of its sky brush and nothing else. Wireframe gives a real floor plan, with rooms and stairs
legible through the ceiling. Perspective views render solid. Pass `solid=true` to a top-down only
when you know the geometry is open.

Overlays: `structural` separates structural from detail brushes, brush entities and patches; `caulk`
shows which surfaces are never drawn in game; `off_grid` marks vertices that miss the grid.

**4 — Validate, in two passes.** Geometry and file format first, then the game's own rules.

```json
{"tool": "validate", "grid": 8}
{"tool": "validate_profile"}
```

**5 — Build something.** Geometry is described as an intersection of half-spaces, so a non-convex
brush is not expressible. Compile it, look at it, then commit it.

A room is a hollowed box, which is **6 brushes** — floor, ceiling, four walls:

```json
{"tool": "solid_compile", "ir": {
  "op": "hollow",
  "solid": {"op": "box", "min": [0, 0, 0], "max": [512, 512, 256]},
  "thickness": 16}}
```

A doorway is a subtraction, and it comes out as **3 brushes** — left column, right column, lintel —
which is what a mapper would draw by hand:

```json
{"tool": "solid_compile", "ir": {
  "op": "subtract",
  "from": {"op": "box", "min": [0, 0, 0], "max": [512, 16, 256]},
  "cut": [{"op": "box", "min": [224, -8, 0], "max": [288, 24, 112]}]}}
```

The cutter deliberately overshoots the wall on both faces (`y` from `-8` to `24` through a 16-thick
wall). If a doorway compiles to more pieces than you expect, that overshoot is usually what is
missing. Composing the two — cutting the same doorway out of the whole shell — gives 10 brushes
rather than 8, because the cutter also crosses the brushes adjoining that wall; `solid_compile`
reports the count before anything is committed, which is the point of running it first.

```json
{"tool": "solid_preview", "ir": {"...": "..."}, "view": "sheet"}
{"tool": "solid_commit", "ir": {"...": "..."}, "label": "north_room",
 "textures": {"default": "caulk", "faces": {"floor": "concrete_01"}}}
{"tool": "map_save"}
```

`label` is required, and it is worth choosing well: it names the shape in the recorded sidecar and is
how you edit it later. Default `textures.default` to `caulk` and override only the faces a player can
see — the compiler discards caulk faces, so a caulked hidden face costs nothing.

`solid_help` is the full operator reference. `solid_list` and `solid_edit_param` then let you change a
committed shape by one field: `solid_edit_param("north_room", "solid.max[1]", 640)` makes the room
deeper and rebuilds it. It previews by default — pass `preview_only=false` to apply.

**6 — Compile.** Start with `draft`; it is the fastest thing that still tells you the truth about
whether the map seals.

```json
{"tool": "compile_map", "path": "maps/mymap.map", "preset": "draft"}
```

Presets are `draft`, `iterate`, `quality`, `final`. If it leaks, `leak_trace` reads the pointfile
and tells you where the hole is.

**7 — Optimize, then ship.**

```json
{"tool": "structural_audit"}
{"tool": "ship_check", "target": "mymap"}
{"tool": "pack_pk3", "bsp": "out/mymap.bsp"}
```

`structural_audit` is the biggest single lever on compile time and runtime visibility cost:
structural brushes generate portals, and most brushes do not need to be structural.

## Tool reference

48 tools. `mise run mcp:tools` prints this list live.

**The map**

| Tool | Does |
| --- | --- |
| `map_open` | Open a `.map` for analysis. Everything else works on it. |
| `map_stats` | Counts, bounds, shader histogram, grid alignment. |
| `map_save` | Write it back. Refuses if the map did not round-trip when opened, or if the write would destroy playable space. |
| `query_entities` | Entities, filterable by classname. |
| `brush_geometry` | Exact vertices and derived properties of one brush. |
| `validate` | Geometry and file-format findings. |
| `validate_profile` | Entities against the game profile's rules. |
| `profile_summary` | What the profile knows, and how much of it is verified. |

**Seeing it**

| Tool | Does |
| --- | --- |
| `render_topdown` | Top-down (XY) view. |
| `render_camera` | Perspective, or a front/side orthographic view. |
| `render_contact_sheet` | Three orthographic views plus a perspective, in one image. |
| `render_player_eye` | What a standing player sees from a floor position. |

**Sculpting**

| Tool | Does |
| --- | --- |
| `solid_help` | Every operator, its fields, and the on-grid caveat. |
| `solid_compile` | Compile geometry and report it, touching nothing. |
| `solid_preview` | Compile and render, without committing. |
| `solid_commit` | Compile and add the brushes to the open map. |
| `solid_inspect` · `solid_list` | Structure of a recorded shape; everything recorded. |
| `solid_edit_param` | Change one parameter and rebuild. |

**Meshes** — the Blender handoff

| Tool | Does |
| --- | --- |
| `asset_plan` | Brush, patch or mesh? Decides, and says why. |
| `blender_brief` | A numerically complete brief plus a ready-to-send prompt. |
| `model_import` | Validate an exported mesh against the brief that asked for it. |
| `model_place` | Build the entity that puts it in the world. |
| `model_make_clip` | Fit a convex collision hull, returned as geometry you can commit. |

**Compiling and optimizing**

| Tool | Does |
| --- | --- |
| `compile_map` | Run q3map2 with a named preset. |
| `bsp_report` · `bsp_entity_diff` | Read a compiled BSP; compare its entities to the source. |
| `structural_audit` | Brushes marked structural that need not be. |
| `hint_suggest` | Hint brush planes, proposed from a portal file. |
| `leak_trace` | Where the map leaks, from the pointfile. |
| `shader_audit` | Shader references against the scripts on disk. |
| `compile_ab` · `ab_history` | Compile two variants and diff what matters; every past comparison. |

**Gameplay analysis** — all constants read from the profile, never hardcoded

| Tool | Does |
| --- | --- |
| `navgrid_stats` | Build the walkable grid; report size and coverage. |
| `balance_report` | Per-team distance from each spawn group to each objective. |
| `sightline_report` | Sightline length distribution and power positions. |
| `movement_check` | Clearances against verified movement constants. |
| `spawn_safety` | Exits per spawn, and distance to the nearest enemy spawn. |
| `playable_space_diff` | What an edit did to the space a player can stand in. |

**Shipping**

| Tool | Does |
| --- | --- |
| `ship_check` | Naming, levelshot, arena file, package contents. |
| `pack_pk3` | Build the release archive. |
| `repack_analyze` | Every resource the BSP actually references. |

**The toolchain itself**

| Tool | Does |
| --- | --- |
| `task_list` · `task_run` | Every capability, discovered from mise; run one. |
| `bench_run` | The fitness suite. |
| `selfdev_protected` | The paths self-modification may never touch, and their hash pins. |
| `upstream_diff` · `pr_plan_status` | Upstream drift; the contribution plan. |

## Resources

| URI | Contents |
| --- | --- |
| `nrc://tasks` | The live mise task list — capability discovery, always current. |
| `nrc://profile/{id}` | A game profile as YAML. `nrc://profile/urt43` ships. |
| `nrc://conventions` | Representation tiers, caulk, grid discipline, authoring order. |
| `nrc://corrections` | Claims from the design document that did not survive verification. |
| `map://current/summary` | The open map. |

**Read `nrc://corrections` before trusting a rule.** The design document was written partly from
recollection; several of its claims were wrong, including three it listed as verified.

## Reading the output

**Findings carry a severity, and severity is earned.** A rule may only fail your build if its
`confidence` is `verified` — meaning it was checked against the gamepack or the engine source.
Anything `unverified` is clamped to `info` and can never fail anything, however plausible it looks.

That mechanism is not bureaucratic. The design document asserted that Team Survivor needs dedicated
spawn entities; the gamepack says otherwise, and a validator built on that claim would have failed
correct maps. `nrc://corrections` records each one.

**A warning is sometimes about the tool, not your map.** `BRUSH_OFF_GRID` on a rotated prism is
unavoidable — a `.map` stores *planes*, and a brush's vertices are wherever three planes meet, which
for anything angled is not on the grid and cannot be made so. The same finding on axis-aligned
geometry is a real defect. `validate` reports the count; deciding needs to know which shape produced
it.

**`BRUSH_NOT_EXACT` means "excluded", not "broken".** See [Limits](#limits).

## Command line

Every capability is a mise task, and the task list is the agent's action surface, so anything an
agent did is a command you can paste into a shell.

```sh
mise tasks                       # all 42
mise run info                    # resolved paths and versions
mise run render corpus/real/ut4_dofa.map out/dofa.png
mise run compile:draft maps/mymap.map
mise run test:diff               # the round-trip gate alone
mise run test:tapes              # end-to-end scenarios over the MCP surface
mise run bench                   # the fitness suite
mise run watch                   # re-run checks on change
```

There is also a direct kernel binary, useful in scripts and pipes. `mise run kernel:build` puts it at
`target/release/nrc`; it is not installed onto `PATH`, so either call it by path or
`export PATH="$PWD/target/release:$PATH"`.

```sh
nrc roundtrip <file.map>...          # verify byte-identical load/save
nrc stats <file.map> [--grid N]      # JSON
nrc validate <file.map> [--grid N]   # JSON; exit 1 if findings
nrc normalize <file.map> --write     # re-serialize in place (refuses without --write)
nrc render <file.map> --out x.png --view top --overlay structural
```

Exit codes: `0` clean, `1` findings or did not round-trip, `2` tool error. Add `--quiet` for JSON
only, `--pretty` to indent it.

## How it works

```
agent ──MCP──► nrc-mcp ──► nrc-core (Rust)   .map I/O, exact geometry, validators
                    │
                    ├──► mise run <task> ──► q3map2 / mbspc / cargo / uv
                    └──► profiles/*.yaml      the only game-specific layer
```

**Lossless before anything else.** Load and re-save any `.map` byte-identically. `mise run test:diff`
checks it two ways: parse and re-serialize and require identical bytes, then compile both the
original and the re-serialized copy and compare the geometry lumps of the resulting BSPs. Current
state over 49 maps — 8 real Urban Terror sources, upstream's 18 pathological regression maps and 23
synthetic ones — is **49/49 byte-identical and 6/6 compiled BSPs identical**.

Getting there meant discovering things the format is not documented with: the exact float formatting
(`%10.10lf`, trailing zeroes stripped, so `-0` is a real literal), the leading newline every file
this fork writes begins with, and trailing whitespace preserved verbatim — one real map ends
`}\r\n\r\n\r\n` and was the last holdout. Numbers remember the text they were parsed from, so an
untouched map reproduces its own bytes and a modified one differs only where it was modified.
Comments, key order, duplicate keys, line endings, layer records and primitives whose syntax is
unrecognized all survive.

**Two gates, guarding two different things.** The round-trip gate protects the *file*. It says
nothing about the *map*, and for a long time nothing did: an edit could fill a room with
playerclip and every check stayed green — it round-tripped, validated, compiled and sealed, and
was unplayable. `map_save` now also compares the map it is about to write against the file it is
about to replace, and refuses on either of two findings that have no legitimate form:

- `PLAYSPACE_INTERIOR_SEALED_BY_CLIP` — floor that had a ceiling over it is now inside a clip
  volume. A roof is sky-exposed by definition, so this is the inside of the map being closed off,
  and clip is what makes it invisible until someone walks into it.
- `PLAYSPACE_CLIP_SURFACE_WALKABLE` — you can now stand on top of a clip volume, held up by
  nothing else. Capping something with clip does not remove the surface, it raises it.

Sealing floor with ordinary geometry is a warning, not an error: putting a wall in a room is
authoring, and a gate that blocks every real edit is a gate that gets switched off. Pass
`acknowledge_regression=true` when you meant it. `playable_space_diff` runs the same comparison
on demand, between any two maps.

**Exact predicates, or an honest refusal.** Coplanarity, convexity, plane identity and grid
membership use integer and rational arithmetic, not epsilons. Brush vertices come from intersecting
every triple of face planes exactly and keeping the points that satisfy every half-space, so
convexity is guaranteed rather than checked. Where the input is off-grid the kernel reports
`Indeterminate` instead of guessing, because a guessed side is a sliver and a sliver is a leak three
weeks later.

**Subtraction that a human would recognize.** `A \ B = ⋃ᵢ (A ∩ h₁ ∩ … ∩ hᵢ₋₁ ∩ ¬hᵢ)` over `B`'s
half-spaces: every term is convex by construction, and terms that would be slivers are *exactly*
empty and vanish. Adjacent pieces then merge where their union is genuinely convex, by an exact test
— for `P` and `Q` sharing plane `h`, the union is convex iff every other plane of `P` contains all
of `Q` and vice versa. That has to be exact, because merging wrongly fills the doorway back in.

**mise as the action surface.** The server never shells out to a raw command; it calls
`mise run <task>`. Capability discovery is therefore free, and new abilities need no server code.

**One game-specific layer, enforced.** Entity ontology, gametype ids, spawn rules and movement
constants live in `profiles/*.yaml` as data. The Urban Terror profile covers 95 entity classes and
carries an explicit confidence marker on 895 entries — 868 verified against the gamepack, 27 marked
unverified and therefore unable to fail a build.
`mise run test:seam` fails the build if a game-specific string appears in code, and it derives its
forbidden vocabulary from the profile itself, so it cannot fall behind.

### Layout

```
crates/nrc-core/     the kernel: lex, parse, write, math, exact, winding, validate, stats
crates/nrc-solid/    half-space geometry: CSG, convex merge, brush emission
crates/nrc-render/   headless rasterizer: ortho and perspective, PNG out, no GPU
crates/nrc-cli/      `nrc` — roundtrip / stats / validate / normalize / render
crates/nrc-py/       PyO3 bindings; the server's in-process kernel
python/src/nrc_mcp/  the MCP server
tools/               corpus import, the differential harness, the q3map2 driver, the seam lint,
                     the scenario-tape runner
bench/tapes/         end-to-end scenarios: tool sequence in, invariants out
profiles/            game profiles — the only game-specific layer
corpus/              real, upstream-regression and synthetic maps
contrib/mcpbridge/   an editor plugin for live editor state (never compiled — see docs/)
docs/                design notes and the spec corrections
```

## Limits

Ask for any of these and you get an honest refusal rather than a plausible number.

**Rotated geometry is partly invisible.** This is the cost of the exact-predicate design and it shows
up on real maps. Any coordinate that is not an exact integer within world bounds is refused, and
everything downstream reports `Indeterminate` rather than picking a side. Geometry whose
*plane-defining points* are off-grid therefore cannot be evaluated at all, and rotated brushes are
the common case.

Measured: `ut4_woolis` and `ut4_megastructunnel` are 100% evaluable, while **`ut4_dofa` has 478 of
1454 brushes the kernel declines**. Those are absent from the navgrid, so every analysis report
carries the count, gives examples, and warns that a path may cross a wall. `validate` reports them as
`BRUSH_NOT_EXACT`.

The honest summary: fully precise about axis-aligned and 45° geometry, partly blind to arbitrarily
rotated geometry, and every report says which it is looking at. Closing the gap means either snapping
input — which changes the map — or adaptive floating-point predicates. `crates/nrc-core/src/exact.rs`
explains why the integer route came first.

**No patch authoring.** Patches are parsed, validated, tessellated and rendered, but cannot be
created. Curved geometry is editor work.

**No measured reference dimensions.** There is no corpus of width/height/length distributions per
space category, so sizing comes from the profile's verified constants. Related: no cover density or
peek-angle analysis.

**No traversal time.** No player movement speed is verified, so every distance is reported in world
units and never in seconds. Inventing a speed is exactly the failure this project is built to avoid —
the design document assumed a 56-unit standing height, and the shipped gamepack says 69.375. A
corridor sized from the wrong number passes every geometric check and still traps the player.

**No live editor state.** `contrib/mcpbridge` is a complete JSON-RPC plugin for NetRadiant-custom,
and **it has never been compiled** — there is no Qt5 environment here and the host compiler cannot
build that codebase at all. It is offered for review, not for use. `docs/editor-bridge.md` explains
the design; `docs/pr-plan.md` tracks readiness and reports this as unmet.

**No model-in-the-loop test.** `mise run test:tapes` runs five scenarios end to end — build from
scratch, continue an existing level, refactor one region, an optimisation pass, and one negative
control that reproduces the `ut4_dofa` clip failure and requires `map_save` to refuse it. Every
step is a real call into the server. What none of them prove is that a *model* would make those
calls: the tape is the tool sequence with the model removed, which is exactly why it costs
nothing to run. Closing that gap means a model driving this MCP against small fixtures, graded by
the same invariants, and it is not built. `bench/tapes/README.md` says the same thing at length.

**No kernel self-modification.** The exact predicates, the differential harness, the fitness
definitions, the corpus and every verified rule are hash-pinned in `bench/protected.json`. The
opt-in self-tuning loop can only touch the prompt and resource layer, where a mistake cannot corrupt
a map. `mise run selfdev:protected` verifies the pins; if it fails, treat every fitness score as
meaningless until you know why.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `mise run test:diff` fails on a map | The kernel could not reproduce that file's bytes. Nothing downstream is trustworthy. Run `nrc roundtrip <file>` for the first differing offset. |
| `semantic: skipped` | No compiler configured. Set `Q3MAP2` in `mise.local.toml`, or `mise run vendor:build`. Not a failure. |
| q3map2 exits immediately, or complains about the path | Almost always WSL path translation. Set `NRC_Q3MAP2_MODE = "windows"`. UNC paths (`\\wsl$\...`) are rejected by q3map2 itself and staging exists to avoid them. |
| `-fs_basepath` errors, or no shaders found | `URT_BASEPATH` must be the directory *containing* `q3ut4`, not `q3ut4` itself. |
| MCP client shows no tools | The server needs to start in the repo. Set `cwd`, or use `mise -C /path/to/nrc-mcp run mcp:serve`. Check `mise run mcp:tools` works first. |
| A rule fires that you believe is wrong | Check `nrc://corrections` — some of the design document's rules were wrong. If it is `info`, it is `unverified` by construction and cannot fail a build. |
| `BRUSH_OFF_GRID` on curves and prisms | Expected. A `.map` stores planes; angled geometry has off-grid vertices necessarily. Judge it only on axis-aligned geometry. |
| Analysis reports fewer brushes than the map has | `BRUSH_NOT_EXACT` — off-grid plane points, excluded rather than approximated. The count is in the report. See [Limits](#limits). |
| A top-down render is one flat grey rectangle | You forced `--solid` on a sealed map and are looking at the sky brush. Orthographic views default to wireframe for this reason. |
| `mise install` fails on an env var | An `mise.local.toml` value referenced from `mise.toml`. Machine paths belong only in `mise.local.toml`. |
| `selfdev:protected` fails | Something that defines *correct* changed. Treat all scores as meaningless and find out why before re-pinning. |

## Contributing

- `mise run ci` must be green. If `test:diff` is red, fix that first.
- The kernel has **no dependencies**, on purpose. A plane-intersection bug arriving through a
  transitive update is the failure mode this project can least afford.
- Task names are an API. Renaming one breaks the agent's action surface.
- Nothing in `python/src` parses `.map` text. Twice a module reached for a second parser because an
  accessor was missing; both times the fix was to add the accessor to the kernel.
- Rules carry a `confidence`, and only `verified` may fail a build. Verify against the gamepack or
  the engine source — not against documentation, and not against recollection.
- `mise run test:seam` keeps game-specific strings out of code. It reads its vocabulary from the
  profile, so it cannot fall behind.

## Licence

GPL-2.0-or-later, matching NetRadiant-custom.
