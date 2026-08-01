"""The MCP surface (§8).

Phase 2 scope: read-only analysis, the mise task surface, and the compile driver. Sculpting
(§4), the Blender handoff (§5) and the optimization suite (§6) are not here yet, and the
tool list deliberately does not pretend otherwise — a tool that exists but does nothing is
worse than one that is absent, because the agent will plan around it.

Two conventions worth knowing:

**One open map.** Tools operate on a session map opened with `map_open`, rather than taking
a path every time. That keeps a sequence of queries consistent with each other, and makes
"the map I am editing" a single explicit thing.

**Nothing writes without being asked.** `map_save` is the only tool that touches a `.map`,
and it verifies the round-trip first: if the kernel cannot reproduce the file it loaded, it
refuses to write, because a tool that cannot reproduce your file has no business replacing
it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    # Current SDK layout.
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - older SDKs
    # The class was called FastMCP and lived elsewhere before. The decorator surface we
    # use (`.tool()`, `.resource()`, `.run()`) is the same, so supporting both costs one
    # import and avoids pinning users to one SDK generation.
    from mcp.server.fastmcp import FastMCP as _Server

from . import profiles, tasks
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
    mirrored planes, off-grid vertices, thin brushes, patch problems. Game-specific rules
    (spawns, gametypes, objectives) come from the profile and are not wired into this tool
    yet; `profile_summary` shows what the profile knows.
    """
    m = SESSION.require()
    return m.validate(
        grid=grid if grid is not None else SESSION.grid,
        severity_min=severity_min,
    )


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


def describe_surface() -> str:
    """Human-readable inventory, for `mise run mcp:tools`."""
    lines = ["nrc-mcp surface", "", "TOOLS"]
    for name in sorted(
        [
            "map_open",
            "map_stats",
            "map_save",
            "query_entities",
            "brush_geometry",
            "validate",
            "task_list",
            "task_run",
            "compile_map",
            "profile_summary",
        ]
    ):
        fn = globals().get(name)
        doc = (fn.__doc__ or "").strip().splitlines()[0] if fn else ""
        lines.append(f"  {name:<18} {doc}")
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
        "NOT YET IMPLEMENTED (spec sections): §4 sculpting/Solid IR, §4.2 rendering,",
        "§5 Blender handoff, §6 optimization suite, §7.3 UrT analysis, §9 editor bridge.",
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
