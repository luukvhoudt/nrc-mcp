"""Phase 5: the Blender handoff — brief, import validation, placement, collision hull."""

from __future__ import annotations

from pathlib import Path

import pytest
from nrc_mcp import blender, kernel, profiles

BOUNDS = {"x": [0, 96], "y": [0, 64], "z": [0, 72]}


@pytest.fixture(scope="module")
def pid() -> str:
    av = profiles.available()
    if not av:
        pytest.skip("no profiles on disk")
    return av[0]


def write_obj(
    path: Path,
    size: tuple[float, float, float],
    *,
    uv=True,
    material="m/a",
    offset=(0.0, 0.0, 0.0),
    ngon=False,
) -> Path:
    """A box OBJ of the requested size, as an export would produce."""
    sx, sy, sz = size
    ox, oy, oz = offset
    verts = [
        (ox, oy, oz),
        (ox + sx, oy, oz),
        (ox + sx, oy + sy, oz),
        (ox, oy + sy, oz),
        (ox, oy, oz + sz),
        (ox + sx, oy, oz + sz),
        (ox + sx, oy + sy, oz + sz),
        (ox, oy + sy, oz + sz),
    ]
    lines = [f"o {path.stem}", f"usemtl {material}"]
    lines += [f"v {x} {y} {z}" for x, y, z in verts]
    if uv:
        lines += ["vt 0 0", "vt 1 0", "vt 1 1", "vt 0 1"]
    quads = [(1, 2, 3, 4), (5, 6, 7, 8), (1, 2, 6, 5), (2, 3, 7, 6), (3, 4, 8, 7), (4, 1, 5, 8)]
    for q in quads:
        if ngon:
            lines.append("f " + " ".join(f"{i}/1" if uv else str(i) for i in (*q, q[0])))
        elif uv:
            lines.append("f " + " ".join(f"{i}/{j + 1}" for j, i in enumerate(q)))
        else:
            lines.append("f " + " ".join(str(i) for i in q))
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# The brief
# ---------------------------------------------------------------------------


def test_the_brief_states_every_number_the_modeller_needs(pid):
    b = blender.blender_brief(
        pid,
        "crate_a",
        "cover object",
        BOUNDS,
        materials=[{"slot": 0, "name": "m/crate"}],
        silhouette_notes="readable from 1200 units",
    )
    assert b["size_qu"] == {"x": 96, "y": 64, "z": 72}
    assert b["units"]["quake_units_per_metre"] == 39.37
    assert b["export"]["path"].endswith("crate_a.obj")
    assert b["collision"]["strategy"] == "brush_hull"
    assert b["collision"]["shader"], "a hull strategy must name the clip shader"

    p = b["prompt"]
    for expected in (
        "96 x 64 x 72",
        "39.37",
        "m/crate",
        "Apply every transform",
        "readable from 1200 units",
        "arbitrary Python",
    ):
        assert expected in p, f"the prompt should mention {expected!r}"


def test_the_unit_scale_comes_from_the_profile_not_the_code(pid, monkeypatch):
    """§7.4 names this constant specifically as a place game specifics leak into code."""
    fake = {
        "assets": {
            "model_entity": "e",
            "units": {"units_per_metre": 100.0},
            "clip_shaders": {"player": "c/p"},
        }
    }
    monkeypatch.setattr(profiles, "load", lambda _p: fake)
    b = blender.blender_brief(pid, "a", "p", BOUNDS)
    assert b["units"]["quake_units_per_metre"] == 100.0
    assert "100.0" in b["prompt"]


def test_a_profile_without_an_assets_section_says_what_is_missing(pid, monkeypatch):
    monkeypatch.setattr(profiles, "load", lambda _p: {})
    with pytest.raises(blender.AssetError, match="assets"):
        blender.blender_brief(pid, "a", "p", BOUNDS)


def test_bad_bounds_are_refused(pid):
    with pytest.raises(blender.AssetError, match="increasing"):
        blender.blender_brief(pid, "a", "p", {"x": [96, 0], "y": [0, 1], "z": [0, 1]})
    with pytest.raises(blender.AssetError, match="\\[lo, hi\\]"):
        blender.blender_brief(pid, "a", "p", {"x": [0], "y": [0, 1], "z": [0, 1]})


def test_the_collision_reason_is_stated_per_strategy(pid):
    for strategy, expect in [
        ("brush_hull", "predictable"),
        ("autoclip", "debugclip"),
        ("nonsolid", "ornament"),
    ]:
        b = blender.blender_brief(pid, "a", "p", BOUNDS, collision=strategy)
        assert expect in b["collision"]["reason"]
    with pytest.raises(blender.AssetError, match="nonsolid, autoclip or brush_hull"):
        blender.blender_brief(pid, "a", "p", BOUNDS, collision="magic")


# ---------------------------------------------------------------------------
# OBJ reading
# ---------------------------------------------------------------------------


def test_obj_parsing_reads_what_validation_needs(tmp_path: Path):
    m = blender.parse_obj(write_obj(tmp_path / "a.obj", (96, 64, 72)))
    assert len(m.vertices) == 8
    assert len(m.faces) == 6
    assert m.triangles() == 12, "six quads fan into twelve triangles"
    assert m.materials == ["m/a"]
    assert m.has_uv is True
    assert m.size() == (96.0, 64.0, 72.0)


def test_obj_handles_negative_indices_and_missing_uvs(tmp_path: Path):
    p = tmp_path / "n.obj"
    p.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf -3 -2 -1\n")
    m = blender.parse_obj(p)
    assert m.faces == [[0, 1, 2]]
    assert m.has_uv is False


def test_a_file_that_is_not_an_obj_says_so(tmp_path: Path):
    p = tmp_path / "empty.obj"
    p.write_text("# nothing here\n")
    with pytest.raises(blender.AssetError, match="no vertices"):
        blender.parse_obj(p)
    with pytest.raises(blender.AssetError, match="does not exist"):
        blender.parse_obj(tmp_path / "absent.obj")


def test_a_malformed_line_names_the_line_number(tmp_path: Path):
    p = tmp_path / "bad.obj"
    p.write_text("v 0 0 0\nv notanumber 0 0\n")
    with pytest.raises(blender.AssetError, match=":2:"):
        blender.parse_obj(p)


# ---------------------------------------------------------------------------
# Import validation — the scale check above all
# ---------------------------------------------------------------------------


def test_a_correctly_sized_mesh_passes(tmp_path: Path, pid):
    brief = blender.blender_brief(pid, "a", "p", BOUNDS, materials=[{"slot": 0, "name": "m/a"}])
    r = blender.model_import(write_obj(tmp_path / "a.obj", (96, 64, 72)), brief)
    assert r["ok"] is True
    assert r["summary"]["error"] == 0
    assert r["triangles"] == 12


def test_the_metres_as_units_mistake_is_named_not_just_measured(tmp_path: Path, pid):
    """§5.3 calls this the #1 failure. Naming the cause is the whole point."""
    brief = blender.blender_brief(pid, "a", "p", BOUNDS)
    small = write_obj(tmp_path / "s.obj", (96 / 39.37, 64 / 39.37, 72 / 39.37))
    r = blender.model_import(small, brief)
    f = next(x for x in r["findings"] if x["code"] == "MODEL_WRONG_SCALE")
    assert f["severity"] == "error"
    assert "multiply by 39.37" in f["message"], f["message"]
    assert r["ok"] is False


def test_the_units_as_metres_mistake_is_also_named(tmp_path: Path, pid):
    brief = blender.blender_brief(pid, "a", "p", BOUNDS)
    big = write_obj(tmp_path / "b.obj", (96 * 39.37, 64 * 39.37, 72 * 39.37))
    r = blender.model_import(big, brief)
    f = next(x for x in r["findings"] if x["code"] == "MODEL_WRONG_SCALE")
    assert "disable unit scaling" in f["message"], f["message"]


def test_a_mesh_larger_than_its_volume_is_an_error(tmp_path: Path, pid):
    brief = blender.blender_brief(pid, "a", "p", BOUNDS)
    r = blender.model_import(write_obj(tmp_path / "a.obj", (96, 64, 100)), brief)
    assert any(f["code"] == "MODEL_EXCEEDS_BOUNDS" for f in r["findings"])


def test_an_offset_origin_is_reported(tmp_path: Path, pid):
    brief = blender.blender_brief(pid, "a", "p", BOUNDS)
    r = blender.model_import(write_obj(tmp_path / "a.obj", (96, 64, 72), offset=(50, 0, 0)), brief)
    f = next(x for x in r["findings"] if x["code"] == "MODEL_ORIGIN_NOT_AT_MIN_CORNER")
    assert f["severity"] == "warning", "an offset is recoverable at placement time"


def test_missing_uvs_and_wrong_material_names_are_errors(tmp_path: Path, pid):
    brief = blender.blender_brief(
        pid, "a", "p", BOUNDS, materials=[{"slot": 0, "name": "m/expected"}]
    )
    r = blender.model_import(
        write_obj(tmp_path / "a.obj", (96, 64, 72), uv=False, material="m/actual"), brief
    )
    codes = {f["code"] for f in r["findings"]}
    assert "MODEL_NO_UV" in codes
    assert "MODEL_MATERIAL_MISSING" in codes
    assert r["ok"] is False


def test_ngons_are_flagged_because_collision_derives_from_faces(tmp_path: Path, pid):
    brief = blender.blender_brief(pid, "a", "p", BOUNDS)
    r = blender.model_import(write_obj(tmp_path / "a.obj", (96, 64, 72), ngon=True), brief)
    assert any(f["code"] == "MODEL_NGONS" for f in r["findings"])


def test_the_triangle_budget_is_advisory_because_it_is_unverified(tmp_path: Path, pid):
    """A community norm must not fail a build — the same clamp the rule engine applies."""
    brief = blender.blender_brief(pid, "a", "p", BOUNDS, triangles=4)
    r = blender.model_import(write_obj(tmp_path / "a.obj", (96, 64, 72)), brief)
    f = next(x for x in r["findings"] if x["code"] == "MODEL_OVER_TRIANGLE_BUDGET")
    assert f["severity"] == "info", "an unverified budget cannot be an error"


def test_findings_are_ordered_worst_first(tmp_path: Path, pid):
    brief = blender.blender_brief(pid, "a", "p", BOUNDS)
    r = blender.model_import(
        write_obj(tmp_path / "a.obj", (2, 2, 2), uv=False, offset=(9, 9, 9)), brief
    )
    order = {"error": 0, "warning": 1, "info": 2}
    sev = [order[f["severity"]] for f in r["findings"]]
    assert sev == sorted(sev)


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


def test_placement_keys_come_from_the_profile(pid):
    keys = blender.model_place_keys(
        pid,
        "models/a.obj",
        [1024, -512, 16],
        angles=[0, 90, 0],
        scale=1.5,
        lightmap_scale=0.25,
        remap={"a/old": "a/new"},
    )
    d = dict(keys)
    a = profiles.load(pid)["assets"]
    assert d["classname"] == a["model_entity"]
    assert d[a["model_key"]] == "models/a.obj"
    assert d["origin"] == "1024 -512 16"
    assert d[a["scale_key"]] == "1.5"
    assert d[a["remap_key_prefix"]] == "a/old;a/new"
    # Order matters in a .map, and classname must come first.
    assert keys[0][0] == "classname"


def test_negative_scale_is_allowed_because_this_compiler_supports_it(pid):
    d = dict(blender.model_place_keys(pid, "m.obj", [0, 0, 0], scale=-1))
    a = profiles.load(pid)["assets"]
    assert d[a["scale_key"]] == "-1"


# ---------------------------------------------------------------------------
# Collision hull
# ---------------------------------------------------------------------------


def test_the_hull_contains_the_mesh_and_is_valid_ir(tmp_path: Path, pid):
    try:
        k = kernel.kernel()
    except kernel.KernelUnavailable as e:
        pytest.skip(str(e))

    obj = write_obj(tmp_path / "a.obj", (96, 64, 72))
    r = blender.model_make_clip(obj, pid, k=14, grid=8)
    assert r["planes"] == 14
    assert r["shader"].endswith("playerclip")

    # It must compile, and it must contain the mesh rather than cutting into it.
    info = k.solid_compile(r["ir"], {"default": r["shader"]}, 8)
    assert info["brushes"] == 1
    b = info["bounds"]
    assert b["min"][0] <= 0.0 and b["max"][0] >= 96.0
    assert b["min"][2] <= 0.0 and b["max"][2] >= 72.0


def test_a_larger_k_fits_more_tightly(tmp_path: Path, pid):
    try:
        k = kernel.kernel()
    except kernel.KernelUnavailable as e:
        pytest.skip(str(e))

    # A diagonal wedge: a box hull wastes a lot of volume, the corner planes recover some.
    p = tmp_path / "w.obj"
    p.write_text(
        "usemtl m/a\nv 0 0 0\nv 128 0 0\nv 0 128 0\nv 0 0 64\nv 128 0 64\nv 0 128 64\n"
        "f 1 2 3\nf 4 5 6\nf 1 2 5\nf 2 3 6\nf 3 1 4\n"
    )
    vols = {}
    for kk in (6, 14, 26):
        info = k.solid_compile(blender.model_make_clip(p, pid, k=kk, grid=1)["ir"], None, 1)
        vols[kk] = info["volume"]
    assert vols[14] < vols[6], f"corner planes should tighten the fit: {vols}"
    assert vols[26] <= vols[14], f"edge planes should not loosen it: {vols}"


def test_the_hull_is_pushed_outward_never_inward(tmp_path: Path, pid):
    """A hull that cuts into the visual lets players clip through a corner."""
    try:
        k = kernel.kernel()
    except kernel.KernelUnavailable as e:
        pytest.skip(str(e))
    # Deliberately awkward size so grid rounding has to do something.
    obj = write_obj(tmp_path / "a.obj", (97, 63, 71))
    info = k.solid_compile(blender.model_make_clip(obj, pid, k=6, grid=16)["ir"], None, 1)
    b = info["bounds"]
    assert b["max"][0] >= 97.0 and b["max"][1] >= 63.0 and b["max"][2] >= 71.0


def test_clip_kinds_and_bad_arguments(tmp_path: Path, pid):
    obj = write_obj(tmp_path / "a.obj", (32, 32, 32))
    assert "weapclip" in blender.model_make_clip(obj, pid, kind="weapon")["shader"]
    with pytest.raises(blender.AssetError, match="clip shader"):
        blender.model_make_clip(obj, pid, kind="nonsense")
    with pytest.raises(blender.AssetError, match="k must be"):
        blender.model_make_clip(obj, pid, k=7)


def test_scale_and_origin_move_the_hull(tmp_path: Path, pid):
    try:
        k = kernel.kernel()
    except kernel.KernelUnavailable as e:
        pytest.skip(str(e))
    obj = write_obj(tmp_path / "a.obj", (32, 32, 32))
    r = blender.model_make_clip(obj, pid, origin=[512.0, 0.0, 0.0], scale=2.0, k=6, grid=1)
    info = k.solid_compile(r["ir"], None, 1)
    assert info["bounds"]["min"][0] >= 512.0
    assert info["bounds"]["size"][0] >= 64.0, "scale 2 doubles a 32-unit box"
