#!/usr/bin/env python3
"""Generate the synthetic ``.map`` corpus.

The real corpus (`tools/import_corpus.py`) is far more valuable than anything synthetic —
it is what actual tools actually wrote. But it has gaps, and synthetic maps fill exactly
those:

- **Valve 220 appears in no real map we have.** The kernel claims to support all three
  texdef conventions (§3.2), so one of the three would otherwise be untested against a file.
- **Every degenerate case in one place.** Upstream's `regression_tests` cover many, but not
  mirrored planes, out-of-bounds coordinates, or an unrecognized primitive block.
- **File-shape edge cases**: CRLF, no final newline, spare trailing blank lines, the leading
  newline and `//@$&` layer records this fork writes.

Two output directories, and the distinction is load-bearing:

``roundtrip/``
    Valid maps. Must round-trip byte-identically *and* compile.

``degenerate/``
    Deliberately broken maps. Must still round-trip byte-identically — losing data is never
    acceptable, however bad the input — but must **not** be compiled, since q3map2 is
    entitled to reject them. `tools/difftest.py` skips this directory for the semantic pass.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TEX = "0 0 0 0.500000 0.500000 0 0 0"


def box_faces(
    x0: int,
    y0: int,
    z0: int,
    x1: int,
    y1: int,
    z1: int,
    shader: str = "common/caulk",
    tail: str = TEX,
) -> list[str]:
    """Six axial face lines for a box, with outward normals.

    Point order follows q3's convention `n = cross(c - a, b - a)` with the solid at
    `n · p <= d`. Getting a triple backwards turns the face inside out and the brush then
    encloses nothing, so these are written once and reused.
    """
    return [
        f"( {x0} {y0} {z1} ) ( {x0} {y0 + 1} {z1} ) ( {x0 + 1} {y0} {z1} ) {shader} {tail}",
        f"( {x0} {y0} {z0} ) ( {x0 + 1} {y0} {z0} ) ( {x0} {y0 + 1} {z0} ) {shader} {tail}",
        f"( {x0} {y0} {z0} ) ( {x0} {y0} {z0 + 1} ) ( {x0 + 1} {y0} {z0} ) {shader} {tail}",
        f"( {x0} {y1} {z0} ) ( {x0 + 1} {y1} {z0} ) ( {x0} {y1} {z0 + 1} ) {shader} {tail}",
        f"( {x0} {y0} {z0} ) ( {x0} {y0 + 1} {z0} ) ( {x0} {y0} {z0 + 1} ) {shader} {tail}",
        f"( {x1} {y0} {z0} ) ( {x1} {y0} {z0 + 1} ) ( {x1} {y0 + 1} {z0} ) {shader} {tail}",
    ]


def brush(faces: list[str], keyword: str | None = None) -> str:
    body = "\n".join(faces)
    if keyword:
        return f"{{\n{keyword}\n{{\n{body}\n}}\n}}\n"
    return f"{{\n{body}\n}}\n"


def entity(keys: list[tuple[str, str]], prims: str = "", leading: str = "") -> str:
    head = f"{leading}\n" if leading else ""
    kv = "".join(f'"{k}" "{v}"\n' for k, v in keys)
    return f"{head}{{\n{kv}{prims}}}\n"


def hollow_room(size: int = 512, height: int = 256, wall: int = 16) -> str:
    """A sealed six-brush room — the minimum a compiler will accept without leaking."""
    s, h, w = size, height, wall
    parts = [
        brush(box_faces(-w, -w, -w, s + w, s + w, 0)),  # floor
        brush(box_faces(-w, -w, h, s + w, s + w, h + w)),  # ceiling
        brush(box_faces(-w, -w, 0, 0, s + w, h)),  # -X wall
        brush(box_faces(s, -w, 0, s + w, s + w, h)),  # +X wall
        brush(box_faces(0, -w, 0, s, 0, h)),  # -Y wall
        brush(box_faces(0, s, 0, s, s + w, h)),  # +Y wall
    ]
    return "".join(parts)


def valid_maps() -> dict[str, str]:
    out: dict[str, str] = {}

    # --- format coverage -----------------------------------------------------------
    out["axial_room.map"] = (
        "// entity 0\n"
        + entity([("classname", "worldspawn")], hollow_room())
        + entity([("classname", "point_entity_a"), ("origin", "256 256 24")])
    )

    # Brush primitives put the 2x3 texture matrix *between* the plane points and the shader
    # name, so keep the three point groups and replace everything after them. The `-0`
    # literals are deliberate: real maps contain them and they must survive a round-trip.
    bp_faces = [
        ") ".join(ln.split(") ")[0:3])
        + ") ( ( 0.0078125 0 -0 ) ( -0 0.0078125 0 ) ) common/caulk 0 0 0"
        for ln in box_faces(0, 0, 0, 128, 128, 128)
    ]
    out["brush_primitives.map"] = entity(
        [("classname", "worldspawn")], brush(bp_faces, keyword="brushDef")
    )

    v220_faces = []
    for ln in box_faces(0, 0, 0, 128, 128, 128):
        pts = ") ".join(ln.split(") ")[0:3]) + ") "
        v220_faces.append(pts + "WALL01 [ 1 0 0 16 ] [ 0 -1 0 -8 ] 0 1 1")
    # Valve 220 appears in no real map we have; this is its only coverage.
    out["valve220.map"] = entity(
        [("mapversion", "220"), ("classname", "worldspawn")], brush(v220_faces)
    )

    patch2 = (
        "{\npatchDef2\n{\ncommon/caulk\n( 3 3 0 0 0 )\n(\n"
        "( ( 0 0 0 0 0 ) ( 0 64 64 0 -0.5 ) ( 0 128 0 0 -1 ) )\n"
        "( ( 64 0 0 0.5 0 ) ( 64 64 64 0.5 -0.5 ) ( 64 128 0 0.5 -1 ) )\n"
        "( ( 128 0 0 1 0 ) ( 128 64 64 1 -0.5 ) ( 128 128 0 1 -1 ) )\n"
        ")\n}\n}\n"
    )
    out["patch_def2.map"] = entity([("classname", "worldspawn")], hollow_room() + patch2)

    mixed = brush(box_faces(0, 0, 0, 64, 64, 64)) + brush(bp_faces, keyword="brushDef")
    out["mixed_texdefs.map"] = entity([("classname", "worldspawn")], mixed)

    # --- file-shape coverage -------------------------------------------------------
    simple = entity([("classname", "worldspawn")], brush(box_faces(0, 0, 0, 64, 64, 64)))
    out["crlf.map"] = simple.replace("\n", "\r\n")
    out["no_final_newline.map"] = simple.rstrip("\n")
    out["trailing_blank_lines.map"] = simple + "\n\n"
    # The shape this fork actually writes: leading newline, then layer records.
    out["fork_layer_records.map"] = (
        '\n//@$& layerdef "0" -1 0 0 0\n// entity 0\n'
        + "{\n"
        + '"classname" "worldspawn"\n'
        + "//@$& layer 0\n// brush 0\n"
        + brush(box_faces(0, 0, 0, 64, 64, 64))
        + "}\n"
    )

    # --- entity-form coverage ------------------------------------------------------
    out["entity_forms.map"] = (
        entity([("classname", "worldspawn")], hollow_room())
        + entity([("classname", "point_entity_a"), ("angle", "90"), ("angle", "180")])
        + "{\n}\n"
        + entity([("classname", "group_entity_a")], brush(box_faces(64, 64, 8, 128, 128, 40)))
        + "// a note from the mapper\n"
    )

    out["block_comments.map"] = "/* a block comment\n   spanning lines */\n" + entity(
        [("classname", "worldspawn")], hollow_room()
    )
    return out


def degenerate_maps() -> dict[str, str]:
    """Broken on purpose. Must round-trip; must not be compiled."""
    out: dict[str, str] = {}
    w = lambda prims: entity([("classname", "worldspawn")], prims)  # noqa: E731

    out["too_few_faces.map"] = w(brush(box_faces(0, 0, 0, 64, 64, 64)[:3]))
    out["mirrored_plane.map"] = w(
        brush(
            [
                f"( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) common/caulk {TEX}",
                f"( 0 0 0 ) ( 0 1 0 ) ( 1 0 0 ) common/caulk {TEX}",
                f"( 0 0 0 ) ( 0 0 1 ) ( 1 0 0 ) common/caulk {TEX}",
                f"( 0 8 0 ) ( 1 8 0 ) ( 0 8 1 ) common/caulk {TEX}",
            ]
        )
    )
    # The same plane written from two different point triples.
    out["duplicate_plane.map"] = w(
        brush(
            box_faces(0, 0, 0, 64, 64, 64)
            + [f"( 512 -256 64 ) ( 512 -255 64 ) ( 513 -256 64 ) common/caulk {TEX}"]
        )
    )
    # A seventh plane touching only the (64,64,64) corner: bounds no area.
    out["redundant_plane.map"] = w(
        brush(
            box_faces(0, 0, 0, 64, 64, 64)
            + [f"( 64 64 64 ) ( 64 128 0 ) ( 128 64 0 ) common/caulk {TEX}"]
        )
    )
    out["off_grid.map"] = w(
        brush(
            [
                f"( 0 0 0.5 ) ( 0 1 0.5 ) ( 1 0 0.5 ) common/caulk {TEX}",
                *box_faces(0, 0, 0, 64, 64, 64)[1:],
            ]
        )
    )
    out["out_of_bounds.map"] = w(
        brush(
            [
                f"( 0 0 999999 ) ( 0 1 999999 ) ( 1 0 999999 ) common/caulk {TEX}",
                *box_faces(0, 0, 0, 64, 64, 64)[1:],
            ]
        )
    )
    out["collinear_face.map"] = w(
        brush(
            [
                f"( 0 0 0 ) ( 8 0 0 ) ( 16 0 0 ) common/caulk {TEX}",
                *box_faces(0, 0, 0, 64, 64, 64)[1:],
            ]
        )
    )
    out["thin_brush.map"] = w(brush(box_faces(0, 0, 0, 256, 256, 1)))
    out["no_worldspawn.map"] = entity([("classname", "point_entity_a"), ("origin", "0 0 24")])
    out["patch_dims_mismatch.map"] = w(
        "{\npatchDef2\n{\ncommon/caulk\n( 3 3 0 0 0 )\n(\n"
        "( ( 0 0 0 0 0 ) ( 0 0 0 0 0 ) )\n)\n}\n}\n"
    )
    # patchDef3 in a Quake 3 map: upstream writes it, then cannot read it back.
    out["patch_def3_unreadable.map"] = w(
        "{\npatchDef3\n{\ncommon/caulk\n( 3 3 4 4 0 0 0 )\n(\n"
        "( ( 0 0 0 0 0 ) ( 0 0 0 0 0 ) ( 0 0 0 0 0 ) )\n"
        "( ( 0 0 0 0 0 ) ( 0 0 0 0 0 ) ( 0 0 0 0 0 ) )\n"
        "( ( 0 0 0 0 0 ) ( 0 0 0 0 0 ) ( 0 0 0 0 0 ) )\n"
        ")\n}\n}\n"
    )
    out["unknown_primitive.map"] = w(
        "{\nsomeFutureDef\n{\n( 0 0 0 ) whatever ( nested { } )\n}\n}\n"
    )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", type=Path, required=True, help="output directory")
    args = ap.parse_args()

    valid = valid_maps()
    bad = degenerate_maps()

    for sub, maps in (("roundtrip", valid), ("degenerate", bad)):
        d = args.out / sub
        d.mkdir(parents=True, exist_ok=True)
        for name, text in maps.items():
            (d / name).write_text(text, newline="")

    (args.out / "README.md").write_text(
        "# Synthetic corpus\n\n"
        "Generated by `tools/gen_corpus.py` — do not edit by hand.\n\n"
        "- `roundtrip/` — valid maps; must round-trip byte-identically and compile.\n"
        "- `degenerate/` — broken on purpose; must round-trip byte-identically but must\n"
        "  **not** be compiled. `tools/difftest.py` excludes this directory from the\n"
        "  semantic pass, because q3map2 is entitled to reject these.\n"
    )
    # A stamp so mise's `outputs` incremental check has something to compare.
    (args.out / ".stamp").write_text(
        json.dumps({"roundtrip": sorted(valid), "degenerate": sorted(bad)}, indent=2) + "\n"
    )

    print(f"generated {len(valid)} valid + {len(bad)} degenerate maps under {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
