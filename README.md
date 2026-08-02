# nrc-mcp

An agent-usable toolchain for designing, sculpting, optimizing and shipping levels for
[NetRadiant-custom](https://github.com/Garux/netradiant-custom), targeting Urban Terror
first.

`nrc-mcp` owns the `.map`. It parses and writes the file losslessly, derives geometry with
exact arithmetic, drives `q3map2`, and exposes all of it over MCP.

**Status: all ten phases implemented; see the table for what each one does and does not include.** See
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

## Sculpting

§4 asks for a representation in which **invalid geometry is not expressible**. The Solid IR
delivers that literally: every shape is an intersection of half-spaces, so it cannot be
non-convex, and every failure is "this shape is wrong" with the path to the node that caused
it — never "this file is corrupt".

```json
{"op": "subtract",
 "from": {"op": "hollow", "solid": {"op": "box", "min": [0,0,0], "max": [512,512,256]},
          "thickness": 16},
 "cut": [{"op": "box", "min": [224,-8,0], "max": [288,24,112]}]}
```

`subtract` is what §4.1 calls the core engineering risk, and it warns that "a naive
decomposition that emits 200 brushes for a doorway is worse than useless". Subtracting `B` from
`A` uses the identity `A \ B = ⋃ᵢ (A ∩ h₁ ∩ … ∩ hᵢ₋₁ ∩ ¬hᵢ)` over `B`'s half-spaces: every term
is convex by construction, and the terms that would be slivers are *exactly* empty and vanish.
A doorway through a wall therefore comes out as **three brushes** — left column, right column,
lintel — which is what a mapper would draw by hand.

Adjacent pieces are then merged where their union is genuinely convex, using an exact test
rather than a volume comparison: for `P` and `Q` sharing plane `h`, the union is convex iff
every other plane of `P` contains all of `Q` and vice versa. That has to be exact, because
merging wrongly would fill the doorway back in.

The IR is recorded in a `<map>.solids.json` sidecar, so `solid_edit_param("corridor",
"solid.max[1]", 160)` widens a corridor as one field change. The `.map` stays canonical; the
sidecar is advisory and re-derivable.

One caveat the spec's "on-grid" wording does not capture: plane-defining points are always
integers, and for every primitive the *vertices* are integers too (a prism's corners are the
ring points it was built from). But an octagon of radius 64 has corners at ±59, which are not
multiples of 8 — and CSG between two off-axis shapes can produce genuinely rational vertices.
Both are counted and reported rather than hidden.

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
| 3 | q3map2 driver, `bsp_report`, packaging, profile validators | **done** — declarative rule engine, BSP introspection, `ship_check` |
| 4 | Solid IR, sculpting tools, convex decomposition | **done** — a doorway compiles to exactly three brushes |
| 5 | Blender handoff: brief, import validation, collision hull | **done** — the metres/inches error is named, not just measured |
| 6 | Optimization suite | **done** — structural audit, hint suggestion, leak trace, shader audit, A/B compiles |
| 7 | UrT analysis: navmesh, balance, sightlines, movement | **done** — all constants read from the profile |
| 8 | Editor bridge + upstream PR machinery | **partial** — the plugin exists and is pushed to a fork branch, but has never been compiled |
| 9 | Fitness suite + self-optimization on the prompt layer | **done** — F1–F6, protected paths, opt-in loop |
| 10 | Self-optimization of the kernel; living PR plan | **deliberately not done for the kernel** — §11.4 gates it behind human review indefinitely. The PR plan generates itself. |

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

## The cost of refusing to guess

The exact-predicate design has a price, and it shows up on real maps rather than on synthetic ones.

`IVec3::try_from_vec3` refuses any coordinate that is not an exact integer within world bounds, and
everything downstream then reports `Indeterminate` rather than picking a side. That is the right
trade for correctness — a guessed side is a sliver and a sliver is a leak — but it means geometry
whose *plane-defining points* are off-grid is not analysable at all. Rotated brushes are the common
case.

Measured on the corpus: `ut4_woolis` and `ut4_megastructunnel` are 100% evaluable, while
**`ut4_dofa` has 478 of 1454 brushes the kernel declines to evaluate.** Those brushes are absent
from the navgrid, so `analysis` reports the count, gives examples, and warns that a path may cross a
wall. `validate` reports them as `BRUSH_NOT_EXACT` — a warning about the tool's reach, not an
accusation against the map.

The honest summary: this toolchain is fully precise about axis-aligned and 45° geometry, and
partially blind to arbitrarily rotated geometry. Every report says which it is looking at. Closing
the gap means either snapping input (which changes the map) or adaptive floating-point predicates
(Shewchuk-style expansions), and `crates/nrc-core/src/exact.rs` explains why the integer route was
taken first.

## What is deliberately not built

Three things are absent on purpose rather than by omission, and knowing which is which saves time:

**Kernel self-modification.** §11.4 gates the exact geometric predicates and anything that writes
user `.map` files behind human review *indefinitely*, and §13 calls automating it the
highest-risk item in the design. `selfdev` therefore restricts itself to the prompt and resource
layer (§11.3), where the return per unit of risk is highest and a mistake cannot corrupt a map.
Widening that list is an edit to a protected file.

**Patch authoring.** Patches are parsed, validated, tessellated and rendered, but the Solid IR
cannot create one. Tier-3 geometry has to be built in the editor.

**The dimension corpus.** §4.3 asks for measured width/height/length distributions per space
category, extracted from released maps. Without it there is no `reference_dimensions`, and sizing
comes from the profile's verified constants instead.

And one thing is absent because it cannot be done from here: the editor bridge **has never been
compiled**. There is no Qt5 environment on this machine and the host compiler cannot build the
codebase at all. `docs/pr-plan.md` reports that as unmet rather than glossing it.

## Notes for contributors

- `mise run test` must be green before anything else is believed. If `test:diff` fails, fix
  that first; nothing downstream of the kernel is trustworthy while it is red.
- The kernel has **no dependencies** on purpose. A plane-intersection bug arriving via a
  transitive update is the failure mode this project can least afford.
- Task names are an API. Renaming one breaks the agent's action surface.
- Rules carry a `confidence`. Only `verified` rules may fail a build. Three of the spec's
  supposedly-verified Urban Terror spawn rules were wrong, and one would have failed correct
  maps — that mechanism is why it did not ship.
- Nothing in `python/src` parses `.map` text. Twice a module reached for a second parser because
  an accessor was missing; both times the right fix was to add the accessor to the kernel
  (`brush_geometry`'s `detail`/`face_contents`, and `Map.patches()`).
- `mise run selfdev:protected` verifies the hash pins on the paths that define what *correct*
  means. If it fails, treat every fitness score as meaningless until you know why.

## Licence

GPL-2.0-or-later, matching NetRadiant-custom.
