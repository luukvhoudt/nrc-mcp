"""Phases 9 and 10: the fitness suite, protected paths, self-dev guardrails, upstream watch.

The tests that matter most here are the *negative* ones. §11.4 calls the protected-path mechanism
"the only thing standing between self-improving and self-congratulating", and §13 says an agent
gaming its own fitness function "is not hypothetical; it is the default behaviour of any
optimization loop with a mutable objective". So most of what follows checks that things are
correctly refused.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def load_tool(name: str):
    """Import a script from tools/ by path, since they are not a package."""
    root = Path(__file__).resolve().parents[2]
    path = root / "tools" / f"{name}.py"
    if not path.is_file():
        pytest.skip(f"{path} does not exist")
    spec = importlib.util.spec_from_file_location(f"_tool_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def protected():
    return load_tool("protected")


@pytest.fixture(scope="module")
def selfdev():
    return load_tool("selfdev")


@pytest.fixture(scope="module")
def bench():
    return load_tool("bench")


@pytest.fixture(scope="module")
def prwatch():
    return load_tool("pr_watch")


# ---------------------------------------------------------------------------
# Protected paths
# ---------------------------------------------------------------------------


def test_the_ruler_is_protected(protected):
    """The bench runner, the gate and the seam lint must all be frozen."""
    for expected in (
        "tools/bench.py",
        "tools/difftest.py",
        "tools/seam_lint.py",
        "bench/fitness.json",
        "bench/labels.json",
    ):
        assert expected in protected.PROTECTED_FILES, f"{expected} should be protected"


def test_the_exact_predicates_are_protected(protected):
    """§11.4 requires human review indefinitely for the geometric predicates."""
    for expected in ("crates/nrc-core/src/exact.rs", "crates/nrc-core/src/winding.rs"):
        assert expected in protected.PROTECTED_FILES


def test_current_pins_verify_clean(protected):
    """No protected *content* has drifted from its pin.

    An uncommitted pin file is filtered out rather than asserted against: re-pinning leaves it dirty
    until it is committed, which is a normal working state. That guard is real and has its own test
    below; conflating the two would make this one fail every time someone re-pins.
    """
    root = protected.repo_root()
    if not protected.pin_path(root).is_file():
        pytest.skip("nothing pinned yet")
    problems = [p for p in protected.verify(root) if "differs from HEAD" not in p]
    assert problems == [], f"protected paths have drifted: {problems}"


def test_an_uncommitted_pin_file_is_reported_separately(protected):
    """The pin file cannot hash itself, so git is what catches a change to it.

    This is the hole a hash list alone would leave: re-pinning after editing a protected file would
    otherwise launder the edit.
    """
    root = protected.repo_root()
    rel = protected.pin_path(root).relative_to(root).as_posix()
    assert rel == "bench/protected.json"
    # Whatever the current state, the check must answer without raising and must be a bool.
    assert isinstance(protected._pin_file_modified(root), bool)


def test_a_changed_protected_file_is_detected(protected, tmp_path: Path):
    """The mechanism itself, on a synthetic tree.

    Pinned with `compute()` rather than a hand-written dict, which is how a real repository is
    pinned — a partial pin would report every unpinned path as a violation, which is correct
    behaviour but not what this test is about.
    """
    (tmp_path / "tools").mkdir()
    (tmp_path / "bench").mkdir()
    target = tmp_path / "tools" / "bench.py"
    target.write_text("original\n")
    protected.pin_path(tmp_path).write_text(json.dumps(protected.compute(tmp_path)))

    assert protected.verify(tmp_path) == []
    target.write_text("tampered\n")
    problems = protected.verify(tmp_path)
    assert any("tools/bench.py" in p for p in problems), problems
    assert len(problems) == 1, f"only the tampered file should be reported: {problems}"


def test_only_verified_rules_are_frozen(protected, tmp_path: Path):
    """A profile may gain unverified rules freely; changing a verified one must be caught.

    Whole-file hashing would mean every documentation fix needed a human re-pin, and the loop would
    learn to avoid profiles entirely rather than to respect the verified rules.
    """
    yaml = pytest.importorskip("yaml")
    (tmp_path / "profiles").mkdir()
    prof = tmp_path / "profiles" / "p.yaml"

    base = {"rules": [{"id": "A", "confidence": "verified", "check": "count_exact"}]}
    prof.write_text(yaml.safe_dump(base))
    before = protected._verified_rule_digest(tmp_path)

    # Adding an unverified rule must not change the digest.
    base["rules"].append({"id": "B", "confidence": "unverified", "check": "count_min"})
    prof.write_text(yaml.safe_dump(base))
    assert protected._verified_rule_digest(tmp_path) == before

    # Changing the verified one must.
    base["rules"][0]["check"] = "count_min"
    prof.write_text(yaml.safe_dump(base))
    assert protected._verified_rule_digest(tmp_path) != before


def test_a_missing_pin_file_says_what_to_do(protected, tmp_path: Path):
    with pytest.raises(protected.ProtectionError, match="--repin"):
        protected.verify(tmp_path)


# ---------------------------------------------------------------------------
# Self-dev guardrails
# ---------------------------------------------------------------------------


def test_selfdev_is_off_unless_opted_in(selfdev, monkeypatch):
    monkeypatch.delenv("NRC_SELFDEV", raising=False)
    assert selfdev.enabled() is False
    with pytest.raises(selfdev.SelfDevError, match="opt-in"):
        selfdev.require_enabled()
    monkeypatch.setenv("NRC_SELFDEV", "1")
    assert selfdev.enabled() is True


def test_the_kernel_is_out_of_reach(selfdev):
    """§11.3: start with prompts, not code. §11.4: the predicates need human review indefinitely."""
    for denied in (
        "crates/nrc-core/src/exact.rs",
        "crates/nrc-core/src/winding.rs",
        "crates/nrc-solid/src/csg.rs",
        "tools/bench.py",
        "tools/difftest.py",
        "tools/protected.py",
    ):
        ok, why = selfdev.path_allowed(denied)
        assert not ok, f"{denied} must not be writable by self-dev"
        assert "§11" in why or "prompt" in why, why


def test_the_prompt_layer_is_in_reach(selfdev):
    for allowed in ("python/src/nrc_mcp/server.py", "profiles/urt43.yaml", "docs/conventions.md"):
        ok, why = selfdev.path_allowed(allowed)
        assert ok, f"{allowed} should be writable: {why}"


def test_path_traversal_is_refused(selfdev):
    for bad in ("../outside.py", "/etc/passwd", "profiles/../../etc/passwd"):
        ok, _ = selfdev.path_allowed(bad)
        assert not ok, f"{bad} must be refused"


def test_the_merge_rule_requires_gate_green_nothing_worse_and_something_better(selfdev):
    def run(gate: bool, f2: float, f5: float) -> dict:
        return {
            "gate_passed": gate,
            "signals": [
                {"id": "F2", "score": f2, "lower_is_better": True},
                {"id": "F5", "score": f5, "higher_is_better": True},
            ],
        }

    base = run(True, 4.0, 0.8)

    # An improvement with nothing worse: accept.
    v = selfdev.compare(base, run(True, 4.0, 0.9))
    assert v["accept"] is True
    assert v["improved"] and not v["regressed"]

    # A red gate can never be traded, however much else improved.
    v = selfdev.compare(base, run(False, 1.0, 1.0))
    assert v["accept"] is False
    assert any("gate" in r for r in v["reasons"])

    # A regression elsewhere blocks it even with an improvement.
    v = selfdev.compare(base, run(True, 6.0, 0.95))
    assert v["accept"] is False
    assert v["regressed"]

    # No change at all: nothing to keep.
    v = selfdev.compare(base, run(True, 4.0, 0.8))
    assert v["accept"] is False
    assert any("nothing improved" in r for r in v["reasons"])


def test_lower_is_better_signals_are_scored_the_right_way_round(selfdev):
    base = {"gate_passed": True, "signals": [{"id": "F2", "score": 5.0}]}
    better = {"gate_passed": True, "signals": [{"id": "F2", "score": 3.0}]}
    v = selfdev.compare(base, better)
    assert v["improved"], "fewer excess brushes is an improvement"
    assert not v["regressed"]


def test_the_gate_signal_is_never_treated_as_a_score(selfdev):
    b = {
        "gate_passed": True,
        "signals": [
            {"id": "F1", "gate": True, "passed": True, "score": 0},
            {"id": "F5", "score": 0.5},
        ],
    }
    assert "F1" not in selfdev.score_map(b), "F1 must not enter the score comparison"


def test_the_rate_limit_is_enforced(selfdev, monkeypatch, tmp_path: Path):
    import time as _time

    monkeypatch.setattr(selfdev, "repo_root", lambda: tmp_path)
    d = tmp_path / selfdev.ATTEMPTS_DIR
    d.mkdir(parents=True)
    (d / "recent.json").write_text(json.dumps({"id": "recent", "started_epoch": _time.time()}))
    with pytest.raises(selfdev.SelfDevError, match="minimum gap"):
        selfdev.check_rate_limit(tmp_path)
    # Driving it by hand is allowed.
    selfdev.check_rate_limit(tmp_path, force=True)


# ---------------------------------------------------------------------------
# The fitness suite
# ---------------------------------------------------------------------------


def test_f2_references_are_the_hand_built_counts(bench):
    by_name = {c["name"]: c for c in bench.F2_CASES}
    assert by_name["doorway_through_wall"]["reference"] == 3
    assert by_name["window_in_wall"]["reference"] == 4
    assert by_name["hollow_room"]["reference"] == 6


def test_f2_measures_the_real_compiler(bench):
    try:
        bench._kernel()
    except ImportError:
        pytest.skip("kernel extension not built")
    r = bench.f2_sculpting_quality()
    assert r["id"] == "F2"
    assert r["lower_is_better"] is True
    assert r["failed_cases"] == 0, [c for c in r["cases"] if "error" in c]
    # The doorway must still be three brushes; that is the §4.1 claim.
    doorway = next(c for c in r["cases"] if c["name"] == "doorway_through_wall")
    assert doorway["brushes"] == 3


def test_f3_scores_against_the_labelled_corpus(bench):
    try:
        bench._kernel()
    except ImportError:
        pytest.skip("kernel extension not built")
    root = bench.repo_root()
    if not (root / "bench" / "labels.json").is_file():
        pytest.skip("no labels")
    r = bench.f3_validator_accuracy(root)
    if "skipped" in r:
        pytest.skip(r["skipped"])
    assert 0.0 <= r["score"] <= 1.0
    assert r["expected_findings"] > 0, "the labels should assert something"
    assert r["misses"] == [], f"validators missed labelled defects: {r['misses']}"


def test_f4_skips_rather_than_inventing_a_number(bench, monkeypatch):
    monkeypatch.delenv("Q3MAP2", raising=False)
    r = bench.f4_optimizer_efficacy(bench.repo_root())
    assert "skipped" in r
    assert "inventing" in r["skipped"] or "needs a real" in r["skipped"]


def test_f6_reports_only_what_it_can_measure(bench):
    r = bench.f6_cost(1.25, [{"id": "F1", "seconds": 0.5}])
    assert r["score"] == 1.25
    assert "not visible from inside this process" in r["note"], (
        "claiming a token count it cannot see would give the loop a number to game for free"
    )


def test_the_fitness_definitions_state_the_gate_rule(bench):
    root = bench.repo_root()
    p = root / "bench" / "fitness.json"
    if not p.is_file():
        pytest.skip("no fitness.json")
    data = json.loads(p.read_text())
    assert data["signals"]["F1"]["type"] == "gate"
    assert "never be traded" in data["gate_rule"]
    assert "at least one improved" in data["merge_rule"]


# ---------------------------------------------------------------------------
# Upstream watch
# ---------------------------------------------------------------------------


def test_signature_hashing_notices_a_changed_declaration(prwatch, tmp_path: Path):
    inc = tmp_path / "include"
    inc.mkdir()
    header = inc / "qerplugin.h"
    header.write_text("class A {\n  virtual void doThing( int x );\n};\n")
    before = prwatch.header_signatures(tmp_path)
    assert "doThing" in before["qerplugin.h"]

    # A changed parameter type is the case that silently breaks an out-of-tree plugin.
    header.write_text("class A {\n  virtual void doThing( float x );\n};\n")
    after = prwatch.header_signatures(tmp_path)
    assert after["qerplugin.h"]["doThing"] != before["qerplugin.h"]["doThing"]

    report = prwatch.diff_snapshots(
        {"signatures": before, "head": "a"}, {"signatures": after, "head": "b"}
    )
    assert any("CHANGED" in c for c in report["signature_changes"])


def test_reformatting_alone_is_not_reported_as_a_change(prwatch, tmp_path: Path):
    inc = tmp_path / "include"
    inc.mkdir()
    header = inc / "qerplugin.h"
    header.write_text("class A {\n  virtual void doThing( int x );\n};\n")
    before = prwatch.header_signatures(tmp_path)
    header.write_text("class A {\n\tvirtual void doThing(int    x);\n};\n")
    assert prwatch.header_signatures(tmp_path) == before, (
        "a watcher that fires on whitespace gets muted within a week"
    )


def test_new_and_removed_flags_are_both_reported(prwatch):
    report = prwatch.diff_snapshots(
        {"flags": ["-bsp", "-vis", "-maxshaderinfo"], "head": "a"},
        {"flags": ["-bsp", "-vis", "-newthing"], "head": "b"},
    )
    assert report["new_flags"] == ["-newthing"]
    assert report["removed_flags"] == ["-maxshaderinfo"]


def test_the_watched_headers_are_the_ones_the_bridge_uses(prwatch):
    for expected in ("qerplugin.h", "iscenegraph.h", "iselection.h", "iundo.h", "ibrush.h"):
        assert expected in prwatch.WATCHED_HEADERS
