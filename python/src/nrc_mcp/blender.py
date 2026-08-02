"""The Blender handoff (§5).

`nrc-mcp` never does mesh authoring. It **specifies, validates and integrates** — Blender
models. The contribution this module makes is that the specification is *numerically complete*,
so the prompt sent to a Blender agent is parametric rather than vibes-based, and so the import
check has something concrete to check against.

Everything game-specific — the unit scale, the model entity's key names, the clip shader names,
the budgets — comes from the profile's `assets` section. §7.4 names "`1 quake unit ≈ 1 inch`
baked into the Blender brief generator" as one of the five predicted seam leaks, so that
constant in particular is data.

# The failure this is built around

§5.3 says wrong-scale imports "are the #1 failure and are trivially detectable". A mesh modelled
in metres and exported without unit conversion arrives 39.37× too small; one modelled in units
and exported *with* conversion arrives 39.37× too large. `model_import` therefore checks bounds
against the brief first and names the likely cause, because "your crate is 2 units wide" is much
less useful than "this looks like the metres/inches mistake, multiply by 39.37".

# Security

§5.1: both Blender MCP servers execute arbitrary Python inside Blender, and blender.org warns
that this runs LLM-generated code without guards. This module never executes anything — it emits
a brief and reads files back. Sandboxing the Blender side is the caller's responsibility and
`blender_brief` says so in the prompt it returns.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import profiles


class AssetError(RuntimeError):
    pass


def _assets(profile_id: str) -> dict[str, Any]:
    data = profiles.load(profile_id)
    a = data.get("assets")
    if not isinstance(a, dict):
        raise AssetError(
            f"profile {profile_id} has no `assets` section, so the unit scale, model entity and "
            f"clip shader names are unknown. Add one rather than assuming defaults — those are "
            f"game-specific values."
        )
    return a


# ---------------------------------------------------------------------------
# The brief
# ---------------------------------------------------------------------------


def blender_brief(
    profile_id: str,
    asset_id: str,
    purpose: str,
    bounds: dict[str, list[float]],
    *,
    collision: str = "brush_hull",
    materials: list[dict] | None = None,
    triangles: int | None = None,
    texel_density: float = 2.0,
    silhouette_notes: str = "",
    fmt: str | None = None,
) -> dict[str, Any]:
    """A numerically complete asset brief, plus a ready-to-send prompt (§5.2).

    `bounds` is `{"x": [lo, hi], "y": [...], "z": [...]}` in world units — the volume the asset
    must fit inside, which is normally the brush it replaces.

    The returned `prompt` embeds every number plus the standing rules, so the Blender side has
    nothing left to infer. That is the whole value: a brief that says "make it crate-sized" gets
    a crate of the wrong size.
    """
    a = _assets(profile_id)
    units = a.get("units", {})
    upm = units.get("units_per_metre")
    if not isinstance(upm, (int, float)):
        raise AssetError(f"profile {profile_id} states no units_per_metre in assets.units")

    for axis in ("x", "y", "z"):
        pair = bounds.get(axis)
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            raise AssetError(f"bounds must give [lo, hi] for {axis}")
        if pair[1] <= pair[0]:
            raise AssetError(f"bounds for {axis} must be increasing, got {pair}")

    size = {ax: bounds[ax][1] - bounds[ax][0] for ax in ("x", "y", "z")}
    budgets = a.get("budgets", {})
    tris = triangles or budgets.get("prop_triangles")
    mats = materials or []
    export_format = fmt or a.get("export", {}).get("default_format", "obj")

    clip = a.get("clip_shaders", {})
    if collision not in ("nonsolid", "autoclip", "brush_hull"):
        raise AssetError(f"collision must be nonsolid, autoclip or brush_hull — got {collision!r}")

    brief = {
        "asset_id": asset_id,
        "purpose": purpose,
        "profile": profile_id,
        "units": {
            "quake_units_per_metre": upm,
            "up_axis": units.get("up_axis", "z"),
            "forward_axis": units.get("forward_axis", "x"),
            "note": (
                f"1 Quake unit is 1 inch. Either model at 1 Blender unit = 1 Quake unit and "
                f"disable unit scaling on export, or model in metres and export with scale "
                f"{upm}. State which and be consistent — mixing them is the single commonest "
                f"import failure."
            ),
        },
        "bounds_qu": bounds,
        "size_qu": size,
        "origin": (
            f"min-corner at local (0,0,0), +{units.get('up_axis', 'z').upper()} up, "
            f"+{units.get('forward_axis', 'x').upper()} = model forward"
        ),
        "budget": {
            "triangles": tris,
            "materials": mats and len(mats) or budgets.get("prop_materials"),
            "budget_confidence": budgets.get("confidence", "unverified"),
        },
        "materials": mats,
        "uv": {
            "channel": 0,
            "required": True,
            "texel_density_px_per_qu": texel_density,
            "overlap_allowed": False,
        },
        "export": {
            "format": export_format,
            "axis_up": units.get("up_axis", "z").upper(),
            "axis_forward": units.get("forward_axis", "x").upper(),
            "path": f"assets/models/{asset_id}.{export_format}",
        },
        "collision": {
            "strategy": collision,
            "shader": clip.get("player") if collision == "brush_hull" else None,
            "reason": _collision_reason(collision, purpose),
            "hull_will_be_generated_by": "nrc-mcp:model_make_clip"
            if collision == "brush_hull"
            else None,
        },
        "silhouette_notes": silhouette_notes,
    }
    brief["prompt"] = _prompt_for(brief)
    return brief


def _collision_reason(strategy: str, purpose: str) -> str:
    if strategy == "brush_hull":
        return (
            f"{purpose}: a convex hull gives snappy, predictable collision. Autoclip is much "
            f"improved in this compiler but still does not match what players expect from a "
            f"competitive shooter, and predictable beats accurate for anything a player can "
            f"peek, slide along or take cover behind."
        )
    if strategy == "autoclip":
        return (
            f"{purpose}: large or organic enough that a hull would be a poor fit; use one of the "
            f"compiler's autoclip modes and verify with -debugclip."
        )
    return f"{purpose}: pure ornament off the playable surface, so no collision at all."


def _prompt_for(brief: dict) -> str:
    """The message to hand to a Blender agent, with every number already decided."""
    b = brief["bounds_qu"]
    s = brief["size_qu"]
    mats = brief["materials"]
    mat_lines = (
        "\n".join(
            f"  slot {m.get('slot', i)}: {m.get('name')}"
            + (f"  (must resolve to {m['must_resolve_to']})" if m.get("must_resolve_to") else "")
            for i, m in enumerate(mats)
        )
        or "  (none specified — use a single material named for the asset)"
    )
    return f"""\
Model one asset: {brief["asset_id"]} — {brief["purpose"]}

DIMENSIONS (Quake units; {brief["units"]["quake_units_per_metre"]} units per metre)
  It must fit inside x {b["x"]}, y {b["y"]}, z {b["z"]}
  That is {s["x"]} x {s["y"]} x {s["z"]} units. Fill it; do not leave it undersized.
  {brief["units"]["note"]}

ORIGIN
  {brief["origin"]}

BUDGET
  triangles: {brief["budget"]["triangles"]}
  materials: {brief["budget"]["materials"]}

MATERIALS — name them exactly as given; the importer resolves them against the game's shaders
{mat_lines}

UV
  channel {brief["uv"]["channel"]}, required, target texel density \
{brief["uv"]["texel_density_px_per_qu"]} px per unit, no overlapping islands.

EXPORT
  format {brief["export"]["format"]}, axis up {brief["export"]["axis_up"]}, axis forward \
{brief["export"]["axis_forward"]}
  write to {brief["export"]["path"]}

COLLISION
  {brief["collision"]["strategy"]} — {brief["collision"]["reason"]}

STANDING RULES
  Apply every transform before export. One object per asset. Triangulate before export.
  No n-gons on any surface collision is derived from. Do not add lights or cameras.
{("  " + brief["silhouette_notes"]) if brief["silhouette_notes"] else ""}
NOTE FOR WHOEVER RUNS THIS
  Blender's MCP servers execute arbitrary Python inside Blender. Run this in a VM or on a
  machine with no sensitive data, especially if the loop is unattended."""


# ---------------------------------------------------------------------------
# Mesh reading
# ---------------------------------------------------------------------------


@dataclass
class Mesh:
    """A triangulated mesh, as much as an OBJ tells us."""

    vertices: list[tuple[float, float, float]] = field(default_factory=list)
    faces: list[list[int]] = field(default_factory=list)
    has_uv: bool = False
    uv_range: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    materials: list[str] = field(default_factory=list)
    normals: bool = False
    objects: int = 0

    def triangles(self) -> int:
        # A face of n corners fans into n-2 triangles, which is what the engine will see.
        return sum(max(0, len(f) - 2) for f in self.faces)

    def bounds(self) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        if not self.vertices:
            return ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        xs, ys, zs = zip(*self.vertices, strict=True)
        return ((min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs)))

    def size(self) -> tuple[float, float, float]:
        lo, hi = self.bounds()
        return (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])


def parse_obj(path: str | Path) -> Mesh:
    """Read a Wavefront OBJ.

    Hand-rolled because OBJ is a handful of line prefixes and pulling in a mesh library for it
    would be the largest dependency in the project. Only what validation needs is read:
    positions, faces, whether UVs and normals are present, and material names.
    """
    p = Path(path)
    if not p.is_file():
        raise AssetError(f"{p} does not exist")

    mesh = Mesh()
    uvs: list[tuple[float, float]] = []
    try:
        text = p.read_text(errors="replace")
    except OSError as e:
        raise AssetError(f"{p} is unreadable: {e}") from e

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        tag = parts[0]
        try:
            if tag == "v" and len(parts) >= 4:
                mesh.vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif tag == "vt" and len(parts) >= 3:
                uvs.append((float(parts[1]), float(parts[2])))
            elif tag == "vn":
                mesh.normals = True
            elif tag == "o":
                mesh.objects += 1
            elif tag == "usemtl" and len(parts) >= 2:
                if parts[1] not in mesh.materials:
                    mesh.materials.append(parts[1])
            elif tag == "f" and len(parts) >= 4:
                idx = []
                for token in parts[1:]:
                    # `v`, `v/vt`, `v/vt/vn` or `v//vn`; negative indices count from the end.
                    first = token.split("/")[0]
                    i = int(first)
                    idx.append(i - 1 if i > 0 else len(mesh.vertices) + i)
                    if "/" in token and token.split("/")[1]:
                        mesh.has_uv = True
                mesh.faces.append(idx)
        except ValueError as e:
            raise AssetError(f"{p}:{lineno}: could not read {tag!r} line: {e}") from e

    if uvs:
        us, vs = zip(*uvs, strict=True)
        mesh.uv_range = (min(us), min(vs), max(us), max(vs))
    if not mesh.vertices:
        raise AssetError(f"{p} contains no vertices — is it really an OBJ?")
    return mesh


# ---------------------------------------------------------------------------
# Import validation
# ---------------------------------------------------------------------------


def _finding(code: str, severity: str, message: str, fix: str = "") -> dict:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "confidence": "verified",
        "fix_hint": fix,
    }


def model_import(path: str | Path, brief: dict, profile_id: str | None = None) -> dict[str, Any]:
    """Validate an exported mesh against the brief that asked for it (§5.3).

    The scale check comes first and names the likely cause, because the metres/inches mistake is
    the commonest failure and the raw numbers alone do not point at it.
    """
    mesh = parse_obj(path)
    findings: list[dict] = []
    lo, hi = mesh.bounds()
    size = mesh.size()
    want = brief.get("size_qu") or {}
    want_v = [float(want.get(ax, 0.0)) for ax in ("x", "y", "z")]

    upm = float(brief.get("units", {}).get("quake_units_per_metre") or 39.37)

    # --- scale --------------------------------------------------------------
    if all(w > 0 for w in want_v) and all(s > 0 for s in size):
        ratios = [s / w for s, w in zip(size, want_v, strict=True)]
        mean = sum(ratios) / 3
        if not 0.8 <= mean <= 1.25:
            cause = ""
            if abs(mean - 1 / upm) / (1 / upm) < 0.25:
                cause = (
                    f" This is almost exactly 1/{upm:.2f}, which is the metres-exported-as-units "
                    f"mistake: multiply by {upm} on export."
                )
            elif abs(mean - upm) / upm < 0.25:
                cause = (
                    f" This is almost exactly {upm}x, which is the units-exported-as-metres "
                    f"mistake: disable unit scaling on export."
                )
            findings.append(
                _finding(
                    "MODEL_WRONG_SCALE",
                    "error",
                    f"the mesh is {size[0]:.3g} x {size[1]:.3g} x {size[2]:.3g} but the brief "
                    f"asked for {want_v[0]:.3g} x {want_v[1]:.3g} x {want_v[2]:.3g} — "
                    f"{mean:.4g}x the requested size.{cause}",
                    "re-export with the correct unit scale, then import again",
                )
            )
        elif max(ratios) - min(ratios) > 0.2:
            findings.append(
                _finding(
                    "MODEL_NON_UNIFORM_SCALE",
                    "warning",
                    f"axis ratios differ ({', '.join(f'{r:.3g}' for r in ratios)}), so the mesh "
                    f"is stretched relative to the brief",
                )
            )

    # --- fit ----------------------------------------------------------------
    bounds = brief.get("bounds_qu") or {}
    if bounds:
        for i, ax in enumerate(("x", "y", "z")):
            pair = bounds.get(ax)
            if not pair:
                continue
            if size[i] > (pair[1] - pair[0]) * 1.001:
                findings.append(
                    _finding(
                        "MODEL_EXCEEDS_BOUNDS",
                        "error",
                        f"the mesh is {size[i]:.4g} units on {ax}, larger than the "
                        f"{pair[1] - pair[0]:.4g} it must fit within",
                        "scale it down or widen the brief",
                    )
                )

    # --- origin -------------------------------------------------------------
    if any(abs(c) > 1e-6 for c in lo):
        findings.append(
            _finding(
                "MODEL_ORIGIN_NOT_AT_MIN_CORNER",
                "warning",
                f"the mesh minimum corner is at {tuple(round(c, 3) for c in lo)}, not the origin; "
                f"the brief asked for the min corner at (0,0,0), so placement will be offset",
                "apply the transform with the object at the origin before exporting",
            )
        )

    # --- budget -------------------------------------------------------------
    tri_budget = (brief.get("budget") or {}).get("triangles")
    conf = (brief.get("budget") or {}).get("budget_confidence", "unverified")
    if isinstance(tri_budget, int) and mesh.triangles() > tri_budget:
        findings.append(
            _finding(
                "MODEL_OVER_TRIANGLE_BUDGET",
                # A community norm, not an engine limit, so never an error.
                "warning" if conf == "verified" else "info",
                f"{mesh.triangles()} triangles against a budget of {tri_budget}",
                "decimate, or raise the budget if the asset earns it",
            )
        )

    # --- materials ----------------------------------------------------------
    wanted = [m.get("name") for m in brief.get("materials") or []]
    if wanted:
        missing = [w for w in wanted if w not in mesh.materials]
        extra = [m for m in mesh.materials if m not in wanted]
        if missing:
            findings.append(
                _finding(
                    "MODEL_MATERIAL_MISSING",
                    "error",
                    f"the brief asked for material(s) {missing} but the mesh has "
                    f"{mesh.materials or 'none'}; the compiler resolves shaders by these names",
                    "rename the materials in Blender to match the brief exactly",
                )
            )
        if extra:
            findings.append(
                _finding(
                    "MODEL_MATERIAL_UNEXPECTED",
                    "warning",
                    f"the mesh has material(s) {extra} the brief did not ask for",
                )
            )

    # --- UVs ----------------------------------------------------------------
    if (brief.get("uv") or {}).get("required") and not mesh.has_uv:
        findings.append(
            _finding(
                "MODEL_NO_UV",
                "error",
                "the mesh has no texture coordinates, so it will render untextured",
                "unwrap it in Blender and export UVs",
            )
        )

    # --- structure ----------------------------------------------------------
    if mesh.objects > 1:
        findings.append(
            _finding(
                "MODEL_MULTIPLE_OBJECTS",
                "warning",
                f"the file contains {mesh.objects} objects; the brief asked for one per asset",
                "join the objects before exporting",
            )
        )
    ngons = sum(1 for f in mesh.faces if len(f) > 4)
    if ngons:
        findings.append(
            _finding(
                "MODEL_NGONS",
                "warning",
                f"{ngons} face(s) have more than four corners; triangulate before export, "
                f"especially if collision is derived from the mesh",
            )
        )
    degenerate = sum(1 for f in mesh.faces if len(set(f)) < 3)
    if degenerate:
        findings.append(
            _finding(
                "MODEL_DEGENERATE_FACES",
                "warning",
                f"{degenerate} face(s) have fewer than three distinct vertices",
            )
        )

    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["code"]))
    return {
        "path": str(path),
        "vertices": len(mesh.vertices),
        "faces": len(mesh.faces),
        "triangles": mesh.triangles(),
        "materials": mesh.materials,
        "has_uv": mesh.has_uv,
        "has_normals": mesh.normals,
        "bounds": {"min": list(lo), "max": list(hi), "size": list(size)},
        "findings": findings,
        "summary": {
            s: sum(1 for f in findings if f["severity"] == s) for s in ("error", "warning", "info")
        },
        "ok": not any(f["severity"] == "error" for f in findings),
    }


# ---------------------------------------------------------------------------
# Placement and collision
# ---------------------------------------------------------------------------


def model_place_keys(
    profile_id: str,
    model_path: str,
    origin: list[float],
    *,
    angles: list[float] | None = None,
    scale: float | None = None,
    lightmap_scale: float | None = None,
    remap: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    """The entity key/value pairs that place a model (§5.3).

    Returned as ordered pairs rather than written directly, so the caller decides when the map
    changes. Every key name comes from the profile.
    """
    a = _assets(profile_id)
    keys: list[tuple[str, str]] = [
        ("classname", str(a.get("model_entity"))),
        (str(a.get("model_key", "model")), model_path),
        ("origin", " ".join(_fmt(c) for c in origin)),
    ]
    if angles:
        keys.append((str(a.get("angles_key", "angles")), " ".join(_fmt(c) for c in angles)))
    if scale is not None:
        # Negative scale is supported by this compiler and is the cheapest way to mirror a prop.
        keys.append((str(a.get("scale_key", "modelscale")), _fmt(scale)))
    if lightmap_scale is not None:
        keys.append((str(a.get("lightmap_scale_key", "_lightmapScale")), _fmt(lightmap_scale)))
    for i, (src, dst) in enumerate(sorted((remap or {}).items())):
        prefix = str(a.get("remap_key_prefix", "_remap"))
        keys.append((prefix if i == 0 else f"{prefix}{i}", f"{src};{dst}"))
    return keys


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


#: Normal families for the discrete convex hull. 6 is a box, 14 adds the corner diagonals,
#: 26 adds the edge diagonals and is a close fit for most props.
DOP_NORMALS: dict[int, list[tuple[int, int, int]]] = {
    6: [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)],
}
DOP_NORMALS[14] = DOP_NORMALS[6] + [
    (sx, sy, sz) for sx in (1, -1) for sy in (1, -1) for sz in (1, -1)
]
DOP_NORMALS[26] = (
    DOP_NORMALS[14]
    + [(a, b, 0) for a in (1, -1) for b in (1, -1)]
    + [(a, 0, b) for a in (1, -1) for b in (1, -1)]
    + [(0, a, b) for a in (1, -1) for b in (1, -1)]
)


def model_make_clip(
    path: str | Path,
    profile_id: str,
    *,
    origin: list[float] | None = None,
    scale: float = 1.0,
    k: int = 14,
    grid: int = 1,
    kind: str = "player",
) -> dict[str, Any]:
    """Fit a convex collision hull to a mesh and return it as Solid IR (§5.4).

    The hull is a **discrete convex hull** (a k-DOP): the tightest intersection of half-spaces
    with `k` fixed normals that still contains every vertex. That is deliberate rather than a
    compromise. §5.4 argues that for a competitive shooter "snappy, predictable collision beats
    accurate collision", and a k-DOP is exactly that — a small, convex, exactly-representable
    shape a player can read, instead of a faithful hull with a hundred faces that behaves
    unpredictably when you slide along it.

    Planes are pushed *outward* to the grid, so the hull always contains the mesh. A hull that
    cut into the visual would let players clip through the corner of a crate.

    Returns Solid IR, not brushes: the caller previews it, then commits it like any other shape.
    """
    if k not in DOP_NORMALS:
        raise AssetError(f"k must be one of {sorted(DOP_NORMALS)}, got {k}")
    a = _assets(profile_id)
    shaders = a.get("clip_shaders", {})
    shader = shaders.get(kind)
    if not shader:
        raise AssetError(
            f"profile {profile_id} states no clip shader for {kind!r}; known: {sorted(shaders)}"
        )

    mesh = parse_obj(path)
    off = origin or [0.0, 0.0, 0.0]
    pts = [
        (v[0] * scale + off[0], v[1] * scale + off[1], v[2] * scale + off[2]) for v in mesh.vertices
    ]

    planes: list[list[int]] = []
    for n in DOP_NORMALS[k]:
        # Support function: the furthest extent of the point set along this normal.
        d = max(n[0] * p[0] + n[1] * p[1] + n[2] * p[2] for p in pts)
        # Round outward. The normal is not unit length for the diagonals, so scale the grid step
        # by its length or the push would be inconsistent between normal families.
        length = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)
        step = max(1, grid) * length
        d_out = math.ceil(d / step) * step
        planes.append([n[0], n[1], n[2], int(round(d_out))])

    ir = {"op": "planes", "planes": planes}
    lo, hi = mesh.bounds()
    return {
        "ir": ir,
        "shader": shader,
        "kind": kind,
        "k": k,
        "planes": len(planes),
        "mesh_bounds": {"min": list(lo), "max": list(hi)},
        "mesh_triangles": mesh.triangles(),
        "note": (
            f"a {k}-plane discrete convex hull, pushed outward to a grid of {grid} so it always "
            f"contains the mesh. Commit it with solid_commit using textures "
            f'{{"default": "{shader}"}}. Raise k for a tighter fit; lower it for collision a '
            f"player can predict."
        ),
        "inverse_check": (
            "If the hull is noticeably larger than the visual mesh, players will complain about "
            "invisible walls. Render it over the model and look before committing."
        ),
    }
