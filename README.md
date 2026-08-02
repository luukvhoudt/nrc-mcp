# nrc-mcp

An agent-usable toolchain for designing, sculpting, optimizing and shipping levels for
[NetRadiant-custom](https://github.com/Garux/netradiant-custom), targeting Urban Terror
first.

`nrc-mcp` owns the `.map`. It parses and writes the file losslessly, derives geometry with
exact arithmetic, drives `q3map2`, and exposes all of it over MCP.

**Status: phases 0, 1 and 2 complete and verified; phase 3 partially done.** See
[Status](#status) for exactly what exists. The design document is
`netradiant-mcp-spec.md`; the claims in it that did not survive verification are recorded in
[`docs/spec-corrections.md`](docs/spec-corrections.md) — **read that before trusting a rule
from the spec.**

## Quick start

Everything runs through [mise](https://mise.jdx.dev). It is the only build/run interface,
and its task list doubles as the agent's action surface.

```sh
mise trust && mise install     # pin the toolchain (rust, python 3.12, uv)
mise run bootstrap             # build the kernel, generate and import the corpus
mise run test                  # unit tests, the differential gate, the seam lint
mise run mcp:tools             # print the MCP surface without starting a server
mise run mcp:serve             # run the server on stdio
```

Machine-specific paths — your game install, your NetRadiant build, your map sources — go in
`mise.local.toml`, which is gitignored. `mise run info` prints what it resolved.

## The gate

§3.2 of the spec makes one property the foundation of everything else: **load and re-save
any `.map` byte-identically.** `mise run test:diff` checks it two ways.

- **Syntactic** — parse and re-serialize; require identical bytes.
- **Semantic** — compile the original and the re-serialized copy with `q3map2`, unpack both
  BSPs to JSON, and compare the geometry lumps.

Current state over 49 maps (real Urban Terror sources, upstream's 18 pathological regression
maps, and 23 synthetic maps): **49/49 byte-identical, 3343 brushes, 8/8 compiled BSPs
identical.**

Getting there required implementing several things the format is not usually documented
with: upstream's exact float formatting (`%10.10lf`, trailing zeros stripped, so `-0` is a
real literal), the leading newline every file this fork saves begins with, and trailing
whitespace preserved verbatim — one real map ends `}\r\n\r\n\r\n` and was the last holdout.

## Seeing the map

§4.2 calls visual feedback non-negotiable, because "sculpting blind fails". So the renderer
draws straight from the `.map` — no editor, no GPU, no display — and a 1454-brush map takes
about 0.2 seconds.

```sh
mise run render corpus/real/ut4_dofa.map out/dofa.png
nrc render map.map --view top --overlay structural --out plan.png
```

Orthographic views render as backface-culled **wireframe**, which the spec asked for and
which turns out to matter: a *solid* top-down of a sealed map can only show the underside of
its sky brush — one flat grey rectangle. Wireframe gives a genuine floor plan instead, with
rooms, corridors and stairs legible through the ceiling. Perspective and player-eye views
render solid, exactly as Radiant splits its 2D and 3D panes.

Overlays colour by what matters: `structural` distinguishes structural from detail brushes,
brush entities and patches (the §6.1 split that dominates vis cost); `caulk` highlights
surfaces never drawn in game; `off_grid` marks vertices that miss the grid.

Counts, dimensions, scale and warnings come back as **structured data** next to the image
rather than being burned into pixels — an agent reads an exact number instead of reading its
own render, and a human gets legible text at any size.

`render_player_eye` places the camera at the player's standing height, read from the profile
rather than hardcoded. That is not pedantry: the design document assumed 56 units and the
shipped gamepack says 69.375.

## Design

```
agent ──MCP──► nrc-mcp ──► nrc-core (Rust)   .map I/O, exact geometry, validators
                    │
                    ├──► mise run <task> ──► q3map2 / mbspc / cargo / uv
                    └──► profiles/*.yaml      the only game-specific layer
```

**Exact predicates, or an honest refusal.** Validity decisions — coplanarity, convexity,
plane identity, grid membership — use integer and rational arithmetic, not epsilons. Brush
vertices come from intersecting every triple of face planes exactly and keeping the points
that satisfy every half-space, so convexity is not checked but guaranteed, and "is this
corner on the grid?" is decidable. When input is off-grid the kernel reports
`Indeterminate` rather than guessing, because a guessed side is a sliver and a sliver is a
leak three weeks later.

**Lossless before anything else.** Numbers remember the text they were parsed from, so an
untouched map reproduces its own bytes and a modified one differs only where it was
modified. Comments, key order, duplicate keys, line endings and even primitive blocks whose
syntax we do not recognize all survive a round-trip.

**mise as the action surface.** The server never shells out to a raw command; it calls
`mise run <task>`. So capability discovery is free (`nrc://tasks` is the live task list),
new abilities need no server code, and anything the agent did is a task name plus arguments
that a human can paste into a shell.

**One game-specific layer, enforced.** Entity ontology, gametype ids, spawn rules and
movement constants live in `profiles/*.yaml` as data. `mise run test:seam` fails the build
if a game-specific string appears in code, and it derives its forbidden vocabulary from the
profile itself, so it cannot fall behind.

## Status

| Phase | Deliverable | State |
| --- | --- | --- |
| 0 | mise bootstrap, toolchain pinned, CI | **done** |
| 1 | `.map` parse/serialize, all three brush formats, geometry kernel | **done** — 120 kernel tests, gate green |
| 2 | Read-only MCP tools + rendering | **done** — 14 tools, 4 resources, and the §4.2 visual feedback loop |
| 3 | q3map2 driver, packaging, profile validators | **partial** — compile presets and BSP JSON introspection work; `bsp_report`, packaging and profile-driven validators not started |
| 4–10 | Sculpting, Blender, optimization, analysis, editor bridge, self-optimization | not started |

Implemented: all three texdef conventions (axial, brush primitives, Valve 220), `patchDef2`
and `patchDef3`, verbatim preservation of unknown primitives, exact brush hulls, 13 geometry
and format validators, the differential harness, the q3map2 driver with WSL/Windows path
translation, the headless renderer, the seam lint, and a 95-entity verified Urban Terror
profile.

Not implemented, and the tool surface says so rather than pretending: the Solid IR and
sculpting (§4), the Blender handoff (§5), the optimization suite (§6), UrT analysis (§7.3),
the editor bridge (§9), and self-optimization (§11).

## Repository layout

```
crates/nrc-core/     the kernel: lex, parse, write, math, exact, winding, validate, stats
crates/nrc-render/   headless rasterizer: ortho and perspective views, PNG out, no GPU
crates/nrc-cli/      `nrc` — roundtrip / stats / validate / normalize / render, JSON out
crates/nrc-py/       PyO3 bindings; the server's in-process kernel
python/src/nrc_mcp/  the MCP server
tools/               corpus import and generation, the differential harness, the q3map2
                     driver, the seam lint
profiles/            game profiles — the only game-specific layer
corpus/              real, upstream-regression and synthetic test maps
docs/                spec corrections and notes
```

## Notes for contributors

- `mise run test` must be green before anything else is believed. If `test:diff` fails, fix
  that first; nothing downstream of the kernel is trustworthy while it is red.
- The kernel has **no dependencies** on purpose. A plane-intersection bug arriving via a
  transitive update is the failure mode this project can least afford.
- Task names are an API. Renaming one breaks the agent's action surface.
- Rules carry a `confidence`. Only `verified` rules may fail a build. Three of the spec's
  supposedly-verified Urban Terror spawn rules were wrong, and one would have failed correct
  maps — that mechanism is why it did not ship.

## Licence

GPL-2.0-or-later, matching NetRadiant-custom.
