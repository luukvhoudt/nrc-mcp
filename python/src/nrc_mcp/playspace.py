"""Playable space: what an edit did to the part of the map a player can stand in.

The kernel already refuses to write a file it cannot reproduce byte-for-byte. That gate
protects the *file*. Nothing protected the *map*: an edit could delete half the walkable
floor and every check would stay green, because no check compared the map before the edit
to the map after it.

This module is that comparison. It voxelizes both states with the same machinery
`navgrid_stats` uses, aligns them in world coordinates, and reports what moved between
walkable and not.

Two findings are errors, because neither has a legitimate form:

``PLAYSPACE_INTERIOR_SEALED_BY_CLIP``
    A cell that was walkable *and under a roof* is now inside a player-clip volume. Roof
    detection written as an upward probe produces exactly this when the probe's step is
    larger than a ceiling slab is thick: the probe passes through the ceiling, reports open
    sky, and the "roof" that gets sealed is somebody's shopping mall. Clip is what makes it
    an error rather than an edit — the room still looks walkable, so the player only finds
    out by walking into it. Sealing an *outdoor* surface is a real intention and is not this
    finding; see `_is_indoors` for how the two are told apart, and why "has something above
    it" is not the test.

``PLAYSPACE_CLIP_SURFACE_WALKABLE``
    A newly walkable cell is resting on a player-clip brush, and on nothing else. Clip stops
    the player, so the top of a clip volume is a floor — an invisible one, floating wherever
    the volume ends. Capping a roof with a 128-unit clip box does not remove the roof, it
    raises it by 128 units.

The third, ``PLAYSPACE_INTERIOR_SEALED``, is the same loss caused by ordinary geometry, and
is only a warning. Putting a wall or a crate in a room removes floor and is entirely normal;
an error there would block every real edit, and a gate that blocks everything gets switched
off — which is precisely how the round-trip guard next door stopped protecting anything.

Neither is a game rule, so neither is guessed: "walkable" comes from the profile's movement
constants and "clip" from the profile's `assets.clip_shaders`. Where those constants are
unverified the findings clamp to `info`, like every other rule here.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import profiles
from .analysis import (
    AnalysisError,
    NavGrid,
    Solid,
    _finding,
    _summary,
    build_navgrid,
    movement_constants,
    resolve_profile,
)

#: Cell size for a before/after comparison.
#:
#: Coarser than `navgrid_stats`' default of 16 on purpose. This runs on the save path, where
#: it is a tax on every write, and a diff only has to be *comparable* — both sides are
#: measured the same way, so a coarse grid shifts both counts together. 32 also keeps a
#: city-sized map well inside the cell cap that 16 would blow through.
DEFAULT_DIFF_CELL = 32.0

#: Keys in the profile's `assets.clip_shaders` whose shader stops a *player*.
#:
#: Schema keys, not shader names — the names themselves stay in the profile. A weapon clip
#: does not stop a player and so cannot hold one up, which is the whole question here.
PLAYER_BLOCKING_CLIP_KEYS = ("player", "both")

#: Fraction of walkable cells that may vanish before it is worth mentioning on its own.
#: Below this an edit that removes a room reads as ordinary authoring.
DEFAULT_LOST_TOLERANCE = 0.02

#: A ceiling this many standing heights up still counts as a ceiling, on a map with no sky
#: brush to give `_is_indoors` its better signal. Three is a tall room and nothing like the
#: several hundred units of air over a rooftop.
NEAR_CEILING_HEIGHTS = 3.0

_SOURCE = "nrc_mcp.playspace: measured before/after, constants from the profile"


class PlayspaceError(RuntimeError):
    pass


#: A map to measure: a path, or a `(path, parsed map)` pair when the state to measure is
#: held in memory and has not been written yet.
MapRef = "str | Path | tuple[str | Path, Any]"


# ---------------------------------------------------------------------------
# Clip shaders
# ---------------------------------------------------------------------------


def player_clip_shaders(profile_id: str | None = None) -> tuple[str, ...]:
    """Shader names the profile says stop a player, lowercased.

    Empty when the profile states none. An empty tuple disables the clip-surface finding
    rather than falling back to a guess: a shader named "clip" in one game is scenery in
    another, and this file is not allowed to know which game it is serving.
    """
    profile = resolve_profile(profile_id)
    assets = profiles.load(profile).get("assets")
    if not isinstance(assets, dict):
        return ()
    shaders = assets.get("clip_shaders")
    if not isinstance(shaders, dict):
        return ()
    out = []
    for key in PLAYER_BLOCKING_CLIP_KEYS:
        name = shaders.get(key)
        if isinstance(name, str) and name.strip():
            out.append(name.strip().lower())
    return tuple(dict.fromkeys(out))


def _is_clip(solid: Solid, clip_shaders: Sequence[str]) -> bool:
    """True when every face of this brush carries a player-blocking clip shader.

    Every face, not any: a brush with one clip face and five stone faces is a stone brush.
    """
    if not clip_shaders or not solid.shaders:
        return False
    return all(s.strip().lower() in clip_shaders for s in solid.shaders)


# ---------------------------------------------------------------------------
# Aligning two grids
# ---------------------------------------------------------------------------


def _alignment(before: NavGrid, after: NavGrid) -> tuple[int, int, int]:
    """Integer cell offset that maps a `before` cell index onto the same place in `after`.

    Both grids anchor their origin to a multiple of the cell size, so when the cell size
    matches, the offset between them is a whole number of cells and the mapping is exact.
    Comparing cell indices directly without this is the easy mistake: an edit that changes
    the map's bounds moves the origin, and every index silently shifts.
    """
    if abs(before.cell - after.cell) > 1e-9:
        raise PlayspaceError(
            f"cannot compare grids built at different cell sizes "
            f"({before.cell:g} and {after.cell:g})"
        )
    offsets = []
    for axis in range(3):
        raw = (before.origin[axis] - after.origin[axis]) / before.cell
        nearest = round(raw)
        if abs(raw - nearest) > 1e-6:
            raise PlayspaceError(
                "the two maps' grid origins are not a whole number of cells apart on axis "
                f"{axis} ({raw:.4f}); they cannot be compared cell by cell"
            )
        offsets.append(int(nearest))
    return (offsets[0], offsets[1], offsets[2])


def _ceiling_distance(grid: NavGrid, cell: tuple[int, int, int]) -> float | None:
    """Height of the first solid above this cell, or None when nothing is above it at all."""
    ix, iy, iz = cell
    nz = grid.dims[2]
    for z in range(iz + 1, nz):
        if grid.is_solid(ix, iy, z):
            return (z - iz) * grid.cell
    return None


def _topmost_solid(grid: NavGrid, ix: int, iy: int) -> int | None:
    """Index of the highest solid cell in this column."""
    for z in range(grid.dims[2] - 1, -1, -1):
        if grid.is_solid(ix, iy, z):
            return z
    return None


def _is_indoors(grid: NavGrid, cell: tuple[int, int, int], near: float) -> float | None:
    """The ceiling distance if this cell is under a roof, else None.

    "Has a solid above it" is not the same question, and getting them confused is easy: a
    sealed map has a sky brush over everything, and a sky brush is solid to the voxelizer. On
    `ut4_dofa`, *zero* of 27,965 walkable cells have nothing above them — so a naive test
    calls every rooftop an interior, and a rule built on it would fire on exactly the roof
    clipping it was written to permit.

    Two signals, each covering the other's blind spot:

    - **The first solid above is not the topmost solid in the column.** Under a real ceiling
      there is the ceiling, and then more map, and then the sky. On a rooftop the first thing
      above *is* the last thing above. This is the reliable signal, and it needs no
      thresholds and no shader names — but it fails on a map with no sky brush at all, where
      a room's own ceiling is the topmost solid.
    - **The gap is small.** Covers that case. A surface with a solid a couple of player
      heights above it is under something; one with several hundred units of clear air is
      not.

    Neither is a proof. A warehouse with a 500-unit ceiling in an unsealed map reads as
    outdoors, which is a missed detection — the safe direction, since
    `PLAYSPACE_CLIP_SURFACE_WALKABLE` does not depend on this question at all and remains the
    broad net.
    """
    distance = _ceiling_distance(grid, cell)
    if distance is None:
        return None
    ceiling = cell[2] + int(round(distance / grid.cell))
    if ceiling != _topmost_solid(grid, cell[0], cell[1]):
        return distance
    return distance if distance <= near else None


def _voxelize(solids: Iterable[Solid], grid: NavGrid) -> set[tuple[int, int, int]]:
    """Cells whose centre lies inside any of `solids`, on `grid`'s lattice.

    Same column-at-a-time walk `build_navgrid` uses — a convex brush meets a vertical line
    in one interval, so a column costs one span rather than one test per cell. Returning a
    set keeps the later "is this cell held up by clip" question O(1) instead of a scan over
    every clip brush for every candidate cell.
    """
    nx, ny, nz = grid.dims
    ox, oy, oz = grid.origin
    cell = grid.cell
    out: set[tuple[int, int, int]] = set()
    for brush in solids:
        cx0 = max(0, math.ceil((brush.mins[0] - ox) / cell - 0.5))
        cx1 = min(nx - 1, math.floor((brush.maxs[0] - ox) / cell - 0.5))
        cy0 = max(0, math.ceil((brush.mins[1] - oy) / cell - 0.5))
        cy1 = min(ny - 1, math.floor((brush.maxs[1] - oy) / cell - 0.5))
        for ix in range(cx0, cx1 + 1):
            x = ox + (ix + 0.5) * cell
            for iy in range(cy0, cy1 + 1):
                y = oy + (iy + 0.5) * cell
                span = brush.z_span(x, y)
                if span is None:
                    continue
                iz0 = max(0, math.ceil((span[0] - oz) / cell - 0.5))
                iz1 = min(nz - 1, math.floor((span[1] - oz) / cell - 0.5))
                for iz in range(iz0, iz1 + 1):
                    out.add((ix, iy, iz))
    return out


class _Lookup:
    """Point-in-any-brush over a subset of solids, bucketed by footprint.

    Needed to answer "is this cell solid for a reason *other* than clip". Clipping the nose
    of a staircase is ordinary practice, and there the clip brush sits inside real geometry —
    without this the surface would read as a clip surface and the finding would fire on
    every well-built map.
    """

    def __init__(self, solids: Iterable[Solid], size: float = 256.0, max_buckets: int = 256):
        self.size = size
        self.buckets: dict[tuple[int, int], list[Solid]] = {}
        # A map-sized brush would otherwise be filed into thousands of buckets; hold those
        # aside and test them on every query instead.
        self.oversized: list[Solid] = []
        for s in solids:
            x0, x1 = int(math.floor(s.mins[0] / size)), int(math.floor(s.maxs[0] / size))
            y0, y1 = int(math.floor(s.mins[1] / size)), int(math.floor(s.maxs[1] / size))
            if (x1 - x0 + 1) * (y1 - y0 + 1) > max_buckets:
                self.oversized.append(s)
                continue
            for ix in range(x0, x1 + 1):
                for iy in range(y0, y1 + 1):
                    self.buckets.setdefault((ix, iy), []).append(s)

    def hit(self, x: float, y: float, z: float) -> bool:
        key = (int(math.floor(x / self.size)), int(math.floor(y / self.size)))
        for s in self.buckets.get(key, ()):
            if s.contains(x, y, z):
                return True
        return any(s.contains(x, y, z) for s in self.oversized)


def _grid_for(ref: Any, cell: float, profile_id: str | None) -> NavGrid:
    if isinstance(ref, (str, Path)):
        return build_navgrid(ref, cell=cell, profile_id=profile_id)
    path, parsed = ref
    return build_navgrid(path, cell=cell, profile_id=profile_id, game_map=parsed)


def _round3(p: tuple[float, float, float]) -> list[float]:
    return [round(v, 1) for v in p]


# ---------------------------------------------------------------------------
# The diff
# ---------------------------------------------------------------------------


def diff(
    before: Any,
    after: Any,
    *,
    cell: float = DEFAULT_DIFF_CELL,
    profile_id: str | None = None,
    lost_tolerance: float = DEFAULT_LOST_TOLERANCE,
    max_examples: int = 6,
) -> dict[str, Any]:
    """Compare the walkable space of two states of a map.

    `before` and `after` are each a path, or a `(path, parsed map)` pair for a state that is
    still in memory. Returns counts, examples, and findings; raises `PlayspaceError` only
    when the two genuinely cannot be compared.
    """
    profile = resolve_profile(profile_id)
    movement = movement_constants(profile)
    confidence = "verified" if movement.headroom_stand.verified else "unverified"

    grid_before = _grid_for(before, cell, profile)
    grid_after = _grid_for(after, cell, profile)
    dx, dy, dz = _alignment(grid_before, grid_after)

    walkable_before = grid_before.walkable_cells()
    walkable_after = grid_after.walkable_cells()

    lost: list[tuple[int, int, int]] = []
    for c in walkable_before:
        if not grid_after.is_walkable((c[0] + dx, c[1] + dy, c[2] + dz)):
            lost.append(c)
    gained: list[tuple[int, int, int]] = []
    for c in walkable_after:
        if not grid_before.is_walkable((c[0] - dx, c[1] - dy, c[2] - dz)):
            gained.append(c)

    clip_shaders = player_clip_shaders(profile)
    clip_solids = [s for s in grid_after.solids if _is_clip(s, clip_shaders)]
    clip_cells = _voxelize(clip_solids, grid_after) if clip_solids else set()
    other_solids = _Lookup(s for s in grid_after.solids if not _is_clip(s, clip_shaders))

    # Interior floor is floor that was under a roof *before* the edit — see `_is_indoors`,
    # and note that "had something above it" is not that test. What sealed it decides whether
    # this is a defect or ordinary authoring: putting a wall or a crate somewhere indoors
    # removes floor and is entirely normal, while filling that space with clip leaves the room
    # looking untouched and walks the player into thin air.
    near = movement.headroom_stand.value * NEAR_CEILING_HEIGHTS
    sealed_by_clip: list[tuple[int, int, int]] = []
    sealed_by_geometry: list[tuple[int, int, int]] = []
    sealed_outdoors = 0
    for c in lost:
        if _is_indoors(grid_before, c, near) is None:
            # Open above: a rooftop or a street. Making one unreachable is a real intention,
            # and `PLAYSPACE_CLIP_SURFACE_WALKABLE` still judges *how* it was done.
            sealed_outdoors += 1
            continue
        (
            sealed_by_clip
            if (c[0] + dx, c[1] + dy, c[2] + dz) in clip_cells
            else sealed_by_geometry
        ).append(c)

    # Newly walkable surfaces resting on player clip — but only where clip is the *only*
    # reason the supporting cell is solid. Clip laid into a staircase is held up by the
    # stairs, and that surface was always there.
    on_clip: list[tuple[int, int, int]] = []
    for c in gained:
        below = (c[0], c[1], c[2] - 1)
        if below not in clip_cells:
            continue
        p = grid_after.centre(below)
        if not other_solids.hit(p[0], p[1], p[2] + grid_after.cell * 0.5):
            on_clip.append(c)

    sizes_before = sorted(grid_before.component_sizes(), reverse=True)
    sizes_after = sorted(grid_after.component_sizes(), reverse=True)
    largest_before = sizes_before[0] if sizes_before else 0
    largest_after = sizes_after[0] if sizes_after else 0

    findings: list[dict] = []

    if sealed_by_clip:
        findings.append(
            _finding(
                "PLAYSPACE_INTERIOR_SEALED_BY_CLIP",
                "error",
                f"{len(sealed_by_clip)} walkable cell(s) that had a ceiling above them are now "
                f"inside a player-clip volume. A cell under a ceiling is an interior floor, "
                f"not a roof, and clip leaves it looking exactly as walkable as it was — the "
                f"player only finds out by walking into it. Examples: "
                + "; ".join(
                    f"{_round3(grid_before.centre(c))} "
                    f"(ceiling {_is_indoors(grid_before, c, near):.0f}u above)"
                    for c in sealed_by_clip[:max_examples]
                ),
                confidence,
                _SOURCE,
                "clip meant for roofs lands here when the roof test is an upward probe that "
                "steps over thin ceilings. Check these positions before writing.",
            )
        )

    if sealed_by_geometry:
        findings.append(
            _finding(
                "PLAYSPACE_INTERIOR_SEALED",
                "warning",
                f"{len(sealed_by_geometry)} walkable cell(s) under a ceiling were removed by "
                f"ordinary geometry. Expected if you placed something there; worth a look if "
                f"you did not. Examples: "
                + "; ".join(
                    repr(_round3(grid_before.centre(c))) for c in sealed_by_geometry[:max_examples]
                ),
                confidence,
                _SOURCE,
                "no action if this was the edit you meant to make.",
            )
        )

    if on_clip:
        findings.append(
            _finding(
                "PLAYSPACE_CLIP_SURFACE_WALKABLE",
                "error",
                f"{len(on_clip)} newly walkable cell(s) are resting on a player-clip brush. "
                f"Clip stops the player, so the top of a clip volume is a floor — the space "
                f"below it was removed and an invisible one put in its place. Examples: "
                + "; ".join(repr(_round3(grid_after.centre(c))) for c in on_clip[:max_examples]),
                confidence,
                _SOURCE,
                "extend the clip volume to the ceiling, or up to where nothing can stand on "
                "it, rather than capping it at a fixed height.",
            )
        )

    lost_fraction = len(lost) / len(walkable_before) if walkable_before else 0.0
    if lost and lost_fraction > lost_tolerance and not sealed_by_clip:
        findings.append(
            _finding(
                "PLAYSPACE_WALKABLE_LOST",
                "warning",
                f"{len(lost)} of {len(walkable_before)} walkable cells "
                f"({lost_fraction * 100:.1f}%) are gone, and {len(gained)} are new. "
                f"Examples of what was lost: "
                + "; ".join(repr(_round3(grid_before.centre(c))) for c in lost[:max_examples]),
                confidence,
                _SOURCE,
                "expected if the edit removed a region; look at the examples if it was not.",
            )
        )

    if largest_before and largest_after < largest_before:
        shrink = (largest_before - largest_after) / largest_before
        if shrink > lost_tolerance:
            findings.append(
                _finding(
                    "PLAYSPACE_CONNECTIVITY_SHRANK",
                    "warning",
                    f"the largest connected walkable region went from {largest_before} to "
                    f"{largest_after} cells ({shrink * 100:.1f}% smaller) and the map now has "
                    f"{len(sizes_after)} disconnected region(s), up from {len(sizes_before)}. "
                    "Space can survive an edit and still be cut off from the rest of the map.",
                    confidence,
                    _SOURCE,
                    "check that every objective and spawn is still in the main region.",
                )
            )

    return {
        "cell": cell,
        "profile": profile,
        "before": {
            "map": grid_before.map_path,
            "walkable_cells": len(walkable_before),
            "largest_region": largest_before,
            "regions": len(sizes_before),
        },
        "after": {
            "map": grid_after.map_path,
            "walkable_cells": len(walkable_after),
            "largest_region": largest_after,
            "regions": len(sizes_after),
        },
        "lost_cells": len(lost),
        "gained_cells": len(gained),
        "lost_fraction": round(lost_fraction, 4),
        "interior_sealed_by_clip_cells": len(sealed_by_clip),
        "interior_sealed_by_geometry_cells": len(sealed_by_geometry),
        "outdoor_cells_removed": sealed_outdoors,
        "walkable_on_clip_cells": len(on_clip),
        "clip_shaders_checked": list(clip_shaders),
        "examples": {
            "interior_sealed_by_clip": [
                _round3(grid_before.centre(c)) for c in sealed_by_clip[:max_examples]
            ],
            "walkable_on_clip": [_round3(grid_after.centre(c)) for c in on_clip[:max_examples]],
            "lost": [_round3(grid_before.centre(c)) for c in lost[:max_examples]],
        },
        "geometry": {"before": grid_before.geometry, "after": grid_after.geometry},
        "findings": findings,
        "summary": _summary(findings),
        "notes": [
            f"measured at cell={cell:g}, so a feature narrower than that is not resolved",
            f"indoors means a ceiling that is not the top of its column, or one within "
            f"{near:.0f}u; a sky brush is solid here, so 'has something above it' would call "
            f"every rooftop an interior",
            "brushes the kernel cannot evaluate exactly are absent from both sides; see "
            "geometry.*.brushes_indeterminate",
        ],
    }


def has_errors(report: dict[str, Any]) -> bool:
    return any(f["severity"] == "error" for f in report.get("findings", []))


# ---------------------------------------------------------------------------
# Tier 0: the invariant library
# ---------------------------------------------------------------------------


@dataclass
class Check:
    """Everything an invariant is allowed to look at.

    Deliberately small. An invariant that needs more than the two map states, the workspace
    and the recorded tool results is probably asserting on the journey rather than the
    destination, and will break the first time the route changes.
    """

    workspace: Path
    #: The map as it was before anything ran. None for a scenario that starts from nothing.
    baseline: Path | None = None
    #: The map as it is now.
    current: Path | None = None
    #: Results of named steps, for invariants that assert on a tool's own report.
    results: dict[str, Any] = field(default_factory=dict)
    profile_id: str | None = None
    cell: float = DEFAULT_DIFF_CELL
    #: Whether invariants that need a compiler may run one.
    allow_compile: bool = False


@dataclass(frozen=True)
class Outcome:
    name: str
    ok: bool
    detail: str
    skipped: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "skipped": self.skipped, "detail": self.detail}


InvariantFn = Callable[..., Outcome]
_REGISTRY: dict[str, InvariantFn] = {}


def invariant(name: str) -> Callable[[InvariantFn], InvariantFn]:
    def register(fn: InvariantFn) -> InvariantFn:
        _REGISTRY[name] = fn
        return fn

    return register


def available() -> list[str]:
    return sorted(_REGISTRY)


def run_invariant(name: str, check: Check, **args: Any) -> Outcome:
    fn = _REGISTRY.get(name)
    if fn is None:
        return Outcome(name, False, f"no such invariant; known: {', '.join(available())}")
    try:
        return fn(check, **args)
    except (AnalysisError, PlayspaceError) as e:
        # An invariant that cannot measure has not proved anything. Say so rather than
        # letting an unmeasurable scenario read as a passing one.
        return Outcome(name, False, f"could not be evaluated: {e}")


def _require_current(check: Check) -> Path:
    if check.current is None or not check.current.exists():
        raise PlayspaceError("no current map to check")
    return check.current


@invariant("round_trip_identical")
def _round_trip_identical(check: Check) -> Outcome:
    """The written map reproduces its own bytes. The kernel's own gate, applied to output."""
    from .kernel import load_map  # noqa: PLC0415 — keeps import cost off module load

    path = _require_current(check)
    rt = load_map(path).round_trip()
    return Outcome(
        "round_trip_identical",
        bool(rt["identical"]),
        "identical" if rt["identical"] else f"differs at {rt.get('first_difference')}",
    )


@invariant("no_playable_space_regression")
def _no_playable_space_regression(
    check: Check, max_lost_fraction: float = DEFAULT_LOST_TOLERANCE
) -> Outcome:
    """No error-level playable-space finding, and no more loss than allowed."""
    if check.baseline is None:
        return Outcome("no_playable_space_regression", True, "no baseline", skipped=True)
    report = diff(
        check.baseline,
        _require_current(check),
        cell=check.cell,
        profile_id=check.profile_id,
        lost_tolerance=max_lost_fraction,
    )
    codes = [f["code"] for f in report["findings"] if f["severity"] == "error"]
    over = report["lost_fraction"] > max_lost_fraction
    detail = (
        f"lost {report['lost_cells']} of {report['before']['walkable_cells']} "
        f"({report['lost_fraction'] * 100:.1f}%), gained {report['gained_cells']}"
    )
    if codes:
        detail += f"; errors: {', '.join(codes)}"
    return Outcome("no_playable_space_regression", not codes and not over, detail)


@invariant("no_clip_surface_walkable")
def _no_clip_surface_walkable(check: Check) -> Outcome:
    """Nothing can stand on top of a player-clip volume."""
    if check.baseline is None:
        return Outcome("no_clip_surface_walkable", True, "no baseline", skipped=True)
    report = diff(
        check.baseline, _require_current(check), cell=check.cell, profile_id=check.profile_id
    )
    n = report["walkable_on_clip_cells"]
    if not report["clip_shaders_checked"]:
        return Outcome(
            "no_clip_surface_walkable",
            True,
            "the profile states no player-clip shader, so this cannot be checked",
            skipped=True,
        )
    return Outcome(
        "no_clip_surface_walkable",
        n == 0,
        "none"
        if n == 0
        else f"{n} cell(s) rest on clip, e.g. {report['examples']['walkable_on_clip'][:3]}",
    )


@invariant("walkable_area_at_least")
def _walkable_area_at_least(check: Check, cells: int = 1) -> Outcome:
    """The map has at least this much standable space. Catches an edit that sealed it shut."""
    grid = build_navgrid(_require_current(check), cell=check.cell, profile_id=check.profile_id)
    n = len(grid.walkable_cells())
    return Outcome("walkable_area_at_least", n >= cells, f"{n} walkable cells (want >= {cells})")


@invariant("single_walkable_region")
def _single_walkable_region(check: Check, min_fraction: float = 0.9) -> Outcome:
    """Most walkable space is one connected region — nothing important got cut off."""
    grid = build_navgrid(_require_current(check), cell=check.cell, profile_id=check.profile_id)
    sizes = sorted(grid.component_sizes(), reverse=True)
    total = sum(sizes)
    if not total:
        return Outcome("single_walkable_region", False, "no walkable space at all")
    share = sizes[0] / total
    return Outcome(
        "single_walkable_region",
        share >= min_fraction,
        f"largest region holds {share * 100:.1f}% of {total} cells across {len(sizes)} regions",
    )


@invariant("validates_clean")
def _validates_clean(check: Check, grid: int = 8) -> Outcome:
    """No error-severity geometry or file-format finding."""
    from .kernel import load_map  # noqa: PLC0415

    report = load_map(_require_current(check)).validate(grid=grid)
    errors = [f for f in report.get("findings", []) if f.get("severity") == "error"]
    codes = sorted({f["code"] for f in errors})
    return Outcome(
        "validates_clean", not errors, "clean" if not errors else f"{len(errors)}: {codes}"
    )


def _standable_near(grid: NavGrid, position: Sequence[float], tolerance: float) -> float | None:
    """The z of a walkable surface in this column within `tolerance` of `position`, if any.

    Tolerant on purpose. A walkable cell's standing height is `origin + iz * cell`, and a
    floor slab 16 units thick under a 32-unit grid does not land on that lattice — asking for
    an exact z would make every such assertion fail for a reason that has nothing to do with
    the map. The tolerance is one cell by default, which is the resolution of the answer.
    """
    ix, iy, _ = grid.cell_at((position[0], position[1], position[2]))
    best: float | None = None
    for iz in grid.columns.get((ix, iy), ()):
        z = grid.origin[2] + iz * grid.cell
        if abs(z - position[2]) <= tolerance and (
            best is None or abs(z - position[2]) < abs(best - position[2])
        ):
            best = z
    return best


@invariant("positions_not_walkable")
def _positions_not_walkable(
    check: Check, positions: Sequence[Sequence[float]] = (), tolerance: float | None = None
) -> Outcome:
    """None of these places can be stood on. "Make the roof unreachable", checked.

    Note what this does *not* say: unreachable. It says not standable. A roof a player can
    still jump to from a crate is a routing question, and the navgrid's connectivity is the
    tool for that — this is the narrower claim, and calling it the narrower thing keeps a
    passing scenario from implying more than was measured.
    """
    grid = build_navgrid(_require_current(check), cell=check.cell, profile_id=check.profile_id)
    tol = check.cell if tolerance is None else float(tolerance)
    still = [
        (list(p), _standable_near(grid, p, tol))
        for p in positions
        if _standable_near(grid, p, tol) is not None
    ]
    return Outcome(
        "positions_not_walkable",
        not still,
        f"all {len(positions)} clear"
        if not still
        else "; ".join(f"{p} still standable at z={z:.0f}" for p, z in still[:4]),
    )


@invariant("positions_walkable")
def _positions_walkable(
    check: Check, positions: Sequence[Sequence[float]] = (), tolerance: float | None = None
) -> Outcome:
    """All of these places can still be stood on. The other half of a sealing task."""
    grid = build_navgrid(_require_current(check), cell=check.cell, profile_id=check.profile_id)
    tol = check.cell if tolerance is None else float(tolerance)
    gone = [list(p) for p in positions if _standable_near(grid, p, tol) is None]
    return Outcome(
        "positions_walkable",
        not gone,
        f"all {len(positions)} standable" if not gone else f"no longer standable: {gone[:4]}",
    )


@invariant("no_new_validation_errors")
def _no_new_validation_errors(check: Check, grid: int = 8) -> Outcome:
    """No validation finding that the map did not already have.

    The right check for editing a real map. `validates_clean` is the wrong one there:
    `ut4_woolis` opens with 30 `BRUSH_OFF_GRID` findings and always will, because a `.map`
    stores planes and a rotated brush's vertices land where they land. Demanding zero would
    mean no tape could ever touch a real map; demanding *no more than before* is the thing
    anyone actually cares about.
    """
    from .kernel import load_map  # noqa: PLC0415

    def counts(path: Path) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in load_map(path).validate(grid=grid).get("findings", []):
            if f.get("severity") in ("error", "warning"):
                out[f["code"]] = out.get(f["code"], 0) + 1
        return out

    after = counts(_require_current(check))
    before = counts(check.baseline) if check.baseline else {}
    worse = {c: (before.get(c, 0), n) for c, n in after.items() if n > before.get(c, 0)}
    return Outcome(
        "no_new_validation_errors",
        not worse,
        "nothing new"
        if not worse
        else "; ".join(f"{c}: {b} -> {a}" for c, (b, a) in sorted(worse.items())),
    )


@invariant("spawns_reach_objectives")
def _spawns_reach_objectives(check: Check) -> Outcome:
    """Every spawn group can walk to every objective. The check a bomb map lives or dies on."""
    from . import analysis as ana  # noqa: PLC0415

    report = ana.balance_report(
        _require_current(check), cell=check.cell, profile_id=check.profile_id
    )
    bad = [f for f in report.get("findings", []) if f["code"] == "BALANCE_OBJECTIVE_UNREACHABLE"]
    pairs = report.get("distances") or []
    if not pairs:
        return Outcome(
            "spawns_reach_objectives",
            True,
            "no spawn/objective pairs in this map",
            skipped=True,
        )
    unreachable = [row for row in pairs if not row.get("reachable")]
    return Outcome(
        "spawns_reach_objectives",
        not unreachable and not bad,
        f"{len(pairs) - len(unreachable)}/{len(pairs)} pairs reachable",
    )


@invariant("map_seals")
def _map_seals(check: Check, preset: str = "draft") -> Outcome:
    """The map compiles without leaking. Needs a compiler, and skips cleanly without one."""
    if not check.allow_compile:
        return Outcome("map_seals", True, "compiling not enabled for this run", skipped=True)
    from . import optimize as opt  # noqa: PLC0415

    path = _require_current(check)
    try:
        result = opt._compile_variant(path, preset)
    except Exception as e:  # noqa: BLE001 — a missing or broken compiler is a skip, not a fail
        return Outcome("map_seals", True, f"no usable compiler: {e}", skipped=True)
    if not result.get("ok"):
        return Outcome("map_seals", False, str(result.get("error") or "the compile failed"))
    # q3map2 writes a pointfile only when the map leaks, so its presence is the signal.
    leak = next((a for a in result.get("artifacts") or [] if a.lower().endswith(".lin")), None)
    return Outcome("map_seals", leak is None, "sealed" if leak is None else f"leaked: {leak}")


@invariant("tool_result")
def _tool_result(check: Check, step: str = "", path: str = "", equals: Any = None) -> Outcome:
    """A recorded step's report matches. For asserting on a tool's own answer, not the map."""
    value: Any = check.results.get(step)
    for part in [p for p in path.split(".") if p]:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.lstrip("-").isdigit():
            idx = int(part)
            value = value[idx] if -len(value) <= idx < len(value) else None
        else:
            value = None
    ok = _matches(value, equals)
    return Outcome("tool_result", ok, f"{step}.{path} = {value!r} (want {equals!r})")


def _matches(value: Any, expected: Any) -> bool:
    """Equality, or a one-key comparison object like `{"$gte": 4}`."""
    if isinstance(expected, dict) and len(expected) == 1:
        ((op, operand),) = expected.items()
        if op in ("$gt", "$gte", "$lt", "$lte"):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return False
            return {
                "$gt": value > operand,
                "$gte": value >= operand,
                "$lt": value < operand,
                "$lte": value <= operand,
            }[op]
        if op == "$ne":
            return value != operand
        if op == "$contains":
            return bool(value) and operand in value
    return value == expected
