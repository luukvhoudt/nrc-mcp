"""Phase 7.3: the voxel navigation grid and the gameplay reports built on it.

Every map here is built as `.map` source in `tmp_path`, so the assertions are about known
geometry rather than about whatever a corpus map happens to contain. The two real-corpus tests
at the end are deliberately cheap; they skip when the corpus has not been imported.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from nrc_mcp import analysis, kernel, profiles

# ---------------------------------------------------------------------------
# Synthetic map construction
# ---------------------------------------------------------------------------

TEX = "0 0 0 0.500000 0.500000 0 0 0"


def _n(value) -> str:
    """A number the way the `.map` format writes one: no exponent, no trailing zeroes."""
    return f"{float(value):.10f}".rstrip("0").rstrip(".") or "0"


def _p(point) -> str:
    return f"( {' '.join(_n(v) for v in point)} )"


def face(a, b, c, shader: str = "t/wall") -> str:
    """One brush face. The interior is behind the plane the three points define."""
    return f"{_p(a)} {_p(b)} {_p(c)} {shader} {TEX}"


def box(mins, maxs, shader: str = "t/wall") -> str:
    """An axis-aligned box brush."""
    x0, y0, z0 = mins
    x1, y1, z1 = maxs
    faces = [
        face((x0, y0, z1), (x0, y0 + 1, z1), (x0 + 1, y0, z1), shader),
        face((x0, y0, z0), (x0 + 1, y0, z0), (x0, y0 + 1, z0), shader),
        face((x0, y0, z0), (x0, y0, z0 + 1), (x0 + 1, y0, z0), shader),
        face((x0, y1, z0), (x0 + 1, y1, z0), (x0, y1, z0 + 1), shader),
        face((x0, y0, z0), (x0, y0 + 1, z0), (x0, y0, z0 + 1), shader),
        face((x1, y0, z0), (x1, y0, z0 + 1), (x1, y0 + 1, z0), shader),
    ]
    return "{\n" + "\n".join(faces) + "\n}\n"


def wedge(length: float = 128, width: float = 128, height: float = 64) -> str:
    """A triangular prism: solid below the plane `x / length + z / height = 1`.

    Not expressible as a bounding box, which is the point of it — it is what proves the
    occupancy test uses half-spaces rather than brush bounds.
    """
    faces = [
        face((0, 0, 0), (1, 0, 0), (0, 1, 0)),
        face((0, 0, 0), (0, 1, 0), (0, 0, 1)),
        face((0, 0, 0), (0, 0, 1), (1, 0, 0)),
        face((0, width, 0), (1, width, 0), (0, width, 1)),
        face((length, 0, 0), (0, 0, height), (length, 1, 0)),
    ]
    return "{\n" + "\n".join(faces) + "\n}\n"


def room(x0, y0, x1, y1, *, floor_top=0, height=256, thickness=16) -> list[str]:
    """A sealed box room whose interior floor is at `floor_top`."""
    return [
        box((x0, y0, floor_top - thickness), (x1, y1, floor_top)),
        box((x0, y0, floor_top + height), (x1, y1, floor_top + height + thickness)),
        box((x0, y0, floor_top), (x0 + thickness, y1, floor_top + height)),
        box((x1 - thickness, y0, floor_top), (x1, y1, floor_top + height)),
        box((x0, y0, floor_top), (x1, y0 + thickness, floor_top + height)),
        box((x0, y1 - thickness, floor_top), (x1, y1, floor_top + height)),
    ]


def point_entity(classname: str, origin, **keys) -> str:
    lines = ["{", f'"classname" "{classname}"', f'"origin" "{" ".join(_n(v) for v in origin)}"']
    lines += [f'"{k}" "{v}"' for k, v in keys.items()]
    return "\n".join([*lines, "}"]) + "\n"


def write_map(path: Path, brushes, entities=(), name: str = "m.map") -> Path:
    target = path / name
    source = '{\n"classname" "worldspawn"\n' + "".join(brushes) + "}\n" + "".join(entities)
    target.write_text(source)
    return target


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def kern():
    try:
        return kernel.kernel()
    except kernel.KernelUnavailable as e:
        pytest.skip(str(e))


@pytest.fixture(scope="module")
def pid() -> str:
    available = profiles.available()
    if not available:
        pytest.skip("no profiles on disk")
    return available[0]


@pytest.fixture(scope="module")
def constants(pid) -> analysis.Movement:
    return analysis.movement_constants(pid)


@pytest.fixture(scope="module")
def role_map(pid) -> analysis.Roles:
    return analysis.roles(pid)


@pytest.fixture
def simple_room(tmp_path, kern, pid) -> analysis.NavGrid:
    path = write_map(tmp_path, room(0, 0, 512, 512))
    return analysis.build_navgrid(path, cell=16, profile_id=pid)


# ---------------------------------------------------------------------------
# Constants come from the profile, and only from the profile
# ---------------------------------------------------------------------------


def test_movement_constants_are_the_gamepack_figures_not_the_design_document_s(constants):
    """The corrections document's W2: the spec assumed 56 units, the gamepack says 69.375.

    Asserted as an exact value because the whole discipline rests on it: a check built on 56
    would pass corridors the player cannot stand up in.
    """
    assert constants.headroom_stand.value == pytest.approx(69.375)
    assert constants.headroom_stand.value != 56
    assert constants.headroom_stand.confidence == "verified"
    assert constants.step_height.value == pytest.approx(18.0)
    assert constants.step_height.confidence == "verified"
    assert constants.headroom_crouch.value == pytest.approx(48.25)
    assert constants.player_width.value == pytest.approx(30.25)
    assert constants.fall_no_damage.value == pytest.approx(200.125)


def test_eye_height_is_inferred_and_says_so(constants):
    """This profile records that the gamepack states no eye height."""
    assert constants.eye_height_is_inferred is True
    assert constants.eye_height.value == pytest.approx(constants.headroom_stand.value)


def test_a_profile_without_movement_constants_is_refused_not_guessed(monkeypatch):
    monkeypatch.setattr(profiles, "movement", lambda _pid: {})
    with pytest.raises(analysis.AnalysisError, match="movement"):
        analysis.movement_constants("test")

    monkeypatch.setattr(
        profiles, "movement", lambda _pid: {"movement": {"step_height": {"value": 18}}}
    )
    with pytest.raises(analysis.AnalysisError, match="headroom_stand"):
        analysis.movement_constants("test")


def test_no_profile_selected_names_what_to_do(monkeypatch):
    monkeypatch.delenv("NRC_PROFILE", raising=False)
    monkeypatch.setattr(profiles, "available", lambda: ["a", "b"])
    with pytest.raises(analysis.AnalysisError, match="NRC_PROFILE"):
        analysis.resolve_profile(None)
    monkeypatch.setattr(profiles, "available", lambda: ["only"])
    assert analysis.resolve_profile(None) == "only"


def test_roles_are_discovered_from_profile_data(role_map):
    """Spawns, objectives and the team and group key names, all read from the profile."""
    assert role_map.spawns, "the profile should categorize some classname as a spawn"
    assert role_map.objectives
    assert role_map.spawns.isdisjoint(role_map.objectives)
    assert role_map.team_key and role_map.group_key
    assert len(role_map.team_labels) >= 2
    # A team taken from a key, and a team taken from the classname, are both understood.
    label = role_map.team_labels[0]
    assert role_map.team_of("whatever", {role_map.team_key: label.upper()}) == label
    assert role_map.team_of(f"prefix_{label}_suffix", {}) == label
    assert role_map.team_of("nothing", {}) == analysis.UNASSIGNED_TEAM


def test_roles_fall_back_to_key_definitions_when_no_rule_names_the_team_key(monkeypatch):
    """A profile need not ship rules for the balance report to know which key names a side."""
    profile = {
        "entities": [
            {
                "classname": "a_spawn_point",
                "category": "spawn_side",
                "keys": [
                    {"name": "direction"},
                    {"name": "side", "values": ["left", "right"]},
                ],
            },
            {"classname": "a_goal", "category": "objective"},
        ]
    }
    monkeypatch.setattr(profiles, "load", lambda _pid: profile)
    monkeypatch.setattr(profiles, "entities", lambda _pid: profile["entities"])
    discovered = analysis.roles("test")
    assert discovered.spawns == {"a_spawn_point"}
    assert discovered.objectives == {"a_goal"}
    assert discovered.team_key == "side"
    assert discovered.team_labels == ("left", "right")
    assert discovered.group_key == "", "nothing names a group key, and that is not an error"


def test_unverified_reasoning_can_never_be_worse_than_info():
    assert analysis._clamp("error", "verified") == "error"
    assert analysis._clamp("error", "unverified") == "info"
    assert analysis._clamp("warning", "") == "info"


# ---------------------------------------------------------------------------
# Voxelization
# ---------------------------------------------------------------------------


def test_a_room_voxelizes_to_exactly_its_interior_floor(simple_room):
    """512-unit room with 16-unit walls: 480 x 480 of interior, 30 x 30 cells at cell=16."""
    grid = simple_room
    assert grid.counts["walkable_columns"] == 900
    assert grid.counts["walkable_cells"] == 900
    assert grid.counts["floor_cells"] == 900
    assert len(grid.component_sizes()) == 1, "one room is one connected component"
    assert grid.cell == 16
    assert grid.dims[0] * grid.dims[1] * grid.dims[2] == grid.counts["cells"]


def test_headroom_is_the_profile_height_rounded_up_to_whole_cells(simple_room, constants):
    grid = simple_room
    assert grid.headroom_cells == math.ceil(constants.headroom_stand.value / 16)
    assert grid.headroom_cells == 5
    assert grid.as_dict()["effective_headroom_units"] == 80.0
    assert grid.step_up_cells == 1, "18 units of step is one 16-unit cell"
    assert grid.fall_cells == 12, "200.125 units of free fall is twelve 16-unit cells"


def test_a_room_too_low_to_stand_in_is_not_walkable(tmp_path, kern, pid):
    """The W2 regression, made mechanical.

    At cell=8 the grid needs ceil(69.375 / 8) = 9 cells, i.e. 72 units. A 64-unit room is
    therefore not walkable and a 72-unit one is. Had the design document's 56 been used, the
    64-unit room would have passed.
    """
    low = write_map(tmp_path, room(0, 0, 256, 256, height=64), name="low.map")
    grid = analysis.build_navgrid(low, cell=8, profile_id=pid)
    assert grid.counts["walkable_cells"] == 0
    assert grid.headroom_cells * grid.cell == 72
    codes = {f["code"] for f in grid.findings}
    assert "NAV_NO_WALKABLE_SPACE" in codes
    assert grid.counts["floor_cells"] > 0, "there is a floor, just no room above it"

    high = write_map(tmp_path, room(0, 0, 256, 256, height=72), name="high.map")
    assert analysis.build_navgrid(high, cell=8, profile_id=pid).counts["walkable_cells"] > 0


def test_occupancy_uses_exact_half_spaces_not_brush_bounds(tmp_path, kern, pid):
    path = write_map(tmp_path, [wedge(length=128, width=128, height=64)], name="wedge.map")
    game_map = kernel.load_map(path)
    solids, provenance = analysis.collect_solids(game_map, analysis.roles(pid))
    assert len(solids) == 1
    solid = solids[0]
    assert provenance["brushes_indeterminate"] == 0
    assert solid.is_box is False
    assert solid.approximated is False
    assert len(solid.planes) == 5, "a triangular prism has five faces"

    # The slope is z = 64 - x/2, so at x=8 the brush stops at z=60.
    assert solid.contains(8, 64, 59) is True
    assert solid.contains(8, 64, 61) is False
    assert solid.contains(8, 64, 63) is False, "63 is inside the bounding box but above the slope"
    assert solid.maxs == (128.0, 128.0, 64.0), "…and the bounding box really does reach 64"
    span = solid.z_span(8, 64)
    assert span is not None
    assert span[0] == pytest.approx(0.0)
    assert span[1] == pytest.approx(60.0)
    assert solid.z_span(200, 64) is None, "outside the brush entirely"


def test_utility_brushes_are_not_collision_geometry(tmp_path, kern, pid):
    """A hint brush across the room must not become a wall."""
    fragment = analysis.NONSOLID_SHADER_FRAGMENTS[0]
    brushes = room(0, 0, 512, 512)
    brushes.append(box((240, 16, 0), (272, 496, 256), f"common/{fragment}"))
    path = write_map(tmp_path, brushes, name="hint.map")
    grid = analysis.build_navgrid(path, cell=16, profile_id=pid)
    assert grid.geometry["brushes_nonsolid_shader"] == 1
    assert grid.counts["walkable_columns"] == 900, "the room is untouched"
    assert len(grid.component_sizes()) == 1


def test_a_brush_the_kernel_cannot_evaluate_is_reported_not_ignored(tmp_path, kern, pid):
    """Off-grid plane points make the kernel refuse, and the report has to say so."""
    brushes = room(0, 0, 512, 512)
    brushes.append(
        "{\n"
        + "\n".join(
            [
                face((100.5, 100, 64), (100.5, 101, 64), (101.5, 100, 64)),
                face((100.5, 100, 0), (101.5, 100, 0), (100.5, 101, 0)),
                face((100.5, 100, 0), (100.5, 100, 1), (101.5, 100, 0)),
                face((100.5, 200, 0), (101.5, 200, 0), (100.5, 200, 1)),
                face((100.5, 100, 0), (100.5, 101, 0), (100.5, 100, 1)),
                face((200.5, 100, 0), (200.5, 100, 1), (200.5, 101, 0)),
            ]
        )
        + "\n}\n"
    )
    path = write_map(tmp_path, brushes, name="offgrid.map")
    grid = analysis.build_navgrid(path, cell=16, profile_id=pid)
    assert grid.geometry["brushes_indeterminate"] == 1
    assert grid.geometry["indeterminate_examples"], "say which brush, not just how many"
    finding = next(f for f in grid.findings if f["code"] == "NAV_GEOMETRY_INCOMPLETE")
    assert finding["severity"] == "warning", "a fact about the input, so not clamped to info"
    assert finding["confidence"] == "verified"


def test_the_cell_budget_is_refused_by_name(tmp_path, kern, pid):
    path = write_map(tmp_path, room(0, 0, 2048, 2048))
    with pytest.raises(analysis.AnalysisError) as excinfo:
        analysis.build_navgrid(path, cell=1, profile_id=pid, max_cells=10_000)
    message = str(excinfo.value)
    assert "max_cells" in message
    assert "10,000" in message
    assert "cell>=" in message, "the error should name a cell size that would fit"


def test_a_map_with_nothing_to_voxelize_is_refused(tmp_path, kern, pid):
    fragment = analysis.NONSOLID_SHADER_FRAGMENTS[0]
    path = write_map(tmp_path, [box((0, 0, 0), (64, 64, 64), f"common/{fragment}")], name="e.map")
    with pytest.raises(analysis.AnalysisError, match="nothing to voxelize"):
        analysis.build_navgrid(path, cell=16, profile_id=pid)


# ---------------------------------------------------------------------------
# Raycasting
# ---------------------------------------------------------------------------


def test_a_ray_along_a_surface_is_not_an_obstruction(tmp_path, kern, pid):
    """Grazing must not block, or every sightline down a corridor would read as blocked."""
    path = write_map(tmp_path, [box((0, 0, -16), (512, 512, 0))], name="floor.map")
    game_map = kernel.load_map(path)
    solids, _ = analysis.collect_solids(game_map, analysis.roles(pid))
    index = analysis.BucketIndex(solids)
    assert analysis.segment_clear(solids, index, (16, 16, 0), (480, 480, 0)) is True
    assert analysis.segment_clear(solids, index, (16, 16, 8), (480, 480, -8)) is False


def test_ray_distance_measures_a_gap_exactly(tmp_path, kern, pid):
    path = write_map(
        tmp_path,
        [box((0, 0, 0), (64, 512, 128)), box((88, 0, 0), (152, 512, 128))],
        name="gap.map",
    )
    game_map = kernel.load_map(path)
    solids, _ = analysis.collect_solids(game_map, analysis.roles(pid))
    index = analysis.BucketIndex(solids)
    middle = (76.0, 256.0, 64.0)
    assert analysis.ray_distance(solids, index, middle, (1.0, 0.0, 0.0), 256) == pytest.approx(12.0)
    assert analysis.ray_distance(solids, index, middle, (-1.0, 0.0, 0.0), 256) == pytest.approx(
        12.0
    )
    assert analysis.ray_distance(solids, index, middle, (0.0, 1.0, 0.0), 256) == pytest.approx(
        256.0
    )


def test_column_intervals_find_the_exact_floor_and_ceiling(tmp_path, kern, pid):
    path = write_map(tmp_path, room(0, 0, 512, 512, height=200), name="column.map")
    game_map = kernel.load_map(path)
    solids, _ = analysis.collect_solids(game_map, analysis.roles(pid))
    index = analysis.BucketIndex(solids)
    intervals = analysis.column_intervals(solids, index, 256.0, 256.0)
    assert intervals == [(-16.0, 0.0), (200.0, 216.0)]
    floor, ceiling = analysis.floor_and_ceiling(intervals, 8.0)
    assert (floor, ceiling) == (0.0, 200.0)


# ---------------------------------------------------------------------------
# A*
# ---------------------------------------------------------------------------


def test_a_straight_walk_is_the_straight_line_distance(simple_room):
    result = analysis.path_distance(simple_room, (40, 40, 0), (40, 456, 0))
    assert result["reachable"] is True
    assert result["distance"] == pytest.approx(416.0, abs=1.0)
    assert result["detour_ratio"] == pytest.approx(1.0, abs=0.01)
    assert result["nodes_expanded"] > 0
    assert result["path"][0] == pytest.approx([40.0, 40.0, 0.0])
    assert len(result["path"]) < result["path_cells"], "a straight run decimates to its ends"


def test_a_wall_with_a_doorway_forces_a_detour(tmp_path, kern, pid):
    brushes = room(0, 0, 512, 512)
    # A wall across the room at y=248..264, with a 64-unit doorway at the far end.
    brushes.append(box((16, 248, 0), (400, 264, 256)))
    brushes.append(box((464, 248, 0), (496, 264, 256)))
    path = write_map(tmp_path, brushes, name="doorway.map")
    grid = analysis.build_navgrid(path, cell=16, profile_id=pid)

    result = analysis.path_distance(grid, (40, 40, 0), (40, 456, 0))
    assert result["reachable"] is True
    assert result["distance"] > result["straight_line"]
    assert result["detour_ratio"] > 1.4, "the only way through is the far corner"
    assert len(result["path"]) >= 3, "a path with corners keeps its corners"


def test_two_sealed_rooms_report_unreachable_rather_than_raising(tmp_path, kern, pid):
    brushes = room(0, 0, 512, 512) + room(1024, 0, 1536, 512)
    path = write_map(tmp_path, brushes, name="sealed.map")
    grid = analysis.build_navgrid(path, cell=16, profile_id=pid)
    assert len(grid.component_sizes()) == 2

    result = analysis.path_distance(grid, (40, 40, 0), (1064, 40, 0))
    assert result["reachable"] is False
    assert result["distance"] is None
    assert "component" in result["reason"]
    assert result["components"][0] != result["components"][1]


def test_a_position_with_no_walkable_cell_nearby_says_so(simple_room):
    result = analysis.path_distance(simple_room, (40, 40, 0), (40, 40, 4096))
    assert result["reachable"] is False
    assert "no walkable cell" in result["reason"]


def test_steps_within_the_profile_limit_are_walkable_and_bigger_ones_are_not(tmp_path, kern, pid):
    """A 16-unit step is one cell and passable; a 64-unit wall is not, but can be dropped off.

    The one-way case is real rather than an artefact: the profile's no-damage fall distance is
    200 units, so walking off a 64-unit ledge is free and climbing it is not.
    """
    for rise, expect_up in ((16, True), (64, False)):
        brushes = [
            box((0, 0, -16), (256, 512, 0)),
            box((256, 0, rise - 16), (512, 512, rise)),
            box((0, 0, 256 + rise), (512, 512, 272 + rise)),
        ]
        path = write_map(tmp_path, brushes, name=f"step{rise}.map")
        grid = analysis.build_navgrid(path, cell=16, profile_id=pid)
        low = (120.0, 256.0, 0.0)
        high = (392.0, 256.0, float(rise))
        assert analysis.path_distance(grid, low, high)["reachable"] is expect_up, rise
        assert analysis.path_distance(grid, high, low)["reachable"] is True, rise


# ---------------------------------------------------------------------------
# movement_check
# ---------------------------------------------------------------------------


def test_movement_check_reports_a_crouch_height_ceiling_exactly(tmp_path, kern, pid, constants):
    """56 units of clearance: too low to stand, high enough to crouch.

    The reported clearance must be 56.0 and not a multiple of the cell size — the grid picks
    the position, exact geometry measures it.
    """
    path = write_map(tmp_path, room(0, 0, 512, 512, height=56), name="crouch.map")
    report = analysis.movement_check(path, profile_id=pid, cell=16)
    assert "error" not in report

    check = next(c for c in report["checks"] if c["check"] == "standing_headroom")
    assert check["constant"]["value"] == pytest.approx(constants.headroom_stand.value)
    assert check["constant"]["confidence"] == "verified"
    assert check["crouch_only_positions"] > 0
    assert check["narrowest"][0]["clearance"] == pytest.approx(56.0)

    finding = next(f for f in report["findings"] if f["code"] == "MOVE_CROUCH_ONLY_SPACE")
    assert finding["severity"] == "warning", "exact measurement against a verified constant"
    assert "69.375" in finding["rule_source"]
    assert finding["confidence"] == "verified"


def test_movement_check_finds_a_gap_the_player_does_not_fit_through(tmp_path, kern, pid, constants):
    """The grid models a point, so it calls a 24-unit slot walkable. The player is 30.25 wide."""
    brushes = [
        box((0, 0, -16), (512, 512, 0)),
        box((0, 0, 0), (232, 512, 256)),
        box((256, 0, 0), (512, 512, 256)),
        box((0, 0, 256), (512, 512, 272)),
    ]
    path = write_map(tmp_path, brushes, name="slot.map")
    report = analysis.movement_check(path, profile_id=pid, cell=16)

    check = next(c for c in report["checks"] if c["check"] == "passage_width")
    assert check["constant"]["value"] == pytest.approx(constants.player_width.value)
    assert check["narrower_than_player"] > 0
    assert check["narrowest"][0]["width"] == pytest.approx(24.0)
    assert check["measured_at"]["value"] == pytest.approx(constants.headroom_crouch.value)

    finding = next(
        f for f in report["findings"] if f["code"] == "MOVE_PASSAGE_NARROWER_THAN_PLAYER"
    )
    assert finding["severity"] == "warning"
    assert "30.25" in finding["rule_source"]


def test_movement_check_says_what_the_profile_cannot_support(tmp_path, kern, pid):
    path = write_map(tmp_path, room(0, 0, 256, 256))
    report = analysis.movement_check(path, profile_id=pid, cell=16)
    unchecked = {row["check"] for row in report["not_checked"]}
    assert "walljump_spacing" in unchecked, "§7.3 asks for it; the profile does not state it"
    assert "slide_runout" in unchecked
    assert "ladder_placement" in unchecked
    for row in report["not_checked"]:
        assert row["reason"], row["check"]


def test_every_check_names_the_constant_it_used(tmp_path, kern, pid):
    brushes = [
        box((0, 0, -16), (512, 512, 0)),
        box((256, 0, 0), (512, 512, 32)),
        box((0, 0, 256), (512, 512, 272)),
    ]
    path = write_map(tmp_path, brushes, name="steps.map")
    report = analysis.movement_check(path, profile_id=pid, cell=16)
    for check in report["checks"]:
        assert check["constant"]["key"], check["check"]
        assert check["constant"]["confidence"] in {"verified", "unverified"}
        assert check["constant"]["source"], check["check"]
    step = next(c for c in report["checks"] if c["check"] == "step_height")
    assert step["steps_needing_a_jump"] > 0, "a 32-unit rise is above the 18-unit step limit"
    assert step["steps_needing_a_ledge_grab"] == 0
    assert step["jump_constant"]["value"] == pytest.approx(66.5)


def test_a_rise_between_the_jump_and_grab_limits_is_classified_as_a_ledge(
    tmp_path, kern, pid, constants
):
    """96 units is above jump_up_max (66.5) and below ledge_grab_max (114.625).

    Three profile constants deciding one classification, which is the point: the ladder of
    limits is data, so the report can say "ledge grab" instead of "too high".
    """
    assert constants.jump_up_max.value < 96 < constants.ledge_grab_max.value
    brushes = [
        box((0, 0, -16), (512, 512, 0)),
        box((256, 0, 0), (512, 512, 96)),
        box((0, 0, 320), (512, 512, 336)),
    ]
    path = write_map(tmp_path, brushes, name="ledge.map")
    report = analysis.movement_check(path, profile_id=pid, cell=16)
    step = next(c for c in report["checks"] if c["check"] == "step_height")
    assert step["steps_needing_a_ledge_grab"] > 0
    assert step["steps_needing_a_jump"] == 0
    assert step["ledge_grab_constant"]["confidence"] == "verified"


# ---------------------------------------------------------------------------
# sightline_report
# ---------------------------------------------------------------------------


def test_sightlines_are_clear_down_an_empty_hall_and_broken_by_a_pillar(tmp_path, kern, pid):
    open_hall = write_map(tmp_path, room(0, 0, 1024, 256), name="hall.map")
    report = analysis.sightline_report(open_hall, samples=24, profile_id=pid, cell=16)
    assert report["clear_fraction"] == 1.0
    assert report["length_percentiles"]["max"] > 700
    assert report["rays"] == 24 * 23 // 2

    brushes = room(0, 0, 1024, 256)
    brushes.append(box((496, 16, 0), (528, 240, 256)))
    blocked = write_map(tmp_path, brushes, name="hall_wall.map")
    walled = analysis.sightline_report(blocked, samples=24, profile_id=pid, cell=16)
    assert walled["blocked"] > 0
    assert walled["clear_fraction"] < 1.0


def test_sightline_report_uses_the_profile_eye_height_and_flags_it_as_inferred(
    tmp_path, kern, pid, constants
):
    path = write_map(tmp_path, room(0, 0, 512, 512))
    report = analysis.sightline_report(path, samples=16, profile_id=pid, cell=16)
    assert report["eye_height"]["value"] == pytest.approx(constants.headroom_stand.value)
    assert report["eye_height_is_inferred"] is True
    for finding in report["findings"]:
        if finding["code"].startswith("SIGHT_"):
            assert finding["severity"] == "info", "an inferred eye height cannot warn"


def test_a_long_lane_is_reported_with_its_derived_threshold(tmp_path, kern, pid):
    path = write_map(tmp_path, room(0, 0, 2048, 192), name="lane.map")
    report = analysis.sightline_report(path, samples=30, profile_id=pid, cell=16, seed=1)
    assert report["long_lanes"] > 0
    assert report["long_lane_threshold"] > 0
    finding = next(f for f in report["findings"] if f["code"] == "SIGHT_LONG_LANE")
    assert finding["severity"] == "info"
    assert "heuristic" in finding["rule_source"]
    assert report["longest_lanes"][0]["length"] >= report["long_lane_threshold"]
    assert report["power_positions"][0]["sees"] >= 1


def test_sightline_sampling_is_deterministic_and_capped(tmp_path, kern, pid):
    path = write_map(tmp_path, room(0, 0, 512, 512))
    first = analysis.sightline_report(path, samples=20, profile_id=pid, cell=16, max_rays=50)
    second = analysis.sightline_report(path, samples=20, profile_id=pid, cell=16, max_rays=50)
    assert first["rays"] == 50
    assert first["pairs_sampled"] is True
    assert first["length_percentiles"] == second["length_percentiles"]


# ---------------------------------------------------------------------------
# balance_report
# ---------------------------------------------------------------------------


def _two_team_map(tmp_path, role_map, *, name, objective_offset=0.0):
    """A 2048 x 256 hall with a spawn group at each end and one neutral objective."""
    spawn_class = sorted(role_map.spawns)[0]
    neutral = sorted(role_map.objectives)[0]
    red, blue = role_map.team_labels[0], role_map.team_labels[1]
    brushes = room(0, 0, 2048, 256)
    entities = []
    for team, x in ((red, 96), (blue, 1952)):
        for i in range(4):
            entities.append(
                point_entity(
                    spawn_class,
                    (x, 64 + i * 32, 8),
                    **{role_map.team_key: team, role_map.group_key: "1"},
                )
            )
    entities.append(point_entity(neutral, (1024 + objective_offset, 128, 8)))
    return write_map(tmp_path, brushes, entities, name=name)


def test_balance_report_measures_both_teams_to_a_neutral_objective(tmp_path, kern, pid, role_map):
    path = _two_team_map(tmp_path, role_map, name="balanced.map")
    report = analysis.balance_report(path, profile_id=pid, cell=16)
    assert "error" not in report
    assert report["spawn_count"] == 8
    assert len(report["spawn_groups"]) == 2
    assert len(report["objectives"]) == 1

    row = report["asymmetry"][0]
    assert row["neutral"] is True
    assert set(row["nearest_by_team"]) == set(role_map.team_labels[:2])
    assert row["ratio"] == pytest.approx(1.0, abs=0.05)
    assert not [f for f in report["findings"] if f["code"] == "BALANCE_OBJECTIVE_ASYMMETRIC"]

    for entry in report["distances"]:
        assert entry["reachable"] is True
        assert entry["distance"] >= entry["straight_line"] - 32


def test_balance_report_flags_a_neutral_objective_one_team_owns(tmp_path, kern, pid, role_map):
    path = _two_team_map(tmp_path, role_map, name="skewed.map", objective_offset=-700)
    report = analysis.balance_report(path, profile_id=pid, cell=16)
    finding = next(f for f in report["findings"] if f["code"] == "BALANCE_OBJECTIVE_ASYMMETRIC")
    assert finding["severity"] == "info", "a voxel-derived heuristic never warns"
    assert finding["confidence"] == "unverified"
    assert report["asymmetry"][0]["ratio"] > 1.25


def test_balance_report_does_not_flag_a_correct_mirrored_objective_pair(
    tmp_path, kern, pid, role_map
):
    """Each team being nearer its own flag is CTF working, not CTF broken."""
    red, blue = role_map.team_labels[0], role_map.team_labels[1]
    per_team = {
        team: next(o for o in sorted(role_map.objectives) if team in o.lower())
        for team in (red, blue)
    }
    spawn_class = sorted(role_map.spawns)[0]
    brushes = room(0, 0, 2048, 256)
    entities = []
    for team, x in ((red, 96), (blue, 1952)):
        for i in range(4):
            entities.append(
                point_entity(
                    spawn_class,
                    (x, 64 + i * 32, 8),
                    **{role_map.team_key: team, role_map.group_key: "1"},
                )
            )
    entities.append(point_entity(per_team[red], (224, 128, 8)))
    entities.append(point_entity(per_team[blue], (1824, 128, 8)))
    path = write_map(tmp_path, brushes, entities, name="ctf.map")

    report = analysis.balance_report(path, profile_id=pid, cell=16)
    balance = report["team_role_balance"]
    assert balance["to_own_objective"]["ratio"] == pytest.approx(1.0, abs=0.05)
    assert balance["to_enemy_objective"]["ratio"] == pytest.approx(1.0, abs=0.05)
    assert not [f for f in report["findings"] if f["code"] == "BALANCE_TEAM_ROLE_ASYMMETRIC"]
    # Per-objective asymmetry is still reported, but never as a finding for a team objective.
    assert all(row["neutral"] is False for row in report["asymmetry"])
    assert any(row["ratio"] > 2 for row in report["asymmetry"])

    assert report["symmetry"]["symmetric"] is True
    assert report["symmetry"]["best"]["transform"] == "mirror_x"
    assert report["symmetry"]["best"]["fraction"] == 1.0


def test_symmetry_detection_distinguishes_a_mirror_from_a_rotation():
    red, blue = "red", "blue"

    def marker(x, y, team):
        return analysis.Marker(0, "c", (x, y, 0.0), team, "")

    mirrored = [marker(100, 100, red), marker(900, 100, blue)]
    result = analysis._symmetry(mirrored, (500.0, 100.0), 16.0)
    assert result["best"]["transform"] == "mirror_x"
    assert result["symmetric"] is True

    rotated = [marker(100, 50, red), marker(900, 150, blue)]
    result = analysis._symmetry(rotated, (500.0, 100.0), 16.0)
    assert result["best"]["transform"] == "rotate_180"
    assert result["symmetric"] is True

    scattered = [marker(100, 50, red), marker(880, 400, blue)]
    assert analysis._symmetry(scattered, (500.0, 100.0), 16.0)["symmetric"] is False
    assert analysis._symmetry([], (0.0, 0.0), 16.0)["tested"] is False


def test_balance_report_says_it_cannot_estimate_time(tmp_path, kern, pid, role_map):
    path = _two_team_map(tmp_path, role_map, name="notime.map")
    report = analysis.balance_report(path, profile_id=pid, cell=16)
    assert any("speed" in note for note in report["notes"]), (
        "§7.3 asks for traversal time; the profile states no speed, so say so"
    )


def test_a_map_with_no_objectives_reports_that_and_nothing_worse(tmp_path, kern, pid):
    path = write_map(tmp_path, room(0, 0, 512, 512))
    report = analysis.balance_report(path, profile_id=pid, cell=16)
    codes = {f["code"] for f in report["findings"]}
    assert {"BALANCE_NO_SPAWNS", "BALANCE_NO_OBJECTIVES"} <= codes
    assert report["summary"]["error"] == 0
    assert report["summary"]["warning"] == 0


# ---------------------------------------------------------------------------
# spawn_safety
# ---------------------------------------------------------------------------


def _spawn_room(tmp_path, role_map, *, corridors, name):
    """A 256 x 256 spawn room opening south into a corridor split into `corridors` lanes."""
    spawn_class = sorted(role_map.spawns)[0]
    brushes = [
        box((0, -512, -16), (256, 256, 0)),  # floor, room and corridor
        box((0, -512, 256), (256, 256, 272)),  # ceiling
        box((0, 240, 0), (256, 256, 256)),  # north wall
        box((0, -512, 0), (16, 256, 256)),  # west wall
        box((240, -512, 0), (256, 256, 256)),  # east wall
    ]
    if corridors == 2:
        brushes.append(box((112, -512, 0), (144, 0, 256)))
    entities = [point_entity(spawn_class, (128, 128, 8))]
    return write_map(tmp_path, brushes, entities, name=name)


def test_spawn_safety_counts_exits(tmp_path, kern, pid, role_map):
    one = _spawn_room(tmp_path, role_map, corridors=1, name="one_exit.map")
    report = analysis.spawn_safety(one, profile_id=pid, cell=16, exit_radius=256)
    assert report["spawn_count"] == 1
    row = report["spawns"][0]
    assert row["on_walkable_grid"] is True
    assert row["exits"] == 1
    finding = next(f for f in report["findings"] if f["code"] == "SPAWN_SINGLE_EXIT")
    assert finding["severity"] == "info", "no profile rule states a required exit count"

    two = _spawn_room(tmp_path, role_map, corridors=2, name="two_exits.map")
    report = analysis.spawn_safety(two, profile_id=pid, cell=16, exit_radius=256)
    assert report["spawns"][0]["exits"] == 2
    assert not [f for f in report["findings"] if f["code"] == "SPAWN_SINGLE_EXIT"]


def test_spawn_safety_reports_the_nearest_enemy_spawn(tmp_path, kern, pid, role_map):
    path = _two_team_map(tmp_path, role_map, name="enemies.map")
    report = analysis.spawn_safety(path, profile_id=pid, cell=16)
    red = role_map.team_labels[0]
    row = next(r for r in report["spawns"] if r["team"] == red)
    nearest = row["nearest_enemy_spawn"]
    assert nearest["team"] != red
    assert nearest["straight_line"] > 1700
    walked = nearest["walked_between_teams"]
    assert walked["reachable"] is True
    assert walked["distance"] >= nearest["straight_line"] - 32
    assert any("speed" in note for note in report["notes"])


def test_a_floating_spawn_is_reported_as_unmeasurable(tmp_path, kern, pid, role_map):
    spawn_class = sorted(role_map.spawns)[0]
    brushes = room(0, 0, 512, 512)
    path = write_map(
        tmp_path, brushes, [point_entity(spawn_class, (256, 256, 1024))], name="float.map"
    )
    report = analysis.spawn_safety(path, profile_id=pid, cell=16)
    assert report["spawns"][0]["on_walkable_grid"] is False
    finding = next(f for f in report["findings"] if f["code"] == "SPAWN_OFF_WALKABLE_GRID")
    assert finding["severity"] == "info"


# ---------------------------------------------------------------------------
# Cross-cutting shape
# ---------------------------------------------------------------------------


def test_every_finding_matches_the_rule_engine_shape(tmp_path, kern, pid, role_map):
    path = _two_team_map(tmp_path, role_map, name="shape.map", objective_offset=-700)
    reports = [
        analysis.balance_report(path, profile_id=pid, cell=16),
        analysis.sightline_report(path, samples=12, profile_id=pid, cell=16),
        analysis.movement_check(path, profile_id=pid, cell=16),
        analysis.spawn_safety(path, profile_id=pid, cell=16),
    ]
    fields = {"code", "severity", "message", "confidence", "rule_source", "entities", "fix_hint"}
    seen = 0
    for report in reports:
        assert "error" not in report
        assert set(report["summary"]) == {"error", "warning", "info"}
        for finding in report["findings"]:
            seen += 1
            assert set(finding) == fields, finding["code"]
            assert finding["severity"] in {"error", "warning", "info"}
            assert finding["confidence"] in {"verified", "unverified"}
            assert finding["rule_source"], finding["code"]
            assert finding["message"]
            if finding["confidence"] != "verified":
                assert finding["severity"] == "info", finding["code"]
        assert report["summary"]["error"] == 0, "analysis is advisory; it never errors"
    assert seen > 0


def test_reports_return_an_error_instead_of_raising_when_the_grid_cannot_be_built(
    tmp_path, kern, pid
):
    """cell=1 over a 1024-unit room is far past the cell budget, and every report says so."""
    path = write_map(tmp_path, room(0, 0, 1024, 1024))
    reports = (
        analysis.balance_report(path, profile_id=pid, cell=1),
        analysis.sightline_report(path, samples=4, profile_id=pid, cell=1),
        analysis.movement_check(path, profile_id=pid, cell=1),
        analysis.spawn_safety(path, profile_id=pid, cell=1),
    )
    for report in reports:
        assert "max_cells" in report["error"]
        assert report["profile"] == pid


# ---------------------------------------------------------------------------
# The real corpus, kept cheap
# ---------------------------------------------------------------------------


def _corpus(name: str) -> Path:
    path = kernel.repo_root() / "corpus" / "real" / name
    if not path.is_file():
        pytest.skip("real corpus not imported")
    return path


def test_a_real_map_voxelizes_and_reports(kern, pid):
    """One real map, at a coarse cell size to stay well under a second."""
    path = _corpus("ut4_megastructunnel.map")
    grid = analysis.build_navgrid(path, cell=32, profile_id=pid)
    assert grid.counts["walkable_cells"] > 500
    assert grid.geometry["brushes_used"] == grid.geometry["brushes"]

    balance = analysis.balance_report(path, profile_id=pid, grid=grid, max_paths=8)
    assert balance["spawn_count"] > 50, "this map really does have many team spawns"
    assert len(balance["objectives"]) >= 2
    assert any(d["reachable"] for d in balance["distances"])
    assert balance["summary"]["error"] == 0

    safety = analysis.spawn_safety(path, profile_id=pid, grid=grid, max_paths=4)
    assert len(safety["spawns"]) == balance["spawn_count"]
    assert sum(1 for row in safety["spawns"] if row["on_walkable_grid"]) > 40

    sight = analysis.sightline_report(path, samples=32, profile_id=pid, grid=grid)
    assert sight["rays"] == 32 * 31 // 2
    assert 0.0 < sight["clear_fraction"] < 1.0, "a real map blocks some lines and not others"


def test_a_real_map_with_brushes_the_kernel_declines_says_so(kern, pid):
    """A third of this map's brushes have off-grid plane points, and that has to be visible."""
    path = _corpus("ut4_dofa.map")
    grid = analysis.build_navgrid(path, cell=32, profile_id=pid)
    assert grid.geometry["brushes_indeterminate"] > 0
    finding = next(f for f in grid.findings if f["code"] == "NAV_GEOMETRY_INCOMPLETE")
    assert finding["severity"] == "warning"
    assert "absent from this grid" in finding["message"]
