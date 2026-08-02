"""The profile layer and the MCP surface.

The profile tests are mostly about the confidence discipline (§7, §8.2). The design document
listed four Urban Terror spawn rules as verified; one was wrong, one was unverifiable, and
implementing the wrong one would have failed correct maps. So these check that the mechanism
which prevents a repeat is actually in place, not just documented.
"""

from __future__ import annotations

import pytest
from nrc_mcp import profiles, tasks

# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


def test_at_least_one_profile_exists():
    assert profiles.available(), "expected profiles/*.yaml to exist"


@pytest.fixture(scope="module")
def pid() -> str:
    av = profiles.available()
    if not av:
        pytest.skip("no profiles on disk")
    return av[0]


def test_profile_loads_and_declares_its_sources(pid):
    data = profiles.load(pid)
    meta = data.get("profile") or {}
    assert meta.get("id"), "a profile must identify itself"
    assert meta.get("sources"), "a profile must say where its facts came from"


def test_every_entity_carries_confidence_and_source(pid):
    ents = profiles.entities(pid)
    assert ents, "expected the profile to define entities"
    for e in ents:
        assert e.get("classname"), e
        assert e.get("confidence") in {"verified", "unverified"}, e
        assert e.get("source"), f"{e.get('classname')} has no source"


def test_unverified_material_is_kept_separate(pid):
    """Verified and unverified facts must not be mixed, or the distinction is worthless."""
    data = profiles.load(pid)
    for e in profiles.entities(pid):
        assert e.get("confidence") == "verified", (
            f"{e.get('classname')} sits in `entities:` but is {e.get('confidence')}; "
            "unverified material belongs in the `unverified:` section"
        )
    unverified = data.get("unverified")
    if unverified:
        assert isinstance(unverified, list)
        for u in unverified:
            assert u.get("confidence") == "unverified", u


def test_movement_constants_come_from_data_not_code(pid):
    """§7.4 names hardcoded physics constants as the most likely seam leak.

    So the profile must actually supply numbers. The design document assumed a 56-unit
    standing height; the shipped gamepack says 69.375. Whatever the right figure is, code
    must get it from here rather than embedding either one.
    """
    mv = profiles.movement(pid)
    if not mv:
        pytest.skip("this profile states no movement constants")

    def numbers(value) -> list[float]:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return [float(value)]
        if isinstance(value, dict):
            return [n for v in value.values() for n in numbers(v)]
        if isinstance(value, list):
            return [n for v in value for n in numbers(v)]
        return []

    assert numbers(mv), f"movement section carries no numeric constants: {mv}"


def test_gametypes_are_ided(pid):
    gts = profiles.gametypes(pid)
    if not gts:
        pytest.skip("this profile lists no gametypes")
    for g in gts:
        assert "id" in g and "name" in g, g
        assert isinstance(g["id"], int), g


def test_summary_is_serializable_and_flags_unverified(pid):
    import json

    s = profiles.summary(pid)
    json.dumps(s)  # must survive being sent over MCP
    assert s["entity_count"] >= s["verified_entity_count"]
    assert "verified" in s["note"]


def test_unknown_profile_reports_what_is_available():
    with pytest.raises(profiles.ProfileError, match="available"):
        profiles.load("no_such_profile_xyz")


# ---------------------------------------------------------------------------
# Task surface
# ---------------------------------------------------------------------------


def test_task_discovery_finds_the_core_tasks():
    try:
        names = tasks.task_names()
    except RuntimeError as e:
        pytest.skip(f"mise unavailable: {e}")
    # These are the tasks the design depends on by name (§1.4: renaming one is a breaking
    # change to the agent's action surface).
    for required in ("test", "test:diff", "test:seam", "kernel:build", "corpus:gen", "bench"):
        assert required in names, f"{required} is missing from the task surface"


def test_every_task_has_a_description():
    """§1.4: descriptions are user-facing through the nrc://tasks resource."""
    try:
        all_tasks = tasks.list_tasks()
    except RuntimeError as e:
        pytest.skip(f"mise unavailable: {e}")
    missing = [t["name"] for t in all_tasks if not t["description"]]
    assert not missing, f"tasks without a description: {missing}"


def test_destructive_tasks_are_flagged():
    try:
        all_tasks = tasks.list_tasks()
    except RuntimeError as e:
        pytest.skip(f"mise unavailable: {e}")
    by_name = {t["name"]: t for t in all_tasks}
    if "pack_pk3" in by_name:
        assert by_name["pack_pk3"]["mutates_user_data"]
    for name, t in by_name.items():
        if name.startswith("vendor:"):
            assert t["slow"], f"{name} should be flagged slow"


def test_task_listing_renders_for_a_model():
    try:
        text = tasks.describe_tasks()
    except RuntimeError as e:
        pytest.skip(f"mise unavailable: {e}")
    assert "mise tasks" in text
    assert "## test" in text


# ---------------------------------------------------------------------------
# Server surface
# ---------------------------------------------------------------------------


def test_server_imports_and_describes_itself():
    from nrc_mcp import server

    text = server.describe_surface()
    for tool in ("map_open", "map_stats", "validate", "task_list", "compile_map"):
        assert tool in text
    # Honesty about scope is part of the surface: an agent must not plan around tools that do not
    # exist. The wording changed as phases landed, so assert on the substance — that the listing
    # names something as unbuilt and warns that the editor bridge is uncompiled.
    assert "NOT BUILT" in text
    assert "never been compiled" in text


def test_tools_refuse_to_run_without_an_open_map():
    from nrc_mcp import server

    server.SESSION.map = None
    with pytest.raises(ValueError, match="no map is open"):
        server.SESSION.require()


def test_tool_names_match_the_decorated_tools():
    """The listed inventory must not drift from what is actually registered."""
    import re
    from pathlib import Path as _P

    from nrc_mcp import server

    src = _P(server.__file__).read_text()
    decorated = re.findall(r"@mcp\.tool\(\)\s*\ndef (\w+)", src)
    assert sorted(decorated) == sorted(server.TOOL_NAMES), (
        f"registered {sorted(decorated)} but TOOL_NAMES lists {sorted(server.TOOL_NAMES)}"
    )
    for name in server.TOOL_NAMES:
        fn = getattr(server, name, None)
        assert fn is not None, f"{name} is listed but not defined"
        assert (fn.__doc__ or "").strip(), f"{name} has no docstring for the agent to read"


def test_render_tools_appear_in_the_surface():
    from nrc_mcp import server

    text = server.describe_surface()
    for tool in ("render_topdown", "render_camera", "render_contact_sheet", "render_player_eye"):
        assert tool in text
    # Rendering is implemented now, so it must not still be listed as missing.
    assert "4.2 rendering" not in text


def test_player_eye_height_comes_from_the_profile():
    """§7.4: a physics constant must be read from data, never hardcoded in the server."""
    from nrc_mcp import profiles as p

    av = p.available()
    if not av:
        pytest.skip("no profiles on disk")
    h = p.standing_height(av[0])
    assert h is not None, "the profile should state a verified standing height"
    assert 32.0 < h < 128.0, f"implausible standing height {h}"
    # And it must not be the figure the design document assumed.
    assert abs(h - 56.0) > 1e-9, "56 is the legacy Quake 3 height, not this game's"


def test_resource_uris_are_game_agnostic():
    """A per-game URI scheme in code would itself be a §7.4 seam violation."""
    from nrc_mcp import server

    text = server.describe_surface()
    assert "nrc://profile/" in text
    assert "urt://" not in text
