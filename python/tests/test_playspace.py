"""Playable space, the save gate, and the tape runner.

These exist because of a specific failure. A session asked to make roofs unreachable filled
the interiors of `ut4_dofa` with playerclip: 5,443 walkable cells under a ceiling became
clip, and 5,735 new ones appeared on top of the clip lids, 128 units up. Every check the
project had stayed green — the map round-tripped, validated, compiled and sealed — because
none of them compared the map to what it had been a moment earlier.

So the tests below are mostly about the comparison, and about the two ways it can be wrong:
missing the disaster, or blocking ordinary work. Both are represented.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from nrc_mcp import playspace
from nrc_mcp import server as srv

pytestmark = pytest.mark.usefixtures("_clean_session")

ROOM = Path("bench/tapes/fixtures/interior_room.map")
SLAB = Path("bench/tapes/fixtures/seed_slab.map")

#: The room fixture's interior: floor top at z=16, ceiling underside at z=240.
CLIP_LID = {"op": "box", "min": [16, 16, 16], "max": [496, 496, 112]}
CRATE = {"op": "box", "min": [16, 16, 16], "max": [496, 496, 112]}


@pytest.fixture
def _clean_session():
    yield
    srv.SESSION.map = None
    srv.SESSION.path = None
    srv.SESSION.warnings = []
    srv.SESSION.opened_round_trip = None


@pytest.fixture
def room(tmp_path: Path) -> Path:
    """A copy of the enclosed-room fixture, writable, in its own directory."""
    root = Path(__file__).resolve().parents[2]
    dst = tmp_path / "work.map"
    shutil.copyfile(root / ROOM, dst)
    dst.chmod(0o644)
    return dst


def _profile() -> str:
    from nrc_mcp import profiles

    available = profiles.available()
    if not available:
        pytest.skip("no profile on disk")
    return available[0]


# ---------------------------------------------------------------------------
# Clip shaders come from the profile, never from code
# ---------------------------------------------------------------------------


def test_clip_shaders_are_read_from_the_profile():
    """§7.4: a shader name is game vocabulary and may not appear in this package."""
    names = playspace.player_clip_shaders(_profile())
    assert names, "the shipped profile states player-clip shaders; none were read"
    for name in names:
        assert name == name.lower()

    source = Path(playspace.__file__).read_text()
    for name in names:
        assert name not in source, f"{name!r} is hardcoded in playspace.py"


def test_a_brush_is_clip_only_when_every_face_is():
    """One clip face on a stone brush does not make it a clip brush."""
    from nrc_mcp.analysis import Solid

    clip = playspace.player_clip_shaders(_profile())
    box = dict(
        planes=(),
        mins=(0, 0, 0),
        maxs=(1, 1, 1),
        entity=0,
        primitive=0,
        is_box=True,
        approximated=False,
    )

    assert playspace._is_clip(Solid(**box, shaders=(clip[0], clip[0])), clip)
    assert not playspace._is_clip(Solid(**box, shaders=(clip[0], "common/caulk")), clip)
    assert not playspace._is_clip(Solid(**box, shaders=()), clip)
    # With no clip shader known, nothing may be assumed to be clip.
    assert not playspace._is_clip(Solid(**box, shaders=(clip[0],)), ())


# ---------------------------------------------------------------------------
# The diff
# ---------------------------------------------------------------------------


def test_a_map_compared_with_itself_reports_nothing():
    """The baseline case. A diff that cannot say "no change" is worthless."""
    report = playspace.diff(ROOM, ROOM, profile_id=_profile())
    assert report["lost_cells"] == 0
    assert report["gained_cells"] == 0
    assert report["findings"] == []


def test_clip_over_an_interior_floor_is_an_error(room: Path, tmp_path: Path):
    """The ut4_dofa failure in miniature: both halves of it, both as errors."""
    srv.map_open(str(room))
    srv.solid_commit(
        ir=CLIP_LID, label="lid", textures={"default": playspace.player_clip_shaders(_profile())[0]}
    )
    report = playspace.diff(room, (str(room), srv.SESSION.map), profile_id=_profile())

    codes = {f["code"]: f["severity"] for f in report["findings"]}
    assert codes.get("PLAYSPACE_INTERIOR_SEALED_BY_CLIP") == "error"
    assert codes.get("PLAYSPACE_CLIP_SURFACE_WALKABLE") == "error"
    assert report["interior_sealed_by_clip_cells"] > 100
    assert report["walkable_on_clip_cells"] > 100
    assert playspace.has_errors(report)


def test_ordinary_geometry_over_an_interior_floor_is_only_a_warning(room: Path):
    """The false positive that matters.

    Placing a solid indoors removes floor too. If that were an error the gate would block
    every real edit, and a gate that blocks everything gets switched off — which is exactly
    how the round-trip guard stopped protecting anything.
    """
    srv.map_open(str(room))
    srv.solid_commit(ir=CRATE, label="crate", textures={"default": "common/caulk"})
    report = playspace.diff(room, (str(room), srv.SESSION.map), profile_id=_profile())

    codes = {f["code"]: f["severity"] for f in report["findings"]}
    assert "PLAYSPACE_INTERIOR_SEALED_BY_CLIP" not in codes
    assert codes.get("PLAYSPACE_INTERIOR_SEALED") == "warning"
    assert not playspace.has_errors(report)


def test_grids_that_cannot_be_compared_say_so():
    """Silently comparing mismatched grids would shift every index by an unknown amount."""
    from nrc_mcp.analysis import build_navgrid

    a = build_navgrid(ROOM, cell=32.0, profile_id=_profile())
    b = build_navgrid(ROOM, cell=64.0, profile_id=_profile())
    with pytest.raises(playspace.PlayspaceError, match="cell size"):
        playspace._alignment(a, b)


def test_findings_clamp_to_the_confidence_of_the_constants_they_rest_on():
    """A finding may only be an error if the movement constant behind it is verified."""
    from nrc_mcp.analysis import movement_constants

    movement = movement_constants(_profile())
    report = playspace.diff(ROOM, ROOM, profile_id=_profile())
    assert report["profile"] == _profile()
    # The shipped profile verifies standing headroom, so this project's findings can bite.
    assert movement.headroom_stand.verified is True


# ---------------------------------------------------------------------------
# Telling a rooftop from an interior, which is harder than it looks
# ---------------------------------------------------------------------------

PLAZA = Path("bench/scenarios/fixtures/plaza_building.map")
ROOF = (384, 256, 256)
INSIDE = (384, 256, 32)
STREET = (832, 256, 0)


@pytest.fixture
def plaza(tmp_path: Path) -> Path:
    """A plaza with one building, under a skybox — the shape of a real map."""
    root = Path(__file__).resolve().parents[2]
    dst = tmp_path / "work.map"
    shutil.copyfile(root / PLAZA, dst)
    dst.chmod(0o644)
    return dst


def _near(profile: str) -> float:
    from nrc_mcp.analysis import movement_constants

    return movement_constants(profile).headroom_stand.value * playspace.NEAR_CEILING_HEIGHTS


def test_a_skybox_does_not_make_every_rooftop_an_interior():
    """The bug this test exists for.

    A sky brush is solid to the voxelizer, so "is there anything above this cell" answers yes
    for every walkable cell in a sealed map — on `ut4_dofa`, all 27,965 of them. A rule built
    on that question calls rooftops interiors and fires on exactly the roof clipping it was
    written to allow. `_is_indoors` asks a different question; this pins the difference.
    """
    from nrc_mcp.analysis import build_navgrid

    profile = _profile()
    grid = build_navgrid(PLAZA, cell=32.0, profile_id=profile)
    near = _near(profile)

    for label, position in (("roof", ROOF), ("street", STREET)):
        z = playspace._standable_near(grid, position, 32.0)
        assert z is not None, f"{label} should be standable in the fixture"
        cell = grid.cell_at((position[0], position[1], z))
        assert playspace._ceiling_distance(grid, cell) is not None, (
            f"the skybox should put something above the {label} — otherwise this fixture "
            "does not reproduce the condition being tested"
        )
        assert playspace._is_indoors(grid, cell, near) is None, f"{label} read as indoors"

    z = playspace._standable_near(grid, INSIDE, 32.0)
    cell = grid.cell_at((INSIDE[0], INSIDE[1], z))
    assert playspace._is_indoors(grid, cell, near) is not None, (
        "the building interior read as outdoors"
    )


def _clip_case(map_path: Path, ir: dict, profile: str) -> tuple[dict, dict]:
    srv.map_open(str(map_path))
    srv.solid_commit(
        ir=ir, label="clip", textures={"default": playspace.player_clip_shaders(profile)[0]}
    )
    saved = srv.map_save()
    report = playspace.diff(PLAZA, (str(map_path), srv.SESSION.map), profile_id=profile)
    return saved, report


def test_clipping_a_roof_all_the_way_up_is_allowed(plaza: Path):
    """The correct technique has to get through, or the gate is just an obstacle."""
    saved, report = _clip_case(
        plaza, {"op": "box", "min": [128, 128, 256], "max": [640, 384, 1024]}, _profile()
    )
    assert saved["written"] is True
    assert not playspace.has_errors(report)
    assert report["outdoor_cells_removed"] > 0, "the roof should have been removed"
    assert report["interior_sealed_by_clip_cells"] == 0
    assert report["walkable_on_clip_cells"] == 0


def test_capping_a_roof_with_a_lid_is_refused(plaza: Path):
    """The ut4_dofa technique. The lid does not remove the roof, it raises it."""
    saved, report = _clip_case(
        plaza, {"op": "box", "min": [128, 128, 256], "max": [640, 384, 384]}, _profile()
    )
    assert saved["written"] is False
    codes = {f["code"] for f in report["findings"] if f["severity"] == "error"}
    assert codes == {"PLAYSPACE_CLIP_SURFACE_WALKABLE"}
    assert report["walkable_on_clip_cells"] > 0


def test_clipping_the_building_interior_is_refused(plaza: Path):
    """The ut4_dofa outcome, on a map where the roof is genuinely outdoors."""
    saved, report = _clip_case(
        plaza, {"op": "box", "min": [144, 144, 16], "max": [624, 368, 112]}, _profile()
    )
    assert saved["written"] is False
    codes = {f["code"] for f in report["findings"] if f["severity"] == "error"}
    assert "PLAYSPACE_INTERIOR_SEALED_BY_CLIP" in codes
    assert report["interior_sealed_by_clip_cells"] > 0
    assert report["outdoor_cells_removed"] == 0


def test_the_position_invariants_read_the_fixture_correctly(plaza: Path):
    check = playspace.Check(workspace=plaza.parent, current=plaza, profile_id=_profile(), cell=32.0)
    assert playspace.run_invariant(
        "positions_walkable", check, positions=[list(INSIDE), list(STREET)]
    ).ok
    assert playspace.run_invariant("positions_walkable", check, positions=[list(ROOF)]).ok
    # The roof is standable before anything happens, which is what makes the scenario real.
    assert not playspace.run_invariant("positions_not_walkable", check, positions=[list(ROOF)]).ok


# ---------------------------------------------------------------------------
# The save gate
# ---------------------------------------------------------------------------


def test_an_edited_map_can_be_written_without_an_override(room: Path, tmp_path: Path):
    """The regression this change fixes.

    `map_save` used to compare the in-memory map against the bytes it was loaded from, so
    every committed brush made it refuse. The only way through was `allow_non_identical`,
    which also disables the check for the case it exists for.
    """
    srv.map_open(str(room))
    srv.solid_commit(ir={"op": "box", "min": [600, 600, 0], "max": [664, 664, 64]}, label="outside")
    out = srv.map_save(str(tmp_path / "out.map"))
    assert out["written"] is True
    assert out["modified_since_open"] is True


def test_a_map_that_did_not_round_trip_when_opened_is_still_refused(room: Path, monkeypatch):
    """The original guard has to survive the fix."""
    srv.map_open(str(room))
    srv.SESSION.opened_round_trip = {"identical": False, "first_difference": {"line": 1}}
    out = srv.map_save()
    assert out["written"] is False
    assert "byte-for-byte" in out["reason"]


def test_the_save_gate_blocks_a_clip_regression_and_can_be_acknowledged(room: Path):
    srv.map_open(str(room))
    srv.solid_commit(
        ir=CLIP_LID, label="lid", textures={"default": playspace.player_clip_shaders(_profile())[0]}
    )

    blocked = srv.map_save()
    assert blocked["written"] is False
    assert any(
        f["code"] == "PLAYSPACE_INTERIOR_SEALED_BY_CLIP"
        for f in blocked["playable_space"]["findings"]
    )

    forced = srv.map_save(acknowledge_regression=True)
    assert forced["written"] is True
    assert any("acknowledged" in w for w in srv.SESSION.warnings)


def test_the_save_gate_can_be_switched_off_without_switching_off_the_file_gate(room: Path):
    srv.map_open(str(room))
    srv.solid_commit(
        ir=CLIP_LID, label="lid", textures={"default": playspace.player_clip_shaders(_profile())[0]}
    )
    out = srv.map_save(check_playable_space=False)
    assert out["written"] is True
    assert "playable_space" not in out


def test_playable_space_diff_is_on_the_tool_surface():
    assert "playable_space_diff" in srv.TOOL_NAMES
    assert callable(srv.playable_space_diff)


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def test_an_unknown_invariant_fails_rather_than_passing_silently():
    """A typo in a tape must not read as a passing check."""
    out = playspace.run_invariant("no_such_thing", playspace.Check(workspace=Path(".")))
    assert out.ok is False
    assert "no such invariant" in out.detail


def test_an_invariant_that_cannot_measure_fails_rather_than_passing():
    """ "Could not evaluate" is not evidence of correctness."""
    out = playspace.run_invariant(
        "walkable_area_at_least", playspace.Check(workspace=Path("."), current=None)
    )
    assert out.ok is False


def test_every_registered_invariant_is_callable():
    assert playspace.available()
    for name in playspace.available():
        assert callable(playspace._REGISTRY[name])


@pytest.mark.parametrize(
    ("value", "expected", "want"),
    [
        (5, 5, True),
        (5, {"$gte": 5}, True),
        (4, {"$gte": 5}, False),
        (True, {"$gte": 0}, False),  # a bool is not a number here
        ("abc", {"$contains": "b"}, True),
        (None, {"$ne": 1}, True),
    ],
)
def test_expectation_matching(value, expected, want):
    assert playspace._matches(value, expected) is want


# ---------------------------------------------------------------------------
# Tapes
# ---------------------------------------------------------------------------


def _tape_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "bench" / "tapes"


def test_every_tape_is_well_formed():
    """A tape that names a tool that does not exist would fail late and confusingly."""
    tapes = sorted(_tape_dir().glob("*.json"))
    assert tapes, "expected scenario tapes in bench/tapes"
    for path in tapes:
        tape = json.loads(path.read_text())
        assert tape.get("note"), f"{path.name} has no note saying what it covers"
        assert tape.get("steps"), f"{path.name} has no steps"
        for step in tape["steps"]:
            assert step["tool"] in srv.TOOL_NAMES, f"{path.name}: unknown tool {step['tool']!r}"
        for spec in tape.get("invariants") or []:
            assert spec["name"] in playspace.available(), (
                f"{path.name}: unknown invariant {spec['name']!r}"
            )


def test_every_tier2_scenario_is_well_formed():
    """Tier 2 costs money, so its mistakes should be found by the free suite.

    The goal invariant of each scenario has to be *false* on the untouched fixture, or the
    scenario cannot tell success from doing nothing. `tools/tier2.py --check` verifies that
    against a live server; this checks the parts that need no server.
    """
    root = Path(__file__).resolve().parents[2]
    scenarios = sorted((root / "bench" / "scenarios").glob("*.json"))
    assert scenarios, "expected Tier 2 scenarios in bench/scenarios"
    for path in scenarios:
        s = json.loads(path.read_text())
        assert s.get("note"), f"{path.name} has no note"
        assert s.get("prompt"), f"{path.name} has no prompt"
        assert "{work}" in s["prompt"], f"{path.name}: the prompt never names the map"
        assert s.get("invariants"), f"{path.name} grades nothing"
        for spec in s["invariants"]:
            assert spec["name"] in playspace.available(), (
                f"{path.name}: unknown invariant {spec['name']!r}"
            )
        fixture = root / s["fixture"]
        assert fixture.is_file(), f"{path.name}: missing fixture {s['fixture']}"


def test_the_terse_and_careful_scenarios_differ_only_in_wording():
    """The pair is the measurement. If the graders drift apart it stops being one."""
    root = Path(__file__).resolve().parents[2] / "bench" / "scenarios"
    careful = json.loads((root / "seal-the-roofs.json").read_text())
    terse = json.loads((root / "seal-the-roofs-terse.json").read_text())
    assert careful["fixture"] == terse["fixture"]
    assert careful["invariants"] == terse["invariants"]
    assert len(terse["prompt"]) < len(careful["prompt"])


def test_the_regression_tape_expects_a_refusal():
    """The negative control has to keep being negative.

    If someone relaxes the save gate, this is the assertion that notices — but only while
    the tape still demands `written: false`, so that demand is itself pinned here.
    """
    tape = json.loads((_tape_dir() / "catches-interior-clip.json").read_text())
    save = next(s for s in tape["steps"] if s["as"] == "blocked_save")
    assert save["expect"]["written"] is False
    assert save["expect"]["playable_space.findings.0.code"] == "PLAYSPACE_INTERIOR_SEALED_BY_CLIP"
