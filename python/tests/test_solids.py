"""Phase 4: the Solid IR through the bindings, and sidecar persistence."""

from __future__ import annotations

from pathlib import Path

import pytest
from nrc_mcp import kernel, solids

ROOM = {
    "op": "hollow",
    "solid": {"op": "box", "min": [0, 0, 0], "max": [512, 512, 256]},
    "thickness": 16,
}
DOORWAY = {
    "op": "carve_opening",
    "wall": {"op": "box", "min": [0, 0, 0], "max": [256, 16, 128]},
    "min": [96, -8, 0],
    "max": [160, 24, 96],
}


@pytest.fixture(scope="module")
def k():
    try:
        return kernel.kernel()
    except kernel.KernelUnavailable as e:
        pytest.skip(str(e))


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


def test_a_doorway_compiles_to_three_brushes(k):
    """The case §4.1 names, end to end through the Python boundary."""
    r = k.solid_compile(DOORWAY, None, 8)
    assert r["brushes"] == 3
    assert r["off_grid_vertices"] == 0
    assert r["warnings"] == []
    assert r["bounds"]["size"] == [256.0, 16.0, 128.0]


def test_a_hollowed_room_compiles_to_six_walls(k):
    r = k.solid_compile(ROOM, None, 8)
    assert r["brushes"] == 6
    assert r["min_thickness"] == 16.0
    assert r["non_integer_plane_faces"] == 0


def test_composing_hollow_and_subtract_gives_a_room_with_a_door(k):
    ir = {
        "op": "subtract",
        "from": ROOM,
        "cut": [{"op": "box", "min": [224, -8, 0], "max": [288, 24, 112]}],
    }
    r = k.solid_compile(ir, None, 8)
    assert r["brushes"] >= 6
    assert r["warnings"] == []


def test_stairs_produce_one_brush_per_step(k):
    ir = {
        "op": "stair",
        "origin": [0, 0, 0],
        "width": 128,
        "steps": 8,
        "rise": 16,
        "run": 32,
        "along": "x",
        "up": "z",
    }
    r = k.solid_compile(ir, None, 8)
    assert r["brushes"] == 8
    assert r["bounds"]["size"] == [256.0, 128.0, 128.0]
    assert r["off_grid_vertices"] == 0, "axis-aligned stairs stay on the grid"


def test_an_angled_prism_has_integer_vertices_but_misses_a_coarse_grid(k):
    """What is actually true, and it is better than expected.

    A prism's vertices coincide with the integer ring points it was built from, so they are
    integers. They are not multiples of 8, though — an octagon of radius 64 has corners at ±59 —
    and that is the number a mapper cares about, so it is what gets reported.
    """
    ir = {
        "op": "prism",
        "min": [-64, -64, 0],
        "max": [64, 64, 128],
        "axis": "z",
        "sides": 8,
        "start_deg": 22.5,
    }
    exact = k.solid_compile(ir, None, 1)
    assert exact["brushes"] == 1
    assert exact["faces"] == 10
    assert exact["off_grid_vertices"] == 0, "the corners are the integer ring points"

    coarse = k.solid_compile(ir, None, 8)
    assert coarse["off_grid_vertices"] > 0, "but they are not multiples of 8"


def test_arrays_and_mirrors_compose(k):
    ir = {
        "op": "mirror",
        "node": {
            "op": "array",
            "node": {"op": "box", "min": [0, 0, 0], "max": [32, 32, 32]},
            "count": 4,
            "offset": [64, 0, 0],
        },
        "axis": "x",
        "at": 0,
    }
    r = k.solid_compile(ir, None, 8)
    assert r["brushes"] == 4
    assert r["bounds"]["max"][0] <= 0.0, "mirroring across x=0 puts it all at negative x"


# ---------------------------------------------------------------------------
# Error reporting
# ---------------------------------------------------------------------------


def test_a_bad_operator_lists_the_real_ones(k):
    with pytest.raises(ValueError, match="carve_opening"):
        k.solid_compile({"op": "extrude_along_spline"}, None, 8)


def test_a_missing_field_names_the_field_and_the_operator(k):
    with pytest.raises(ValueError, match='"max"'):
        k.solid_compile({"op": "box", "min": [0, 0, 0]}, None, 8)


def test_fractional_coordinates_are_refused_with_the_reason(k):
    with pytest.raises(ValueError, match="integer grid"):
        k.solid_compile({"op": "box", "min": [0, 0, 0.5], "max": [64, 64, 64]}, None, 8)


def test_a_nested_failure_reports_its_path(k):
    ir = {
        "op": "subtract",
        "from": {"op": "box", "min": [0, 0, 0], "max": [64, 64, 64]},
        "cut": [{"op": "box", "min": [0, 0, 0], "max": [0, 0, 0]}],
    }
    with pytest.raises(ValueError, match=r"cut\[0\]"):
        k.solid_compile(ir, None, 8)


def test_hollow_on_a_multi_part_shape_explains_the_fix(k):
    ir = {
        "op": "hollow",
        "thickness": 8,
        "solid": {
            "op": "union",
            "parts": [
                {"op": "box", "min": [0, 0, 0], "max": [64, 64, 64]},
                {"op": "box", "min": [128, 0, 0], "max": [192, 64, 64]},
            ],
        },
    }
    with pytest.raises(ValueError, match="single convex shape"):
        k.solid_compile(ir, None, 8)


def test_a_wall_thickness_with_no_cavity_says_how_big_the_shape_is(k):
    ir = {
        "op": "hollow",
        "solid": {"op": "box", "min": [0, 0, 0], "max": [64, 64, 64]},
        "thickness": 40,
    }
    with pytest.raises(ValueError, match="no cavity"):
        k.solid_compile(ir, None, 8)


def test_an_axis_typo_lists_the_valid_axes(k):
    ir = {"op": "prism", "min": [0, 0, 0], "max": [64, 64, 64], "axis": "w", "sides": 8}
    with pytest.raises(ValueError, match="x, y or z"):
        k.solid_compile(ir, None, 8)


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


def test_commit_adds_brushes_and_the_result_round_trips(k):
    """Authored geometry must satisfy the same §3.2 gate as anything else."""
    m = k.Map.parse('{\n"classname" "worldspawn"\n}\n')
    r = k.solid_commit(m, ROOM, None, 8, "worldspawn", False, "room")
    assert r["committed"] is True
    assert r["brushes_created"] == 6
    assert r["undo_group"] == "solid_commit:room"

    src = m.source()
    assert k.Map.parse(src).round_trip()["identical"], "authored maps must round-trip"
    assert "// nrc-mcp hollow" in src, "brushes should say where they came from"


def test_a_dry_run_commits_nothing(k):
    m = k.Map.parse('{\n"classname" "worldspawn"\n}\n')
    r = k.solid_commit(m, ROOM, None, 8, "worldspawn", True, None)
    assert r["committed"] is False
    assert r["brushes_created"] == 6
    assert "{" in m.source() and "// nrc-mcp" not in m.source()


def test_commit_into_a_named_entity_creates_it_when_absent(k):
    m = k.Map.parse('{\n"classname" "worldspawn"\n}\n')
    r = k.solid_commit(m, ROOM, None, 8, "group_entity_a", False, "door")
    assert r.get("created_entity") == "group_entity_a"
    assert len(m.entities(classname="group_entity_a")) == 1


def test_the_detail_bit_is_written_when_asked(k):
    m = k.Map.parse('{\n"classname" "worldspawn"\n}\n')
    k.solid_commit(m, ROOM, {"detail": True}, 8, "worldspawn", False, "trim")
    assert "134217728" in m.source()


def test_committed_geometry_validates_clean_for_axis_aligned_shapes(k):
    m = k.Map.parse('{\n"classname" "worldspawn"\n}\n')
    k.solid_commit(m, ROOM, None, 8, "worldspawn", False, "room")
    v = k.Map.parse(m.source()).validate(grid=8, severity_min="error")
    assert v["summary"]["error"] == 0, [f["message"] for f in v["findings"]]


# ---------------------------------------------------------------------------
# Sidecar
# ---------------------------------------------------------------------------


def test_sidecar_path_sits_beside_the_map():
    assert solids.sidecar_path("maps/ut4_x.map").name == "ut4_x.solids.json"


def test_a_missing_sidecar_is_an_empty_store_not_an_error(tmp_path: Path):
    assert solids.load(tmp_path / "none.map")["solids"] == {}
    assert solids.names(tmp_path / "none.map") == []


def test_put_get_and_remove(tmp_path: Path):
    m = tmp_path / "a.map"
    solids.put(m, "room", ROOM, brushes=6, notes="the main hall")
    assert solids.names(m) == ["room"]
    e = solids.get(m, "room")
    assert e["ir"] == ROOM
    assert e["brushes"] == 6
    assert e["notes"] == "the main hall"
    assert e["updated"].endswith("Z")
    assert solids.remove(m, "room") is True
    assert solids.remove(m, "room") is False


def test_replacing_a_solid_keeps_one_previous_version(tmp_path: Path):
    m = tmp_path / "a.map"
    solids.put(m, "room", ROOM)
    changed = {**ROOM, "thickness": 32}
    solids.put(m, "room", changed)
    e = solids.get(m, "room")
    assert e["ir"]["thickness"] == 32
    assert e["superseded"]["ir"]["thickness"] == 16


def test_an_unknown_name_lists_what_is_known(tmp_path: Path):
    m = tmp_path / "a.map"
    solids.put(m, "room", ROOM)
    with pytest.raises(solids.SolidStoreError, match="room"):
        solids.get(m, "hallway")


def test_a_corrupt_sidecar_says_the_map_is_unaffected(tmp_path: Path):
    m = tmp_path / "a.map"
    solids.sidecar_path(m).write_text("{not json")
    with pytest.raises(solids.SolidStoreError, match="\\.map itself is unaffected"):
        solids.load(m)
    # And the tolerant listing path must not raise.
    assert solids.names(m) == []


def test_bad_names_are_refused_because_they_reach_filenames(tmp_path: Path):
    with pytest.raises(solids.SolidStoreError, match="alphanumeric"):
        solids.put(tmp_path / "a.map", "../../etc/passwd", ROOM)


# ---------------------------------------------------------------------------
# Parametric editing — the payoff of keeping the IR
# ---------------------------------------------------------------------------


def test_edit_param_changes_one_field_and_leaves_the_rest(k):
    edited = solids.edit_param(ROOM, "thickness", 32)
    assert edited["thickness"] == 32
    assert ROOM["thickness"] == 16, "the original must not be mutated"
    assert edited["solid"] == ROOM["solid"]


def test_edit_param_reaches_into_nested_nodes_and_lists():
    ir = {
        "op": "subtract",
        "from": {"op": "box", "min": [0, 0, 0], "max": [256, 16, 128]},
        "cut": [{"op": "box", "min": [96, -8, 0], "max": [160, 24, 96]}],
    }
    # Widen the wall: one field, where by hand it would be several faces.
    wider = solids.edit_param(ir, "from.max[1]", 32)
    assert wider["from"]["max"] == [256, 32, 128]
    # Move the opening.
    moved = solids.edit_param(ir, "cut[0].min", [120, -8, 0])
    assert moved["cut"][0]["min"] == [120, -8, 0]
    assert ir["cut"][0]["min"] == [96, -8, 0]


def test_widening_a_corridor_is_one_edit_and_recompiles(k):
    """§4.4's motivating example: "make that corridor 32 units wider"."""
    corridor = {
        "op": "hollow",
        "solid": {"op": "box", "min": [0, 0, 0], "max": [1024, 128, 128]},
        "thickness": 16,
    }
    before = k.solid_compile(corridor, None, 8)
    wider = solids.edit_param(corridor, "solid.max[1]", 160)
    after = k.solid_compile(wider, None, 8)
    assert after["bounds"]["size"][1] == before["bounds"]["size"][1] + 32
    assert after["brushes"] == before["brushes"], "the shape changed size, not structure"


def test_a_bad_path_lists_what_is_available():
    with pytest.raises(solids.SolidStoreError, match="available"):
        solids.edit_param(ROOM, "thikness", 32)
    with pytest.raises(solids.SolidStoreError, match="does not exist"):
        solids.edit_param(ROOM, "solid.nope.deeper", 1)


def test_an_out_of_range_index_is_refused():
    ir = {"op": "union", "parts": [{"op": "box", "min": [0, 0, 0], "max": [8, 8, 8]}]}
    with pytest.raises(solids.SolidStoreError, match="out of range|does not exist"):
        solids.edit_param(ir, "parts[5].min", [0, 0, 0])


def test_describe_outlines_the_tree_with_editable_paths():
    ir = {
        "op": "subtract",
        "from": ROOM,
        "cut": [{"op": "box", "min": [0, 0, 0], "max": [8, 8, 8]}],
    }
    lines = solids.describe(ir)
    text = "\n".join(lines)
    assert "subtract" in text
    assert "hollow" in text
    assert "thickness=16" in text
    assert "cut[0]" in text
