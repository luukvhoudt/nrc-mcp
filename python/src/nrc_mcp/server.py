"""The MCP surface (§8).

Read-only analysis, rendering, game-profile validation, the compile driver, BSP
introspection, packaging and the mise task surface. Sculpting (§4) and the Blender handoff
(§5) are not here yet, and the tool list deliberately does not pretend otherwise — a tool
that exists but does nothing is worse than one that is absent, because the agent will plan
around it.

Three conventions worth knowing:

**One open map.** Tools operate on a session map opened with `map_open`, rather than taking
a path every time. That keeps a sequence of queries consistent with each other, and makes
"the map I am editing" a single explicit thing.

**Nothing writes without being asked.** `map_save` is the only tool that touches a `.map`,
and it verifies the round-trip first: if the kernel cannot reproduce the file it loaded, it
refuses to write, because a tool that cannot reproduce your file has no business replacing
it.

**Confidence is enforced, not advertised.** Findings from game rules carry a `confidence`,
and an unverified rule is downgraded so it can never fail a build. That is not ceremony:
three of the four spawn rules the design document called verified were wrong, and one would
have failed correct maps (`nrc://corrections`).
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    # Current SDK layout.
    from mcp.server.mcpserver import Image as _Image
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - older SDKs
    # The class was called FastMCP and lived elsewhere before. The decorator surface we
    # use (`.tool()`, `.resource()`, `.run()`) is the same, so supporting both costs one
    # import and avoids pinning users to one SDK generation.
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp import Image as _Image

from . import analysis as anamod
from . import blender as blendermod
from . import bsp as bspmod
from . import optimize as optmod
from . import pack as packmod
from . import profiles, rules, solids, tasks
from .kernel import KernelUnavailable, load_map, repo_root, run_mise_task

mcp = _Server("nrc-mcp")


@dataclass
class Session:
    """The one open map, plus what we know about it."""

    map: Any = None
    path: Path | None = None
    grid: int = 8
    warnings: list[str] = field(default_factory=list)

    def require(self):
        if self.map is None:
            raise ValueError(
                "no map is open — call map_open(path) first. "
                "Use task_list() to see what else the project can do."
            )
        return self.map


SESSION = Session()


def active_profile() -> str:
    return os.environ.get("NRC_PROFILE", "")


def _kernel():
    from .kernel import kernel

    return kernel()


# ---------------------------------------------------------------------------
# Session / map
# ---------------------------------------------------------------------------


@mcp.tool()
def map_open(path: str, grid: int = 8) -> dict:
    """Open a `.map` file for analysis.

    Args:
        path: path to the `.map`. Relative paths resolve against the repo root.
        grid: authoring grid that off-grid geometry is measured against.

    Returns a summary plus a round-trip check. If `round_trip.identical` is false, treat
    every later answer about this map with suspicion and report it — that is the §3.2 gate
    failing on a real file, which is a kernel bug worth fixing before anything else.
    """
    p = Path(path)
    if not p.is_absolute():
        p = repo_root() / p
    m = load_map(p)

    SESSION.map = m
    SESSION.path = p
    SESSION.grid = grid
    SESSION.warnings = []

    rt = m.round_trip()
    if not rt["identical"]:
        SESSION.warnings.append(
            "this map does not round-trip byte-identically; map_save will refuse to write"
        )

    stats = m.stats(grid=grid)
    return {
        "path": str(p),
        "round_trip": rt,
        "entities": stats["entities"],
        "brushes": stats["brushes"],
        "patches": stats["patches"],
        "texdef_kinds": stats["texdef_kinds"],
        "bounds": stats["bounds"],
        "grid": grid,
        "warnings": SESSION.warnings,
    }


@mcp.tool()
def map_stats(grid: int | None = None) -> dict:
    """Statistics for the open map: counts, bounds, shader histogram, grid alignment.

    `structural_brushes` vs `detail_brushes` is the split that matters most for vis
    performance (§6.1): structural brushes block visibility and are expensive, detail
    brushes do not.
    """
    m = SESSION.require()
    return m.stats(grid=grid if grid is not None else SESSION.grid)


@mcp.tool()
def map_save(path: str | None = None, allow_non_identical: bool = False) -> dict:
    """Write the open map back to disk.

    Refuses if the kernel cannot reproduce the loaded bytes, unless
    `allow_non_identical` is set. That guard is the point: a round-trip failure means the
    kernel misunderstands something in the file, and writing anyway risks silently
    discarding it.

    Args:
        path: destination. Defaults to the file the map was opened from.
        allow_non_identical: write even though the round-trip check failed.
    """
    m = SESSION.require()
    rt = m.round_trip()
    if not rt["identical"] and not allow_non_identical:
        return {
            "written": False,
            "reason": "the kernel cannot reproduce this file byte-for-byte, so writing it "
            "could lose data. Inspect round_trip.first_difference, or pass "
            "allow_non_identical=true if you accept the risk.",
            "round_trip": rt,
        }
    written = m.save(path)
    return {"written": True, "path": written, "round_trip": rt}


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


@mcp.tool()
def query_entities(classname: str | None = None, with_keys: bool = True) -> dict:
    """Entities in the open map, optionally filtered by classname.

    Keys come back as ordered pairs rather than a mapping, because key order is meaningful
    in a `.map` and duplicate keys occur in real files.
    """
    m = SESSION.require()
    ents = m.entities(classname=classname, with_keys=with_keys)
    return {"count": len(ents), "filter": classname, "entities": ents}


@mcp.tool()
def brush_geometry(entity: int, primitive: int) -> dict:
    """Exact vertices and derived properties of one brush.

    Vertices are computed with exact integer arithmetic. When a brush cannot be evaluated
    exactly — off-grid plane points, too few faces — this reports `usable: false` with a
    reason rather than returning approximate numbers that look authoritative.
    """
    m = SESSION.require()
    return m.brush_geometry(entity, primitive)


@mcp.tool()
def validate(grid: int | None = None, severity_min: str = "warning") -> dict:
    """Validate the open map's geometry and file format.

    Every finding carries a `rule_source` and a `confidence`. Only `verified` findings are
    safe to treat as hard failures.

    Scope note: these are the game-agnostic checks — degenerate brushes, duplicate and
    mirrored planes, off-grid vertices, thin brushes, patch problems. For the game's own rules
    (spawn arrangement, objective counts, gametype keys) call `validate_profile`.
    """
    m = SESSION.require()
    return m.validate(
        grid=grid if grid is not None else SESSION.grid,
        severity_min=severity_min,
    )


# ---------------------------------------------------------------------------
# See (§4.2)
# ---------------------------------------------------------------------------


def _image_and_notes(png: bytes, ann: dict, extra: str = "") -> list:
    """Return an image plus its numbers.

    §4.2 wants renders annotated with what the agent cannot see. The spatial parts are drawn;
    the counts and dimensions come back as text so they can be read exactly rather than
    inferred from pixels.
    """
    lines = [f"{ann['view']} view, {ann['width']}x{ann['height']}, overlay {ann['overlay']}"]
    b = ann.get("bounds")
    if b:
        lines.append(f"bounds {b['min']} .. {b['max']}  (size {b['size']})")
    else:
        lines.append("no geometry was drawn")
    c = ann["counts"]
    lines.append(
        f"structural {c['structural_brushes']}, detail {c['detail_brushes']}, "
        f"brush entities {c['brush_entities']}, patches {c['patches']}, "
        f"surfaces drawn {c['facets']} ({c['invisible_facets']} not rendered in game)"
    )
    if ann.get("units_per_pixel"):
        lines.append(f"scale: {ann['units_per_pixel']:.3g} units per pixel")
    if ann.get("camera_eye"):
        lines.append(f"camera at {ann['camera_eye']}")
    if ann["off_grid_vertices"]:
        lines.append(f"{ann['off_grid_vertices']} vertices off a grid of {ann['grid']}")
    if ann["skipped_brushes"]:
        lines.append(
            f"WARNING: {ann['skipped_brushes']} brush(es) not drawn: "
            + "; ".join(ann["skipped_examples"])
        )
    lines += ann.get("notes", [])
    if extra:
        lines.append(extra)
    return [_Image(data=png, format="png"), "\n".join(lines)]


@mcp.tool()
def render_topdown(
    overlay: str = "shaded",
    width: int = 1000,
    height: int = 800,
    grid: int | None = None,
    grid_spacing: float = 64.0,
    solid: bool = False,
) -> list:
    """Render a top-down (XY) view of the open map.

    Orthographic views render as backface-culled wireframe, which gives a readable
    architectural floor plan. A *solid* top-down of a sealed map shows only the underside of
    its sky brush — one flat rectangle — so pass `solid=True` only when you know the geometry
    is open.

    Args:
        overlay: `shaded`, `structural` (colour by structural/detail/entity/patch),
            `caulk` (highlight surfaces not drawn in game), or `off_grid` (mark bad vertices).
        grid: grid that off-grid vertices are measured against. Defaults to the session grid.
        grid_spacing: world-unit spacing of the drawn grid; 0 disables it.
        solid: fill faces instead of drawing wireframe.
    """
    m = SESSION.require()
    png, ann = m.render(
        view="top",
        overlay=overlay,
        width=width,
        height=height,
        grid=grid if grid is not None else SESSION.grid,
        grid_spacing=grid_spacing,
        wireframe=False if solid else None,
    )
    return _image_and_notes(png, ann)


@mcp.tool()
def render_camera(
    eye: list[float] | None = None,
    target: list[float] | None = None,
    fov_deg: float = 55.0,
    overlay: str = "shaded",
    width: int = 1000,
    height: int = 800,
    hide_invisible: bool = True,
    view: str = "perspective",
) -> list:
    """Render a perspective view, or one of the front/side orthographic views.

    Args:
        eye: camera position. Omit for an automatic three-quarter framing of the whole map.
        target: what to look at. Defaults to the centre of the geometry.
        view: `perspective`, `front` or `side`.
        hide_invisible: skip caulk, nodraw, clip and trigger surfaces. On by default because a
            sealed map is wrapped in such a shell, and a perspective view of the shell shows
            nothing useful.
    """
    m = SESSION.require()
    if view not in ("perspective", "front", "side", "top"):
        raise ValueError(f"view must be perspective, front, side or top — got {view!r}")
    png, ann = m.render(
        view=view,
        overlay=overlay,
        width=width,
        height=height,
        grid=SESSION.grid,
        grid_spacing=64.0,
        hide_invisible=hide_invisible,
        eye=list(eye) if eye else None,
        target=list(target) if target else None,
        fov_deg=fov_deg,
    )
    return _image_and_notes(png, ann)


@mcp.tool()
def render_contact_sheet(
    overlay: str = "shaded",
    width: int = 1200,
    height: int = 900,
    hide_invisible: bool = True,
    grid_spacing: float = 64.0,
) -> list:
    """Render three orthographic views plus a perspective view in one image (§4.2).

    This is the default way to look at geometry: one call, one image, and a shape that three
    orthographic views plus a perspective view make unambiguous. Panels clockwise from
    top-left are top (XY), front (XZ), side (YZ), perspective.
    """
    m = SESSION.require()
    png, ann = m.render(
        view="sheet",
        overlay=overlay,
        width=width,
        height=height,
        grid=SESSION.grid,
        grid_spacing=grid_spacing,
        hide_invisible=hide_invisible,
    )
    return _image_and_notes(png, ann)


@mcp.tool()
def render_player_eye(
    position: list[float],
    yaw_deg: float = 0.0,
    fov_deg: float = 90.0,
    width: int = 900,
    height: int = 700,
    eye_height: float | None = None,
) -> list:
    """Render the view a standing player would have from a floor position.

    Args:
        position: floor point to stand on, `[x, y, z]`.
        yaw_deg: facing, degrees counter-clockwise from +X.
        eye_height: height above `position`. Omit to read the verified standing height from
            the active game profile, which is where it belongs — the design document's assumed
            figure was wrong for this game by 13 units.

    Position `z` should be the floor, not the eye. If the result looks empty, the point is
    probably inside solid geometry or outside the map.
    """
    m = SESSION.require()
    if eye_height is None:
        pid = active_profile()
        eye_height = profiles.standing_height(pid) if pid else None
        if eye_height is None:
            raise ValueError(
                "no eye_height given and the active profile states no verified standing "
                "height. Pass eye_height explicitly, or check profile_summary(). Guessing a "
                "player height here would put the camera where a player cannot stand."
            )
        source = f"eye height {eye_height} from profile {pid} (verified)"
    else:
        source = f"eye height {eye_height} supplied by caller"

    png, ann = m.render(
        view="player_eye",
        overlay="shaded",
        width=width,
        height=height,
        grid=SESSION.grid,
        grid_spacing=0.0,
        eye=list(position),
        yaw_deg=yaw_deg,
        eye_height=eye_height,
        fov_deg=fov_deg,
        hide_invisible=True,
    )
    return _image_and_notes(png, ann, source)


# ---------------------------------------------------------------------------
# Analyze (profile-driven) and ship (§6.2, §6.4, §7.1)
# ---------------------------------------------------------------------------


@mcp.tool()
def validate_profile(profile_id: str | None = None, severity_min: str = "info") -> dict:
    """Validate the open map's entities against the active game profile.

    Complements `validate`, which covers game-agnostic geometry. This checks the game's own
    rules: spawn arrangement, objective counts, gametype keys.

    Every rule is data in the profile, and every finding carries a `confidence`. **An
    unverified rule can never produce an error** — it is downgraded to `info` — because three
    of the four spawn rules the design document called verified were wrong, and one would have
    failed correct maps. `nrc://corrections` has the details.
    """
    m = SESSION.require()
    pid = profile_id or active_profile()
    if not pid:
        return {
            "error": "no profile selected; set NRC_PROFILE or pass profile_id",
            "available": profiles.available(),
        }
    try:
        ents = m.entities()
        found = rules.evaluate(pid, ents) + rules.unknown_classnames(pid, ents)
    except profiles.ProfileError as e:
        return {"error": str(e), "available": profiles.available()}

    floor = rules.SEVERITIES.index(severity_min) if severity_min in rules.SEVERITIES else 0
    kept = [f for f in found if rules.SEVERITIES.index(f.severity) >= floor]
    order = {"error": 0, "warning": 1, "info": 2}
    kept.sort(key=lambda f: (order[f.severity], f.code))
    return {
        "profile": pid,
        "entities_checked": len(ents),
        "summary": rules.summarize(found),
        "findings": [f.as_dict() for f in kept],
    }


@mcp.tool()
def bsp_report(lumps_path: str, profile_id: str | None = None) -> dict:
    """Read a compiled BSP's structure from an unpacked `-json` lump directory (§6.2).

    Produce the directory with `task_run("bsp:json-unpack", [path_to_bsp])`.

    Reports surfaces per shader (which shader is eating draw calls), surface types, lightmap
    coverage, unreferenced shaders, and headroom. Headroom comes in two parts and the
    distinction matters: **compiler limits** are read from this fork's source and are real,
    but most of the classic Quake 3 ceilings were removed upstream, so there is nothing to
    report for them. **Engine limits** come from the profile and are what decide whether the
    map loads at all — a map can compile cleanly and still be rejected in game.
    """
    p = Path(lumps_path)
    if not p.is_absolute():
        p = repo_root() / p
    try:
        return bspmod.report(p, profile_id or active_profile() or None)
    except bspmod.BspError as e:
        return {"error": str(e)}


@mcp.tool()
def bsp_entity_diff(lumps_path: str, source_map: str | None = None) -> dict:
    """Compare a compiled BSP's entity lump against the source `.map` (§6.2).

    Entities can be dropped silently at compile time — the editor's writer discards empty
    group entities and q3map2 drops what it cannot place — so a count that quietly changed is
    a real bug this catches.
    """
    p = Path(lumps_path)
    if not p.is_absolute():
        p = repo_root() / p
    src = Path(source_map) if source_map else SESSION.path
    if src is None:
        return {"error": "no source map given and none is open"}
    try:
        return bspmod.entity_diff(p, src)
    except bspmod.BspError as e:
        return {"error": str(e)}


@mcp.tool()
def pack_pk3(bsp: str, complevel: int | None = None, png: bool = False) -> dict:
    """Build a release archive for a compiled BSP with `q3map2 -pk3` (§6.4).

    The packer's own naming is the oracle: a `_FAILEDpack.pk3` means the BSP references a
    resource that is not on disk, so the map would show missing content on a server even
    though it compiled. That is reported as a failure whatever the exit code.
    """
    try:
        return packmod.pack_pk3(bsp, complevel=complevel, png=png)
    except packmod.PackError as e:
        return {"error": str(e)}


@mcp.tool()
def repack_analyze(bsp: str) -> dict:
    """List every resource a compiled BSP references, via `q3map2 -repack -analyze` (§6.4).

    Parsed from the compiler's own dump rather than traced independently — the compiler's view
    of what a BSP needs is the one that decides whether the map works.
    """
    try:
        return packmod.repack_analyze(bsp)
    except packmod.PackError as e:
        return {"error": str(e)}


@mcp.tool()
def ship_check(
    target: str | None = None, profile_id: str | None = None, pk3: str | None = None
) -> dict:
    """Run the release checklist (§6.4): naming, levelshot, arena file, package contents.

    Conventions come from the profile's `packaging` section, and unverified ones produce
    `info` rather than failures — the levelshot size and arena key list are community practice
    rather than anything the gamepack documents.
    """
    t = target or (str(SESSION.path) if SESSION.path else None)
    if t is None:
        return {"error": "no target given and no map is open"}
    pid = profile_id or active_profile()
    if not pid:
        return {"error": "no profile selected", "available": profiles.available()}
    try:
        return packmod.ship_check(t, pid, pk3=pk3)
    except packmod.PackError as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Sculpt (§4)
# ---------------------------------------------------------------------------

_IR_HELP = """\
Solid IR: a tree of shape operators that always compiles to valid convex brushes.

Primitives — box, wedge, prism (a "cylinder"; the engine has no curves), cone, pyramid,
stair, pipe, arch. Operators — union, intersect, subtract, hollow, carve_opening, translate,
mirror, array.

    {"op": "box", "min": [0,0,0], "max": [512,512,256]}
    {"op": "hollow", "solid": {...}, "thickness": 16, "open_faces": [5]}
    {"op": "carve_opening", "wall": {...}, "min": [224,-8,0], "max": [288,24,112]}
    {"op": "subtract", "from": {...}, "cut": [{...}]}
    {"op": "union", "parts": [{...}, {...}]}
    {"op": "stair", "origin": [0,0,0], "width": 128, "steps": 8, "rise": 16, "run": 32,
     "along": "x", "up": "z"}
    {"op": "prism", "min": [...], "max": [...], "axis": "z", "sides": 8, "start_deg": 22.5}
    {"op": "arch", "centre": [...], "outer_radius": 128, "thickness": 32, "depth": 64,
     "segments": 8, "axis": "z"}
    {"op": "array", "node": {...}, "count": 4, "offset": [64,0,0]}

All coordinates are whole numbers. A room with a door is `hollow` then `carve_opening`; that
composition yields exactly the brushes a mapper would draw by hand — three for a doorway.

One honest limit: plane-defining points are always integers, but a brush's *vertices* are
wherever three planes meet, and for angled shapes (prisms, cones, arches) those miss the grid.
That is a property of the format, not of this tool — Radiant's cylinders are off-grid too. The
count comes back as `off_grid_vertices`, and `validate` flags them. Stick to boxes, wedges and
stairs if you need strictly on-grid geometry."""


@mcp.tool()
def solid_help() -> str:
    """The Solid IR reference: every operator, its fields, and the on-grid caveat.

    Read this before authoring geometry. It is short.
    """
    return _IR_HELP


@mcp.tool()
def solid_compile(ir: dict, textures: dict | None = None, grid: int | None = None) -> dict:
    """Compile a Solid IR tree and report what it would produce, touching nothing.

    Use this first. It returns brush and face counts, bounds, minimum thickness, off-grid
    vertex count and any warnings, so a shape can be checked before it is drawn or committed.

    Errors name the failing node by path (e.g. `subtract/cut[0]:box`), because a nested tree is
    otherwise very hard to debug from the outside. Call `solid_help` for the operator list.
    """
    k = _kernel()
    return k.solid_compile(ir, textures, grid if grid is not None else SESSION.grid)


@mcp.tool()
def solid_preview(
    ir: dict,
    textures: dict | None = None,
    grid: int | None = None,
    view: str = "sheet",
    width: int = 1100,
    height: int = 850,
) -> list:
    """Compile a Solid IR tree and render it **without committing** (§4.4).

    The sculpting loop: author IR, preview, adjust, commit. Nothing touches the open map, so
    this is free to call repeatedly.

    If a map is open the geometry is previewed in place against it, so a new piece can be seen
    in context rather than floating in isolation.
    """
    k = _kernel()
    g = grid if grid is not None else SESSION.grid
    # Preview against a *copy*, so an unwanted shape never has to be undone.
    base = SESSION.map.source() if SESSION.map is not None else '{\n"classname" "worldspawn"\n}\n'
    scratch = k.Map.parse(base)
    info = k.solid_commit(scratch, ir, textures, g, "worldspawn", False, "preview")
    png, ann = scratch.render(
        view=view,
        overlay="structural",
        width=width,
        height=height,
        grid=g,
        grid_spacing=64.0,
        hide_invisible=False,
    )
    extra = (
        f"preview only — nothing was committed. {info['brushes_created']} brush(es), "
        f"{info['faces']} faces"
    )
    if info["warnings"]:
        extra += "\nwarnings: " + "; ".join(info["warnings"][:4])
    return _image_and_notes(png, ann, extra)


@mcp.tool()
def solid_commit(
    ir: dict,
    label: str,
    textures: dict | None = None,
    grid: int | None = None,
    target_classname: str = "worldspawn",
    remember: bool = True,
) -> dict:
    """Compile a Solid IR tree and add the brushes to the open map (§4.4).

    Nothing is written to disk: call `map_save` for that. The brushes are tagged with a comment
    naming `label`, so a human reading the `.map` can see where they came from and delete them
    as a unit.

    With `remember`, the IR is recorded in a `<map>.solids.json` sidecar under `label`, so the
    shape can be re-parameterized later with `solid_edit_param` — "make that corridor 32 units
    wider" instead of moving faces by hand. The `.map` stays canonical; the sidecar is
    advisory and re-derivable.
    """
    m = SESSION.require()
    k = _kernel()
    g = grid if grid is not None else SESSION.grid
    result = k.solid_commit(m, ir, textures, g, target_classname, False, label)

    if remember and SESSION.path is not None:
        try:
            solids.put(SESSION.path, label, ir, brushes=result["brushes_created"])
            result["sidecar"] = str(solids.sidecar_path(SESSION.path))
        except solids.SolidStoreError as e:
            # The brushes are already in the map; failing to record the IR is a lesser problem
            # and must not be reported as though the commit failed.
            result["sidecar_error"] = str(e)
    result["next"] = "call map_save to write the map, or render_contact_sheet to look at it"
    return result


@mcp.tool()
def solid_inspect(name: str | None = None, ir: dict | None = None) -> dict:
    """Show a Solid IR tree's structure — either a recorded one by `name`, or one passed in.

    The outline names every operator and its parameters with the paths `solid_edit_param`
    accepts, which is how you find out what there is to change.
    """
    if ir is None:
        if name is None:
            return {"error": "pass either a recorded name or an ir tree"}
        if SESSION.path is None:
            return {"error": "no map is open, so there is no sidecar to read"}
        try:
            entry = solids.get(SESSION.path, name)
        except solids.SolidStoreError as e:
            return {"error": str(e)}
        ir = entry["ir"]
        meta = {k: v for k, v in entry.items() if k != "ir"}
    else:
        meta = {}
    return {"name": name, "outline": solids.describe(ir), "ir": ir, "recorded": meta}


@mcp.tool()
def solid_list() -> dict:
    """Solid IR trees recorded for the open map, with their brush counts."""
    if SESSION.path is None:
        return {"error": "no map is open"}
    try:
        store = solids.load(SESSION.path)
    except solids.SolidStoreError as e:
        return {"error": str(e)}
    return {
        "sidecar": str(solids.sidecar_path(SESSION.path)),
        "solids": {
            n: {k: v for k, v in e.items() if k not in ("ir", "superseded")}
            for n, e in sorted(store["solids"].items())
        },
    }


@mcp.tool()
def solid_edit_param(
    name: str,
    path: str,
    value: Any,
    preview_only: bool = True,
) -> dict:
    """Change one parameter of a recorded solid and recompile it (§4.4).

    This is the point of keeping the IR: `solid_edit_param("corridor", "max[1]", 192)` widens a
    corridor, where editing brushes by hand would mean moving several faces consistently.

    `path` is dotted with bracket indices — `from.solid.max[0]`, `cut[0].min`. Run
    `solid_inspect` to see what paths exist.

    Defaults to `preview_only`: it reports what the change would produce without touching the
    map. The old brushes are **not** removed automatically — that would mean guessing which
    brushes came from this solid, and guessing wrong would delete a mapper's work. Delete them
    yourself, then commit the edited IR.
    """
    if SESSION.path is None:
        return {"error": "no map is open, so there is no sidecar to read"}
    try:
        entry = solids.get(SESSION.path, name)
        edited = solids.edit_param(entry["ir"], path, value)
    except solids.SolidStoreError as e:
        return {"error": str(e)}

    k = _kernel()
    try:
        before = k.solid_compile(entry["ir"], None, SESSION.grid)
        after = k.solid_compile(edited, None, SESSION.grid)
    except ValueError as e:
        return {
            "error": f"the edited tree does not compile: {e}",
            "hint": "the parameter was accepted but produces invalid geometry; try another value",
            "ir": edited,
        }

    out = {
        "name": name,
        "path": path,
        "value": value,
        "before": {kk: before[kk] for kk in ("brushes", "faces", "bounds", "volume")},
        "after": {kk: after[kk] for kk in ("brushes", "faces", "bounds", "volume")},
        "ir": edited,
        "preview_only": preview_only,
    }
    if not preview_only:
        solids.put(SESSION.path, name, edited, brushes=after["brushes"], notes=f"edited {path}")
        out["recorded"] = True
        out["next"] = (
            "the sidecar now holds the edited tree; delete the old brushes, then call "
            "solid_commit to draw the new ones"
        )
    return out


# ---------------------------------------------------------------------------
# Assets — the Blender handoff (§5)
# ---------------------------------------------------------------------------


@mcp.tool()
def asset_plan(
    blocks_movement: bool,
    blocks_visibility: bool,
    is_axis_aligned: bool,
    is_curved: bool,
    needs_clean_collision: bool = False,
    is_organic: bool = False,
) -> dict:
    """Decide whether a feature should be a brush, a patch or a mesh (§4.3, §5.5).

    The decision rule, in order: *does it block movement, block vis, or need cheap clean
    collision?* → brush. *Is it axis-aligned architecture?* → brush. *Is it curved but simple?*
    → patch. *Everything else* → Blender.

    That ordering is not arbitrary. Brushes are the only representation that seals a map and
    blocks visibility, patches are never structural, and meshes are always non-structural — so
    anything load-bearing has to be a brush regardless of how it looks.
    """
    if blocks_visibility:
        return _tier(
            1,
            "brush, structural, caulked",
            "it blocks visibility, and only a structural brush can do that",
            grid=16,
        )
    if blocks_movement or needs_clean_collision:
        return _tier(
            2,
            "brush, detail",
            "it blocks movement or needs cheap predictable collision, which a brush "
            "gives and a mesh does not",
            grid=8,
        )
    if is_axis_aligned and not is_curved:
        return _tier(
            2, "brush, detail", "axis-aligned architecture is cheaper and tidier as a brush", grid=8
        )
    if is_curved and not is_organic:
        return _tier(3, "patch", "curved but simple, so a patch — and a patch is never structural")
    # The model entity's classname is game-specific, so it comes from the profile. The seam lint
    # caught this line naming it directly, which is exactly the drift §7.4 predicts.
    entity = "a model entity"
    pid = active_profile()
    if pid:
        with contextlib.suppress(profiles.ProfileError):
            entity = str((profiles.load(pid).get("assets") or {}).get("model_entity") or entity)
    return _tier(
        4,
        f"mesh, placed with {entity}",
        "ornament, clutter or organic geometry, which is what Blender is for; always "
        "non-structural, so pair it with an explicit collision decision",
    )


def _tier(tier: int, representation: str, why: str, grid: int | None = None) -> dict:
    out = {
        "tier": tier,
        "representation": representation,
        "why": why,
        "next": {
            1: "author it with solid_commit; texture hidden faces with the caulk shader",
            2: "author it with solid_commit and set detail=true in textures",
            3: "patches are not yet authorable by this tool — build it in the editor for now",
            4: "call blender_brief, send the prompt to Blender, then model_import",
        }[tier],
    }
    if grid:
        out["minimum_grid"] = grid
    return out


@mcp.tool()
def blender_brief(
    asset_id: str,
    purpose: str,
    bounds: dict,
    collision: str = "brush_hull",
    materials: list[dict] | None = None,
    triangles: int | None = None,
    silhouette_notes: str = "",
    profile_id: str | None = None,
) -> dict:
    """Emit a numerically complete asset brief plus a ready-to-send Blender prompt (§5.2).

    `bounds` is `{"x": [lo, hi], "y": [...], "z": [...]}` in world units — normally the brush
    volume the asset replaces.

    The point is that the returned `prompt` leaves nothing to infer: dimensions, origin, budget,
    material names, UV density, export axes and the collision decision are all decided here. A
    brief that says "make it crate-sized" gets a crate of the wrong size.

    The unit scale comes from the profile, not from this code — §7.4 names that constant
    specifically as a place game specifics leak in.
    """
    pid = profile_id or active_profile()
    if not pid:
        return {"error": "no profile selected", "available": profiles.available()}
    try:
        return blendermod.blender_brief(
            pid,
            asset_id,
            purpose,
            bounds,
            collision=collision,
            materials=materials,
            triangles=triangles,
            silhouette_notes=silhouette_notes,
        )
    except (blendermod.AssetError, profiles.ProfileError) as e:
        return {"error": str(e)}


@mcp.tool()
def model_import(path: str, brief: dict, profile_id: str | None = None) -> dict:
    """Validate an exported mesh against the brief that asked for it (§5.3).

    Checks scale, fit, origin, triangle budget, material names, UVs and structure. The scale
    check comes first and names the likely cause: a mesh 1/39.37 of the requested size is the
    metres-exported-as-units mistake, and saying so is far more useful than reporting the raw
    numbers.
    """
    p = Path(path)
    if not p.is_absolute():
        p = repo_root() / p
    try:
        return blendermod.model_import(p, brief, profile_id or active_profile() or None)
    except blendermod.AssetError as e:
        return {"error": str(e)}


@mcp.tool()
def model_place(
    model_path: str,
    origin: list[float],
    angles: list[float] | None = None,
    scale: float | None = None,
    lightmap_scale: float | None = None,
    remap: dict | None = None,
    profile_id: str | None = None,
    commit: bool = False,
) -> dict:
    """Build the entity that places a model in the world (§5.3).

    Defaults to returning the key/value pairs without touching the map, so the placement can be
    checked first. Pass `commit=True` to add the entity to the open map.

    Every key name comes from the profile: the model entity's classname, the scale keys, the
    remap prefix. Negative scale is supported by this compiler and is the cheapest way to mirror
    a prop.
    """
    pid = profile_id or active_profile()
    if not pid:
        return {"error": "no profile selected", "available": profiles.available()}
    try:
        keys = blendermod.model_place_keys(
            pid,
            model_path,
            origin,
            angles=angles,
            scale=scale,
            lightmap_scale=lightmap_scale,
            remap=remap,
        )
    except (blendermod.AssetError, profiles.ProfileError) as e:
        return {"error": str(e)}

    out = {"keys": keys, "committed": False}
    if commit:
        m = SESSION.require()
        src = m.source()
        entity = "{\n" + "".join(f'"{k}" "{v}"\n' for k, v in keys) + "}\n"
        k = _kernel()
        merged = k.Map.parse(src + entity)
        SESSION.map = merged
        out["committed"] = True
        out["entities_now"] = merged.entity_count
        out["next"] = "call map_save to write it, and model_make_clip if it needs collision"
    return out


@mcp.tool()
def model_make_clip(
    path: str,
    origin: list[float] | None = None,
    scale: float = 1.0,
    k: int = 14,
    grid: int = 1,
    kind: str = "player",
    profile_id: str | None = None,
) -> dict:
    """Fit a convex collision hull to a mesh, returned as Solid IR (§5.4).

    The hull is a k-DOP: the tightest intersection of half-spaces with `k` fixed normals that
    contains every vertex, pushed outward to the grid so it never cuts into the visual. That is a
    deliberate choice, not a shortcut — §5.4 argues that for a competitive shooter snappy,
    predictable collision beats accurate collision, and a 14-plane hull is something a player can
    read while sliding along it.

    `kind` selects the clip shader from the profile: `player` for movement only, `weapon` to stop
    bullets where the visual has gaps, `both` for solid.

    Returns IR rather than brushes, so preview it with `solid_preview` and look at it against the
    model before committing. A hull noticeably larger than the visual is what players report as
    an invisible wall.
    """
    pid = profile_id or active_profile()
    if not pid:
        return {"error": "no profile selected", "available": profiles.available()}
    p = Path(path)
    if not p.is_absolute():
        p = repo_root() / p
    try:
        return blendermod.model_make_clip(
            p, pid, origin=origin, scale=scale, k=k, grid=grid, kind=kind
        )
    except (blendermod.AssetError, profiles.ProfileError) as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Optimize (§6.1, §6.3)
# ---------------------------------------------------------------------------


@mcp.tool()
def structural_audit(grid: int | None = None, limit: int = 50) -> dict:
    """Find brushes marked structural that need not be (§6.1) — the biggest lever on vis cost.

    A structural brush blocks visibility and costs portals; a detail brush does not. Anything not
    sealing the map or acting as a major visual blocker should be detail, and converting it is
    usually the cheapest large win available on a Q3-engine map.

    The estimated benefit per brush is a **heuristic, not a measurement**. The real number needs a
    `-vis` compile before and after, which `compile_ab` can do. Treat the list as candidates to
    look at, not as a to-do list to apply blindly.
    """
    if SESSION.path is None:
        return {"error": "no map is open"}
    try:
        return optmod.structural_audit(
            SESSION.path, grid if grid is not None else SESSION.grid, limit=limit
        )
    except (OSError, ValueError) as e:
        return {"error": str(e)}


@mcp.tool()
def hint_suggest(prt_path: str, limit: int = 10) -> dict:
    """Propose hint brush planes from a compiled portal file (§6.1).

    Needs a `.prt`, which `-vis -saveprt` writes: compile with the `final` preset, or run
    `task_run("compile:final", [map])`.

    §6.1 calls this "tedious, high-skill, high-payoff work that almost nobody does properly by
    hand". Each suggestion names the leaf, its portal count and a proposed splitting plane, with a
    predicted before/after — predicted, because confirming it needs another `-vis` run.
    """
    p = Path(prt_path)
    if not p.is_absolute():
        p = repo_root() / p
    try:
        return optmod.hint_suggest(p, limit=limit)
    except (OSError, ValueError) as e:
        return {"error": str(e)}


@mcp.tool()
def leak_trace(lin_path: str | None = None) -> dict:
    """Read a leak pointfile and report the path out of the map (§6.1).

    q3map2 writes a `.lin` beside the map when the world is not sealed. The path runs from the
    entity that leaked to the void, so the first point is inside the map and the last is outside;
    the gap is somewhere along it.

    Defaults to the `.lin` beside the open map.
    """
    if lin_path:
        p = Path(lin_path)
        if not p.is_absolute():
            p = repo_root() / p
    elif SESSION.path is not None:
        p = SESSION.path.with_suffix(".lin")
    else:
        return {"error": "no pointfile given and no map is open"}
    if not p.is_file():
        return {
            "error": f"{p} does not exist",
            "hint": "a pointfile only exists when the last compile leaked; a sealed map has none",
        }
    try:
        return optmod.leak_trace(p, SESSION.path)
    except (OSError, ValueError) as e:
        return {"error": str(e)}


@mcp.tool()
def shader_audit(shader_dirs: list[str] | None = None, profile_id: str | None = None) -> dict:
    """Audit shader references against the shader scripts on disk (§6.3).

    Reports shaders the map references but nothing defines, shaders defined but never used,
    shaders shadowing a base-game path — a classic cause of "works for me, broken on the server" —
    and the watercaulk trap, where a visible surface's shader draws nothing.

    `shader_dirs` defaults to the game's script directory from the environment.
    """
    if SESSION.path is None:
        return {"error": "no map is open"}
    dirs: list[str] = list(shader_dirs or [])
    if not dirs:
        base = os.environ.get("NRC_FS_BASEPATH") or os.environ.get("URT_BASEPATH")
        game = os.environ.get("NRC_FS_GAME")
        if base and game:
            dirs = [str(Path(base) / game / "scripts")]
    if not dirs:
        return {
            "error": "no shader directories given and none could be inferred",
            "hint": "pass shader_dirs, or set the game path in mise.local.toml",
        }
    try:
        return optmod.shader_audit(SESSION.path, dirs, profile_id or active_profile() or None)
    except (OSError, ValueError) as e:
        return {"error": str(e)}


@mcp.tool()
def compile_ab(map_a: str, map_b: str, preset: str = "draft") -> dict:
    """Compile two variants and diff the numbers that matter (§6.1).

    Reports per-stage wall time, draw surface count, leaf count, brush count and BSP size for each,
    and appends a row to `bench/ab-history.jsonl` so regressions stay visible over the life of a
    project rather than being rediscovered.

    This is how a structural_audit suggestion gets *confirmed* rather than assumed: apply it to a
    copy, compile both, and read the portal delta.
    """
    a, b = Path(map_a), Path(map_b)
    if not a.is_absolute():
        a = repo_root() / a
    if not b.is_absolute():
        b = repo_root() / b
    try:
        return optmod.compile_ab(a, b, preset=preset)
    except (OSError, ValueError) as e:
        return {"error": str(e)}


@mcp.tool()
def ab_history() -> dict:
    """Every recorded A/B comparison, oldest first (§6.1)."""
    try:
        return {"rows": optmod.ab_history()}
    except (OSError, ValueError) as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Analyze gameplay (§7.3)
# ---------------------------------------------------------------------------


def _profile_or_error(profile_id: str | None) -> tuple[str, dict | None]:
    pid = profile_id or active_profile()
    if not pid:
        return "", {
            "error": "no profile selected; the movement constants live there",
            "available": profiles.available(),
        }
    return pid, None


@mcp.tool()
def navgrid_stats(cell: float = 16, profile_id: str | None = None) -> dict:
    """Build the walkable grid and report its size and coverage (§7.3).

    A cell is walkable when it is empty, the cell below is solid, and there is at least the
    profile's standing height of clear space above it — so the grid is the space a player could
    actually occupy, not merely the empty space.

    This is a **voxel approximation, not compiled AAS**. It is good enough for distances and
    reachability and will disagree with the engine at the margins. Everything else here is built on
    it, so run this first to see whether the map is tractable at the cell size you want.
    """
    if SESSION.path is None:
        return {"error": "no map is open"}
    pid, err = _profile_or_error(profile_id)
    if err:
        return err
    try:
        g = anamod.build_navgrid(SESSION.path, cell=cell, profile_id=pid)
        return (
            anamod.navgrid_summary(g)
            if hasattr(anamod, "navgrid_summary")
            else {
                "cell": cell,
                "walkable_cells": len(getattr(g, "walkable", []) or []),
                "note": "grid built; see balance_report or sightline_report to use it",
            }
        )
    except (OSError, ValueError, MemoryError) as e:
        return {"error": str(e)}


@mcp.tool()
def balance_report(cell: float = 16, profile_id: str | None = None) -> dict:
    """Per-team traversal distance from each spawn group to each objective (§7.3).

    Reports path lengths over the walkable grid, the asymmetry between teams, and whether the map
    is mirror-symmetric about its centre. Which classnames count as spawns and objectives comes
    from the profile, so this works for any game with a profile.

    Distances are walked, not straight-line — a 500-unit gap with a wall in it is not 500 units.
    """
    if SESSION.path is None:
        return {"error": "no map is open"}
    pid, err = _profile_or_error(profile_id)
    if err:
        return err
    try:
        return anamod.balance_report(SESSION.path, pid, cell=cell)
    except (OSError, ValueError, MemoryError) as e:
        return {"error": str(e)}


@mcp.tool()
def sightline_report(samples: int = 200, cell: float = 16, profile_id: str | None = None) -> dict:
    """Sightline length distribution and power positions (§7.3).

    Samples walkable positions, casts rays between them at the profile's eye height, and reports how
    far a player can see. Long uncontested lanes are the finding that matters in a sniper-sensitive
    game; "power positions" are the points that see the most of the map.

    Sampled, so the numbers move a little between runs. Read the distribution, not any single value.
    """
    if SESSION.path is None:
        return {"error": "no map is open"}
    pid, err = _profile_or_error(profile_id)
    if err:
        return err
    try:
        return anamod.sightline_report(SESSION.path, samples=samples, profile_id=pid, cell=cell)
    except (OSError, ValueError, MemoryError) as e:
        return {"error": str(e)}


@mcp.tool()
def movement_check(cell: float = 16, profile_id: str | None = None) -> dict:
    """Check clearances against the profile's movement constants (§7.3).

    Standing headroom, crouch headroom, step height, doorway widths. Every finding names the
    constant it used and that constant's confidence, and **anything derived from an unverified
    constant is reported as info, never an error**.

    That clamp is not ceremony. The design document's own standing height was wrong by 13 units, and
    a corridor sized from it would pass every geometric check while being unusable.
    """
    if SESSION.path is None:
        return {"error": "no map is open"}
    pid, err = _profile_or_error(profile_id)
    if err:
        return err
    try:
        return anamod.movement_check(SESSION.path, pid, cell=cell)
    except (OSError, ValueError, MemoryError) as e:
        return {"error": str(e)}


@mcp.tool()
def spawn_safety(cell: float = 16, profile_id: str | None = None) -> dict:
    """Exits per spawn and distance to the nearest enemy spawn (§7.3).

    A spawn with one exit is a spawn that can be held; a spawn close to an enemy spawn is a spawn
    that gets contested immediately. Both are design smells worth seeing before playtesting finds
    them.
    """
    if SESSION.path is None:
        return {"error": "no map is open"}
    pid, err = _profile_or_error(profile_id)
    if err:
        return err
    try:
        return anamod.spawn_safety(SESSION.path, pid, cell=cell)
    except (OSError, ValueError, MemoryError) as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Project / meta
# ---------------------------------------------------------------------------


@mcp.tool()
def bench_run() -> dict:
    """Run the fitness suite and return the scores (§11.1).

    F1 (kernel correctness) is a **gate**, not a score: a run with it red has no score to compare,
    whatever the other signals say. F4 skips without a compiler and F5 is a declarative proxy for
    the natural-language-brief version; both say so in their output rather than reporting a number
    they cannot justify.

    Also reports whether the protected paths are unchanged. If they are not, treat every score as
    meaningless — §11.4 names editing the ruler as the most likely failure mode of any
    optimization loop.
    """
    r = run_mise_task("bench")
    root = repo_root()
    results = sorted((root / "bench" / "results").glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not results:
        return {"error": "bench produced no result file", "output": r["stdout"][-1500:]}
    import json as _json

    data = _json.loads(results[-1].read_text())
    return {
        "sha": data["sha"],
        "dirty": data["dirty"],
        "gate_passed": data["gate_passed"],
        "protected_paths_ok": data["protected_paths_ok"],
        "protected_problems": data["protected_problems"],
        "signals": [{k: v for k, v in s.items() if k not in ("cases",)} for s in data["signals"]],
        "result_file": str(results[-1].relative_to(root)),
    }


@mcp.tool()
def upstream_diff(fetch: bool = False) -> dict:
    """Report upstream drift that would break the editor bridge (§10.1).

    Hashes the individual declarations in `include/` the plugin binds to, rather than whole files,
    so it fires when something the plugin calls has actually moved instead of on every comment
    change. Also reports new or removed compiler flags, because each new flag is a potential
    optimizer capability (§6) — that feed pays for itself even if the pull request never happens.
    """
    args = ["--fetch"] if fetch else []
    r = run_mise_task("pr:watch" if fetch else "pr:baseline", args)
    return {
        "ok": r["ok"],
        "output": r["stdout"] or r["stderr"],
        "note": (
            "a breaking change deliberately does not advance the baseline, so it keeps being "
            "reported until it is dealt with"
        ),
    }


@mcp.tool()
def pr_plan_status() -> dict:
    """Regenerate and summarize the upstream contribution plan (§10.2).

    Every number is measured, not asserted. The three criteria that cannot be met from here — never
    compiled, no usage telemetry, no maintainer asked — are reported as unmet rather than glossed,
    and the telemetry one matters most: §10.1's rule is that any RPC method with zero real-session
    usage is cut before submission, so inventing those numbers would defeat the feed's purpose.
    """
    r = run_mise_task("pr:report")
    plan = repo_root() / "docs" / "pr-plan.md"
    return {
        "ok": r["ok"],
        "plan": str(plan),
        "summary": r["stdout"].strip(),
        "content": plan.read_text() if plan.is_file() else None,
    }


@mcp.tool()
def selfdev_protected() -> dict:
    """List the paths self-modification may never touch, and verify their hash pins (§11.4).

    Read-only, and available whether or not self-dev is enabled: knowing what is frozen is useful
    on its own. §11.4 calls this mechanism "the only thing standing between self-improving and
    self-congratulating".
    """
    r = run_mise_task("selfdev:status")
    return {"ok": r["ok"], "status": r["stdout"] or r["stderr"]}


@mcp.tool()
def task_list() -> dict:
    """Everything this project can do, discovered from mise.

    This is the action surface (§1.2). Prefer a task over improvising a shell command: the
    task is reproducible by a human and recorded as a name plus arguments. Tasks flagged
    `mutates_user_data` need explicit intent before running.
    """
    return {"tasks": tasks.list_tasks()}


@mcp.tool()
def task_run(name: str, args: list[str] | None = None, acknowledge_mutation: bool = False) -> dict:
    """Run a mise task by name.

    Args:
        name: exact task name from `task_list`.
        args: arguments passed after `--`.
        acknowledge_mutation: required for tasks that mutate user data.
    """
    known = tasks.task_names()
    if name not in known:
        close = sorted(n for n in known if name.split(":")[0] in n)[:8]
        return {
            "ok": False,
            "error": f"no task named {name!r}",
            "did_you_mean": close,
            "hint": "call task_list() for the full surface",
        }
    info = next(t for t in tasks.list_tasks() if t["name"] == name)
    if info["mutates_user_data"] and not acknowledge_mutation:
        return {
            "ok": False,
            "error": f"{name} mutates user data; re-call with acknowledge_mutation=true",
            "description": info["description"],
        }
    return run_mise_task(name, args)


@mcp.tool()
def compile_map(preset: str = "draft", path: str | None = None) -> dict:
    """Compile a map with q3map2.

    Presets (§6.1): `draft` geometry-only seconds-long check, `iterate` playable test build,
    `quality` review build, `final` release build.

    On a WSL host where q3map2 is a Windows binary, the wrapper stages the map onto the
    Windows filesystem and copies artefacts back to `out/` — paths are handled for you.
    """
    if preset not in ("draft", "iterate", "quality", "final"):
        return {"ok": False, "error": f"unknown preset {preset!r}"}
    target = path
    if target is None:
        if SESSION.path is None:
            return {"ok": False, "error": "no map open and no path given"}
        target = str(SESSION.path)
    return run_mise_task(f"compile:{preset}", [target])


@mcp.tool()
def profile_summary(profile_id: str | None = None) -> dict:
    """What the active game profile knows, and how much of it is verified.

    The profile is the only game-specific layer (§7.4). Anything marked `unverified` came
    from documentation rather than a shipped gamepack and must not be treated as a hard
    rule.
    """
    pid = profile_id or active_profile()
    if not pid:
        return {
            "error": "no profile selected; set NRC_PROFILE or pass profile_id",
            "available": profiles.available(),
        }
    try:
        return profiles.summary(pid)
    except profiles.ProfileError as e:
        return {"error": str(e), "available": profiles.available()}


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource("nrc://tasks")
def resource_tasks() -> str:
    """The live mise task list — what this project can do."""
    return tasks.describe_tasks()


@mcp.resource("nrc://profile/{profile_id}")
def resource_profile(profile_id: str) -> str:
    """A game profile as YAML.

    Note the scheme is `nrc://`, not a per-game one: a game-specific URI scheme in code
    would itself be a seam violation (§7.4). The game is identified by the profile id.
    """
    path = profiles.profiles_dir() / f"{profile_id}.yaml"
    if not path.is_file():
        return f"no profile {profile_id!r}; available: {', '.join(profiles.available()) or 'none'}"
    return path.read_text()


@mcp.resource("nrc://conventions")
def resource_conventions() -> str:
    """The design tier rules and authoring guidance (§4.3).

    Read this before authoring geometry. It says which representation to use and why, what to caulk,
    the authoring order, the composition patterns that work — and what is not built yet, which saves
    more time than it costs.
    """
    p = repo_root() / "docs" / "conventions.md"
    return p.read_text() if p.is_file() else "docs/conventions.md is missing"


@mcp.resource("nrc://corrections")
def resource_corrections() -> str:
    """Design claims that turned out to be wrong when verified against real sources.

    Read this before trusting a rule from the specification. It records which claims were
    checked, which failed, and what the evidence was.
    """
    p = repo_root() / "docs" / "spec-corrections.md"
    return p.read_text() if p.is_file() else "docs/spec-corrections.md is missing"


@mcp.resource("map://current/summary")
def resource_current_map() -> str:
    """A summary of the open map, or an explanation of why there isn't one."""
    if SESSION.map is None:
        return "No map is open. Call map_open(path)."
    s = SESSION.map.stats(grid=SESSION.grid)
    b = s["bounds"]
    lines = [
        f"path: {SESSION.path}",
        f"entities: {s['entities']}   brushes: {s['brushes']}   patches: {s['patches']}",
        f"faces: {s['faces']}   structural: {s['structural_brushes']}   detail: {s['detail_brushes']}",
        f"texdef formats: {', '.join(s['texdef_kinds']) or 'none'}",
        f"grid {s['grid']}: {s['vertices_on_grid']}/{s['vertices_total']} vertices aligned",
    ]
    if b:
        lines.append(f"bounds: {b['min']} .. {b['max']}  (size {b['size']})")
    if s["unevaluated_brushes"]:
        lines.append(f"NOTE: {s['unevaluated_brushes']} brush(es) could not be evaluated exactly")
    for w in SESSION.warnings:
        lines.append(f"WARNING: {w}")
    top = ", ".join(f"{t['shader']} ({t['faces']})" for t in s["top_shaders"][:8])
    lines.append(f"most used shaders: {top}")
    return "\n".join(lines)


#: Every tool this server exposes, in listing order.
#:
#: Kept as an explicit list so the inventory reads in a sensible order rather than
#: alphabetically. `test_tool_names_match_the_decorated_tools` fails if it drifts from the
#: `@mcp.tool()` decorators, so it cannot silently fall out of date.
TOOL_NAMES = (
    "map_open",
    "map_stats",
    "map_save",
    "query_entities",
    "brush_geometry",
    "validate",
    "render_topdown",
    "render_camera",
    "render_contact_sheet",
    "render_player_eye",
    "validate_profile",
    "bsp_report",
    "bsp_entity_diff",
    "pack_pk3",
    "repack_analyze",
    "ship_check",
    "solid_help",
    "solid_compile",
    "solid_preview",
    "solid_commit",
    "solid_inspect",
    "solid_list",
    "solid_edit_param",
    "asset_plan",
    "blender_brief",
    "model_import",
    "model_place",
    "model_make_clip",
    "structural_audit",
    "hint_suggest",
    "leak_trace",
    "shader_audit",
    "compile_ab",
    "ab_history",
    "navgrid_stats",
    "balance_report",
    "sightline_report",
    "movement_check",
    "spawn_safety",
    "bench_run",
    "upstream_diff",
    "pr_plan_status",
    "selfdev_protected",
    "task_list",
    "task_run",
    "compile_map",
    "profile_summary",
)


def describe_surface() -> str:
    """Human-readable inventory, for `mise run mcp:tools`."""
    lines = ["nrc-mcp surface", "", "TOOLS"]
    for name in TOOL_NAMES:
        fn = globals().get(name)
        doc = (fn.__doc__ or "").strip().splitlines()[0] if fn else "(missing)"
        lines.append(f"  {name:<22} {doc}")
    lines += [
        "",
        "RESOURCES",
        "  nrc://tasks                the live mise task list",
        "  nrc://profile/{id}         a game profile as YAML",
        "  nrc://conventions          design tiers, caulk, authoring order, what is not built",
        "  nrc://corrections          verified corrections to the design document",
        "  map://current/summary      the open map",
        "",
        f"active profile: {active_profile() or '(none)'}",
        f"profiles available: {', '.join(profiles.available()) or 'none'}",
        "",
        "NOT BUILT: patch authoring (§4 tier 3), the dimension corpus (§4.3), cover_report (§7.3).",
        "",
        "The editor bridge (§9) exists at contrib/mcpbridge but has never been compiled, so it is",
        "not reachable from here. Kernel self-modification (§11) is gated behind human review",
        "indefinitely and deliberately not automated; the prompt-layer loop is opt-in via",
        "NRC_SELFDEV=1.",
    ]
    try:
        lines.append(f"mise tasks discovered: {len(tasks.list_tasks())}")
    except RuntimeError as e:
        lines.append(f"mise task discovery failed: {e}")
    return "\n".join(lines)


def main() -> int:
    try:
        from .kernel import kernel

        kernel()
    except KernelUnavailable as e:
        print(f"nrc-mcp: {e}")
        return 2
    mcp.run()
    return 0
