"""Tests for the Python side of the geometry kernel bridge."""

from __future__ import annotations

from pathlib import Path

import pytest

from nrc_mcp import kernel

BOX = """\
{
"classname" "worldspawn"
{
( 0 0 64 ) ( 0 1 64 ) ( 1 0 64 ) t/top 0 0 0 0.500000 0.500000 0 0 0
( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) t/bot 0 0 0 0.500000 0.500000 0 0 0
( 0 0 0 ) ( 0 0 1 ) ( 1 0 0 ) t/side 0 0 0 0.500000 0.500000 0 0 0
( 0 64 0 ) ( 1 64 0 ) ( 0 64 1 ) t/side 0 0 0 0.500000 0.500000 0 0 0
( 0 0 0 ) ( 0 1 0 ) ( 0 0 1 ) t/side 0 0 0 0.500000 0.500000 0 0 0
( 64 0 0 ) ( 64 0 1 ) ( 64 1 0 ) t/side 0 0 0 0.500000 0.500000 0 0 0
}
}
"""


@pytest.fixture(scope="module")
def k():
    try:
        return kernel.kernel()
    except kernel.KernelUnavailable as e:
        pytest.skip(str(e))


def test_round_trip_is_byte_identical(k):
    m = k.Map.parse(BOX)
    rt = m.round_trip()
    assert rt["identical"] is True
    assert rt["input_bytes"] == rt["output_bytes"]
    assert m.source() == BOX


def test_texdef_formatting_is_preserved_not_normalized(k):
    # 0.500000 must not become 0.5. This is the property the whole gate rests on.
    assert "0.500000" in k.Map.parse(BOX).source()


def test_stats_describe_the_box(k):
    s = k.Map.parse(BOX).stats(grid=8)
    assert s["brushes"] == 1
    assert s["faces"] == 6
    assert s["vertices_total"] == 8
    assert s["vertices_on_grid"] == 8
    assert s["grid_fraction"] == 1.0
    assert s["bounds"]["size"] == [64.0, 64.0, 64.0]
    assert s["texdef_kinds"] == ["axial"]
    # No detail bit set anywhere, so every brush is structural.
    assert (s["structural_brushes"], s["detail_brushes"]) == (1, 0)


def test_shader_histogram_ranks_by_face_count(k):
    top = k.Map.parse(BOX).stats()["top_shaders"]
    assert top[0]["shader"] == "t/side"
    assert top[0]["faces"] == 4


def test_clean_box_has_no_findings(k):
    v = k.Map.parse(BOX).validate(grid=8)
    assert v["summary"]["error"] == 0
    assert v["findings"] == []


def test_off_grid_is_reported_as_an_error_with_provenance(k):
    v = k.Map.parse(BOX).validate(grid=128)
    codes = {f["code"] for f in v["findings"]}
    assert "BRUSH_OFF_GRID" in codes
    finding = next(f for f in v["findings"] if f["code"] == "BRUSH_OFF_GRID")
    assert finding["severity"] == "error"
    # §8.2: every finding carries a rule source and a confidence.
    assert finding["rule_source"]
    assert finding["confidence"] in {"verified", "unverified"}
    assert finding["entity"] == 0


def test_severity_filter_excludes_lower_levels(k):
    m = k.Map.parse(BOX)
    all_f = m.validate(grid=128, severity_min="info")["findings"]
    only_err = m.validate(grid=128, severity_min="error")["findings"]
    assert len(only_err) <= len(all_f)
    assert all(f["severity"] == "error" for f in only_err)


def test_bad_severity_name_is_rejected(k):
    with pytest.raises(ValueError, match="severity_min"):
        k.Map.parse(BOX).validate(severity_min="loud")


def test_entities_preserve_key_order_and_duplicates(k):
    src = '{\n"classname" "x"\n"angle" "90"\n"angle" "180"\n}\n'
    ents = k.Map.parse(src).entities()
    keys = ents[0]["keys"]
    assert keys == [("classname", "x"), ("angle", "90"), ("angle", "180")]


def test_entity_filter(k):
    src = BOX + '{\n"classname" "point_entity_a"\n"origin" "8 16 24"\n}\n'
    m = k.Map.parse(src)
    assert len(m.entities(classname="point_entity_a")) == 1
    assert m.entities(classname="point_entity_a")[0]["origin"] == [8.0, 16.0, 24.0]
    assert m.entities(classname="nothing_here") == []


def test_brush_geometry_returns_exact_vertices(k):
    g = k.Map.parse(BOX).brush_geometry(0, 0)
    assert g["usable"] is True
    assert len(g["vertices"]) == 8
    assert g["min_thickness"] == 64.0
    assert g["redundant_faces"] == []
    assert sorted(g["vertices"])[0] == [0.0, 0.0, 0.0]


def test_brush_geometry_declines_on_degenerate_input(k):
    three_faces = "{\n" + "\n".join(BOX.splitlines()[3:6]) + "\n}\n"
    m = k.Map.parse('{\n"classname" "worldspawn"\n' + three_faces + "}\n")
    g = m.brush_geometry(0, 0)
    assert g["usable"] is False
    assert "at least 4" in g["reason"]


def test_out_of_range_indices_raise_clearly(k):
    m = k.Map.parse(BOX)
    with pytest.raises(ValueError, match="no entity"):
        m.brush_geometry(99, 0)
    with pytest.raises(ValueError, match="no primitive"):
        m.brush_geometry(0, 99)


def test_parse_error_names_the_line(k):
    with pytest.raises(ValueError, match="line 3"):
        k.Map.parse('{\n"classname"\n}\n')


def test_save_requires_a_path_when_parsed_from_a_string(k):
    with pytest.raises(ValueError, match="no path given"):
        k.Map.parse(BOX).save()


def test_save_then_load_is_stable(k, tmp_path: Path):
    p = tmp_path / "out.map"
    k.Map.parse(BOX).save(str(p))
    assert k.Map.load(str(p)).source() == BOX


def test_loading_a_non_map_says_so(k, tmp_path: Path):
    p = tmp_path / "not.map"
    p.write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(ValueError, match="really a .map"):
        k.Map.load(str(p))


def test_missing_file_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        kernel.load_map("/nonexistent/nope.map")


def test_corpus_round_trips_byte_identically(k):
    """The gate, from Python. Skips cleanly if the corpus has not been imported."""
    corpus = kernel.repo_root() / "corpus"
    maps = sorted(corpus.rglob("*.map"))
    if not maps:
        pytest.skip("no corpus; run `mise run corpus:gen` and `mise run corpus:import`")

    failures = []
    for path in maps:
        rt = k.Map.load(str(path)).round_trip()
        if not rt["identical"]:
            failures.append((path.name, rt.get("first_difference")))
    assert not failures, f"{len(failures)} of {len(maps)} maps did not round-trip: {failures[:3]}"
