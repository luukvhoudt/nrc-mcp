"""The optimization suite (§6.1, §6.3).

§6.1 calls the structural-versus-detail split "the single biggest lever on a Q3-engine map",
and it is right for a reason worth stating: every *structural* brush face is a candidate BSP
splitting plane, every split multiplies leaves, and vis cost grows with the square of the
portals between them. A crate marked structural therefore costs compile minutes and frame
time and returns nothing, because it occludes nothing. Nobody does this audit by hand at the
scale it needs, which is exactly why it belongs to a tool.

Four of the five entry points here read a file format rather than guessing at one, and each
was checked against the code that writes it before a line of parser was written:

- `.prt` — written by `WritePortalFile` in `tools/quake3/q3map2/prtfile.cpp` (header at
  :351-354, portal records at :124-153, solid faces at :216-238) and read back by
  `LoadPortals` in `tools/quake3/q3map2/vis.cpp:694-830`. Both halves agree, and a real
  869-cluster portal file from a shipped map parses to its own declared counts exactly.
- `.lin` — written by `LeakFile` in `tools/quake3/q3map2/leakfile.cpp:74-114`. Three
  `%f`-formatted floats per line, one line per point.
- `.shader` — tokenized by `GetToken` in `tools/quake3/common/scriplib.cpp:182-270` and
  given structure by `ParseShaderFile` in `tools/quake3/q3map2/shaders.cpp:809-1000`.

Nothing here parses a `.map`. Everything about the map's geometry — vertices, bounds, minimum
thickness, per-face shaders and contents, and the derived detail flag — is asked of the kernel
through `nrc_py.Map`. That is a deliberate constraint rather than a convenience: a second, worse
reader of the `.map` text is the one bug this project can least afford, and the kernel is where
the exact arithmetic and the round-trip guarantee live.

# What is a measurement and what is a guess

Findings carry the project's `confidence`, and this module is stricter than it looks:
`verified` means the fact was read out of a file some other program wrote, or out of upstream
source. Every *judgement* — "this brush would be better as detail", "a hint plane here would
help" — is `unverified`, because the honest answer needs a `-vis` compile A/B and this module
cannot produce one from a `.map` alone.

`rules.py` clamps an unverified rule all the way down to `info`, because a profile rule is an
assertion about a game and a wrong assertion must never fail a build. The clamp here stops at
`warning` instead, and the difference is deliberate: these findings are *labelled suggestions
for a human to review*, and a suggestion filed at `info` next to genuine `info` noise is a
suggestion nobody reads. Nothing unverified can reach `error`, so the build-gate property
§7 asks for still holds.
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any

from . import bsp, profiles
from .kernel import load_map, repo_root, run_mise_task

SEVERITIES = ("info", "warning", "error")

#: Bit 27 of a face's *contents* marks its brush as detail — upstream's `BRUSH_DETAIL_FLAG`
#: (`radiant/brush.h`), read by q3map2 as `C_DETAIL`. The kernel applies this mask itself and
#: reports the result as `brush_geometry(...)["detail"]`, so nothing here tests it; the constant
#: is what makes the `face_contents` list interpretable, and what a caller acting on a finding
#: has to set.
BRUSH_DETAIL_MASK = 1 << 27

#: The world is entity 0, and only the world gets a vis tree. `tools/quake3/q3map2/bsp.cpp:540`
#: dispatches on `entityNum == 0`: `ProcessWorldModel` calls `MakeTreePortals` and
#: `FilterStructuralBrushesIntoTree` (:292-293), while `ProcessSubModel` (:448) builds no tree
#: portals at all. So a brush inside a brush entity cannot cost a portal however it is flagged,
#: and auditing it would be noise.
WORLD_ENTITY = 0

#: `.prt` header magic — `PORTALFILE` in `tools/quake3/q3map2/q3map2.h:150`.
PORTALFILE_MAGIC = "PRT1"

#: Hard ceilings vis enforces while reading a `.prt`. Exceeding either kills the `-vis` stage
#: with `Error()`, so a portal file that trips one is a compile failure waiting to happen.
#: `MAX_PORTALS_ON_LEAF` is `q3map2.h:155`, checked at `vis.cpp:784`, `:805` and `:846`;
#: `MAX_POINTS_ON_WINDING` is `tools/quake3/common/polylib.h:57`, checked at `vis.cpp:744`.
MAX_PORTALS_ON_LEAF = 1024
MAX_POINTS_ON_WINDING = 512

#: Stage directives that name an image, from `shaders.cpp:846-852`. A stage carrying none of
#: these draws no texture.
STAGE_IMAGE_DIRECTIVES = ("map", "clampmap", "animmap", "clampanimmap", "mapcomp", "mapnocomp")

#: Body-level directives that give a shader an image without a stage, from `shaders.cpp:960-990`
#: (`implicit*`) and the editor/light image handling at `:844`.
BODY_IMAGE_DIRECTIVES = (
    "qer_editorimage",
    "q3map_lightimage",
    "implicitmap",
    "implicitblend",
    "implicitmask",
)

#: The surfaceparm that tells the compiler to emit no draw surface for a face:
#: `tools/quake3/q3map2/games.cpp:148` maps it to `C_NODRAW`, and `shaders.cpp:648` skips image
#: loading for anything carrying it. This is the "watercaulk trap" of §6.3 in one token.
NODRAW_SURFACEPARM = "nodraw"

#: The prefix q3map2's own readers prepend to every shader name a `.map` mentions —
#: `map.cpp:1009` for brush faces and `patch.cpp:207` for patches. The `.map` stores the name
#: without it, so any comparison against a shader script has to add it back.
SHADER_NAME_PREFIX = "textures/"

#: An empty shader is spelled this way on disk (`crates/nrc-core/src/write.rs`), and it names
#: no shader at all.
EMPTY_SHADER = "NULL"

#: Compile presets, one mise task each. Task names are an API (see the README), so these
#: mirror `tools/q3map2.py`'s `PRESETS` rather than being derived from it — `tools/` is a
#: script directory, not an importable package.
COMPILE_PRESETS = ("draft", "iterate", "quality", "final")

#: Where `compile_ab` appends its history, so a regression is visible over a project's life
#: rather than only against whatever was compiled last (§6.1).
AB_HISTORY_RELATIVE = Path("bench") / "ab-history.jsonl"


class OptimizeError(RuntimeError):
    """An input this module could not read, or a tool it could not drive."""


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def _clamp(asked: str, confidence: str) -> str:
    """Severity after the confidence clamp: unverified never reaches `error`."""
    if asked not in SEVERITIES:
        asked = "warning"
    if confidence != "verified" and asked == "error":
        return "warning"
    return asked


def _finding(
    code: str,
    severity: str,
    message: str,
    confidence: str,
    *,
    source: str,
    fix_hint: str = "",
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One finding, in the shape `rules.Finding.as_dict` produces.

    Structured facts go under `detail` rather than being spliced into the top level, so a
    caller can always rely on the same seven keys being present and nothing else colliding
    with them.
    """
    return {
        "code": code,
        "severity": _clamp(severity, confidence),
        "message": message,
        "confidence": confidence,
        "rule_source": source,
        "fix_hint": fix_hint,
        "detail": detail or {},
    }


def _summarize(findings: list[dict]) -> dict[str, int]:
    return {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES}


def _sorted(findings: list[dict]) -> list[dict]:
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(findings, key=lambda f: (order[f["severity"]], f["code"]))


# ---------------------------------------------------------------------------
# §6.1 structural audit
# ---------------------------------------------------------------------------


def _brushes(mp, entity_index: int, info: dict) -> list[tuple[int, dict]]:
    """Every brush of an entity as `(primitive_index, geometry)`, asked of the kernel.

    `brush_geometry` refuses patches and unrecognized blocks, and `entities()` states how many
    brushes an entity has, so probing until that many have answered identifies the brushes
    without this module forming an opinion about primitive kinds — and returns the geometry it
    had to fetch anyway.
    """
    want = int(info.get("brushes") or 0)
    cap = want + int(info.get("patches") or 0) + 64
    found: list[tuple[int, dict]] = []
    p = 0
    while len(found) < want and p < cap:
        try:
            g = mp.brush_geometry(entity_index, p)
        except Exception as e:  # noqa: BLE001 - the kernel raises a plain ValueError here
            if "no primitive" in str(e):
                break
        else:
            found.append((p, g))
        p += 1
    return found


def _extent(bounds: Any) -> tuple[list[float], list[float], list[float]] | None:
    """`((min, max))` from `brush_geometry` as `(min, max, size)`."""
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
        return None
    lo, hi = bounds
    if not (isinstance(lo, (list, tuple)) and isinstance(hi, (list, tuple))):
        return None
    lo = [float(v) for v in lo]
    hi = [float(v) for v in hi]
    return lo, hi, [hi[i] - lo[i] for i in range(3)]


def _touches_shell(lo: list[float], hi: list[float], world: dict, margin: float) -> bool:
    """Whether a brush reaches within `margin` of the map's outer bounding box.

    A crude stand-in for "is part of the hull that seals the map", and knowingly so: an
    interior ceiling does not touch the world box even though it seals a room. It is used only
    to *withhold* a suggestion, never to make one, so erring towards "this seals something" is
    the safe direction.

    `margin` defaults to zero — the brush has to actually reach the outer box — because no
    non-zero value works. The distance from a crate standing on the floor to the outer box is
    the floor's own thickness, so any margin generous enough to catch a hull brush drawn
    slightly inside the box also catches everything resting on the ground. Raise it only for a
    map whose hull sits well outside its visible geometry, and expect ground-level furniture to
    fall out of the audit when you do.
    """
    wlo = [float(v) for v in world["min"]]
    whi = [float(v) for v in world["max"]]
    return any(lo[i] - wlo[i] <= margin or whi[i] - hi[i] <= margin for i in range(3))


def structural_audit(
    map_path: str | Path,
    grid: int = 8,
    *,
    small_max_extent: float = 64.0,
    thin_min_thickness: float = 8.0,
    shell_margin: float = 0.0,
    interior_max_extent: float = 128.0,
    interior_max_fraction: float = 0.125,
    limit: int = 200,
) -> dict[str, Any]:
    """Structural brushes that look like they should be detail (§6.1).

    **The per-brush benefit figure is a heuristic, not a measurement.** It counts the face
    planes that would leave the structural set, because that is what drives BSP splits — but
    how many portals a given split actually produced depends on the tree, and the tree is only
    known after a compile. The real number comes from a `-vis` A/B: convert the candidates,
    run `compile_ab(before, after, preset="quality")`, and read the portal and leaf delta.
    Treat everything here as a shortlist for that experiment.

    Three heuristics, each with its reasoning:

    `STRUCT_SMALL_BRUSH`
        Every dimension is under `small_max_extent`. A brush that small cannot occlude a
        sightline across a room, but its planes still split the tree, so it pays the full vis
        cost and returns nothing. Crates, cover, kerbs, bollards.

    `STRUCT_THIN_BRUSH`
        `min_thickness` is under `thin_min_thickness` *and* the brush does not reach the map's
        shell. Two structural planes less than an authoring grid step apart produce sliver
        leaves and near-degenerate portals, and an interior slab is not holding the seal, so
        the slivers buy nothing. Trim, panels, signage, thin ledges.

    `STRUCT_INTERIOR_ISLAND`
        The brush does not reach the map's outer bounding box on any axis, *and* its largest
        dimension is under both `interior_max_extent` and `interior_max_fraction` of the map's
        narrower horizontal span. Only geometry on or near the hull can seal the map, so
        interior geometry has to justify itself purely as a visual blocker.

        Both size limits are needed, and finding that out is worth recording. With the
        map-relative one alone this rule flagged 680 of 984 structural brushes on a real
        1454-brush map — on a map several thousand units across, an eighth of its width is a
        whole wall segment, and advising someone to detail their interior walls is worse than
        saying nothing. The absolute cap is what keeps the output a shortlist.

    Structural-ness comes from `brush_geometry(...)["detail"]`, which the kernel derives by
    OR-ing the `1 << 27` contents bit across a brush's faces — the same mask and the same rule
    `stats()` counts with, so the audit and the totals beside it cannot disagree.
    """
    path = Path(map_path)
    mp = load_map(path)
    stats = mp.stats(grid=grid)
    entities = mp.entities(with_keys=False)

    findings: list[dict] = []
    notes: list[str] = []
    world_info = entities[WORLD_ENTITY] if entities else {"brushes": 0, "patches": 0}
    world_brushes = _brushes(mp, WORLD_ENTITY, world_info)

    world = stats.get("bounds")
    if not isinstance(world, dict):
        notes.append(
            "the map has no evaluable bounds, so the shell tests were skipped and only the "
            "size test ran"
        )

    horizontal_span = None
    if isinstance(world, dict):
        size = [float(v) for v in world["size"]]
        horizontal_span = min(size[0], size[1])

    candidates: list[dict] = []
    unevaluated = 0

    for p, g in world_brushes:
        if g.get("detail"):
            continue
        if not g.get("usable"):
            unevaluated += 1
            continue
        ext = _extent(g.get("bounds"))
        if ext is None:
            unevaluated += 1
            continue
        lo, hi, size = ext
        thickness = g.get("min_thickness")
        faces = int(g.get("faces") or 0)

        shell = _touches_shell(lo, hi, world, shell_margin) if isinstance(world, dict) else True
        codes: list[str] = []
        if max(size) < small_max_extent:
            codes.append("STRUCT_SMALL_BRUSH")
        if isinstance(thickness, (int, float)) and thickness < thin_min_thickness and not shell:
            codes.append("STRUCT_THIN_BRUSH")
        if (
            not shell
            and horizontal_span is not None
            and max(size) < min(interior_max_extent, interior_max_fraction * horizontal_span)
        ):
            codes.append("STRUCT_INTERIOR_ISLAND")
        if not codes:
            continue

        candidates.append(
            {
                "entity": WORLD_ENTITY,
                "primitive": p,
                "codes": codes,
                "faces": faces,
                "min": lo,
                "max": hi,
                "size": [round(v, 3) for v in size],
                "min_thickness": thickness,
                "touches_shell": shell,
                "shaders": sorted(set(g.get("shaders") or [])),
                "estimated_portal_reduction": faces,
            }
        )

    candidates.sort(key=lambda c: (-c["estimated_portal_reduction"], c["primitive"]))
    truncated = len(candidates) > limit
    shown = candidates[:limit]

    for c in shown:
        code = c["codes"][0]
        findings.append(
            _finding(
                code,
                "warning",
                f"brush {c['primitive']} of entity {c['entity']} is structural but measures "
                f"{c['size'][0]:g} x {c['size'][1]:g} x {c['size'][2]:g} units"
                + (
                    f" and is only {c['min_thickness']:g} thick"
                    if isinstance(c["min_thickness"], (int, float))
                    else ""
                )
                + (
                    ", clear of the map shell"
                    if not c["touches_shell"]
                    else ", reaching the map shell"
                )
                + f"; converting it to detail removes {c['faces']} splitting plane(s) from the "
                "vis tree. Estimated, not measured — confirm with a -vis A/B.",
                "unverified",
                source="nrc_mcp.optimize structural heuristics",
                fix_hint=(
                    "set the detail contents bit (1 << 27) on the brush, or select it in the "
                    "editor and make it detail"
                ),
                detail=c,
            )
        )

    total_reduction = sum(c["estimated_portal_reduction"] for c in candidates)
    world_structural = sum(1 for _, g in world_brushes if not g.get("detail"))

    if unevaluated:
        findings.append(
            _finding(
                "STRUCT_BRUSH_UNEVALUATED",
                "info",
                f"{unevaluated} structural brush(es) in entity {WORLD_ENTITY} could not be "
                "evaluated exactly — off-grid or degenerate plane points — so they were left "
                "out of the audit rather than guessed at",
                "verified",
                source="nrc_py.Map.brush_geometry",
                fix_hint="snap the brush to the grid, then re-run the audit",
            )
        )

    notes.append(
        "Estimated benefit counts splitting planes, not portals. Portals depend on the BSP "
        "tree, which only exists after a compile; compile_ab on a converted copy is the "
        "measurement."
    )
    if truncated:
        notes.append(
            f"{len(candidates)} candidates found, {limit} reported — raise `limit` for the rest."
        )

    by_code: dict[str, int] = {}
    for c in candidates:
        for code in c["codes"]:
            by_code[code] = by_code.get(code, 0) + 1

    return {
        "map": str(path),
        "grid": grid,
        "world_entity": WORLD_ENTITY,
        "bounds": world,
        "thresholds": {
            "small_max_extent": small_max_extent,
            "thin_min_thickness": thin_min_thickness,
            "shell_margin": shell_margin,
            "interior_max_extent": interior_max_extent,
            "interior_max_fraction": interior_max_fraction,
        },
        "totals": {
            "brushes": stats.get("brushes"),
            "detail_brushes": stats.get("detail_brushes"),
            "structural_brushes": stats.get("structural_brushes"),
            "world_brushes": len(world_brushes),
            "world_structural_brushes": world_structural,
            "unevaluated_brushes": unevaluated,
            "candidates": len(candidates),
        },
        "candidates_by_code": by_code,
        "candidates": shown,
        "truncated": truncated,
        "estimated_portal_reduction_total": total_reduction,
        "findings": _sorted(findings),
        "summary": _summarize(findings),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# §6.1 portal file and hint suggestions
# ---------------------------------------------------------------------------

_PAREN = re.compile(r"([()])")


def _winding(tokens: list[str], i: int, count: int, where: str) -> tuple[list[list[float]], int]:
    """Read `count` parenthesised points starting at `tokens[i]`."""
    points: list[list[float]] = []
    for _ in range(count):
        if i >= len(tokens) or tokens[i] != "(":
            raise OptimizeError(f"{where}: expected '(' at token {i}, found {tokens[i : i + 1]}")
        try:
            point = [float(tokens[i + 1]), float(tokens[i + 2]), float(tokens[i + 3])]
        except (IndexError, ValueError) as e:
            raise OptimizeError(f"{where}: malformed point at token {i}: {e}") from e
        if i + 4 >= len(tokens) or tokens[i + 4] != ")":
            raise OptimizeError(f"{where}: expected ')' closing the point at token {i}")
        points.append(point)
        i += 5
    return points, i


def parse_portal_file(prt_path: str | Path) -> dict[str, Any]:
    """Parse the `.prt` a `-vis -saveprt` run leaves behind.

    The format is verified in both directions. `WritePortalFile`
    (`tools/quake3/q3map2/prtfile.cpp:343-360`) writes four header lines — the magic `PRT1`,
    the cluster count, the visportal count, the solid-face count — then one line per visportal
    and one line per solid face. A visportal line is
    `numpoints cluster_a cluster_b flags (x y z ) (x y z ) …` (`:124-153`), where flags bit 0
    is `C_HINT` and bit 1 is `C_SKY` (`:133-140`). A solid-face line carries a single cluster:
    `numpoints cluster (x y z ) …` (`:216-238`). `LoadPortals`
    (`tools/quake3/q3map2/vis.cpp:694-830`) reads exactly that back, and its
    `fscanf("(%f %f %f ) ")` confirms the spacing.

    One consequence worth knowing before reading the counts: each *file* portal becomes two
    *memory* portals, one registered against each of its two clusters (`vis.cpp:769-812`). So a
    cluster's portal count is the number of lines naming it in either slot, which is what
    `portals_per_cluster` reports.
    """
    path = Path(prt_path)
    if not path.is_file():
        raise OptimizeError(
            f"{path} does not exist. A portal file comes from a vis run that was asked to keep "
            f"it: `mise run compile:final <map>` uses `-vis -saveprt`."
        )

    lines = path.read_text(errors="replace").splitlines()
    if len(lines) < 4:
        raise OptimizeError(f"{path} has {len(lines)} line(s); a portal file needs at least 4")

    magic = lines[0].strip()
    if magic != PORTALFILE_MAGIC:
        raise OptimizeError(
            f"{path} starts with {magic!r}, not {PORTALFILE_MAGIC!r}. vis.cpp:706 rejects "
            f"anything else as 'not a portal file', so this is not a portal file."
        )
    try:
        clusters, declared_portals, declared_faces = (int(lines[i].strip()) for i in (1, 2, 3))
    except ValueError as e:
        raise OptimizeError(f"{path}: header counts on lines 2-4 are not integers: {e}") from e

    portals: list[dict[str, Any]] = []
    faces: list[dict[str, Any]] = []
    per_cluster: dict[int, int] = {}
    row = 4

    for n in range(declared_portals):
        if row >= len(lines):
            break
        tokens = _PAREN.sub(r" \1 ", lines[row]).split()
        row += 1
        if not tokens:
            continue
        where = f"{path.name}:{row} (portal {n})"
        try:
            count, cluster_a, cluster_b, flags = (int(tokens[i]) for i in range(4))
        except (IndexError, ValueError) as e:
            raise OptimizeError(
                f"{where}: expected 'numpoints clusterA clusterB flags': {e}"
            ) from e
        points, _ = _winding(tokens, 4, count, where)
        portals.append(
            {
                "index": n,
                "points": count,
                "clusters": [cluster_a, cluster_b],
                "flags": flags,
                "hint": bool(flags & 1),
                "sky": bool(flags & 2),
                "winding": points,
                "center": [sum(p[i] for p in points) / count for i in range(3)],
            }
        )
        for c in (cluster_a, cluster_b):
            per_cluster[c] = per_cluster.get(c, 0) + 1

    for n in range(declared_faces):
        if row >= len(lines):
            break
        tokens = _PAREN.sub(r" \1 ", lines[row]).split()
        row += 1
        if not tokens:
            continue
        where = f"{path.name}:{row} (solid face {n})"
        try:
            count, cluster = int(tokens[0]), int(tokens[1])
        except (IndexError, ValueError) as e:
            raise OptimizeError(f"{where}: expected 'numpoints cluster': {e}") from e
        points, _ = _winding(tokens, 2, count, where)
        faces.append({"index": n, "points": count, "cluster": cluster, "winding": points})

    return {
        "path": str(path),
        "magic": magic,
        "clusters": clusters,
        "declared_portals": declared_portals,
        "declared_solid_faces": declared_faces,
        "portals": portals,
        "solid_faces": faces,
        "complete": len(portals) == declared_portals and len(faces) == declared_faces,
        "portals_per_cluster": dict(sorted(per_cluster.items())),
        "hint_portals": sum(1 for p in portals if p["hint"]),
        "sky_portals": sum(1 for p in portals if p["sky"]),
        "format_source": (
            "netradiant-custom tools/quake3/q3map2/prtfile.cpp (writer) and vis.cpp LoadPortals "
            "(reader)"
        ),
    }


def _snap(value: float, grid: int) -> float:
    if grid <= 0:
        return value
    return float(round(value / grid) * grid)


def _propose_split(centers: list[list[float]], grid: int, min_spread: float) -> dict | None:
    """The axial plane that divides a cluster's portals most evenly.

    A hint brush works by forcing a BSP split, so the useful plane is the one that halves the
    portal set: the median of the portal centres along the axis with the most spread. Snapped
    to the grid because a hint brush a mapper cannot draw is not a suggestion.
    """
    if not centers:
        return None
    best: dict | None = None
    best_score = (0.0, 0.0)
    for axis in range(3):
        values = sorted(c[axis] for c in centers)
        spread = values[-1] - values[0]
        if spread < min_spread:
            continue
        position = _snap(values[len(values) // 2], grid)
        low = sum(1 for v in values if v < position)
        high = len(values) - low
        # A plane with everything on one side of it is not a split.
        if low == 0 or high == 0:
            continue
        score = (min(low, high) / len(values), spread)
        if best is None or score > best_score:
            best_score = score
            best = {
                "axis": "xyz"[axis],
                "position": position,
                "spread": spread,
                "balance": round(score[0], 3),
                "portals_each_side": [low, high],
            }
    return best


def hint_suggest(
    prt_path: str | Path,
    limit: int = 10,
    *,
    warn_portals: int = 48,
    grid: int = 8,
    min_spread: float = 64.0,
) -> dict[str, Any]:
    """Clusters with pathological portal counts, and a hint plane for each (§6.1).

    Two very different kinds of statement come out of here, and they are labelled as such.

    The portal counts are **measurements**, read from the file the compiler wrote. So is the
    ceiling they are compared against: `MAX_PORTALS_ON_LEAF` is 1024 (`q3map2.h:155`) and vis
    calls `Error("Leaf with too many portals")` on reaching it (`vis.cpp:784`, `:805`, `:846`),
    which means a cluster at or above that number is a `-vis` stage that will not finish.

    The hint planes are **proposals**. The predicted after-count assumes a hint plane splits a
    cluster's portal set at the median and adds one portal of its own; in reality portals whose
    winding straddles the plane are split in two, so the prediction is optimistic, and where
    the real BSP put its splits is not knowable from a `.prt`. Compile, `-saveprt` again, and
    compare — `compile_ab` records the before/after so the guess can be scored.

    `warn_portals` has no upstream basis and is not claimed to have one. Vis cost per cluster
    grows with the square of its portals, so there is no threshold at which a cluster becomes
    "bad"; 48 is a starting point that keeps the output to a readable shortlist on the maps
    tested. The one number here that *is* grounded is the 1024 ceiling above.
    """
    parsed = parse_portal_file(prt_path)
    per_cluster: dict[int, int] = parsed["portals_per_cluster"]
    findings: list[dict] = []

    if not parsed["complete"]:
        findings.append(
            _finding(
                "PRT_TRUNCATED",
                "warning",
                f"{Path(parsed['path']).name} declares {parsed['declared_portals']} portals and "
                f"{parsed['declared_solid_faces']} solid faces but only "
                f"{len(parsed['portals'])} and {len(parsed['solid_faces'])} are present; the "
                "compile that wrote it was probably interrupted",
                "verified",
                source="declared header counts versus records present",
                fix_hint="re-run the vis stage",
            )
        )

    by_size = sorted(per_cluster.items(), key=lambda kv: (-kv[1], kv[0]))
    worst = [{"cluster": c, "portals": n} for c, n in by_size[:limit]]

    over_limit = [(c, n) for c, n in by_size if n >= MAX_PORTALS_ON_LEAF]
    for cluster, n in over_limit[:limit]:
        findings.append(
            _finding(
                "VIS_LEAF_PORTAL_LIMIT",
                "error",
                f"cluster {cluster} has {n} portals, at or over the {MAX_PORTALS_ON_LEAF} vis "
                "allows per leaf; the -vis stage aborts with 'Leaf with too many portals' "
                "rather than producing a slow map",
                "verified",
                source="netradiant-custom tools/quake3/q3map2/vis.cpp LoadPortals, q3map2.h:155",
                fix_hint="split the cluster with hint brushes, or make its detail geometry detail",
                detail={"cluster": cluster, "portals": n, "limit": MAX_PORTALS_ON_LEAF},
            )
        )

    centers_by_cluster: dict[int, list[list[float]]] = {}
    for p in parsed["portals"]:
        for c in p["clusters"]:
            centers_by_cluster.setdefault(c, []).append(p["center"])

    proposals: list[dict] = []
    for cluster, n in by_size:
        # by_size descends, so the first cluster under the threshold is the last one worth
        # looking at.
        if n < warn_portals or len(proposals) >= limit:
            break
        split = _propose_split(centers_by_cluster.get(cluster, []), grid, min_spread)
        low, high = split["portals_each_side"] if split else (0, 0)
        after = max(low, high) + 1 if split else None
        proposals.append(
            {
                "cluster": cluster,
                "portals_before": n,
                "plane": split,
                "portals_after_estimate": after,
                "reduction_estimate": (n - after) if after is not None else None,
                "confidence": "unverified",
            }
        )
        if split is None:
            findings.append(
                _finding(
                    "HINT_NO_PROPOSAL",
                    "info",
                    f"cluster {cluster} carries {n} portals, but no axial plane through their "
                    f"median divides them — either their centres sit within {min_spread:g} units "
                    "on every axis, or the median leaves every portal on one side. The cluster is "
                    "probably shaped by detail geometry rather than by a missing split.",
                    "verified",
                    source="portal centres measured from the .prt",
                    detail={"cluster": cluster, "portals": n},
                )
            )
            continue
        findings.append(
            _finding(
                "HINT_SUGGESTED",
                "warning",
                f"cluster {cluster} carries {n} portals. A hint plane at {split['axis']} = "
                f"{split['position']:g} divides its portals {low}/{high}, which would leave "
                f"roughly {after} on the worse side. Estimate only: straddling portals get "
                "split, so the true figure needs a -vis run with -saveprt to compare.",
                "unverified",
                source="nrc_mcp.optimize hint heuristics",
                fix_hint=(
                    f"draw a brush textured with the game's hint shader spanning the cluster, "
                    f"its face on the {split['axis']} = {split['position']:g} plane"
                ),
                detail=proposals[-1],
            )
        )

    return {
        "prt": parsed["path"],
        "clusters": parsed["clusters"],
        "portals": len(parsed["portals"]),
        "solid_faces": len(parsed["solid_faces"]),
        "hint_portals_present": parsed["hint_portals"],
        "sky_portals": parsed["sky_portals"],
        "portal_limit_per_leaf": MAX_PORTALS_ON_LEAF,
        "max_points_on_winding": MAX_POINTS_ON_WINDING,
        "warn_portals": warn_portals,
        "worst_clusters": worst,
        "proposals": proposals,
        "findings": _sorted(findings),
        "summary": _summarize(findings),
        "notes": [
            "Portal counts are per cluster and count each file portal once for each of the two "
            "clusters it joins, which is how vis registers them (vis.cpp:769-812).",
            "Hint planes are proposals with an optimistic prediction. Score them with a "
            "-saveprt A/B before believing the numbers.",
        ],
    }


# ---------------------------------------------------------------------------
# §6.1 leak trace
# ---------------------------------------------------------------------------


def parse_pointfile(lin_path: str | Path) -> dict[str, Any]:
    """Parse a leak pointfile.

    Verified against `LeakFile` in `tools/quake3/q3map2/leakfile.cpp:58-118`: the file is
    written to `<source>.lin` (`:74`) as one `"%f %f %f\\n"` line per point (`:101`, `:109`),
    walking portal centres from the outside node inwards, and the **last** point is the
    `origin` of the entity that leaked (`:107`).
    """
    path = Path(lin_path)
    if not path.is_file():
        raise OptimizeError(
            f"{path} does not exist. A pointfile only appears when a compile leaked; a sealed "
            f"map produces none."
        )

    points: list[list[float]] = []
    for number, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 3:
            raise OptimizeError(
                f"{path}:{number}: a pointfile line is three floats "
                f"(leakfile.cpp writes '%f %f %f'); found {len(parts)} field(s): {line!r}"
            )
        try:
            points.append([float(v) for v in parts])
        except ValueError as e:
            raise OptimizeError(f"{path}:{number}: not three floats: {e}") from e

    if not points:
        raise OptimizeError(f"{path} contains no points")
    return {"path": str(path), "points": points, "count": len(points)}


def leak_trace(lin_path: str | Path, map_path: str | Path | None = None) -> dict[str, Any]:
    """The leak path as a polyline, and the entity it leaked from (§6.1).

    **The `.lin` file does not name the entity.** `leakfile.cpp:116` reports it over q3map2's
    XML feedback channel (`xml_Select("Entity leaked", …)`), not into the file; all the file
    carries is the entity's `origin` as its final point (`:107`). So the entity is identified
    here by matching that point against the origins in the source `.map`, and without a
    `map_path` the origin is returned unresolved rather than invented.

    The polyline runs from outside the map inwards, so its first point is where the leak
    escapes and its last is the entity that leaked — which is the direction to read it in when
    hunting for the hole.
    """
    parsed = parse_pointfile(lin_path)
    points = parsed["points"]
    findings: list[dict] = []

    length = sum(math.dist(points[i], points[i + 1]) for i in range(len(points) - 1))
    lo = [min(p[i] for p in points) for i in range(3)]
    hi = [max(p[i] for p in points) for i in range(3)]
    origin = points[-1]

    entity: dict[str, Any] | None = None
    if map_path is not None:
        mp = load_map(map_path)
        # An exact float match would be wrong: the writer prints %f, six decimals, so an
        # origin of 1/3 does not round-trip. Nearest-origin within a unit is unambiguous
        # because two entities at the same point would be a different bug.
        best: tuple[float, dict] | None = None
        for e in mp.entities(with_keys=True):
            o = e.get("origin")
            if not isinstance(o, (list, tuple)) or len(o) != 3:
                continue
            d = math.dist([float(v) for v in o], origin)
            if best is None or d < best[0]:
                best = (d, e)
        if best is not None and best[0] <= 1.0:
            entity = {
                "index": best[1]["index"],
                "classname": best[1].get("classname"),
                "origin": best[1].get("origin"),
                "distance": round(best[0], 6),
                "keys": best[1].get("keys"),
            }

    if entity is not None:
        findings.append(
            _finding(
                "LEAK_TRACED",
                "error",
                f"the map leaked from entity {entity['index']} ({entity['classname']}) at "
                f"{origin}; the leak path is {len(points)} point(s) and {length:.0f} units long, "
                "running from the hole in the hull inwards to that entity",
                "verified",
                source="q3map2 pointfile matched against the source .map by origin",
                fix_hint=(
                    "follow the polyline from its first point: the hull is open where the path "
                    "crosses it"
                ),
                detail={"entity": entity, "first_point": points[0]},
            )
        )
    else:
        findings.append(
            _finding(
                "LEAK_ENTITY_UNRESOLVED",
                "error",
                f"the map leaked. The pointfile's last point, {origin}, is the origin of the "
                "entity that leaked, but the pointfile does not record which entity that is"
                + (
                    " and no entity in the given .map sits within a unit of it"
                    if map_path is not None
                    else " — pass map_path to resolve it"
                ),
                "verified",
                source="netradiant-custom tools/quake3/q3map2/leakfile.cpp:101-116",
                fix_hint="seal the hull along the polyline; the leak is where the path crosses it",
                detail={"leaked_from_origin": origin, "first_point": points[0]},
            )
        )

    return {
        "lin": parsed["path"],
        "map": str(map_path) if map_path else None,
        "polyline": points,
        "point_count": len(points),
        "path_length": round(length, 3),
        "bounds": {"min": lo, "max": hi, "size": [hi[i] - lo[i] for i in range(3)]},
        "leaked_from_origin": origin,
        "entity": entity,
        "findings": findings,
        "summary": _summarize(findings),
        "notes": [
            "Read the polyline from its first point: that end is outside the map, and the hull "
            "is open wherever the path crosses it.",
            "The entity is not in the file. q3map2 reports it over its XML feedback channel "
            "only, so it is resolved here by matching the final point against .map origins.",
        ],
    }


# ---------------------------------------------------------------------------
# §6.3 shader audit
# ---------------------------------------------------------------------------


def _shader_tokens(text: str) -> list[tuple[int, str]]:
    """Tokenize a shader script the way the compiler does.

    Faithful to `GetToken` in `tools/quake3/common/scriplib.cpp:182-270`: whitespace splits
    tokens, `;` `#` and `//` start line comments, `/* */` is a block comment, `"` quotes a
    token, and a bare token also ends at `;`. Notably `{` and `}` are *not* punctuation to
    this tokenizer — they are separate tokens only because every shader file in existence puts
    them on their own line. Reproducing that limitation is the point: a file the compiler
    misreads should be misread here identically, or the audit describes a shader nobody else
    sees.

    One directive is deliberately not followed: `$include` (`scriplib.cpp:267`) pushes another
    script onto the stack, and following it would mean resolving game paths, which is not this
    module's business. No shader script in the installed game uses it. An included file's
    shaders therefore read as undefined, which surfaces as `SHADER_MISSING` rather than as
    silence.
    """
    tokens: list[tuple[int, str]] = []
    i = 0
    line = 1
    n = len(text)
    while i < n:
        c = text[i]
        if ord(c) <= 32:
            if c == "\n":
                line += 1
            i += 1
            continue
        if c in ";#" or text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text.startswith("/*", i):
            i += 2
            while i < n and not text.startswith("*/", i):
                if text[i] == "\n":
                    line += 1
                i += 1
            i += 2
            continue
        if c == '"':
            i += 1
            start = i
            while i < n and text[i] != '"':
                if text[i] == "\n":
                    line += 1
                i += 1
            tokens.append((line, text[start:i]))
            i += 1
            continue
        start = i
        while i < n and ord(text[i]) > 32 and text[i] != ";":
            i += 1
        tokens.append((line, text[start:i]))
    return tokens


def parse_shader_file(path: str | Path) -> list[dict[str, Any]]:
    """Every shader definition in one `.shader` script.

    Structure from `ParseShaderFile` (`tools/quake3/q3map2/shaders.cpp:809-1000`): a name, then
    a `{ … }` body, in which a nested `{ … }` is a stage and everything else is a directive.
    A `:q3map` suffix on the name is stripped and the shader is compiler-only (`:817-820`).
    """
    p = Path(path)
    tokens = _shader_tokens(p.read_text(errors="replace"))
    out: list[dict[str, Any]] = []
    i = 0

    while i < len(tokens):
        line, name = tokens[i]
        i += 1
        compiler_only = name.lower().endswith(":q3map")
        if compiler_only:
            name = name[: -len(":q3map")]
        if i >= len(tokens) or tokens[i][1] != "{":
            raise OptimizeError(
                f"{p}:{line}: shader {name!r} is not followed by '{{'. shaders.cpp:828 raises "
                f"the same error and stops loading the file."
            )
        i += 1

        body: list[str] = []
        stages: list[list[str]] = []
        surfaceparms: list[str] = []
        images: list[str] = []
        depth = 1
        while i < len(tokens) and depth:
            _, tok = tokens[i]
            i += 1
            if tok == "}":
                depth -= 1
                continue
            if tok == "{":
                stage: list[str] = []
                inner = 1
                while i < len(tokens) and inner:
                    _, st = tokens[i]
                    i += 1
                    if st == "}":
                        inner -= 1
                        continue
                    if st == "{":
                        inner += 1
                        continue
                    stage.append(st)
                stages.append(stage)
                for j, st in enumerate(stage):
                    low = st.lower()
                    if low in STAGE_IMAGE_DIRECTIVES and j + 1 < len(stage):
                        # animMap and clampAnimMap take a frequency before their frames.
                        offset = 2 if low in ("animmap", "clampanimmap") else 1
                        if j + offset < len(stage):
                            images.append(stage[j + offset])
                continue
            body.append(tok)

        for j, tok in enumerate(body):
            low = tok.lower()
            if low == "surfaceparm" and j + 1 < len(body):
                surfaceparms.append(body[j + 1].lower())
            elif low in BODY_IMAGE_DIRECTIVES and j + 1 < len(body):
                images.append(body[j + 1])

        out.append(
            {
                "name": name,
                "file": str(p),
                "line": line,
                "compiler_only": compiler_only,
                "directives": [t.lower() for t in body],
                "surfaceparms": surfaceparms,
                "stages": len(stages),
                "stage_tokens": stages,
                "images": images,
            }
        )
    return out


def _referenced_shaders(mp) -> tuple[dict[str, int], int]:
    """Shader names the map references, with a surface count each.

    Covers brush faces *and* patches. Patches used to be excluded because the binding exposed no
    way to reach a patch's shader, and skipping them misreported twice over: a patch-only shader
    looked unreferenced, and a patch's missing shader went unreported. `Map.patches()` closes that,
    so nothing here parses `.map` text.

    Names get the `textures/` prefix back, because that is what q3map2's own readers do
    (`map.cpp:1009` for faces, `patch.cpp:207` for patches) and a shader script is written with it.
    """
    counts: dict[str, int] = {}
    patches = 0
    for index, info in enumerate(mp.entities(with_keys=False)):
        for _, g in _brushes(mp, index, info):
            for shader in g.get("shaders") or []:
                if shader == EMPTY_SHADER:
                    continue
                key = SHADER_NAME_PREFIX + shader
                counts[key] = counts.get(key, 0) + 1
    for patch in mp.patches():
        patches += 1
        shader = patch.get("shader") or ""
        if shader and shader != EMPTY_SHADER:
            key = SHADER_NAME_PREFIX + shader
            counts[key] = counts.get(key, 0) + 1
    return counts, patches


def _draws_nothing(shader: dict) -> str | None:
    """Why a shader would draw nothing, or None if it might draw something."""
    if NODRAW_SURFACEPARM in shader["surfaceparms"]:
        return "nodraw"
    if shader["stages"] == 0 and not shader["images"]:
        return "no_stages"
    return None


def shader_audit(
    map_path: str | Path,
    shader_dirs: list[str | Path],
    profile_id: str | None = None,
) -> dict[str, Any]:
    """Missing, unused, shadowing and non-drawing shaders (§6.3).

    Four reports, and the difference between them matters:

    `SHADER_MISSING`
        The map references a name that none of `shader_dirs` defines. Stated as a measurement
        over the directories given, not as a claim that the shader does not exist — a bare
        texture image with no shader block is perfectly legal, and the base game's own scripts
        are only in scope if they were passed in.

    `SHADER_UNREFERENCED`
        Defined but never named by this map. Informational: a shared script legitimately holds
        shaders for other maps.

    `SHADER_SHADOWS_BASEGAME`
        Defined under a path the base game owns. The reserved prefixes come from the profile's
        `packaging.reserved_shader_prefixes` — this module has no idea which game it is serving
        — and inherit the profile's own confidence, so an unverified prefix list cannot produce
        a hard failure. Shipping such a shader overrides it for every other map on the server,
        which is the classic "works for me, broken on the server" bug.

        `shader_dirs` should therefore be **the scripts this map ships**, not the game's own.
        Pointed at the base game's script directory this check fires on every base shader,
        correctly but uselessly: those files are the originals, not copies shadowing them.

    `SHADER_DRAWS_NOTHING`
        The watercaulk trap. A shader the map applies to surfaces that the compiler will emit
        no draw surface for, because it carries `surfaceparm nodraw` (`games.cpp:148` maps that
        to `C_NODRAW`) — verified. Or one with no stages and no image directive at all, which is
        reported separately and unverified, because whether the *engine* then falls back to an
        implicit image was not confirmed against engine source.

    Covers brush faces and patches both. Patches were once out of reach — the binding exposed no
    patch shader — and skipping them skewed two of the four reports: a patch-only shader read as
    unreferenced, and a patch's missing shader went unreported. `Map.patches()` closed that, so the
    counts here are complete without anything re-parsing the `.map`.
    """
    mp = load_map(map_path)
    referenced, patches = _referenced_shaders(mp)

    definitions: dict[str, dict] = {}
    duplicates: dict[str, list[str]] = {}
    files: list[str] = []
    parse_errors: list[str] = []

    for d in shader_dirs:
        root = Path(d)
        if not root.exists():
            parse_errors.append(f"{root} does not exist")
            continue
        candidates = sorted(root.rglob("*.shader")) if root.is_dir() else [root]
        for f in candidates:
            files.append(str(f))
            try:
                parsed = parse_shader_file(f)
            except (OptimizeError, OSError) as e:
                parse_errors.append(str(e))
                continue
            for shader in parsed:
                name = shader["name"]
                if name in definitions:
                    duplicates.setdefault(name, [definitions[name]["file"]]).append(shader["file"])
                    continue
                definitions[name] = shader

    reserved, reserved_confidence = _reserved_prefixes(profile_id)
    findings: list[dict] = []

    missing = sorted(n for n in referenced if n not in definitions)
    for name in missing:
        findings.append(
            _finding(
                "SHADER_MISSING",
                "warning",
                f"{referenced[name]} surface(s) reference {name}, which none of the "
                f"{len(files)} shader script(s) scanned define. That is legal if a plain texture "
                "image of that name exists, or if the definition lives in a script outside the "
                "directories given.",
                "verified",
                source=f"map references versus {len(files)} shader script(s)",
                fix_hint="add the shader, fix the name, or pass the directory that defines it",
                detail={"shader": name, "surfaces": referenced[name]},
            )
        )

    unused = sorted(n for n in definitions if n not in referenced)
    if unused:
        findings.append(
            _finding(
                "SHADER_UNREFERENCED",
                "info",
                f"{len(unused)} shader(s) are defined but not referenced by this map "
                f"(for example {', '.join(unused[:5])}). Expected for a shared script; worth "
                "trimming for a map-specific one, since -repack strips only what the BSP uses.",
                "verified",
                source="shader definitions versus map references",
                detail={"shaders": unused[:200], "count": len(unused)},
            )
        )

    shadowing = sorted(
        n
        for n in definitions
        if any(n.lower().startswith(f"{SHADER_NAME_PREFIX}{p}") for p in reserved)
    )
    for name in shadowing:
        findings.append(
            _finding(
                "SHADER_SHADOWS_BASEGAME",
                "error",
                f"{name} is defined in {Path(definitions[name]['file']).name} but sits under a "
                "path the base game owns; shipping it overrides that shader for every other map "
                "on the server",
                reserved_confidence,
                source=f"profile {profile_id} packaging.reserved_shader_prefixes",
                fix_hint="move the shader under a directory named for this map",
                detail={"shader": name, "file": definitions[name]["file"]},
            )
        )

    for name in sorted(referenced):
        shader = definitions.get(name)
        if shader is None:
            continue
        reason = _draws_nothing(shader)
        if reason is None:
            continue
        if reason == "nodraw":
            findings.append(
                _finding(
                    "SHADER_DRAWS_NOTHING",
                    "warning",
                    f"{referenced[name]} surface(s) use {name}, which declares "
                    f"`surfaceparm {NODRAW_SURFACEPARM}` — the compiler emits no draw surface "
                    "for it, so anywhere it is visible there will be a hole you can see through",
                    "verified",
                    source="netradiant-custom tools/quake3/q3map2/games.cpp:148 (nodraw -> C_NODRAW)",
                    fix_hint=(
                        "use this shader only where the surface is meant to be invisible; give "
                        "visible faces a drawing shader"
                    ),
                    detail={
                        "shader": name,
                        "surfaces": referenced[name],
                        "reason": reason,
                        "file": shader["file"],
                    },
                )
            )
        else:
            findings.append(
                _finding(
                    "SHADER_NO_STAGES",
                    "warning",
                    f"{referenced[name]} surface(s) use {name}, whose definition has no stages "
                    "and names no image. The compiler finds nothing to draw with; whether the "
                    "engine falls back to an image of the same name was not verified here.",
                    "unverified",
                    source=f"{shader['file']}:{shader['line']}",
                    fix_hint="add a stage with a map directive, or a qer_editorimage",
                    detail={
                        "shader": name,
                        "surfaces": referenced[name],
                        "reason": reason,
                        "file": shader["file"],
                    },
                )
            )

    for name, where in sorted(duplicates.items()):
        findings.append(
            _finding(
                "SHADER_DUPLICATE_DEFINITION",
                "info",
                f"{name} is defined {len(where)} times ({', '.join(Path(w).name for w in where)}); "
                "which definition wins depends on script load order, which was not verified",
                "unverified",
                source="duplicate names across the scanned scripts",
                fix_hint="keep one definition",
                detail={"shader": name, "files": where},
            )
        )

    for message in parse_errors:
        findings.append(
            _finding(
                "SHADER_SCRIPT_UNREADABLE",
                "warning",
                f"a shader script could not be read, so anything it defines counts as missing: "
                f"{message}",
                "verified",
                source="nrc_mcp.optimize shader parser",
            )
        )

    return {
        "map": str(map_path),
        "profile": profile_id,
        "shader_dirs": [str(d) for d in shader_dirs],
        "scripts_scanned": files,
        "definitions": len(definitions),
        "referenced": len(referenced),
        "reference_counts": dict(sorted(referenced.items())),
        "missing": missing,
        "unreferenced": unused,
        "shadowing_basegame": shadowing,
        "reserved_prefixes": sorted(reserved),
        "findings": _sorted(findings),
        "summary": _summarize(findings),
        "patches_not_scanned": patches,  # kept for compatibility; patches ARE scanned now
        "notes": [
            "Shader names are compared with the textures/ prefix q3map2's readers add "
            "(map.cpp:1009, patch.cpp:207); the .map stores them without it.",
            "'Missing' means 'not defined in the directories scanned', which is a measurement, "
            "not proof the shader does not exist.",
            "References are counted from brush faces only; the binding exposes no patch shader.",
        ],
    }


def _reserved_prefixes(profile_id: str | None) -> tuple[set[str], str]:
    """The base game's own shader paths, from the profile.

    In the profile because which paths a game reserves is a property of the game (§7.4). The
    profile's own confidence rides along, so an unverified prefix list cannot produce an error.
    """
    if not profile_id:
        return set(), "unverified"
    try:
        pkg = profiles.load(profile_id).get("packaging")
    except profiles.ProfileError:
        return set(), "unverified"
    if not isinstance(pkg, dict):
        return set(), "unverified"
    prefixes = pkg.get("reserved_shader_prefixes")
    if not isinstance(prefixes, list):
        return set(), "unverified"
    confidence = str(pkg.get("confidence", "unverified"))
    return {str(p).lower().strip("/") + "/" for p in prefixes}, confidence


# ---------------------------------------------------------------------------
# §6.1 A/B benchmarking
# ---------------------------------------------------------------------------


def _last_json_object(text: str) -> dict | None:
    """The last top-level JSON object in a command's output.

    `tools/q3map2.py` prints its result with `json.dumps(indent=2)`, so the object opens on a
    line that is exactly `{`. Scanning candidates from the end and keeping the first that
    decodes survives q3map2's own chatter above it, and survives `kernel.run_mise_task`
    trimming the head of a long stdout.
    """
    starts = [m.start() for m in re.finditer(r"(?m)^\{$", text)]
    decoder = json.JSONDecoder()
    for start in reversed(starts):
        try:
            value, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _compile_variant(map_path: Path, preset: str) -> dict[str, Any]:
    """Compile one variant and unpack its BSP, both through mise tasks."""
    run = run_mise_task(f"compile:{preset}", [str(map_path)])
    result = _last_json_object(run["stdout"])
    if result is None:
        raise OptimizeError(
            f"`{run['command']}` produced no parseable result for {map_path.name} "
            f"(exit {run['returncode']}). Output tail:\n{run['stdout'][-1500:] or run['stderr'][-1500:]}"
        )

    stages = {
        " ".join(s.get("flags") or []): s.get("seconds")
        for s in result.get("stages") or []
        if isinstance(s, dict)
    }
    artifacts = [Path(a) for a in result.get("artifacts") or []]
    bsp_file = next((a for a in artifacts if a.suffix.lower() == ".bsp"), None)

    metrics: dict[str, Any] = {
        "map": str(map_path),
        "ok": bool(result.get("ok")),
        "stage_seconds": stages,
        "total_seconds": result.get("total_seconds"),
        "bsp": str(bsp_file) if bsp_file else None,
        "bsp_bytes": bsp_file.stat().st_size if bsp_file and bsp_file.is_file() else None,
        "artifacts": [str(a) for a in artifacts],
        "prt": next((str(a) for a in artifacts if a.suffix.lower() == ".prt"), None),
    }
    if not metrics["ok"] or bsp_file is None:
        metrics["error"] = "the compile did not produce a .bsp"
        return metrics

    unpack = run_mise_task("bsp:json-unpack", [str(bsp_file)])
    unpacked = _last_json_object(unpack["stdout"])
    lumps_dir = None
    if unpacked:
        for a in unpacked.get("artifacts") or []:
            if Path(a).is_dir():
                lumps_dir = Path(a)
    if lumps_dir is None:
        metrics["error"] = "the BSP compiled but could not be unpacked to JSON lumps"
        return metrics

    report = bsp.report(lumps_dir)
    counts = report["counts"]
    metrics["lumps"] = str(lumps_dir)
    metrics["counts"] = {
        key: counts.get(key)
        for key in (
            "draw_surfaces",
            "draw_verts",
            "leafs",
            "nodes",
            "brushes",
            "planes",
            "shaders",
            "vis_clusters",
            "lighting_bytes",
            "vis_bytes",
        )
    }
    return metrics


def _delta(a: Any, b: Any) -> dict[str, Any] | None:
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return None
    change = b - a
    return {
        "a": a,
        "b": b,
        "change": round(change, 3),
        "percent": round(change / a * 100.0, 2) if a else None,
    }


def compile_ab(
    map_a: str | Path,
    map_b: str | Path,
    preset: str = "draft",
    *,
    history_path: str | Path | None = None,
) -> dict[str, Any]:
    """Compile two variants and diff the numbers that matter (§6.1).

    Both compiles and both unpacks go through mise tasks, so every step is a line a human can
    paste into a shell and reproduce — §1.2's rule, and the reason this does not shell out to
    `q3map2` directly.

    The diff covers what §6.1 asks for and can be obtained: per-stage wall time, draw surface
    count, leaf count, brush count, plane count, vis cluster count, lightmap bytes and BSP
    size. A row goes to `bench/ab-history.jsonl` every time, because the value of an A/B is
    cumulative — one comparison tells you about one change, and a history tells you when the
    map started getting slower.

    Wall time is the softest number here: it is one sample on a loaded machine, so read a 5%
    difference as noise and a 2x difference as real.

    The two maps must have different stems. `tools/q3map2.py` keys its output directory on the
    stem, so two variants sharing a name would silently overwrite each other's artifacts and
    the diff would compare a BSP against itself.
    """
    if preset not in COMPILE_PRESETS:
        raise OptimizeError(f"preset must be one of {', '.join(COMPILE_PRESETS)} — got {preset!r}")

    a, b = Path(map_a), Path(map_b)
    for p in (a, b):
        if not p.is_file():
            raise OptimizeError(f"{p} does not exist")
    if a.stem == b.stem:
        raise OptimizeError(
            f"both variants are named {a.stem!r}; compile artifacts are keyed on the stem, so "
            "the second compile would overwrite the first. Rename one variant."
        )

    left = _compile_variant(a, preset)
    right = _compile_variant(b, preset)

    deltas: dict[str, Any] = {}
    for key in ("total_seconds", "bsp_bytes"):
        d = _delta(left.get(key), right.get(key))
        if d:
            deltas[key] = d
    for key in sorted(set(left.get("counts") or {}) | set(right.get("counts") or {})):
        d = _delta((left.get("counts") or {}).get(key), (right.get("counts") or {}).get(key))
        if d:
            deltas[key] = d

    stage_deltas = {}
    for flags in sorted(set(left["stage_seconds"]) | set(right["stage_seconds"])):
        d = _delta(left["stage_seconds"].get(flags), right["stage_seconds"].get(flags))
        if d:
            stage_deltas[flags] = d

    findings: list[dict] = []
    for side, metrics in (("a", left), ("b", right)):
        if metrics.get("error"):
            findings.append(
                _finding(
                    "AB_VARIANT_INCOMPLETE",
                    "error",
                    f"variant {side} ({Path(metrics['map']).name}) did not produce comparable "
                    f"numbers: {metrics['error']}",
                    "verified",
                    source=f"mise run compile:{preset}",
                    fix_hint="fix the compile before reading the diff",
                )
            )

    for key in ("draw_surfaces", "leafs", "vis_clusters"):
        d = deltas.get(key)
        if d and d["percent"] is not None and abs(d["percent"]) >= 1.0:
            findings.append(
                _finding(
                    "AB_GEOMETRY_MOVED",
                    "info",
                    f"{key} went from {d['a']} to {d['b']} ({d['percent']:+.2f}%)",
                    "verified",
                    source="q3map2 -json lump counts",
                    detail={"metric": key, **d},
                )
            )

    row = {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "preset": preset,
        "a": {"map": str(a), **{k: left.get(k) for k in ("total_seconds", "bsp_bytes", "counts")}},
        "b": {"map": str(b), **{k: right.get(k) for k in ("total_seconds", "bsp_bytes", "counts")}},
        "deltas": deltas,
    }
    history = Path(history_path) if history_path else repo_root() / AB_HISTORY_RELATIVE
    history.parent.mkdir(parents=True, exist_ok=True)
    with history.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")

    return {
        "preset": preset,
        "a": left,
        "b": right,
        "stage_seconds_delta": stage_deltas,
        "deltas": deltas,
        "history": str(history),
        "history_rows": sum(1 for _ in history.open(encoding="utf-8")),
        "findings": _sorted(findings),
        "summary": _summarize(findings),
        "notes": [
            "Wall time is one sample per stage on whatever else the machine was doing; treat "
            "small differences as noise.",
            f"Every run appends to {history}, so a slow drift over a project's life stays "
            "visible instead of being compared only against the previous build.",
        ],
    }


def ab_history(history_path: str | Path | None = None) -> list[dict]:
    """Every A/B row recorded so far, oldest first."""
    history = Path(history_path) if history_path else repo_root() / AB_HISTORY_RELATIVE
    if not history.is_file():
        return []
    rows = []
    for line in history.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows
