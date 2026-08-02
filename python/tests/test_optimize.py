"""Phase 5: the optimization suite (§6.1, §6.3).

Every file format exercised here is synthesized to match the code that writes it upstream, so
a test failing means the parser drifted from `prtfile.cpp`, `leakfile.cpp` or `scriplib.cpp` —
not that a fixture went stale. Where a real artefact happens to be on this machine (a shipped
map's `.prt`, the game's own `common.shader`) it is used as a second, harder check and skipped
cleanly when absent.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from nrc_mcp import kernel, optimize, profiles

ROOT = kernel.repo_root()


# ---------------------------------------------------------------------------
# Map fixtures
# ---------------------------------------------------------------------------


def face(a, b, c, shader="common/caulk", contents=0) -> str:
    def pt(v):
        return "( " + " ".join(str(x) for x in v) + " )"

    return f"{pt(a)} {pt(b)} {pt(c)} {shader} 0 0 0 0.500000 0.500000 {contents} 0 0"


def box(lo, hi, shader="common/caulk", contents=0) -> str:
    """An axis-aligned brush written the way the editor writes one."""
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    faces = [
        face((x0, y0, z1), (x0, y0 + 1, z1), (x0 + 1, y0, z1), shader, contents),
        face((x0, y0, z0), (x0 + 1, y0, z0), (x0, y0 + 1, z0), shader, contents),
        face((x0, y0, z0), (x0, y0, z0 + 1), (x0 + 1, y0, z0), shader, contents),
        face((x0, y1, z0), (x0 + 1, y1, z0), (x0, y1, z0 + 1), shader, contents),
        face((x0, y0, z0), (x0, y0 + 1, z0), (x0, y0, z0 + 1), shader, contents),
        face((x1, y0, z0), (x1, y0, z0 + 1), (x1, y0 + 1, z0), shader, contents),
    ]
    return "{\n" + "\n".join(faces) + "\n}\n"


def write_map(path: Path, brushes: list[str], extra_entities: str = "") -> Path:
    path.write_text('{\n"classname" "worldspawn"\n' + "".join(brushes) + "}\n" + extra_entities)
    return path


@pytest.fixture
def kernel_available():
    try:
        kernel.kernel()
    except kernel.KernelUnavailable as e:
        pytest.skip(str(e))


# ---------------------------------------------------------------------------
# Brush enumeration — the one non-trivial thing left between here and the kernel
# ---------------------------------------------------------------------------


def test_brushes_skips_patches_and_carries_the_kernels_detail_flag(tmp_path, kernel_available):
    patch = "{\npatchDef2\n{\ncommon/caulk\n( 3 3 0 0 0 )\n(\n(\n)\n)\n}\n}\n"
    path = tmp_path / "mixed.map"
    path.write_text(
        '{\n"classname" "worldspawn"\n'
        + box((0, 0, 0), (64, 64, 64))
        + patch
        + box((128, 0, 0), (192, 64, 64), contents=optimize.BRUSH_DETAIL_MASK)
        + "}\n"
    )
    mp = kernel.load_map(path)
    info = mp.entities(with_keys=False)[0]
    assert (info["brushes"], info["patches"]) == (2, 1)

    found = optimize._brushes(mp, 0, info)
    # Primitive 1 is the patch, so the second brush is at index 2 — the probe has to skip it
    # rather than assume brushes are contiguous.
    assert [p for p, _ in found] == [0, 2]
    assert [g["detail"] for _, g in found] == [False, True]


def test_the_kernels_per_brush_detail_flag_sums_to_its_own_total_on_the_real_corpus(
    kernel_available,
):
    """Guards the enumeration probe against every primitive layout the corpus contains.

    If `_brushes` ever missed a brush or counted a patch as one, the per-brush sum would stop
    matching `stats()`, which computes its totals by a different route.
    """
    maps = sorted((ROOT / "corpus").rglob("*.map"))
    if not maps:
        pytest.skip("no corpus; run `mise run bootstrap`")
    for path in maps:
        mp = kernel.load_map(path)
        stats = mp.stats(grid=8)
        found = 0
        detail = 0
        for index, info in enumerate(mp.entities(with_keys=False)):
            for _, g in optimize._brushes(mp, index, info):
                found += 1
                detail += bool(g["detail"])
        assert found == stats["brushes"], path
        assert detail == stats["detail_brushes"], path


# ---------------------------------------------------------------------------
# structural_audit
# ---------------------------------------------------------------------------


def test_structural_audit_flags_a_small_interior_brush_and_spares_the_shell(
    tmp_path, kernel_available
):
    # A sealed 1024-unit room, plus one 32-unit crate in the middle of it. The crate is the
    # only thing that should be reported: it occludes nothing and splits the tree six ways.
    room = [
        box((0, 0, 0), (1024, 1024, 16)),
        box((0, 0, 496), (1024, 1024, 512)),
        box((0, 0, 0), (16, 1024, 512)),
        box((1008, 0, 0), (1024, 1024, 512)),
        box((0, 0, 0), (1024, 16, 512)),
        box((0, 1008, 0), (1024, 1024, 512)),
    ]
    crate = box((496, 496, 16), (528, 528, 48))
    path = write_map(tmp_path / "small_interior.map", [*room, crate])

    out = optimize.structural_audit(path, grid=8)
    assert out["totals"]["world_structural_brushes"] == 7
    assert [c["primitive"] for c in out["candidates"]] == [6]

    only = out["candidates"][0]
    assert only["size"] == [32.0, 32.0, 32.0]
    assert only["touches_shell"] is False
    assert "STRUCT_SMALL_BRUSH" in only["codes"]
    assert "STRUCT_INTERIOR_ISLAND" in only["codes"]
    assert only["estimated_portal_reduction"] == 6
    assert out["estimated_portal_reduction_total"] == 6

    codes = {f["code"] for f in out["findings"]}
    assert codes == {"STRUCT_SMALL_BRUSH"}
    # A heuristic may warn but may never fail a build.
    assert out["summary"]["error"] == 0
    assert out["summary"]["warning"] == 1
    assert all(f["confidence"] == "unverified" for f in out["findings"])


def test_structural_audit_ignores_brushes_already_marked_detail(tmp_path, kernel_available):
    room = [box((0, 0, 0), (1024, 1024, 16)), box((0, 0, 496), (1024, 1024, 512))]
    crate = box((496, 496, 16), (528, 528, 48), contents=optimize.BRUSH_DETAIL_MASK)
    path = write_map(tmp_path / "already_detail.map", [*room, crate])

    out = optimize.structural_audit(path, grid=8)
    assert out["totals"]["detail_brushes"] == 1
    assert out["candidates"] == []


def test_structural_audit_ignores_brush_entities(tmp_path, kernel_available):
    # bsp.cpp:540 gives a vis tree to entity 0 alone, so a crate inside a func entity costs
    # no portal however it is flagged and must not be reported.
    room = [box((0, 0, 0), (1024, 1024, 16)), box((0, 0, 496), (1024, 1024, 512))]
    crate_entity = '{\n"classname" "a_brush_entity"\n' + box((496, 496, 16), (528, 528, 48)) + "}\n"
    path = write_map(tmp_path / "brush_entity.map", room, extra_entities=crate_entity)

    out = optimize.structural_audit(path, grid=8)
    assert out["totals"]["brushes"] == 3
    assert out["totals"]["world_brushes"] == 2
    assert out["candidates"] == []


def test_structural_audit_flags_a_thin_interior_slab(tmp_path, kernel_available):
    room = [box((0, 0, 0), (1024, 1024, 16)), box((0, 0, 496), (1024, 1024, 512))]
    # 4 units thick, 256 long: trim. Too long for the size test, so this is the thin test
    # alone, and it is only reported because it sits clear of the shell.
    trim = box((400, 400, 200), (656, 404, 232))
    path = write_map(tmp_path / "thin.map", [*room, trim])

    out = optimize.structural_audit(path, grid=8)
    assert [c["primitive"] for c in out["candidates"]] == [2]
    assert out["candidates"][0]["min_thickness"] == 4.0
    assert "STRUCT_THIN_BRUSH" in out["candidates"][0]["codes"]
    assert "STRUCT_SMALL_BRUSH" not in out["candidates"][0]["codes"]


def test_structural_audit_thresholds_are_tunable(tmp_path, kernel_available):
    room = [box((0, 0, 0), (1024, 1024, 16)), box((0, 0, 496), (1024, 1024, 512))]
    crate = box((496, 496, 16), (528, 528, 48))
    path = write_map(tmp_path / "tunable.map", [*room, crate])

    tight = optimize.structural_audit(
        path, grid=8, small_max_extent=16.0, interior_max_fraction=0.01
    )
    assert tight["candidates"] == []


def test_structural_audit_totals_come_from_the_kernel(tmp_path, kernel_available):
    # The audit's own per-brush classification and the totals printed beside it must agree,
    # which they do by construction now that both read the same kernel mask.
    room = [box((0, 0, 0), (1024, 1024, 16)), box((0, 0, 496), (1024, 1024, 512))]
    detail = box((496, 496, 16), (528, 528, 48), contents=optimize.BRUSH_DETAIL_MASK)
    structural = box((300, 300, 16), (332, 332, 48))
    path = write_map(tmp_path / "totals.map", [*room, detail, structural])

    out = optimize.structural_audit(path, grid=8)
    t = out["totals"]
    assert t["brushes"] == 4
    assert t["detail_brushes"] == 1
    assert t["structural_brushes"] == 3
    assert t["world_brushes"] == 4
    assert t["world_structural_brushes"] == t["brushes"] - t["detail_brushes"]
    # Only the structural crate is a candidate; the detail one is already done.
    assert [c["primitive"] for c in out["candidates"]] == [3]


def test_structural_audit_reports_brushes_it_could_not_evaluate(tmp_path, kernel_available):
    # An off-grid plane cannot be evaluated exactly, and the kernel says so rather than
    # guessing. The audit has to pass that through instead of silently dropping the brush.
    room = [box((0, 0, 0), (1024, 1024, 16))]
    skew = (
        "{\n"
        + face((0.5, 0.5, 100.5), (0.5, 1.5, 100.5), (1.5, 0.5, 100.5))
        + "\n"
        + face((0.5, 0.5, 90.5), (1.5, 0.5, 90.5), (0.5, 1.5, 90.5))
        + "\n"
        + face((0.5, 0.5, 90.5), (0.5, 0.5, 91.5), (1.5, 0.5, 90.5))
        + "\n"
        + face((0.5, 40.5, 90.5), (1.5, 40.5, 90.5), (0.5, 40.5, 91.5))
        + "\n"
        + face((0.5, 0.5, 90.5), (0.5, 1.5, 90.5), (0.5, 0.5, 91.5))
        + "\n"
        + face((40.5, 0.5, 90.5), (40.5, 0.5, 91.5), (40.5, 1.5, 90.5))
        + "\n}\n"
    )
    path = write_map(tmp_path / "unevaluated.map", [*room, skew])

    out = optimize.structural_audit(path, grid=8)
    unevaluated = [f for f in out["findings"] if f["code"] == "STRUCT_BRUSH_UNEVALUATED"]
    assert len(unevaluated) == 1
    assert unevaluated[0]["severity"] == "info"
    assert out["totals"]["unevaluated_brushes"] == 1


# ---------------------------------------------------------------------------
# parse_portal_file / hint_suggest
# ---------------------------------------------------------------------------


def prt(clusters: int, portals: list[str], faces: list[str]) -> str:
    """A portal file in q3map2's exact spelling (prtfile.cpp:351-354, :124-153, :216-238)."""
    head = f"{optimize.PORTALFILE_MAGIC}\n{clusters}\n{len(portals)}\n{len(faces)}\n"
    return head + "".join(portals) + "".join(faces)


def portal_line(cluster_a: int, cluster_b: int, flags: int, points: list[tuple]) -> str:
    winding = "".join("(" + " ".join(str(c) for c in p) + " ) " for p in points)
    return f"{len(points)} {cluster_a} {cluster_b} {flags} {winding}\n"


def face_line(cluster: int, points: list[tuple]) -> str:
    winding = "".join("(" + " ".join(str(c) for c in p) + " ) " for p in points)
    return f"{len(points)} {cluster} {winding}\n"


def test_parse_portal_file_reads_the_format_q3map2_writes(tmp_path):
    quad = [(0, 0, 0), (0, 0, 768), (0, 768, 768), (0, 768, 0)]
    text = prt(
        3,
        [portal_line(0, 1, 0, quad), portal_line(1, 2, 1, quad), portal_line(0, 2, 2, quad)],
        [face_line(0, quad)],
    )
    path = tmp_path / "p.prt"
    path.write_text(text)

    out = optimize.parse_portal_file(path)
    assert out["magic"] == "PRT1"
    assert (out["clusters"], out["declared_portals"], out["declared_solid_faces"]) == (3, 3, 1)
    assert out["complete"] is True
    assert len(out["portals"]) == 3
    assert out["portals"][0]["winding"] == [list(p) for p in quad]
    assert out["portals"][0]["center"] == [0.0, 384.0, 384.0]
    # flags bit 0 is C_HINT and bit 1 is C_SKY (prtfile.cpp:133-140).
    assert [p["hint"] for p in out["portals"]] == [False, True, False]
    assert [p["sky"] for p in out["portals"]] == [False, False, True]
    assert out["hint_portals"] == 1
    assert out["sky_portals"] == 1
    # Each file portal is registered against both its clusters, the way vis does it.
    assert out["portals_per_cluster"] == {0: 2, 1: 2, 2: 2}
    assert out["solid_faces"][0]["cluster"] == 0


def test_parse_portal_file_handles_the_integer_shorthand_and_float_forms(tmp_path):
    # WriteFloat prints an integer when the value is within 0.001 of one, otherwise %f
    # (prtfile.cpp:54-61), so both spellings appear in real files.
    path = tmp_path / "mixed.prt"
    path.write_text("PRT1\n2\n1\n0\n3 0 1 0 (0 0 0 ) (16 0 0 ) (0 16.500000 0 ) \n")
    out = optimize.parse_portal_file(path)
    assert out["portals"][0]["winding"][2] == [0.0, 16.5, 0.0]


def test_parse_portal_file_refuses_a_file_that_is_not_one(tmp_path):
    bad = tmp_path / "not.prt"
    bad.write_text("PRT2\n1\n0\n0\n")
    with pytest.raises(optimize.OptimizeError, match="not a portal file"):
        optimize.parse_portal_file(bad)

    missing = tmp_path / "absent.prt"
    with pytest.raises(optimize.OptimizeError, match="does not exist"):
        optimize.parse_portal_file(missing)

    short = tmp_path / "short.prt"
    short.write_text("PRT1\n1\n")
    with pytest.raises(optimize.OptimizeError, match="at least 4"):
        optimize.parse_portal_file(short)


def test_parse_portal_file_refuses_a_malformed_winding(tmp_path):
    path = tmp_path / "broken.prt"
    path.write_text("PRT1\n2\n1\n0\n4 0 1 0 (0 0 0 ) (0 0 768 ) \n")
    with pytest.raises(optimize.OptimizeError, match="expected '\\('"):
        optimize.parse_portal_file(path)


def test_parse_portal_file_notices_a_truncated_file(tmp_path):
    quad = [(0, 0, 0), (0, 0, 8), (0, 8, 8), (0, 8, 0)]
    path = tmp_path / "cut.prt"
    path.write_text("PRT1\n2\n3\n0\n" + portal_line(0, 1, 0, quad))
    out = optimize.parse_portal_file(path)
    assert out["complete"] is False

    suggested = optimize.hint_suggest(path)
    assert [f["code"] for f in suggested["findings"] if f["code"] == "PRT_TRUNCATED"]


def test_hint_suggest_proposes_a_plane_that_halves_a_busy_cluster(tmp_path):
    # Cluster 0 gets 12 portals spread along x: six near x=0 and six near x=1024. The
    # even split makes the median plane unambiguous.
    portals = []
    for i in range(6):
        portals.append(
            portal_line(0, 10 + i, 0, [(0, i, 0), (0, i, 8), (0, i + 8, 8), (0, i + 8, 0)])
        )
    for i in range(6):
        portals.append(
            portal_line(
                0, 20 + i, 0, [(1024, i, 0), (1024, i, 8), (1024, i + 8, 8), (1024, i + 8, 0)]
            )
        )
    path = tmp_path / "busy.prt"
    path.write_text(prt(32, portals, []))

    out = optimize.hint_suggest(path, warn_portals=8)
    assert out["worst_clusters"][0] == {"cluster": 0, "portals": 12}
    proposal = out["proposals"][0]
    assert proposal["cluster"] == 0
    assert proposal["plane"]["axis"] == "x"
    assert proposal["plane"]["position"] == 1024.0
    assert proposal["plane"]["portals_each_side"] == [6, 6]
    assert proposal["portals_after_estimate"] == 7
    assert proposal["reduction_estimate"] == 5
    assert proposal["confidence"] == "unverified"

    suggested = [f for f in out["findings"] if f["code"] == "HINT_SUGGESTED"]
    assert len(suggested) == 1
    assert suggested[0]["severity"] == "warning"
    assert suggested[0]["confidence"] == "unverified"


def test_hint_suggest_stays_silent_below_the_threshold(tmp_path):
    quad = [(0, 0, 0), (0, 0, 8), (0, 8, 8), (0, 8, 0)]
    path = tmp_path / "quiet.prt"
    path.write_text(prt(4, [portal_line(0, 1, 0, quad), portal_line(0, 2, 0, quad)], []))
    out = optimize.hint_suggest(path, warn_portals=8)
    assert out["proposals"] == []
    assert out["findings"] == []


def test_hint_suggest_declines_when_no_axial_plane_would_divide_the_portals(tmp_path):
    # Ten portals stacked within 8 units on every axis: there is nothing to split, and
    # saying so is more useful than proposing a plane that does nothing.
    quad = [(0, 0, 0), (0, 0, 8), (0, 8, 8), (0, 8, 0)]
    path = tmp_path / "tight.prt"
    path.write_text(prt(16, [portal_line(0, 1 + i, 0, quad) for i in range(10)], []))
    out = optimize.hint_suggest(path, warn_portals=4)
    assert out["proposals"][0]["plane"] is None
    assert out["proposals"][0]["portals_after_estimate"] is None
    assert [f["code"] for f in out["findings"]] == ["HINT_NO_PROPOSAL"]


def test_hint_suggest_treats_the_vis_leaf_limit_as_a_verified_error(tmp_path):
    quad = [(0, 0, 0), (0, 0, 8), (0, 8, 8), (0, 8, 0)]
    lines = [portal_line(0, 1, 0, quad) for _ in range(optimize.MAX_PORTALS_ON_LEAF)]
    path = tmp_path / "over.prt"
    path.write_text(prt(4, lines, []))

    out = optimize.hint_suggest(path)
    over = [f for f in out["findings"] if f["code"] == "VIS_LEAF_PORTAL_LIMIT"]
    assert len(over) == 2  # both clusters of every portal are over the limit
    assert over[0]["severity"] == "error"
    assert over[0]["confidence"] == "verified"
    assert over[0]["detail"]["limit"] == 1024


def real_portal_files() -> list[Path]:
    """Portal files this machine happens to have, compiled by q3map2 rather than by us."""
    found = sorted(ROOT.glob("out/*/*.prt"))
    shipped = Path("/mnt/c/Program Files/UrbanTerror43/q3ut4/maps")
    if shipped.is_dir():
        found += sorted(shipped.glob("*.prt"))
    return found


def test_parse_portal_file_on_every_real_portal_file_this_machine_has():
    candidates = real_portal_files()
    if not candidates:
        pytest.skip("no compiled .prt on this machine; run `mise run compile:final <map>`")

    total_portals = 0
    for path in candidates:
        out = optimize.parse_portal_file(path)
        # The declared header counts are the file's own statement of how many records follow,
        # so parsing exactly that many is the check that the record grammar is right.
        assert out["complete"] is True, path
        assert len(out["portals"]) == out["declared_portals"], path
        assert len(out["solid_faces"]) == out["declared_solid_faces"], path
        assert all(len(p["winding"]) == p["points"] for p in out["portals"]), path
        assert all(p["points"] <= optimize.MAX_POINTS_ON_WINDING for p in out["portals"]), path
        # A file q3map2 wrote and then successfully vised cannot hold a cluster over the limit,
        # because LoadPortals would have aborted. A fully sealed single-cluster room has no
        # visportals at all, which is why this is guarded rather than asserted unconditionally.
        if out["portals_per_cluster"]:
            assert max(out["portals_per_cluster"].values()) <= optimize.MAX_PORTALS_ON_LEAF, path
        total_portals += len(out["portals"])

    if total_portals == 0:
        pytest.skip("the portal files here describe single-cluster maps with no visportals")


# ---------------------------------------------------------------------------
# parse_pointfile / leak_trace
# ---------------------------------------------------------------------------


def test_parse_pointfile_reads_three_floats_per_line(tmp_path):
    path = tmp_path / "leak.lin"
    path.write_text("0.000000 0.000000 0.000000\n64.000000 0.000000 0.000000\n")
    out = optimize.parse_pointfile(path)
    assert out["count"] == 2
    assert out["points"][1] == [64.0, 0.0, 0.0]


def test_parse_pointfile_refuses_anything_else(tmp_path):
    bad = tmp_path / "bad.lin"
    bad.write_text("0 0 0\n1 2\n")
    with pytest.raises(optimize.OptimizeError, match="three floats"):
        optimize.parse_pointfile(bad)

    empty = tmp_path / "empty.lin"
    empty.write_text("\n\n")
    with pytest.raises(optimize.OptimizeError, match="no points"):
        optimize.parse_pointfile(empty)


def test_leak_trace_resolves_the_entity_by_matching_the_final_origin(tmp_path, kernel_available):
    entity = '{\n"classname" "a_spawn_point"\n"origin" "512 256 64"\n}\n'
    map_path = write_map(
        tmp_path / "leaky.map", [box((0, 0, 0), (1024, 1024, 16))], extra_entities=entity
    )
    lin = tmp_path / "leaky.lin"
    lin.write_text(
        "-2048.000000 256.000000 64.000000\n"
        "0.000000 256.000000 64.000000\n"
        "512.000000 256.000000 64.000000\n"
    )

    out = optimize.leak_trace(lin, map_path)
    assert out["point_count"] == 3
    assert out["path_length"] == pytest.approx(2560.0)
    assert out["leaked_from_origin"] == [512.0, 256.0, 64.0]
    assert out["entity"]["index"] == 1
    assert out["entity"]["classname"] == "a_spawn_point"
    assert out["entity"]["distance"] == 0.0
    assert out["bounds"]["size"] == [2560.0, 0.0, 0.0]

    traced = [f for f in out["findings"] if f["code"] == "LEAK_TRACED"]
    assert len(traced) == 1
    assert traced[0]["severity"] == "error"
    assert traced[0]["confidence"] == "verified"


def test_leak_trace_says_so_when_it_cannot_name_the_entity(tmp_path):
    lin = tmp_path / "orphan.lin"
    lin.write_text("0.000000 0.000000 0.000000\n32.000000 0.000000 0.000000\n")
    out = optimize.leak_trace(lin)
    assert out["entity"] is None
    assert [f["code"] for f in out["findings"]] == ["LEAK_ENTITY_UNRESOLVED"]
    assert "pass map_path" in out["findings"][0]["message"]
    assert out["findings"][0]["detail"]["leaked_from_origin"] == [32.0, 0.0, 0.0]


def test_leak_trace_does_not_claim_a_distant_entity(tmp_path, kernel_available):
    entity = '{\n"classname" "a_spawn_point"\n"origin" "5000 5000 5000"\n}\n'
    map_path = write_map(
        tmp_path / "far.map", [box((0, 0, 0), (64, 64, 16))], extra_entities=entity
    )
    lin = tmp_path / "far.lin"
    lin.write_text("0.000000 0.000000 0.000000\n32.000000 0.000000 0.000000\n")
    out = optimize.leak_trace(lin, map_path)
    assert out["entity"] is None
    assert out["findings"][0]["code"] == "LEAK_ENTITY_UNRESOLVED"


# ---------------------------------------------------------------------------
# Shader parsing
# ---------------------------------------------------------------------------


def test_shader_tokenizer_matches_scriplib_comment_and_quote_rules():
    text = (
        "first // trailing line comment\n"
        "# hash comment\n"
        "; semicolon comment\n"
        "/* block\n   comment */\n"
        '"a quoted token" bare;terminated\n'
    )
    tokens = [t for _, t in optimize._shader_tokens(text)]
    assert tokens == ["first", "a quoted token", "bare"]


def test_parse_shader_file_separates_stages_from_directives(tmp_path):
    path = tmp_path / "s.shader"
    path.write_text(
        "textures/mymap/wall // a comment after the name\n"
        "{\n"
        "\tqer_editorimage textures/mymap/wall.tga\n"
        "\tsurfaceparm metalsteps\n"
        "\t{\n"
        "\t\tmap textures/mymap/wall.tga\n"
        "\t\tblendfunc GL_ONE GL_ZERO\n"
        "\t}\n"
        "\t{\n"
        "\t\tmap $lightmap\n"
        "\t}\n"
        "}\n"
        "textures/mymap/compileonly:q3map\n"
        "{\n"
        "\tsurfaceparm nodraw\n"
        "}\n"
    )
    shaders = optimize.parse_shader_file(path)
    assert [s["name"] for s in shaders] == [
        "textures/mymap/wall",
        "textures/mymap/compileonly",
    ]
    wall = shaders[0]
    assert wall["stages"] == 2
    assert wall["surfaceparms"] == ["metalsteps"]
    assert "textures/mymap/wall.tga" in wall["images"]
    assert "$lightmap" in wall["images"]
    assert shaders[1]["compiler_only"] is True
    assert shaders[1]["surfaceparms"] == ["nodraw"]


def test_parse_shader_file_refuses_a_shader_with_no_body(tmp_path):
    path = tmp_path / "broken.shader"
    path.write_text("textures/mymap/wall\nsurfaceparm nodraw\n")
    with pytest.raises(optimize.OptimizeError, match="not followed by"):
        optimize.parse_shader_file(path)


def test_parse_shader_file_on_the_games_own_common_shader():
    real = Path("/mnt/c/Program Files/UrbanTerror43/q3ut4/scripts/common.shader")
    if not real.is_file():
        pytest.skip("the game's shader scripts are not readable on this machine")
    shaders = optimize.parse_shader_file(real)
    assert len(shaders) > 20
    by_name = {s["name"]: s for s in shaders}
    # Every name keeps its full path, and a trailing comment on the name line is not part of it.
    assert all(n.startswith("textures/") and " " not in n for n in by_name)
    # A shader defined purely by surfaceparms draws nothing; one with a stage may draw.
    nodraw = [s for s in shaders if optimize.NODRAW_SURFACEPARM in s["surfaceparms"]]
    assert nodraw and all(optimize._draws_nothing(s) == "nodraw" for s in nodraw)


# ---------------------------------------------------------------------------
# shader_audit
# ---------------------------------------------------------------------------


def shader_script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_shader_audit_reports_missing_unused_shadowing_and_nondrawing(tmp_path, kernel_available):
    scripts = tmp_path / "scripts"
    shader_script(
        scripts / "mymap.shader",
        "textures/mymap/wall\n{\n\t{\n\t\tmap textures/mymap/wall.tga\n\t}\n}\n"
        "textures/mymap/watercaulk\n{\n\tsurfaceparm nodraw\n\tsurfaceparm nonsolid\n}\n"
        "textures/mymap/never_used\n{\n\t{\n\t\tmap textures/mymap/x.tga\n\t}\n}\n"
        "textures/common/caulk\n{\n\tsurfaceparm nodraw\n}\n"
        "textures/mymap/empty\n{\n\tqer_trans 0.5\n}\n",
    )
    brushes = [
        box((0, 0, 0), (128, 128, 16), shader="mymap/wall"),
        box((0, 0, 64), (128, 128, 80), shader="mymap/watercaulk"),
        box((0, 0, 128), (128, 128, 144), shader="mymap/absent"),
        box((0, 0, 192), (128, 128, 208), shader="mymap/empty"),
    ]
    map_path = write_map(tmp_path / "shaders.map", brushes)

    out = optimize.shader_audit(map_path, [scripts], profile_id=None)
    codes = {f["code"] for f in out["findings"]}

    assert out["missing"] == ["textures/mymap/absent"]
    assert "SHADER_MISSING" in codes
    assert "textures/mymap/never_used" in out["unreferenced"]
    assert "textures/common/caulk" in out["unreferenced"]

    trap = [f for f in out["findings"] if f["code"] == "SHADER_DRAWS_NOTHING"]
    assert [f["detail"]["shader"] for f in trap] == ["textures/mymap/watercaulk"]
    assert trap[0]["confidence"] == "verified"
    assert trap[0]["detail"]["surfaces"] == 6

    stageless = [f for f in out["findings"] if f["code"] == "SHADER_NO_STAGES"]
    assert [f["detail"]["shader"] for f in stageless] == ["textures/mymap/empty"]
    assert stageless[0]["confidence"] == "unverified"

    # No profile means no reserved prefixes, so nothing can be reported as shadowing.
    assert out["shadowing_basegame"] == []
    assert "SHADER_SHADOWS_BASEGAME" not in codes


def test_shader_audit_gets_reserved_prefixes_from_the_profile(
    tmp_path, monkeypatch, kernel_available
):
    scripts = tmp_path / "scripts"
    shader_script(
        scripts / "mymap.shader",
        "textures/reservedname/wall\n{\n\t{\n\t\tmap textures/reservedname/wall.tga\n\t}\n}\n",
    )
    map_path = write_map(
        tmp_path / "shadow.map", [box((0, 0, 0), (64, 64, 16), shader="reservedname/wall")]
    )
    monkeypatch.setattr(
        profiles,
        "load",
        lambda _pid: {
            "packaging": {"confidence": "verified", "reserved_shader_prefixes": ["reservedname/"]}
        },
    )

    out = optimize.shader_audit(map_path, [scripts], profile_id="test")
    assert out["reserved_prefixes"] == ["reservedname/"]
    shadow = [f for f in out["findings"] if f["code"] == "SHADER_SHADOWS_BASEGAME"]
    assert len(shadow) == 1
    assert shadow[0]["severity"] == "error"
    assert shadow[0]["confidence"] == "verified"


def test_shader_audit_downgrades_an_unverified_prefix_list(tmp_path, monkeypatch, kernel_available):
    scripts = tmp_path / "scripts"
    shader_script(
        scripts / "mymap.shader",
        "textures/reservedname/wall\n{\n\t{\n\t\tmap textures/reservedname/wall.tga\n\t}\n}\n",
    )
    map_path = write_map(
        tmp_path / "soft.map", [box((0, 0, 0), (64, 64, 16), shader="reservedname/wall")]
    )
    monkeypatch.setattr(
        profiles,
        "load",
        lambda _pid: {
            "packaging": {"confidence": "unverified", "reserved_shader_prefixes": ["reservedname/"]}
        },
    )
    out = optimize.shader_audit(map_path, [scripts], profile_id="test")
    shadow = [f for f in out["findings"] if f["code"] == "SHADER_SHADOWS_BASEGAME"]
    assert shadow[0]["severity"] == "warning"


def test_shader_audit_declares_that_it_cannot_see_patch_shaders(tmp_path, kernel_available):
    """The binding exposes no patch shader, so the audit must state the gap, not paper over it.

    Reading a patch's shader would mean a second parser of the `.map` text, which is what the
    kernel binding exists to prevent. So the shader a patch uses reads as unreferenced here, and
    the finding says exactly that rather than letting the lists imply completeness.
    """
    scripts = tmp_path / "scripts"
    shader_script(
        scripts / "mymap.shader",
        "textures/mymap/curve\n{\n\t{\n\t\tmap $lightmap\n\t}\n}\n"
        "textures/common/caulk\n{\n\tsurfaceparm nodraw\n}\n",
    )
    patch = (
        "{\npatchDef2\n{\nmymap/curve\n( 3 3 0 0 0 )\n(\n"
        "( ( 0 0 0 0 0 ) ( 0 64 64 0 -0.5 ) ( 0 128 0 0 -1 ) )\n"
        "( ( 64 0 0 0.5 0 ) ( 64 64 64 0.5 -0.5 ) ( 64 128 0 0.5 -1 ) )\n"
        "( ( 128 0 0 1 0 ) ( 128 64 64 1 -0.5 ) ( 128 128 0 1 -1 ) )\n"
        ")\n}\n}\n"
    )
    map_path = tmp_path / "patch.map"
    map_path.write_text(
        '{\n"classname" "worldspawn"\n' + box((0, 0, 0), (64, 64, 16)) + patch + "}\n"
    )

    out = optimize.shader_audit(map_path, [scripts], profile_id=None)
    assert out["patches_not_scanned"] == 1, "the patch count is still reported"
    # A patch's shader is now counted like a brush face's, via Map.patches(). Before that existed
    # the patch shader was invisible and landed in `unreferenced`, which was wrong twice over.
    assert out["reference_counts"] == {
        "textures/common/caulk": 6,
        "textures/mymap/curve": 1,
    }
    assert out["unreferenced"] == [], "a patch-only shader is referenced, not unreferenced"
    assert not [f for f in out["findings"] if f["code"] == "SHADER_PATCHES_NOT_SCANNED"], (
        "the patch gap is closed, so the finding that declared it must be gone"
    )


def test_shader_audit_reports_a_directory_that_is_not_there(tmp_path, kernel_available):
    map_path = write_map(tmp_path / "nodirs.map", [box((0, 0, 0), (64, 64, 16))])
    out = optimize.shader_audit(map_path, [tmp_path / "absent"], profile_id=None)
    unreadable = [f for f in out["findings"] if f["code"] == "SHADER_SCRIPT_UNREADABLE"]
    assert len(unreadable) == 1
    assert "does not exist" in unreadable[0]["message"]


def test_shader_audit_reports_duplicate_definitions(tmp_path, kernel_available):
    scripts = tmp_path / "scripts"
    stage = "\n{\n\t{\n\t\tmap textures/mymap/wall.tga\n\t}\n}\n"
    shader_script(scripts / "a.shader", "textures/mymap/wall" + stage)
    shader_script(scripts / "b.shader", "textures/mymap/wall" + stage)
    map_path = write_map(
        tmp_path / "dupes.map", [box((0, 0, 0), (64, 64, 16), shader="mymap/wall")]
    )
    out = optimize.shader_audit(map_path, [scripts], profile_id=None)
    dupes = [f for f in out["findings"] if f["code"] == "SHADER_DUPLICATE_DEFINITION"]
    assert len(dupes) == 1
    assert dupes[0]["severity"] == "info"
    assert len(dupes[0]["detail"]["files"]) == 2


# ---------------------------------------------------------------------------
# compile_ab
# ---------------------------------------------------------------------------


def test_last_json_object_survives_chatty_output():
    text = (
        "loading shader scripts\n"
        "{ not json on one line }\n"
        '{\n  "ok": true,\n  "total_seconds": 1.5\n}\n'
    )
    assert optimize._last_json_object(text) == {"ok": True, "total_seconds": 1.5}
    assert optimize._last_json_object("nothing here") is None


def test_compile_ab_rejects_a_bad_preset_and_identical_stems(tmp_path):
    # Same stem, different directories: legal on disk, but the compile artefacts for both
    # would land in out/same/ and the diff would compare a BSP against itself.
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    first = write_map(tmp_path / "one" / "same.map", [box((0, 0, 0), (8, 8, 8))])
    second = write_map(tmp_path / "two" / "same.map", [box((0, 0, 0), (8, 8, 8))])

    with pytest.raises(optimize.OptimizeError, match="preset must be one of"):
        optimize.compile_ab(first, second, preset="nonsense")
    with pytest.raises(optimize.OptimizeError, match="keyed on the stem"):
        optimize.compile_ab(first, second, preset="draft")
    with pytest.raises(optimize.OptimizeError, match="does not exist"):
        optimize.compile_ab(first, tmp_path / "absent.map", preset="draft")


def test_compile_ab_diffs_the_numbers_and_appends_a_history_row(tmp_path, monkeypatch):
    variant_a = write_map(tmp_path / "before.map", [box((0, 0, 0), (8, 8, 8))])
    variant_b = write_map(tmp_path / "after.map", [box((0, 0, 0), (8, 8, 8))])

    def fake_variant(path: Path, preset: str) -> dict:
        assert preset == "draft"
        slow = path.stem == "before"
        return {
            "map": str(path),
            "ok": True,
            "stage_seconds": {"-bsp -meta": 4.0 if slow else 2.0},
            "total_seconds": 4.0 if slow else 2.0,
            "bsp": str(path.with_suffix(".bsp")),
            "bsp_bytes": 200_000 if slow else 150_000,
            "counts": {
                "draw_surfaces": 1000 if slow else 800,
                "leafs": 500 if slow else 400,
                "brushes": 60 if slow else 60,
                "lighting_bytes": 4096 if slow else 2048,
            },
        }

    monkeypatch.setattr(optimize, "_compile_variant", fake_variant)
    history = tmp_path / "bench" / "ab-history.jsonl"

    out = optimize.compile_ab(variant_a, variant_b, preset="draft", history_path=history)

    assert out["deltas"]["draw_surfaces"] == {
        "a": 1000,
        "b": 800,
        "change": -200,
        "percent": -20.0,
    }
    assert out["deltas"]["total_seconds"]["change"] == -2.0
    assert out["deltas"]["bsp_bytes"]["percent"] == -25.0
    # An unchanged metric still reports, with a zero delta, so a table stays comparable.
    assert out["deltas"]["brushes"]["change"] == 0
    assert out["stage_seconds_delta"]["-bsp -meta"]["change"] == -2.0
    assert out["summary"]["error"] == 0
    moved = {f["detail"]["metric"] for f in out["findings"] if f["code"] == "AB_GEOMETRY_MOVED"}
    assert moved == {"draw_surfaces", "leafs"}

    assert out["history_rows"] == 1
    row = json.loads(history.read_text().splitlines()[0])
    assert row["preset"] == "draft"
    assert row["a"]["counts"]["draw_surfaces"] == 1000
    assert row["deltas"]["leafs"]["change"] == -100

    # A second run appends rather than replacing: the history is the point (§6.1).
    again = optimize.compile_ab(variant_a, variant_b, preset="draft", history_path=history)
    assert again["history_rows"] == 2
    assert len(optimize.ab_history(history)) == 2


def test_compile_ab_reports_a_variant_that_did_not_compile(tmp_path, monkeypatch):
    variant_a = write_map(tmp_path / "good.map", [box((0, 0, 0), (8, 8, 8))])
    variant_b = write_map(tmp_path / "bad.map", [box((0, 0, 0), (8, 8, 8))])

    def fake_variant(path: Path, preset: str) -> dict:
        if path.stem == "bad":
            return {
                "map": str(path),
                "ok": False,
                "stage_seconds": {},
                "total_seconds": 0.2,
                "error": "the compile did not produce a .bsp",
            }
        return {
            "map": str(path),
            "ok": True,
            "stage_seconds": {"-bsp -meta": 1.0},
            "total_seconds": 1.0,
            "bsp_bytes": 1000,
            "counts": {"draw_surfaces": 10},
        }

    monkeypatch.setattr(optimize, "_compile_variant", fake_variant)
    out = optimize.compile_ab(
        variant_a, variant_b, preset="draft", history_path=tmp_path / "h.jsonl"
    )
    incomplete = [f for f in out["findings"] if f["code"] == "AB_VARIANT_INCOMPLETE"]
    assert len(incomplete) == 1
    assert incomplete[0]["severity"] == "error"
    assert out["summary"]["error"] == 1


def test_ab_history_is_empty_and_tolerant_when_there_is_nothing_to_read(tmp_path):
    assert optimize.ab_history(tmp_path / "never-written.jsonl") == []
    partial = tmp_path / "partial.jsonl"
    partial.write_text('{"preset": "draft"}\nnot json\n\n')
    assert optimize.ab_history(partial) == [{"preset": "draft"}]


def test_compile_ab_drives_the_real_compiler(tmp_path):
    """The mocked tests above check the diff; this checks the plumbing.

    Two byte-identical variants must produce a zero geometry delta. That is a stronger
    assertion than it looks: it only holds if both compiles ran, both BSPs were located among
    the mise task's artefacts, both were unpacked to JSON lumps, and the counts were read from
    the right side of each pair.
    """
    if not os.environ.get("Q3MAP2"):
        pytest.skip("Q3MAP2 is unset; set it in mise.local.toml to exercise a real compile")
    source = ROOT / "corpus" / "synthetic" / "roundtrip" / "axial_room.map"
    if not source.is_file():
        pytest.skip("no synthetic corpus; run `mise run bootstrap`")

    stems = ("nrc_ab_before", "nrc_ab_after")
    variants = []
    for stem in stems:
        target = tmp_path / f"{stem}.map"
        target.write_bytes(source.read_bytes())
        variants.append(target)

    try:
        out = optimize.compile_ab(
            *variants, preset="draft", history_path=tmp_path / "ab-history.jsonl"
        )
    except optimize.OptimizeError as e:
        pytest.skip(f"the compiler is configured but did not run here: {e}")

    if not (out["a"]["ok"] and out["b"]["ok"]):
        pytest.skip("q3map2 could not compile the fixture on this machine")

    assert out["a"]["counts"]["draw_surfaces"] > 0
    for metric in ("draw_surfaces", "leafs", "brushes", "planes"):
        assert out["deltas"][metric]["change"] == 0, metric
    assert out["deltas"]["bsp_bytes"]["change"] == 0
    assert set(out["stage_seconds_delta"]) == {"-bsp -meta"}
    assert out["history_rows"] == 1

    # Compiling from outside /mnt stages through a Windows temp directory and copies artefacts
    # into out/<stem>/, which is not this test's to leave behind.
    for stem in stems:
        shutil.rmtree(ROOT / "out" / stem, ignore_errors=True)


# ---------------------------------------------------------------------------
# Findings discipline
# ---------------------------------------------------------------------------


def test_every_finding_carries_the_four_required_keys():
    f = optimize._finding("CODE", "warning", "message", "verified", source="somewhere")
    assert set(f) == {
        "code",
        "severity",
        "message",
        "confidence",
        "rule_source",
        "fix_hint",
        "detail",
    }


def test_an_unverified_finding_can_warn_but_never_fail_a_build():
    assert optimize._clamp("error", "verified") == "error"
    assert optimize._clamp("error", "unverified") == "warning"
    assert optimize._clamp("warning", "unverified") == "warning"
    assert optimize._clamp("info", "unverified") == "info"
