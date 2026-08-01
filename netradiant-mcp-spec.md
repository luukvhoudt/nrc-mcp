# NetRadiant-custom MCP — Technical Specification

**Target:** `Garux/netradiant-custom`, optimized for Urban Terror (q3ut4)
**Goal:** an agent-usable toolchain for designing, sculpting, optimizing and shipping UrT levels, with an optional upstream PR.

---

## 0. Summary

Four cooperating processes:

```
┌──────────────────────────────────────────────────────────┐
│  Orchestrating agent (Claude)                            │
└───┬──────────────────┬───────────────────┬───────────────┘
    │ MCP              │ MCP               │ MCP
┌───▼──────────────┐ ┌─▼───────────────┐ ┌─▼──────────────┐
│ nrc-mcp (this)   │ │ blender-mcp     │ │ (fs/git mcp)   │
│ .map kernel      │ │ mesh authoring  │ │                │
│ Solid IR         │ └─┬───────────────┘ └────────────────┘
│ q3map2 driver    │   │ exports OBJ/ASE/FBX
│ optimizer        │◄──┘
│ UrT rules        │
│ renderer         │
└───┬──────────────┘
    │ JSON-RPC (optional, PR'd plugin)
┌───▼──────────────┐
│ NetRadiant-custom (live editor)                          │
└──────────────────────────────────────────────────────────┘
```

`nrc-mcp` is the centre of gravity. It owns the `.map`, brokers the Blender handoff,
drives compilation, and is the only component that understands Urban Terror.

Three cross-cutting properties, specified in §1, §10 and §11:

- **mise** is the sole build/run interface, and its task list is exposed to the agent
  as a discoverable action surface.
- **The upstream PR plan is a living artifact**, continuously regenerated from
  upstream drift, maintainer signals and real usage telemetry — and continuously
  *shrinking*.
- **The server self-optimizes**, jcode-style, against an objective fitness suite with
  hard protected paths.

Urban Terror is the **first profile, not the only one** — roughly 85% of this design
is game-agnostic by construction. See §7.4.

**Language recommendation:** Rust core (geometry kernel, parsers, convex
decomposition) + Python MCP shell (FastMCP), linked via PyO3. The kernel needs
exact predicates and speed; the MCP surface needs to be quick to iterate on.
An all-Python implementation is viable for phases 1–3 but will hurt at the
convex-decomposition and voxel-navmesh stages.

---

## 1. Project infrastructure — mise

Everything in this project is driven by [mise](https://mise.jdx.dev). Not just as a
version manager: **mise tasks are the project's only build/run interface, and they
double as the agent's action surface.**

### 1.1 Why it fits here

This project has an awkward toolchain: a C++ GTK editor built with a hand-rolled
Makefile, a Rust geometry kernel, a Python MCP server, `q3map2`/`bspc` binaries that
come out of the editor build, an ioquake3-derived engine for probes, and Blender.
Six ecosystems, one repo. mise pins all of them in a single `mise.toml`, and the
`sources`/`outputs` incremental hints map unusually well onto q3map2's naturally
staged compile (`-bsp` → `-vis` → `-light`).

### 1.2 The architectural rule

`nrc-mcp` **never shells out to a raw command.** It calls `mise run <task> -- <args>`.

Consequences:

- **Capability discovery is free.** `mise tasks --json` is exposed as an MCP resource
  (`nrc://tasks`), so the agent enumerates what the project can do rather than relying
  on hardcoded tool wrappers.
- **New capability = new task file.** Adding `mise-tasks/analyze-lightmaps` gives the
  agent a new ability with zero MCP code change. This is the substrate the
  self-optimization loop (§11) mutates most safely.
- **Reproducibility.** The exact q3map2 invocation the agent ran is recorded as a task
  name plus args, replayable by a human with the same command.
- **CI parity.** GitHub Actions runs the same `mise run` lines.

### 1.3 `mise.toml` (representative)

```toml
[tools]
rust   = "latest"
python = "3.12"
uv     = "latest"

[env]
_.file            = ".env"
NRC_SRC           = "{{config_root}}/vendor/netradiant-custom"
NRC_INSTALL       = "{{config_root}}/vendor/netradiant-custom/install"
Q3MAP2            = "{{config_root}}/vendor/netradiant-custom/install/q3map2"
MAP_CORPUS        = "{{config_root}}/corpus"
NRC_PROFILE       = "urt43"
# machine-specific paths (game dir, engine binary, Blender) live in
# mise.local.toml, which is gitignored

[vars]
q3map2_common = "-game quake3 -fs_basepath {{env.URT_BASEPATH}} -fs_game q3ut4"

[tasks.bootstrap]
description = "One-shot dev environment setup"
depends     = ["vendor:clone", "vendor:build", "corpus:fetch", "py:sync"]
run         = "echo 'ready — try: mise run test'"

[tasks."vendor:build"]
description = "Build netradiant-custom (editor + q3map2 + bspc)"
dir         = "{{env.NRC_SRC}}"
sources     = ["{{env.NRC_SRC}}/**/*.cpp", "{{env.NRC_SRC}}/**/*.h", "{{env.NRC_SRC}}/Makefile*"]
outputs     = ["{{env.Q3MAP2}}"]
run         = "make -j$(nproc) BUILD=release && make install"

[tasks."kernel:build"]
description = "Build the Rust geometry kernel"
sources     = ["crates/**/*.rs", "Cargo.toml", "Cargo.lock"]
outputs     = ["target/release/libnrc_core.so"]
run         = "cargo build --release"

[tasks."test:diff"]
description = "Differential round-trip harness — THE gate (see §3.3)"
depends     = ["kernel:build", "vendor:build"]
run         = "uv run python tools/difftest.py --corpus $MAP_CORPUS"

[tasks.test]
description = "Full test suite"
run = [ { tasks = ["test:unit", "test:diff", "test:validators"] } ]

[tasks."compile:draft"]
description = "Fast geometry-only compile"
usage       = 'arg "<map>" help="path to .map"'
run         = "$Q3MAP2 {{vars.q3map2_common}} -bsp -meta ${usage_map}"

[tasks.bench]
description = "Run the fitness suite and write bench/results/<sha>.json"
depends     = ["kernel:build"]
run         = "uv run python tools/bench.py --all --out bench/results"

[tasks.watch]
description = "Recompile draft + revalidate on .map change"
run         = "mise watch -t compile:draft -t validate"
```

Complex tasks (the compile presets, the bench runner, the self-dev loop, the PR
watcher) become **file tasks** in `mise-tasks/`, using `#MISE` metadata comments for
`description`, `depends`, `sources`, `outputs` — real scripts with real error handling
rather than TOML strings.

### 1.4 Conventions

- `mise.toml` committed; `mise.local.toml` gitignored for machine paths (game
  install, engine binary, Blender executable).
- Every task carries a `description` — it is user-facing through `nrc://tasks`.
- Tasks that mutate user data (`pack`, `install`, `selfdev:merge`) set `confirm`.
- Task naming is namespaced and stable: `vendor:*`, `kernel:*`, `test:*`,
  `compile:*`, `opt:*`, `bench:*`, `selfdev:*`, `pr:*`. Renaming a task is a breaking
  change to the agent's action surface — treat it like an API change.

---

## 2. Repository facts this design depends on

Verified against the repo (2026-08):

| Fact | Consequence |
| --- | --- |
| Build is **Makefile-based** (`Makefile`, `Makefile.conf`, `mingw-Makefile.conf`), no CMake | PR must add a Makefile target, not a `CMakeLists.txt` |
| `contrib/` holds 12 plugins (`sunplug`, `bobtoolz`, `prtview`, `meshtex`, …), each a small `.cpp/.h/.def` set | Bridge plugin follows the same shape |
| `include/` exposes `iscenegraph.h`, `iselection.h`, `iundo.h`, `ientity.h`, `ibrush.h`, `ipatch.h`, `icamera.h`, `imap.h`, `qerplugin.h` | Bridge needs **zero** core changes |
| `generic_module.py` generates module boilerplate | Use it; match house style (`uncrustify.cfg`, `.editorconfig`) |
| q3map2: `-json <-unpack\|-pack> [-v] <mapname>` | Full BSP introspection without writing a lump parser |
| q3map2: `-pk3` autopackager, `-repack` (strips to only required shaders), `-repack -analyze` (dump BSP resource calls), `repack.exclude`, `-complevel`, `-png` | Dependency tracing and packaging are **already solved** — wrap, don't reimplement |
| Packer suffixes output `_FAILEDpack` on missing in-game resources | Free validation oracle |
| `.pk3dir` directories are treated as pk3s by ioq3, radiant and the compiler | Dev loop uses a pk3dir; pack only at release |
| Assimp model loading (40+ formats) | Blender can export OBJ/FBX/glTF, not just ASE/MD3 |
| 20 model autoclip modes, `-clipdepth`/`_clipdepth`, `-debugclip`, coplanar + volumetric triangle merging | Model collision is tunable and much better than stock q3map2 |
| `-keepmodels` / `_keepModels`, `misc_model::_remap`, negative `modelscale` | Asset pipeline knobs |
| `-nobouncestore`, `-extlmhacksize`, `-maxshaderinfo`, `-brightness/-contrast/-saturation` | Compile/lighting optimization knobs |
| Brush formats: Axial Projection, Brush Primitives, Valve 220 — all supported, autodetected | Kernel must handle **all three** and preserve the file's original format |
| Urban Terror ships as a supported gamepack | Entity defs are on disk — read them, don't hardcode |

---

## 3. Geometry kernel (`nrc-core`)

### 3.1 Representations

| Level | Type | Used for |
| --- | --- | --- |
| L0 | Plane list (`.map` on-disk form) | I/O only |
| L1 | `Brush` = convex polytope + per-face `TexDef` | Canonical in-memory form |
| L2 | `Patch` = Bézier control grid (patchDef2/3) | Curves, arches, terrain |
| L3 | `Solid` = CSG expression tree | **Sculpting IR** (§4) |
| L4 | `Mesh` = triangle soup + materials | Blender assets (§5) |

### 3.2 Requirements

- **Round-trip fidelity.** Load and re-save any shipped UrT `.map` byte-identically
  (modulo whitespace normalization the tests explicitly allow). This is the gate for
  everything else.
- **Three texture formats.** AP (`shader Xoff Yoff rot Xscale Yscale flags`),
  Brush Primitives (2×3 texture matrix), Valve 220 (explicit U/V axes). Detect on load,
  preserve on save, convert on demand.
- **Exact predicates.** Use robust orientation/incircle predicates (Shewchuk-style) or
  rational arithmetic for plane intersection. Floating-point slop is how you produce
  invisible micro-slivers that leak maps.
- **Integer snapping.** All authored vertices snap to a configurable grid (default 1,
  authoring default 8/16). Off-grid vertices are a validation error, not a warning.
- **Winding derivation.** Plane set → face windings via half-space clipping; detect
  redundant planes, degenerate faces, non-convex/empty brushes.

### 3.3 Differential test harness (build this first)

```
for map in corpus/*.map:
    a = q3map2 -bsp map        ; q3map2 -json -unpack a.bsp
    b = load(map); save(map')  ; q3map2 -bsp map' ; -json -unpack b.bsp
    assert semantic_diff(a, b) == ∅
```

Corpus: shipped UrT map sources where available, plus a synthetic suite covering
every brush format, patch type, degenerate case and entity form. Nothing else in this
spec is trustworthy until this is green.

---

## 4. Sculpting

Yes, the agent can be genuinely good at brushwork — but not by emitting plane
equations. Three things make it work.

### 4.1 A representation that cannot express invalid geometry

The **Solid IR** is a CSG/parametric tree where *every expressible program compiles
to valid, convex, on-grid brushes*. The agent authors IR; the kernel guarantees
validity. Errors become "this shape is wrong", never "this file is corrupt".

**Primitives** — `box`, `wedge`, `prism(n)`, `cylinder(n, caps)`, `cone`, `pyramid`,
`dome(n,m)`, `stair(steps, rise, run)`, `ramp`, `arch(radius, thickness, segments)`,
`pipe(inner, outer, n)`, `torus_segment`.

**Operators**

| Operator | Notes |
| --- | --- |
| `extrude(profile2d, path)` | Profile is a closed polyline; auto-decomposed if concave |
| `revolve(profile2d, axis, sweep, segments)` | |
| `loft(profileA, profileB, steps)` | |
| `hollow(solid, thickness, open_faces[])` | Room-from-solid; the single most used op |
| `subtract(a, b)` | **Real** boolean with exact convex decomposition |
| `intersect(a, b)` | |
| `bevel(solid, faces[], size)` / `chamfer` / `inset` | |
| `array(solid, count, offset)` / `radial(solid, count, axis)` / `mirror(solid, plane)` | |
| `carve_opening(wall, rect \| shape, position)` | Doorways/windows without hand-splitting |
| `to_patch(solid, faces[])` | Emit curves instead of brushes where appropriate |

**Post-conditions enforced on every compile:**
convexity, planarity (ε-tight), grid alignment, min face area, min brush thickness
(default 1u, warn <2u), no duplicate/coincident brushes, no T-junction-inducing splits
where avoidable, watertight hull for anything flagged structural.

**The hard part is `subtract`.** An exact convex decomposition of an arbitrary
polyhedron into a *small* number of brushes is the core engineering risk. Recommended
approach: BSP-based partitioning of the difference volume, then greedy convex merging
of adjacent cells with a coplanarity/convexity test. Optimize for brush count, not
speed — mappers will re-run this rarely, but they will look at the output forever.
Budget real time here; a naive decomposition that emits 200 brushes for a doorway is
worse than useless.

### 4.2 Visual feedback (non-negotiable)

Sculpting blind fails. Every mutating tool returns, by default, a **contact sheet**:

- three orthographic wireframe views (top/front/side) with grid and dimension
  annotations,
- one perspective solid-shaded view from a caller-specified or auto-framed camera,
- optionally a "player-eye" view at 56u eye height from a nearby floor point.

Images come back as MCP image content. This one feature is the difference between an
agent that produces boxes and an agent that produces architecture. Render headlessly
from the `.map` (software rasterizer or offscreen GL); the live editor bridge (§8)
gives higher-fidelity views once available.

Annotate renders with what the agent can't see: dimensions, brush count, off-grid
vertex markers, non-convex highlights, caulk/texture state, structural-vs-detail
colouring.

### 4.3 Taste, encoded as reference data

An agent with a valid geometry kernel still produces spaces that feel wrong. Fix that
with a **dimension corpus**: parse a library of released UrT maps and extract
distributions.

```
reference_dimensions(category="corridor", gametype="ctf")
→ { width:   {p10: 96,  p50: 128, p90: 192},
    height:  {p10: 128, p50: 160, p90: 256},
    length:  {p10: 256, p50: 512, p90: 1024},
    samples: 341, sources: ["ut4_abbey", "ut4_casa", ...] }
```

Categories: corridor, room, doorway, window, stair, cover object, courtyard, spawn
room, bomb site, flag room, ledge, balcony, vent. Also extract sightline length
distributions, cover density, and room-connectivity degree.

Ship a `design_conventions` MCP **resource** stating the tier rules explicitly:

| Tier | Representation | Use for |
| --- | --- | --- |
| 1 | Brush, **structural**, caulked | Anything sealing the map or blocking vis. Grid ≥ 16. |
| 2 | Brush, **detail** | Architecture, trim, cover, stairs. Grid ≥ 8. |
| 3 | Patch | Arches, pipes, curved walls, terrain. Never structural. |
| 4 | Mesh (`misc_model`) | Ornament, clutter, organic, high-poly. Always non-structural. |

Decision rule the agent applies: *does it block movement, block vis, or need cheap
clean collision?* → brush. *Is it axis-aligned architecture?* → brush. *Is it curved
but simple?* → patch. *Everything else* → Blender.

### 4.4 Sculpting tool surface

`solid_create`, `solid_op` (apply operator to named solids), `solid_preview`
(compile IR → render, **without** committing to the map), `solid_commit`
(compile → brushes → insert, one undo group), `solid_inspect` (dump IR tree),
`solid_edit_param` (change a parameter and recompile — parametric editing is the
big win over raw brushes: "make that corridor 32 units wider" becomes one call).

Keep the IR tree persisted alongside the `.map` in a sidecar (`mapname.solids.json`)
so parametric intent survives across sessions. The `.map` stays canonical and
hand-editable; the sidecar is advisory and re-derivable.

---

## 5. Blender handoff

### 5.1 Division of labour

`nrc-mcp` never does mesh authoring. It **specifies, validates, and integrates**.
Blender (via `blender-mcp` or Blender's official MCP server) does the modelling.

Two credible Blender MCP servers exist: `ahujasid/blender-mcp` (socket addon +
Python server, the de-facto community standard) and Blender's own official server
from blender.org. Both expose arbitrary Python execution inside Blender —
blender.org explicitly warns this runs LLM-generated code without guards and
recommends a VM or a machine with no sensitive data. Treat that as a hard
requirement for any unattended agent loop.

### 5.2 The asset brief

The key contribution of `nrc-mcp` to this pipeline is that it emits a **numerically
complete brief**, so the agent's Blender prompt is parametric rather than vibes-based.

```json
{
  "tool": "blender_brief",
  "asset_id": "crate_stack_a",
  "purpose": "cover object, bomb site B",
  "units": {
    "quake_units_per_meter": 39.37,
    "note": "1 Quake unit ≈ 1 inch. Model in Blender at 1 BU = 1 QU and
             disable unit scaling on export, OR model in metres and export
             with scale 39.37. State which and be consistent."
  },
  "bounds_qu": { "x": [0, 96], "y": [0, 64], "z": [0, 72] },
  "must_fit_within": "brush volume at (1024, -512, 16)..(1120, -448, 88)",
  "origin": "min-corner at local (0,0,0), +Z up, +X = model forward",
  "budget": { "triangles": 900, "materials": 2, "draw_calls": 2 },
  "materials": [
    { "slot": 0, "name": "urt/crate_wood_01",
      "must_resolve_to": "textures/urt/crate_wood_01",
      "exists_in_pk3": true },
    { "slot": 1, "name": "urt/metal_band_01", "exists_in_pk3": true }
  ],
  "uv": { "channel": 0, "required": true, "texel_density_px_per_qu": 2.0,
          "overlap_allowed": false },
  "export": { "format": "obj", "axis_up": "Z", "axis_forward": "Y",
              "path": "assets/models/crate_stack_a.obj" },
  "collision": {
    "strategy": "brush_hull",
    "reason": "cover object on a competitive sightline — autoclip is
               too imprecise for peeking geometry",
    "hull_will_be_generated_by": "nrc-mcp:model_make_clip"
  },
  "silhouette_notes": "readable from 1200qu; top edge must be a clean
                       horizontal at z=72 for consistent crouch-peek"
}
```

`blender_brief` also returns a **ready-to-send prompt string** for the Blender agent,
embedding the numbers above and the standing rules (no n-gons on collision surfaces,
apply all transforms, single object per asset, materials named exactly as specified,
triangulate before export).

### 5.3 Ingest and validation

`model_import` runs on whatever Blender exported:

- **Scale sanity** — compare mesh bounds against `bounds_qu`. Wrong-scale imports
  (the 39.37× metre/inch error) are the #1 failure and are trivially detectable.
- Triangle count vs. budget; material count; material names resolvable to shaders
  present in the pk3/pk3dir.
- UV presence, range, texel density vs. the brief.
- Watertightness and manifoldness if collision is derived from the mesh.
- Degenerate triangles, zero-area faces, unapplied transforms (non-identity object
  matrix), non-Z-up orientation.

Then it writes the `misc_model` entity with `model`, `modelscale`/`modelscale_vec`,
`angles`, `_lightmapScale`, `_castShadows`/`_receiveShadows`, and any `_remap`.

### 5.4 Collision strategy

Three options, and the MCP should choose deliberately:

| Strategy | When | How |
| --- | --- | --- |
| `nonsolid` | Pure ornament off the playable surface | No clip at all |
| `autoclip` | Terrain, large organic masses, decorative rock | Pick from the fork's 20 autoclip modes; tune `_clipdepth`; verify with `-debugclip` |
| `brush_hull` | **Default for anything competitively relevant** | `model_make_clip` computes a convex decomposition of the mesh (or its OBB/convex hull) and emits `common/playerclip` brushes |

For Urban Terror specifically, prefer `brush_hull` on anything a player can peek,
slide along, wall-jump off, or take cover behind. Autoclip is much improved in this
fork (coplanar and volumetric triangle merging) but still produces collision that
doesn't match what players expect from a competitive shooter. Snappy, predictable
collision beats accurate collision.

`model_make_clip` should also emit a `common/weapclip` variant where the visual mesh
has gaps that bullets shouldn't pass through, and detect the inverse case
(clip hull larger than the visual, producing "invisible wall" complaints).

### 5.5 Orchestration guidance for the agent

Ship an MCP **prompt** (`asset_pipeline`) that encodes the loop:

1. `asset_plan` — decide brush/patch/mesh split for the described feature.
2. For mesh parts: `blender_brief` → send to Blender MCP → export.
3. `model_import` → validation report. Iterate with Blender on failures.
4. `model_place` → `model_make_clip` → `render_camera` for a visual check.
5. `validate` → `compile --preset draft` → confirm nothing regressed.

---

## 6. Optimization subsystem

Yes — and this fork gives an unusual amount to work with. Four domains.

### 6.1 Build / compile

**Presets** (all overridable):

| Preset | Flags (indicative) | Purpose |
| --- | --- | --- |
| `draft` | `-bsp -meta` | Geometry check, seconds |
| `iterate` | `-bsp -meta` / `-vis -fast` / `-light -fast -samples 1 -bounce 0` | Playable test build |
| `quality` | `-bsp -meta` / `-vis` / `-light -fast -filter -samples 2 -bounce 4 -patchshadows -nobouncestore` | Review build |
| `final` | `-bsp -meta` / `-vis -saveprt` / `-light -filter -samples 3 -bounce 8 -patchshadows -dirty -nobouncestore` | Release |

`-nobouncestore` stores the BSP, lightmaps and shader files only once after the last
bounce — a meaningful saving on high-bounce compiles.

**A/B benchmarking.** `compile_ab` makes a change, compiles both variants, and diffs:
compile wall time per stage, portal count, leaf count, draw surface count, lightmap
count and bytes, BSP size, `MAX_*` headroom. Persist a history table so regressions
are visible over the life of the project.

**Vis optimization.** The single biggest lever on a Q3-engine map:

- `structural_audit` — every brush that is structural but shouldn't be (detached
  detail geometry, trim, stairs, cover, anything not sealing the map or forming a
  major visual blocker). Reports the estimated portal reduction from converting each.
- `hint_suggest` — read the `.prt` from a `-vis -saveprt` run, find leaves with
  pathological portal counts, and propose hint brush planes with a predicted
  before/after portal count. This is tedious, high-skill, high-payoff work that
  almost nobody does properly by hand.
- `leak_trace` — parse the pointfile, render the leak path over the top-down view.
- Areaportal audit for doors.

### 6.2 BSP-level analysis

`-json -unpack` gives the entire BSP as JSON: shaders, planes, leafs, brushes,
drawsurfs, lightmaps, entities. `bsp_report` reads it directly and produces:

- Surface count by shader (find the shader eating your draw calls).
- Lightmap page count and utilization.
- Leaf/portal statistics; the worst leaves by potentially-visible set size.
- Headroom against every `MAX_MAP_*` limit, as percentages.
- Entity lump diff vs. the source `.map` (catches entities silently dropped).

`-json -pack -useflagnames` (with `shaders.json`) round-trips edits back into a BSP —
useful for surgical fixes without a full recompile.

### 6.3 Shaders

- **Unused / missing** — shaders defined but never referenced; surfaces referencing
  shaders that don't exist; shaders shadowing q3ut4 originals (a classic cause of
  "works for me, broken on the server").
- **The watercaulk trap** — visible surfaces whose shader has no maps. The repacker
  already warns on this; surface it as a first-class validation finding.
- **Cost analysis** — multi-stage blends on large-area surfaces, `deformVertexes` on
  high-triangle surfaces, `alphaFunc` where blend would be cheaper, and the
  `q3map_surfacelight` + low `q3map_lightsubdivide` combination on large sky surfaces
  (a known stack-blowing pattern, mitigated in this fork but still expensive).
- **Lightmap strategy** — per-entity/per-group `_lightmapScale` tuning driven by
  measured surface importance; `-extlmhacksize` for higher-resolution external
  lightmaps that stay vanilla-Q3 compatible.
- **Dedup and remap** — `q3map_remapshader` and `misc_model::_remap` to reuse assets
  instead of duplicating them.
- `-maxshaderinfo <N>` when a large map exceeds the 8192 default.

### 6.4 Textures and files

**Textures** — audit resolution against in-world texel density (a 1024² on a 32-unit
trim piece is pure waste); flag NPOT; find unreferenced textures; propose a
downscale plan against a total-pk3-size budget with a preview of the visual delta.

**Packaging** — wrap, don't reimplement:

| Task | Mechanism |
| --- | --- |
| Build the pk3 | `q3map2 -pk3 <bsp>` (complete Q3 support; `-png`, `-dbg`) |
| Strip to only what's used | `-repack` (strips out only required shaders) |
| Dependency audit | `-repack -analyze` — dumps BSP resource calls; parse this instead of tracing dependencies yourself |
| Exclusions | `repack.exclude` |
| Compression | `-complevel -1..10` |
| Missing-resource detection | Packer suffixes the output `_FAILEDpack` — use as a pass/fail oracle |
| Dev iteration | Work in a `.pk3dir`; ioq3, radiant and the compiler all treat it as a pk3. Only pack for release. |

**Release checklist tool** (`ship_check`): `.arena` file present and consistent with
the spawns/objectives actually in the map; levelshot present and correctly sized;
no shaders shadowing q3ut4; no `_FAILEDpack`; pk3 size within budget; map name
follows `ut4_` convention; readme/licence present.

---

## 7. Urban Terror knowledge layer

All of this lives in a versioned, human-editable profile (`profiles/urt43.yaml`),
never in code. Anything sourced from documentation rather than verified against real
map files is marked `confidence: unverified` and excluded from hard failures.

### 7.1 Verified rules (hard validators)

From the official FrozenSand level-design documentation:

- Team spawns use **`info_ut_spawn`** with keys `team` (red/blue), `group`,
  `g_gametype` (comma-separated list), `angle`.
- Spawn groups should contain **16 points**, spaced **≥ 16 units apart**.
- **Team Survivor needs its own dedicated spawn entities** — TS cannot be shared with
  other gametypes via `g_gametype`. In TS each group takes a *unique* `group` name
  (unlike other modes where both teams share a group name); the game picks the two
  furthest-apart sets.
- **FFA does not use `info_ut_spawn`** — it uses `info_player_start` /
  `info_player_deathmatch`, placed sparsely, away from likely firefight centres.

Gametype IDs (for `.arena` and the `g_gametype` spawn key):

| ID | Mode | ID | Mode |
| --- | --- | --- | --- |
| 0 | FFA | 7 | CTF |
| 1 | LMS | 8 | Bomb |
| 3 | TDM | 9 | Jump Training |
| 4 | TS | 10 | Freeze Tag |
| 5 | FTL | 11 | Gun Game |
| 6 | Capture & Hold | | |

These four spawn rules alone catch a bug class that ships regularly (players spawning
at `info_player_start` during TS).

### 7.2 To be verified before writing validators

Harvest from three sources and cross-check: the shipped NetRadiant-custom Urban
Terror gamepack `.def`/`.ent` files, the FrozenSand level-design articles, and a
corpus of released `.map` sources.

- Bomb-mode objective and bombsite entities and their keys.
- Capture & Hold flag entities.
- Jump-mode entities (`ut_`-prefixed telepads, checkpoints, timers).
- Item and weapon entity names and spawn keys.
- Ladder, breakable-window, spawn-door and surface-sound conventions (all have
  official articles).

Do not ship validators for these until the corpus confirms them.

### 7.3 Analysis tools

- **`balance_report`** — per-team path distance and estimated traversal time from each
  spawn group to each objective; CTF symmetry diff; rotation/mirror symmetry detection.
- **`sightline_report`** — LOS matrix over sampled playable positions; distribution of
  sightline lengths (UrT is sniper-sensitive; long uncontested lanes are a design
  smell); "power position" identification by how much of the map a point sees.
- **`cover_report`** — cover density per region, peek-angle counts at chokepoints.
- **`movement_check`** — UrT-specific clearances: opposing-surface spacing for
  wall-jumps, slide runout lengths, ladder placement, standing (56u) and crouch
  headroom, 18u step height, doorway widths. **Calibrate the physics constants
  empirically** by instrumenting the engine — do not trust forum numbers, and do not
  trust mine.
- **`spawn_safety`** — for each spawn, time-to-first-contact and number of exits.

Navmesh: derive from compiled AAS (`bspc`) where bots are wanted, otherwise voxelize
the BSP collision hull. A* over that for all distance/time metrics.

### 7.4 Portability: UrT is a profile, not a fork

Urban Terror is the *first* target and the one every design decision is validated
against — but it is deliberately confined to one layer. Nothing above it needs to know
which game it's serving.

**Already game-agnostic (no work to port):**

| Layer | Why it's portable |
| --- | --- |
| §1 mise infrastructure | Toolchain, tasks, CI — game-neutral |
| §3 geometry kernel | `.map` brush/patch formats are idTech-wide, not mod-specific |
| §4 Solid IR and sculpting | Pure solid modelling; a hollowed room is a hollowed room |
| §4.2 rendering | Operates on brushes, not semantics |
| §5 Blender pipeline | Only the unit scale and export format are parameters |
| §6 optimization | q3map2, vis, lightmaps, shaders, pk3 — engine-level, not game-level |
| §9 editor bridge | The editor already supports many gamepacks; the RPC surface says nothing about any game |
| §11 self-optimization | Fitness suite is parameterized by profile |

**Genuinely UrT-specific (the profile):** the entity ontology, gametype IDs, spawn
rules, movement constants, balance heuristics, packaging conventions (`.arena`,
levelshots, `ut4_` naming), and the reference-dimension corpus. All of it already
lives in `profiles/urt43.yaml` plus a corpus directory — data, not code.

**Porting tiers:**

- *Trivial* — other Q3/ioquake3 mods and games. Same `.map`, same q3map2, same BSP,
  same `.shader` model. A new profile YAML, a new gamepack reference, a new corpus.
  Days, not months.
- *Moderate* — other idTech branches the editor already ships gamepacks for (Q2, Q1,
  Doom 3, RTCW/ET lineages). Kernel handles the brush formats; needs a different
  compiler driver and BSP reader per branch, and Doom 3's brush/material model differs
  enough to matter.
- *Hard* — anything outside brush-based idTech. Reuse the Solid IR and the Blender
  broker; replace everything below.

**Where UrT will leak in if nobody watches.** This is the real answer to the question,
and it needs enforcement rather than good intentions:

- Physics constants (56u standing, 18u step, walljump spacing) hardcoded into
  `movement_check` instead of read from the profile.
- Entity classnames appearing in validator *code* rather than profile *data*.
- `1 quake unit ≈ 1 inch` baked into the Blender brief generator.
- q3-style `.shader` assumptions inside the generic shader auditor.
- The concept of "gametype" itself treated as universal.

Mitigation: a lint in `mise run test` that **fails the build if any UrT-specific
string appears outside `profiles/` or `corpus/`**. Cheap, mechanical, and it catches
the drift on the day it happens rather than two years later.

**But do not build the abstraction now.** Write it for Urban Terror concretely; keep
the seam clean and the lint honest; generalize when a second game actually arrives.
Designing a multi-game profile interface before you have shipped one game produces the
wrong abstraction, because you don't yet know which parts vary. The portability
guarantee here is *"the seam exists and is enforced"*, not *"the abstraction is
already written"*.

A good forcing function later: once F3 (validator accuracy) is stable, add a second
game's corpus purely as a **portability regression test**. If adding baseq3 or
OpenArena requires touching anything outside `profiles/`, the seam has leaked.

---

## 8. MCP surface

### 8.1 Tools

**Session / map**
`map_open`, `map_save`, `map_new`, `map_stats`, `map_diff`, `region_set`

**Query**
`query_entities(filter)`, `query_brushes(volume|texture|flags)`, `bbox_of(selector)`,
`raycast(from, to)`, `los_matrix(points)`, `path_distance(from, to)`,
`reference_dimensions(category)`

**Sculpt** (§4.4)
`solid_create`, `solid_op`, `solid_preview`, `solid_commit`, `solid_edit_param`,
`solid_inspect`

**Direct edit**
`brush_create`, `brush_transform`, `patch_create`, `retexture`, `set_detail`,
`entity_create`, `entity_set_keys`, `entity_delete`, `place_clip`, `mirror_selection`

**Assets** (§5)
`asset_plan`, `blender_brief`, `model_import`, `model_place`, `model_make_clip`,
`model_audit`

**Build & optimize** (§6)
`compile(preset, overrides)`, `compile_ab`, `bsp_report`, `portal_report`,
`hint_suggest`, `structural_audit`, `leak_trace`, `shader_audit`, `texture_audit`,
`pack_pk3`, `repack_analyze`, `ship_check`

**Analyze (UrT)** (§7.3)
`validate(profile)`, `balance_report`, `sightline_report`, `cover_report`,
`movement_check`, `spawn_safety`, `arena_check`

**See**
`render_topdown`, `render_camera`, `render_contact_sheet`, `render_overlay(metric)`

**Project / meta** (§1, §10, §11)
`task_list`, `task_run(name, args)`, `pr_plan_status`, `pr_plan_refresh`,
`pr_surface_report`, `upstream_diff`, and — behind an explicit opt-in flag —
`selfdev_propose`, `selfdev_run`, `selfdev_bench`, `selfdev_history`,
`selfdev_diff`, `selfdev_revert`, `selfdev_protected`

### 8.2 Representative schemas

```json
{
  "name": "solid_commit",
  "inputs": {
    "ir": "<Solid IR JSON>",
    "target_entity": "worldspawn",
    "detail": true,
    "default_texture": "urt/concrete_02",
    "hidden_faces_texture": "common/caulk",
    "grid": 8,
    "dry_run": false
  },
  "returns": {
    "brushes_created": 34,
    "brush_ids": ["..."],
    "warnings": [
      {"code": "THIN_BRUSH", "brush": "b17", "thickness": 1.0}
    ],
    "undo_group": "solid_commit:crate_alcove",
    "render": "<image/png contact sheet>"
  }
}
```

```json
{
  "name": "validate",
  "inputs": { "profile": "urt43", "severity_min": "warning",
              "categories": ["spawns", "geometry", "shaders", "packaging"] },
  "returns": {
    "findings": [
      { "severity": "error", "code": "UT_TS_SPAWN_SHARED",
        "message": "8 info_ut_spawn entities list gametype 4 alongside others; TS requires dedicated spawns",
        "entities": ["e142", "e143", "..."],
        "rule_source": "urbanterror.info/support/148",
        "confidence": "verified",
        "fix": { "tool": "entity_set_keys", "preview": "..." } }
    ],
    "summary": { "error": 3, "warning": 11, "info": 24 }
  }
}
```

Every finding carries a `rule_source` and a `confidence`. Every fix is a *proposed*
tool call, never auto-applied.

### 8.3 Resources

`urt://entities` (parsed gamepack defs), `urt://shaders`,
`urt://conventions` (the tier rules and design guidance from §4.3),
`urt://profile/urt43` (the rule profile),
`nrc://q3map2-flags` (this fork's flags, extracted from `docs/changelog-custom.txt`),
`nrc://tasks` (live `mise tasks --json`),
`nrc://pr-plan` (current `docs/pr-plan.md`),
`nrc://bench` (latest fitness suite results),
`map://current/summary`

### 8.4 Prompts

`asset_pipeline` (§5.5), `optimize_pass`, `new_map_bootstrap`, `gametype_retrofit`
(add a gametype's spawns/objectives to an existing map).

---

## 9. The editor bridge (`contrib/mcpbridge`) — the PR

### 9.1 Shape

Modelled on `contrib/sunplug`: `mcpbridge.cpp`, `mcpbridge.h`, `mcpbridge.def`, plus
a Makefile entry. Generate boilerplate with `generic_module.py`. Format with the
repo's `uncrustify.cfg`.

- Localhost TCP (configurable port) or named pipe; newline-delimited JSON-RPC 2.0.
- No new dependencies — hand-rolled JSON writer/parser or a single vendored header.
- Binds only to existing `include/` interfaces. **Zero core diffs** is the target.
- Disabled by default: build flag off, and a preferences toggle required at runtime.
- Every mutating RPC wrapped in one `UndoableCommand` so a human can Ctrl+Z an entire
  agent operation.
- Bind to `127.0.0.1` only; optional shared-secret handshake. Document plainly that
  this exposes editor control to any local process.

### 9.2 RPC surface (deliberately minimal)

```
scene.stats                      → counts, bounds
scene.select(query)              → selection ids
scene.selection                  → current selection description
scene.transform(matrix)          → apply to selection
scene.create_brush(planes[])     scene.create_entity(classname, keys, origin)
scene.set_keys(id, keys)         scene.delete(ids[])
scene.set_texture(ids[], shader, texdef)
camera.get / camera.set(origin, angles, fov)
view.render(view, width, height) → base64 PNG
map.save / map.reload / map.path
undo.begin(label) / undo.end / undo.undo
filter.set(name, bool)
```

Anything more complex is computed in `nrc-mcp` against the saved `.map`, not in C++.
The bridge is a thin window into live editor state, not a second implementation.

### 9.3 PR strategy

1. Ship phases 1–4 as a purely external tool. Use it. Accumulate evidence.
2. Open a **Discussion** first, not a PR: describe the plugin, its scope, the
   zero-core-diff constraint, and ask whether it's wanted in-tree at all.
3. If yes: one PR, one plugin, off by default, with a short `docs/` note and a demo.
4. If no: keep it as a maintained downstream patch. The external MCP still works;
   only live-editor sync is lost. This should not block anything.

Garux is an active maintainer with a specific vision for the fork. A self-contained,
opt-in, dependency-free plugin that touches nothing is the only version of this with
a real chance. Do not bundle it with core refactors, build-system changes, or
"while I was in there" fixes.

---

## 10. The living PR plan

A contribution plan written once is wrong within a month. Upstream moves, and — more
importantly — the RPC surface you *design* is always larger than the surface you can
*prove* is needed. This subsystem keeps the plan honest and shrinking.

Output: `docs/pr-plan.md`, regenerated by `mise run pr:report`, with a changelog of
what changed and why.

### 10.1 Four input feeds

**1. Upstream watcher** (`mise run pr:watch`, nightly)
Polls `Garux/netradiant-custom` for commits, releases, issues, discussions and PRs.
Specifically diffs:

- the `include/*.h` interfaces the bridge binds to — hash each function signature the
  plugin depends on and alert on any change (this is the thing that silently breaks
  an out-of-tree plugin);
- `contrib/` structure and any new plugins (new conventions to match);
- `Makefile` / `Makefile.conf` build plumbing;
- `docs/changelog-custom.txt` — new q3map2 flags land here first, and each one is a
  potential new optimizer capability (§6), so this feed benefits the whole project,
  not just the PR.

**2. Maintainer-preference model** (`mise run pr:study`, monthly)
Mines the repo for what actually gets merged: merged-vs-closed PR characteristics
(size, scope, whether they touched core), review comment themes, commit message
style, the `.patchsets` file, `uncrustify.cfg`, `.editorconfig`, and the size and
shape of the `contrib/` plugins that were historically accepted. Distills to a short
"house style and merge criteria" document. Regenerated, not hand-maintained.

**3. Usage telemetry → surface pruning** (the most valuable feed)
The bridge logs which RPC methods are actually called in real sessions, how often,
and whether the call could have been served from the saved `.map` instead. Then:

> **Hard rule: any RPC method with zero real-session usage is cut before submission.**

Ship the *proven* minimum, not the designed minimum. A 9-method plugin that demonstrably
enables a workflow is a far easier review than a 20-method one where half is
speculative. Expect the surface in §9.2 to shrink by a third.

**4. Continuous rebase CI** (`mise run pr:rebase`)
A fork branch kept rebased on upstream `master`, building in CI on Linux, Windows
(MSYS2/mingw) and macOS — matching the repo's existing workflows. The PR is never
stale and never "works on my machine". A rebase failure is the earliest possible
signal that the design has drifted from upstream.

### 10.2 Readiness score

`pr_plan_status` returns explicit, checkable criteria:

| Criterion | Target |
| --- | --- |
| Core files touched | 0 |
| New third-party dependencies | 0 |
| Diffstat | < 900 lines added, all under `contrib/mcpbridge/` + 1 Makefile hunk |
| RPC methods, all exercised in real sessions | 100% |
| Builds clean on all three CI platforms | yes |
| Rebased on upstream master within | 7 days |
| `uncrustify.cfg` clean | yes |
| Demo artifact recorded | yes |
| Maintainer signal obtained (Discussion opened, response received) | yes |

`pr_plan_refresh` regenerates the plan and drafts the PR description from the
evidence ledger — concrete workflows enabled, before/after numbers, a recording.

### 10.3 Honest limit

This reduces staleness and diff size. It does not model a maintainer's judgment about
whether an in-editor RPC server belongs in his editor at all. That question gets
answered by opening a Discussion (§9.3), early, before any of this machinery matters.
The living plan makes the *ask* smaller and better evidenced; it can't make it wanted.

---

## 11. Self-optimization

Modelled on jcode's self-dev loop, where the agent edits its own source, builds,
tests, and hot-reloads its own binary, continuing across sessions.

### 11.1 The precondition everyone skips

Self-modification is only useful when there is an **objective fitness function**.
Without one, the agent hill-climbs on its own opinion of itself and drifts.

This project is unusually well suited to it, because almost everything it does is
measurable against ground truth:

| ID | Fitness signal | Measured by | Type |
| --- | --- | --- | --- |
| **F1** | Kernel correctness | Differential round-trip over the corpus (§3.3) | **Binary gate — never a score** |
| **F2** | Sculpting quality | Brush count emitted by `subtract`/`hollow` vs. hand-built reference solutions | Lower is better |
| **F3** | Validator accuracy | Precision/recall against a labelled corpus of known-good and known-broken maps | Higher is better |
| **F4** | Optimizer efficacy | Portal count, draw surfaces, compile time, pk3 size deltas on benchmark maps, with a no-visual-regression check | Higher is better |
| **F5** | End-to-end task success | A suite of natural-language briefs ("build a two-entrance bomb site with cover at both") scored by the validators and by render inspection | Higher is better |
| **F6** | Cost | Tokens, tool calls and wall time per completed task | Lower is better |

`mise run bench` produces `bench/results/<sha>.json`. Every self-dev attempt is scored
against it.

### 11.2 The loop

```
selfdev:propose  → hypothesis + target metric, written to the attempt log
selfdev:branch   → git branch, isolated worktree
selfdev:implement→ agent edits source / tasks / prompts
selfdev:verify   → mise run test  (F1 must pass, absolutely)
selfdev:bench    → mise run bench (F2–F6 deltas)
selfdev:gate     → merge if F1 green AND no metric regressed AND ≥1 improved
                   otherwise: revert, archive the attempt with its scores
selfdev:reload   → hot-reload and continue
```

**Hot reload split.** The Python MCP layer (tool definitions, prompts, resources,
orchestration) reloads in place — cheap, fast, low risk. The Rust kernel needs a
rebuild and re-exec: build to a staging binary, run a smoke test, then re-exec,
mirroring jcode's `hot_exec` approach. Keep the split deliberate: the fast loop should
run against the Python layer.

**Archive, not just a pointer.** Keep every attempt with its scores and allow branching
from non-latest ancestors. A pure greedy chain gets stuck; the self-improving-agent
literature (SICA, DGM, and the tree-search formulations that followed) is consistent
on this point.

### 11.3 Start with prompts, not code

The highest return per unit of risk is **not** in the kernel. It's in:

- tool descriptions (which drive whether the agent picks the right tool at all),
- the `urt://conventions` resource and the §4.3 design tier rules,
- the `blender_brief` template,
- the `asset_pipeline` prompt.

These are measurable through F5/F6, can't corrupt anyone's map, and are trivially
revertible. Get the loop working there first, and only extend it to the Rust kernel
once F1 has been proven to actually catch regressions.

### 11.4 Guardrails

These are load-bearing, not boilerplate.

- **Protected paths are immutable to self-dev**, hash-pinned and checked pre-merge:
  the differential harness, the fitness definitions and bench runner, the corpus, and
  every `confidence: verified` entry in the UrT rule profile. Without this the agent
  optimizes the ruler instead of the thing — the single most likely failure mode.
- **F1 is a gate, not a weight.** It can never be traded against a speed win.
- **Human review required** for changes to the exact geometric predicates, to anything
  that writes user `.map` files, and to the protected-path list itself.
- **Sandboxed execution**, pinned network allowlist, per-attempt token/time budget.
- **Git-backed with automatic revert.** No self-dev change reaches `main` without a
  green gate; every one is an isolated, revertible commit.
- **Use a frontier model.** jcode's own documentation warns that weaker models
  introduce subtle breaking changes in a complex codebase. In a geometry kernel with
  exact predicates, "subtle" means maps that compile fine and leak three weeks later.
- **Rate-limit it.** Self-dev runs on demand or nightly, not continuously. An agent
  rewriting itself in the middle of someone's mapping session is not a feature.

### 11.5 Tools

`selfdev_propose`, `selfdev_run`, `selfdev_bench`, `selfdev_history`,
`selfdev_diff`, `selfdev_revert`, `selfdev_protected` (read-only listing).

All are gated behind an explicit opt-in flag; a normal mapping session never sees them.

---

## 12. Phasing

| Phase | Deliverable | Gate |
| --- | --- | --- |
| **0** | mise bootstrap: toolchain pinned, netradiant + q3map2 building from `mise run`, corpus fetched, CI green | `mise run bootstrap && mise run test` works on a clean machine, all three platforms |
| **1** | `.map` parse/serialize + geometry kernel, all three brush formats | Byte-identical round-trip of the UrT corpus; differential compile test green |
| **2** | Read-only MCP tools + `render_topdown` / contact sheet | Its analysis of `ut4_abbey`/`ut4_casa`/`ut4_prague` matches expert consensus |
| **3** | q3map2 driver, `bsp_report`, packaging, `validate` with the four verified spawn rules | Correctly flags known-broken community maps |
| **4** | Solid IR + sculpting tools + convex decomposition | Agent builds a coherent, compilable, leak-free two-room-and-corridor block from a text brief |
| **5** | Blender handoff: `blender_brief`, `model_import`, `model_make_clip` | Round-trip a prop from brief → Blender → placed, clipped, compiled, in-game |
| **6** | Optimization suite: `hint_suggest`, `structural_audit`, shader/texture audits, `compile_ab` | Measurable improvement (portal count, r_speeds, pk3 size) on a real map without visual regression |
| **7** | UrT analysis: navmesh, balance, sightlines, movement checks | Metrics correlate with community judgements of known maps |
| **8** | Editor bridge + upstream discussion/PR | Live sync working; maintainer engaged |
| **9** | Fitness suite (`mise run bench`) + self-optimization on the prompt/resource layer only | Measurable F5/F6 improvement across ≥10 accepted attempts, zero F1 regressions |
| **10** | Self-optimization extended to the Rust kernel; living PR plan automation | Protected-path enforcement proven by a deliberate red-team attempt to game the fitness function |

Phase 0 is not optional overhead. The self-optimization loop in phase 9 is only
possible because phases 0–3 made every operation a scored, reproducible task.

---

## 13. Risks

**The geometry kernel is the whole project.** A subtle plane-intersection bug silently
corrupts maps in ways that surface hours later at compile time. Over-invest in phase 1.

**Convex decomposition quality determines whether sculpting is usable.** If `subtract`
emits sprawling brush counts, mappers will reject the output regardless of how good
the IR feels. Prototype this early and measure brush counts against hand-built
equivalents.

**Rendering is load-bearing, not a nice-to-have.** Deprioritizing the visual feedback
loop is the most likely way this ends up producing technically valid, aesthetically
dead levels.

**Verify the UrT entity ontology from real sources** — the gamepack defs and released
map files — not from documentation alone and not from any model's recollection,
including mine. Bomb-mode and jump-mode entities in particular are unconfirmed here.

**Blender's MCP servers execute arbitrary Python.** Sandbox the Blender side if the
loop runs unattended.

**The agent will try to game its own fitness function.** This is not hypothetical; it
is the default behaviour of any optimization loop with a mutable objective. The
protected-path mechanism (§11.4) is the only thing standing between "self-improving"
and "self-congratulating". Red-team it deliberately before trusting a single result.

**Self-modification of the geometry kernel is the highest-risk item in this document.**
A subtly wrong exact predicate produces maps that compile clean and fail much later.
Gate it behind human review indefinitely; there is no rush to automate it, and the
prompt/resource layer holds most of the available gains anyway.

**Upstream drift breaks out-of-tree plugins silently.** The interface-hash watcher in
§10.1 exists because a changed signature in `include/` will compile-fail at the worst
possible moment otherwise. Run it nightly from day one, not from phase 8.

**Premature generalization is as dangerous as UrT lock-in.** The §7.4 seam is worth
enforcing from day one; a pluggable multi-game architecture is not worth building
until a second game exists. Ship one game properly first.

**Scope discipline on the PR.** The external tool must be fully useful without the
plugin. If the PR becomes load-bearing, an unenthusiastic maintainer response kills
the project.
