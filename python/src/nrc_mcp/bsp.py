"""BSP introspection from `q3map2 -json` (§6.2).

`-json` unpacks a compiled BSP into a directory of per-lump JSON files, so the whole BSP is
readable without writing a lump parser. This module reads that directory.

# A correction to §6.2

The spec asks for "headroom against every `MAX_MAP_*` limit, as percentages". Most of those
limits **no longer exist in this compiler**: the Quake 3 lumps are `std::vector` and the
static ceilings on brushes, planes, shaders, draw surfaces and draw verts were removed
upstream. Only six remain in `tools/quake3/q3map2/q3map2.h`, and those are the ones reported
as `compiler_limits` here, each cited.

That is not the whole story, though, and the difference matters for shipping: **the game
engine still has limits the compiler no longer enforces.** A map can compile cleanly and
still fail to load. Those ceilings are a property of the engine, not of this tool, so they
live in the game profile under `engine_limits` and are reported separately and marked with
their confidence. Anything unverified is advisory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import profiles

#: Limits that still exist in this fork's compiler, read from
#: `tools/quake3/q3map2/q3map2.h`. Verified by reading the header, not from memory.
COMPILER_LIMITS: dict[str, int] = {
    "areas": 0x100,
    "leafs": 0x20000,
    "portals": 0x20000,
    "lighting_bytes": 0x800000,
    "lightgrid_bytes": 0x100000,
    "visclusters": 0x4000,
}

COMPILER_LIMIT_SOURCE = "netradiant-custom tools/quake3/q3map2/q3map2.h (MAX_MAP_* defines)"

#: Lumps whose absence is worth explaining rather than silently reporting zero.
LUMP_FILES = (
    "shaders.json",
    "planes.json",
    "nodes.json",
    "leafs.json",
    "LeafSurfaces.json",
    "LeafBrushes.json",
    "models.json",
    "Brushes.json",
    "BrushSides.json",
    "DrawVert.json",
    "DrawSurfaces.json",
    "DrawIndexes.json",
    "fogs.json",
    "entities.json",
    "VisBytes.json",
    "LightBytes.json",
    "GridPoints.json",
)

#: `surfaceType` values in a Quake 3 BSP draw surface.
SURFACE_TYPES = {
    0: "bad",
    1: "planar",
    2: "patch",
    3: "triangle_soup",
    4: "flare",
    5: "foliage",
}


class BspError(RuntimeError):
    pass


@dataclass
class Lumps:
    """The unpacked lump directory."""

    path: Path
    data: dict[str, Any]

    def get(self, name: str) -> Any:
        return self.data.get(name)

    def count(self, name: str) -> int:
        v = self.data.get(name)
        return len(v) if isinstance(v, (dict, list)) else 0

    def rows(self, name: str) -> list[dict]:
        """A lump's entries as a list, dropping the `Thing#N` keys.

        q3map2 writes each lump as an object keyed by index, which preserves ordering in the
        file but is awkward to iterate. Sorting numerically rather than lexically matters:
        `#10` must not come before `#9`.
        """
        v = self.data.get(name)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
        if not isinstance(v, dict):
            return []

        def key(k: str) -> int:
            try:
                return int(k.rsplit("#", 1)[1])
            except (IndexError, ValueError):
                return 0

        return [v[k] for k in sorted(v, key=key) if isinstance(v[k], dict)]


def load_lumps(path: str | Path) -> Lumps:
    """Read an unpacked BSP directory."""
    p = Path(path)
    if p.is_file() and p.suffix.lower() == ".bsp":
        # A caller who has the .bsp probably means the directory beside it, which is where
        # `-json` puts the lumps.
        p = p.with_suffix("")
    if not p.is_dir():
        raise BspError(
            f"{p} is not an unpacked BSP directory. Produce one with "
            f"`mise run bsp:json-unpack <file.bsp>`, which writes the lumps beside the BSP."
        )

    data: dict[str, Any] = {}
    for name in LUMP_FILES:
        f = p / name
        if not f.is_file():
            continue
        try:
            data[name] = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise BspError(f"{f} is unreadable: {e}") from e

    if not data:
        raise BspError(f"{p} contains no recognizable BSP lumps")
    return Lumps(path=p, data=data)


def _byte_len(v: Any) -> int:
    """Length of a byte-array lump, whatever shape it was written in."""
    if isinstance(v, (list, str)):
        return len(v)
    if isinstance(v, dict):
        return len(v)
    return 0


def _headroom(used: int, limit: int) -> dict[str, Any]:
    pct = (used / limit * 100.0) if limit else 0.0
    return {
        "used": used,
        "limit": limit,
        "percent": round(pct, 2),
        "remaining": max(0, limit - used),
        "over": used > limit,
    }


def report(path: str | Path, profile_id: str | None = None) -> dict[str, Any]:
    """A structured report on a compiled BSP."""
    lumps = load_lumps(path)

    shaders = lumps.rows("shaders.json")
    shader_names = [str(s.get("shader", "")) for s in shaders]
    surfaces = lumps.rows("DrawSurfaces.json")
    leafs = lumps.rows("leafs.json")
    models = lumps.rows("models.json")

    # Surfaces and triangles per shader: the histogram that finds what is eating draw calls.
    by_shader: dict[str, dict[str, int]] = {}
    by_type: dict[str, int] = {}
    lightmapped = 0
    for s in surfaces:
        idx = s.get("shaderNum")
        name = (
            shader_names[idx]
            if isinstance(idx, int) and 0 <= idx < len(shader_names)
            else "<out of range>"
        )
        entry = by_shader.setdefault(name, {"surfaces": 0, "verts": 0, "indexes": 0})
        entry["surfaces"] += 1
        entry["verts"] += int(s.get("numVerts") or 0)
        entry["indexes"] += int(s.get("numIndexes") or 0)

        st = s.get("surfaceType")
        by_type[SURFACE_TYPES.get(st, f"unknown_{st}")] = (
            by_type.get(SURFACE_TYPES.get(st, f"unknown_{st}"), 0) + 1
        )
        lm = s.get("lightmapNum")
        if isinstance(lm, list) and any(isinstance(x, int) and x >= 0 for x in lm):
            lightmapped += 1

    top_shaders = sorted(
        ({"shader": k, **v} for k, v in by_shader.items()),
        key=lambda r: (-r["surfaces"], r["shader"]),
    )

    clusters = {leaf.get("cluster") for leaf in leafs if isinstance(leaf.get("cluster"), int)}
    real_clusters = sorted(c for c in clusters if c >= 0)

    counts = {
        "shaders": len(shaders),
        "planes": lumps.count("planes.json"),
        "nodes": lumps.count("nodes.json"),
        "leafs": len(leafs),
        "leaf_surfaces": lumps.count("LeafSurfaces.json"),
        "leaf_brushes": lumps.count("LeafBrushes.json"),
        "models": len(models),
        "brushes": lumps.count("Brushes.json"),
        "brush_sides": lumps.count("BrushSides.json"),
        "draw_verts": lumps.count("DrawVert.json"),
        "draw_surfaces": len(surfaces),
        "draw_indexes": lumps.count("DrawIndexes.json"),
        "fogs": lumps.count("fogs.json"),
        "vis_clusters": len(real_clusters),
        "lighting_bytes": _byte_len(lumps.get("LightBytes.json")),
        "lightgrid_points": lumps.count("GridPoints.json"),
        "vis_bytes": _byte_len(lumps.get("VisBytes.json")),
    }

    compiler = {
        "areas": _headroom(
            len({leaf.get("area") for leaf in leafs if isinstance(leaf.get("area"), int)}),
            COMPILER_LIMITS["areas"],
        ),
        "leafs": _headroom(counts["leafs"], COMPILER_LIMITS["leafs"]),
        "visclusters": _headroom(counts["vis_clusters"], COMPILER_LIMITS["visclusters"]),
        "lighting_bytes": _headroom(counts["lighting_bytes"], COMPILER_LIMITS["lighting_bytes"]),
    }

    out: dict[str, Any] = {
        "path": str(lumps.path),
        "lumps_present": sorted(lumps.data),
        "lumps_missing": [n for n in LUMP_FILES if n not in lumps.data],
        "counts": counts,
        "surface_types": by_type,
        "lightmapped_surfaces": lightmapped,
        "vertex_lit_surfaces": len(surfaces) - lightmapped,
        "top_shaders": top_shaders[:25],
        "shader_count_unreferenced": sum(1 for n in shader_names if n not in by_shader),
        "unreferenced_shaders": sorted(n for n in shader_names if n not in by_shader)[:25],
        "compiler_limits": compiler,
        "compiler_limit_source": COMPILER_LIMIT_SOURCE,
        "notes": [
            "This compiler removed the static Quake 3 ceilings on brushes, planes, shaders, "
            "draw surfaces and draw verts — those lumps are dynamically sized, so there is no "
            "compiler headroom to report for them. Only the limits above still exist in "
            "q3map2.h.",
        ],
    }

    if models:
        m = models[0]
        mm = m.get("minmax") or {}
        out["worldspawn_model"] = {
            "mins": mm.get("mins"),
            "maxs": mm.get("maxs"),
            "surfaces": m.get("numBSPSurfaces"),
            "brushes": m.get("numBSPBrushes"),
        }
        out["submodels"] = len(models) - 1

    engine = _engine_limits(profile_id, counts) if profile_id else None
    if engine:
        out["engine_limits"] = engine
        out["notes"].append(
            "Engine limits come from the game profile and are what decide whether the map "
            "loads at all. A map can compile cleanly here and still be rejected in game."
        )
    elif profile_id:
        out["notes"].append(
            f"profile {profile_id} states no engine_limits, so only compiler headroom is "
            "reported; a clean compile does not by itself prove the map will load."
        )
    return out


def _engine_limits(profile_id: str, counts: dict[str, int]) -> dict[str, Any] | None:
    """Headroom against the engine's own ceilings, read from the profile.

    In the profile because they are a property of the game, not of this tool — the §7.4 seam
    again. Each entry carries its own confidence, and an unverified ceiling is reported as
    advisory rather than as a limit that has been exceeded.
    """
    try:
        data = profiles.load(profile_id)
    except profiles.ProfileError:
        return None
    limits = data.get("engine_limits")
    if not isinstance(limits, dict):
        return None

    out: dict[str, Any] = {}
    for name, spec in limits.items():
        if not isinstance(spec, dict):
            continue
        limit = spec.get("value")
        if not isinstance(limit, int) or limit <= 0:
            continue
        used = counts.get(str(spec.get("counts", name)))
        if used is None:
            continue
        entry = _headroom(used, limit)
        entry["confidence"] = str(spec.get("confidence", "unverified"))
        entry["source"] = str(spec.get("source", f"profile {profile_id}"))
        if entry["over"] and entry["confidence"] != "verified":
            entry["advisory"] = (
                "this ceiling is unverified, so treat exceeding it as a warning to check "
                "rather than a certainty"
            )
        out[name] = entry
    return out


def entity_diff(lumps_path: str | Path, source_map: str | Path) -> dict[str, Any]:
    """Compare the BSP's entity lump against the source `.map`.

    §6.2 asks for this because entities can be silently dropped at compile time — the editor's
    own writer discards empty group entities, and q3map2 drops entities it cannot place. A
    count that quietly changed is exactly the bug this catches.
    """
    from .kernel import load_map

    lumps = load_lumps(lumps_path)
    ents = lumps.rows("entities.json")
    if not ents:
        raw = lumps.get("entities.json")
        if isinstance(raw, list):
            ents = [e for e in raw if isinstance(e, dict)]

    def classname(d: dict) -> str:
        for k in ("classname", "Classname"):
            if k in d:
                return str(d[k])
        return ""

    bsp_counts: dict[str, int] = {}
    for e in ents:
        bsp_counts[classname(e) or "<none>"] = bsp_counts.get(classname(e) or "<none>", 0) + 1

    src = load_map(source_map)
    map_counts: dict[str, int] = {}
    for e in src.entities(with_keys=False):
        cn = e.get("classname") or "<none>"
        map_counts[cn] = map_counts.get(cn, 0) + 1

    names = sorted(set(bsp_counts) | set(map_counts))
    dropped = {}
    added = {}
    for n in names:
        a, b = map_counts.get(n, 0), bsp_counts.get(n, 0)
        if b < a:
            dropped[n] = a - b
        elif b > a:
            added[n] = b - a

    return {
        "map_entities": sum(map_counts.values()),
        "bsp_entities": sum(bsp_counts.values()),
        "dropped": dropped,
        "added": added,
        "identical": not dropped and not added,
        "note": (
            "worldspawn is expected to appear in both. Dropped entities are worth "
            "investigating: the compiler discards what it cannot place, silently."
        ),
    }
