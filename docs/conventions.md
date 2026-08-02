# Design conventions

Exposed as the MCP resource `nrc://conventions`. §4.3 asks for this explicitly, because "an agent
with a valid geometry kernel still produces spaces that feel wrong".

This file is in `selfdev`'s allowed paths on purpose: §11.3 argues the highest return per unit of
risk is in the prompt and resource layer, not the kernel, and this is that layer. It is measurable
through F5, it cannot corrupt anyone's map, and it is trivially revertible.

---

## The four representation tiers

| Tier | Representation | Use for | Minimum grid |
| --- | --- | --- | --- |
| 1 | Brush, **structural**, caulked | Anything sealing the map or blocking visibility | 16 |
| 2 | Brush, **detail** | Architecture, trim, cover, stairs | 8 |
| 3 | Patch | Arches, pipes, curved walls, terrain. **Never structural.** | — |
| 4 | Mesh (a model entity) | Ornament, clutter, organic, high-poly. **Always non-structural.** | — |

### The decision rule, in order

1. **Does it block visibility?** → tier 1. Only a structural brush can, so this outranks
   everything else regardless of how the thing looks.
2. **Does it block movement, or need cheap predictable collision?** → tier 2.
3. **Is it axis-aligned architecture?** → tier 2. Cheaper and tidier as a brush.
4. **Is it curved but simple?** → tier 3.
5. **Everything else** → tier 4.

`asset_plan` implements exactly this order. The ordering is the substance: a decorative-looking
pillar that happens to seal a room is still tier 1.

### Why the tiers matter more than they look

Structural brushes are the dominant cost on a Q3-engine map. Every one of them contributes portals,
and portal count drives both compile time and runtime visibility work. §6.1 calls the
structural-to-detail conversion the single biggest lever available, and `structural_audit` exists to
find candidates. The corollary for authoring: **default to detail, and promote to structural only
for geometry that genuinely seals or blocks.**

---

## Caulk

Every face a player cannot see should be the caulk shader. Not for tidiness — the compiler discards
caulk faces, so a caulked hidden face costs nothing while a textured one costs a draw surface and a
lightmap allocation.

In practice that means the outward faces of a room's shell, the backs and undersides of everything,
and the interior faces of anything solid. `render_topdown` with `overlay="caulk"` shows what is
currently caulked and what is not.

`solid_commit` takes `textures.default`, and defaulting it to caulk then overriding the visible
faces is usually less work than the reverse.

---

## Dimensions

Read them from the profile, not from here. `profile_summary` reports the movement constants and
their confidence, and `render_player_eye` places a camera at the verified standing height.

The one durable lesson: **do not carry a remembered number**. The design document assumed a 56-unit
standing height; the shipped gamepack says 69.375. A corridor sized from the wrong figure is a
corridor the player cannot stand up in, and it will pass every geometric check.

§4.3 also asks for a dimension corpus — width, height and length distributions per space category,
extracted from released maps. That is not built. `reference_dimensions` does not exist, and until it
does, sizing comes from the profile's verified constants plus looking at real maps with
`render_topdown`.

---

## Grid discipline

Author on a coarse grid and drop to a finer one only when the geometry demands it. 16 for structural
shells, 8 for detail, 1 only where something genuinely needs it.

Two things are worth knowing precisely, because the usual phrase "keep it on grid" hides them:

- A `.map` stores **planes**, not vertices, and a brush's vertices are wherever three of its planes
  meet. For axis-aligned geometry those land on the grid. For anything angled they do not, and no
  choice of plane can make them — Radiant's own cylinders are off-grid for the same reason.
- So `off_grid_vertices` in a compile report is not automatically a defect. It is a defect when
  axis-aligned geometry produces it, and unavoidable when a prism or an arch does. `validate` reports
  the count; judging it needs to know which kind of shape produced it.

---

## Authoring order

1. `asset_plan` — decide the tier before building anything.
2. `solid_compile` — check counts, bounds and warnings without touching the map.
3. `solid_preview` — look at it. §4.2 is blunt that sculpting blind fails.
4. `solid_commit` — with a label, so the brushes say where they came from and the IR is recorded.
5. `validate` and `validate_profile` — geometry, then the game's own rules.
6. `compile_map` with the `draft` preset — the compiler is the only authority on whether it builds.
7. `render_contact_sheet` — confirm nothing else moved.

Steps 2 and 3 are cheap and step 4 is not, which is the whole reason the preview exists.

---

## Composition patterns that work

**A room** is a hollowed box. `hollow` gives six brushes — floor, ceiling, four walls — which is
what a mapper draws.

**A doorway** is `carve_opening` on a wall, or `subtract` on a room. Either yields three brushes:
left column, right column, lintel. If it yields more, the cutter is not spanning the wall's full
thickness; extend it past both faces.

**A window** is the same with the opening clear of floor and ceiling, giving four brushes.

**Stairs** are one brush per step, each solid from the base up rather than a floating tread. Keep the
rise at or below the profile's step height or the player cannot walk up them.

**A corridor between two rooms** is a hollowed box overlapping both, with an opening cut at each end.
Overlapping brushes are legal and splitting them gains nothing.

---

## What is not built yet

Said plainly, because planning around a tool that does not exist wastes more time than the tool would
have saved:

- **Patches** are readable and renderable but not authorable. Tier 3 geometry has to be built in the
  editor for now.
- **The dimension corpus** (§4.3) does not exist, so there are no measured distributions to size
  against.
- **`reference_dimensions`, `balance_report`, `sightline_report`, `cover_report`** — the §7.3 analysis
  tools are not wired up.
