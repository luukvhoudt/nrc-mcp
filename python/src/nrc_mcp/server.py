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

from . import bsp as bspmod
from . import pack as packmod
from . import profiles, rules, tasks
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
# Project / meta
# ---------------------------------------------------------------------------


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
        "  nrc://corrections          verified corrections to the design document",
        "  map://current/summary      the open map",
        "",
        f"active profile: {active_profile() or '(none)'}",
        f"profiles available: {', '.join(profiles.available()) or 'none'}",
        "",
        "NOT YET IMPLEMENTED (spec sections): §4 sculpting/Solid IR, §5 Blender handoff,",
        "§6 optimization suite, §7.3 UrT analysis, §9 editor bridge, §11 self-optimization.",
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
