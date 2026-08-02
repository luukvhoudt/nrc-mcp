#!/usr/bin/env python3
"""The fitness suite (§11.1).

§11.1 makes the precondition explicit: "Self-modification is only useful when there is an
**objective fitness function**. Without one, the agent hill-climbs on its own opinion of itself
and drifts." So this measures, and every signal is measured against something outside the model's
judgement — the corpus, upstream's compiler, hand-built reference brush counts.

| ID | Signal | Measured by | Type |
| --- | --- | --- | --- |
| F1 | Kernel correctness | the differential round-trip gate | **binary gate, never a score** |
| F2 | Sculpting quality | brush count vs hand-built references | lower is better |
| F3 | Validator accuracy | precision/recall on the labelled corpus | higher is better |
| F4 | Optimizer efficacy | compile deltas on benchmark maps | higher is better |
| F5 | End-to-end success | declarative task cases scored by the validators | higher is better |
| F6 | Cost | wall time per completed task | lower is better |

**F1 is a gate, not a weight** (§11.4). It is reported separately and can never be traded against
a speed win: a run with F1 red is not a worse score, it is not a score at all.

Two signals are honestly narrower than the spec describes, and say so in their output. F4 needs a
`-vis` compile to be meaningful, so it is skipped unless a compiler is available. F5's "natural
language briefs scored by render inspection" is not runnable without a model in the loop, so it
runs a declarative case file instead — each case an IR tree plus the properties its output must
have. That is a proxy for the real thing and is labelled as one.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def repo_root() -> Path:
    return Path(os.environ.get("NRC_ROOT") or Path(__file__).resolve().parent.parent)


def git_sha(root: Path) -> str:
    p = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return p.stdout.strip() or "unknown"


def git_dirty(root: Path) -> bool:
    p = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=False
    )
    return bool(p.stdout.strip())


def _kernel():
    root = repo_root()
    sys.path.insert(0, str(root / "python" / "src"))
    import nrc_py  # noqa: PLC0415

    return nrc_py


# ---------------------------------------------------------------------------
# F1 — kernel correctness. A gate.
# ---------------------------------------------------------------------------


def f1_kernel_correctness(root: Path) -> dict[str, Any]:
    """Run the differential harness. Pass or fail; never a number to trade against.

    Invoked as a subprocess rather than imported, so the measurement uses exactly the harness a
    developer and CI use. A fitness suite that measured a private copy would be measuring the
    wrong thing.
    """
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(root / "tools" / "difftest.py"), "--no-semantic"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    out = proc.stdout + proc.stderr
    maps = identical = 0
    for line in out.splitlines():
        if "byte-identical" in line:
            # "  syntactic: 49/49 byte-identical (...)"
            try:
                frac = line.split(":")[1].split("byte-identical")[0].strip()
                identical, maps = (int(x) for x in frac.split("/"))
            except (IndexError, ValueError):
                pass
    return {
        "id": "F1",
        "name": "kernel correctness",
        "gate": True,
        "passed": proc.returncode == 0,
        "maps": maps,
        "identical": identical,
        "seconds": round(time.monotonic() - t0, 2),
        "note": "a binary gate (§11.4); it can never be traded against another signal",
    }


# ---------------------------------------------------------------------------
# F2 — sculpting quality. Brush count against hand-built references.
# ---------------------------------------------------------------------------

#: The reference counts are what a mapper builds by hand, which is the comparison §11.1 asks for.
#: A doorway is three brushes because you cut left column, right column, lintel; a window is four
#: because it also has a sill.
F2_CASES: list[dict[str, Any]] = [
    {
        "name": "doorway_through_wall",
        "reference": 3,
        "reason": "left column, right column, lintel",
        "ir": {
            "op": "carve_opening",
            "wall": {"op": "box", "min": [0, 0, 0], "max": [256, 16, 128]},
            "min": [96, -8, 0],
            "max": [160, 24, 96],
        },
    },
    {
        "name": "window_in_wall",
        "reference": 4,
        "reason": "left, right, sill, lintel",
        "ir": {
            "op": "carve_opening",
            "wall": {"op": "box", "min": [0, 0, 0], "max": [256, 16, 128]},
            "min": [96, -8, 32],
            "max": [160, 24, 96],
        },
    },
    {
        "name": "hollow_room",
        "reference": 6,
        "reason": "floor, ceiling, four walls",
        "ir": {
            "op": "hollow",
            "solid": {"op": "box", "min": [0, 0, 0], "max": [512, 512, 256]},
            "thickness": 16,
        },
    },
    {
        "name": "room_with_doorway",
        "reference": 8,
        "reason": "six walls, one of them split into three by the opening",
        "ir": {
            "op": "subtract",
            "from": {
                "op": "hollow",
                "solid": {"op": "box", "min": [0, 0, 0], "max": [512, 512, 256]},
                "thickness": 16,
            },
            "cut": [{"op": "box", "min": [224, -8, 0], "max": [288, 24, 112]}],
        },
    },
    {
        "name": "corner_cut",
        "reference": 2,
        "reason": "an L split into two boxes",
        "ir": {
            "op": "subtract",
            "from": {"op": "box", "min": [0, 0, 0], "max": [64, 64, 64]},
            "cut": [{"op": "box", "min": [32, 32, -8], "max": [96, 96, 72]}],
        },
    },
]


def f2_sculpting_quality() -> dict[str, Any]:
    k = _kernel()
    cases = []
    excess = 0
    failed = 0
    for case in F2_CASES:
        try:
            r = k.solid_compile(case["ir"], None, 8)
            got = r["brushes"]
        except ValueError as e:
            cases.append({"name": case["name"], "error": str(e)[:200]})
            failed += 1
            continue
        delta = got - case["reference"]
        excess += max(0, delta)
        cases.append(
            {
                "name": case["name"],
                "reference": case["reference"],
                "brushes": got,
                "excess": delta,
                "reason": case["reason"],
            }
        )
    return {
        "id": "F2",
        "name": "sculpting quality",
        "lower_is_better": True,
        "score": excess + failed * 100,
        "excess_brushes": excess,
        "failed_cases": failed,
        "cases": cases,
        "note": (
            "score is total brushes above the hand-built reference; a failed case counts 100 so "
            "it cannot look better than a merely inefficient one"
        ),
    }


# ---------------------------------------------------------------------------
# F3 — validator accuracy against the labelled corpus.
# ---------------------------------------------------------------------------


def f3_validator_accuracy(root: Path) -> dict[str, Any]:
    """Precision and recall against `bench/labels.json`.

    The labels name, per corpus map, which finding codes *should* appear. Recall is what fraction
    of expected findings were made; precision is measured against the maps labelled clean, since a
    finding on a map known to be good is a false positive and nothing else.
    """
    labels_path = root / "bench" / "labels.json"
    if not labels_path.is_file():
        return {
            "id": "F3",
            "name": "validator accuracy",
            "skipped": f"{labels_path.relative_to(root)} does not exist; run tools/gen_corpus.py",
        }
    labels = json.loads(labels_path.read_text())
    k = _kernel()

    expected_total = found_total = false_positives = clean_maps = 0
    misses: list[str] = []
    for rel, spec in sorted(labels.get("maps", {}).items()):
        p = root / rel
        if not p.is_file():
            continue
        try:
            v = k.Map.load(str(p)).validate(grid=int(spec.get("grid", 1)), severity_min="info")
        except ValueError:
            continue
        codes = {f["code"] for f in v["findings"]}
        want = set(spec.get("expect", []))
        if want:
            expected_total += len(want)
            found_total += len(want & codes)
            for missing in sorted(want - codes):
                misses.append(f"{Path(rel).name}: expected {missing}")
        if spec.get("clean"):
            clean_maps += 1
            errs = [f for f in v["findings"] if f["severity"] == "error"]
            false_positives += len(errs)

    recall = found_total / expected_total if expected_total else 0.0
    # Precision here is "how often a clean map was left alone", which is the number that matters:
    # a validator that fires on good maps gets ignored, and then it protects nothing.
    precision = 1.0 if not clean_maps else max(0.0, 1.0 - false_positives / max(1, clean_maps))
    f1 = 0.0 if (precision + recall) == 0 else 2 * precision * recall / (precision + recall)
    return {
        "id": "F3",
        "name": "validator accuracy",
        "higher_is_better": True,
        "score": round(f1, 4),
        "recall": round(recall, 4),
        "precision_on_clean_maps": round(precision, 4),
        "expected_findings": expected_total,
        "found": found_total,
        "false_positive_errors": false_positives,
        "clean_maps": clean_maps,
        "misses": misses[:10],
    }


# ---------------------------------------------------------------------------
# F4 — optimizer efficacy. Needs a compiler.
# ---------------------------------------------------------------------------


def f4_optimizer_efficacy(root: Path) -> dict[str, Any]:
    q = os.environ.get("Q3MAP2")
    if not q or not Path(q).exists():
        return {
            "id": "F4",
            "name": "optimizer efficacy",
            "skipped": (
                "no compiler available (set Q3MAP2). This signal compares portal and draw-surface "
                "counts before and after an optimization pass, which needs a real -vis compile; "
                "reporting anything without one would be inventing a measurement."
            ),
        }
    return {
        "id": "F4",
        "name": "optimizer efficacy",
        "higher_is_better": True,
        "score": 0.0,
        "note": (
            "a compiler is present but no optimization pass is wired in yet, so there is nothing "
            "to measure a delta against. This stays 0 until §6.1's structural_audit and "
            "hint_suggest can be applied and re-compiled."
        ),
    }


# ---------------------------------------------------------------------------
# F5 — end-to-end task success, as a declarative proxy.
# ---------------------------------------------------------------------------


def f5_task_success(root: Path) -> dict[str, Any]:
    """Run `bench/tasks.json`: each case an IR tree plus the properties its output must have.

    §11.1 describes F5 as natural-language briefs "scored by the validators and by render
    inspection". That needs a model in the loop, so it cannot run here. This is the runnable part
    of it — the same scoring, with the authoring step fixed — and it is labelled a proxy so nobody
    reads it as the real signal.
    """
    tasks_path = root / "bench" / "tasks.json"
    if not tasks_path.is_file():
        return {"id": "F5", "name": "task success", "skipped": "bench/tasks.json does not exist"}
    tasks = json.loads(tasks_path.read_text())
    k = _kernel()

    results = []
    passed = 0
    for case in tasks.get("cases", []):
        name = case.get("name", "?")
        try:
            m = k.Map.parse('{\n"classname" "worldspawn"\n}\n')
            k.solid_commit(
                m,
                case["ir"],
                case.get("textures"),
                int(case.get("grid", 8)),
                "worldspawn",
                False,
                name,
            )
            src = m.source()
            reparsed = k.Map.parse(src)
            checks: dict[str, bool] = {
                "round_trips": reparsed.round_trip()["identical"],
            }
            v = reparsed.validate(grid=int(case.get("grid", 8)), severity_min="error")
            checks["no_validation_errors"] = v["summary"]["error"] == 0

            expect = case.get("expect", {})
            stats = reparsed.stats(grid=int(case.get("grid", 8)))
            if "brushes" in expect:
                checks["brush_count"] = stats["brushes"] == expect["brushes"]
            if "max_brushes" in expect:
                checks["within_brush_budget"] = stats["brushes"] <= expect["max_brushes"]
            ok = all(checks.values())
            passed += ok
            results.append({"name": name, "ok": ok, "checks": checks, "brushes": stats["brushes"]})
        except ValueError as e:
            results.append({"name": name, "ok": False, "error": str(e)[:200]})

    total = len(tasks.get("cases", []))
    return {
        "id": "F5",
        "name": "task success",
        "higher_is_better": True,
        "score": round(passed / total, 4) if total else 0.0,
        "passed": passed,
        "total": total,
        "cases": results,
        "note": (
            "a proxy for §11.1's F5: the authoring step is fixed rather than driven by a natural "
            "language brief, so this measures the compile-and-validate half only"
        ),
    }


# ---------------------------------------------------------------------------
# F6 — cost.
# ---------------------------------------------------------------------------


def f6_cost(elapsed: float, signals: list[dict]) -> dict[str, Any]:
    """Wall time. Tokens and tool calls are not observable from inside a benchmark run.

    Reporting only what can be measured is the point: an invented token count would be a number
    the loop could optimize without changing anything real.
    """
    return {
        "id": "F6",
        "name": "cost",
        "lower_is_better": True,
        "score": round(elapsed, 3),
        "seconds_total": round(elapsed, 3),
        "per_signal_seconds": {
            s["id"]: s.get("seconds") for s in signals if s.get("seconds") is not None
        },
        "note": (
            "wall time only. Tokens and tool calls per completed task are part of F6 in §11.1 but "
            "are not visible from inside this process; the harness that drives the agent has to "
            "record them."
        ),
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_all(root: Path, *, include_f4: bool = True) -> dict[str, Any]:
    t0 = time.monotonic()
    signals: list[dict] = []

    f1 = f1_kernel_correctness(root)
    signals.append(f1)
    # Everything downstream measures behaviour that only means something if the kernel is right.
    # Running the rest anyway, but flagging it, is more useful than refusing outright.
    for fn in (f2_sculpting_quality,):
        signals.append(fn())
    signals.append(f3_validator_accuracy(root))
    if include_f4:
        signals.append(f4_optimizer_efficacy(root))
    signals.append(f5_task_success(root))

    elapsed = time.monotonic() - t0
    signals.append(f6_cost(elapsed, signals))

    protected_ok = None
    problems: list[str] = []
    try:
        sys.path.insert(0, str(root / "tools"))
        import protected as prot  # noqa: PLC0415

        problems = prot.verify(root)
        protected_ok = not problems
    except Exception as e:  # noqa: BLE001 - a missing pin file must not break benching
        protected_ok = None
        problems = [str(e)]

    return {
        "sha": git_sha(root),
        "dirty": git_dirty(root),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gate_passed": bool(f1.get("passed")),
        "protected_paths_ok": protected_ok,
        "protected_problems": problems[:10],
        "signals": signals,
        "note": (
            "F1 is a gate, not a weight (§11.4): a run with gate_passed false has no score to "
            "compare, whatever the other signals say."
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--all", action="store_true", help="run every signal (the default)")
    ap.add_argument("--out", type=Path, default=None, help="directory for <sha>.json")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    root = repo_root()
    result = run_all(root)

    out_dir = args.out or (root / "bench" / "results")
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-dirty" if result["dirty"] else ""
    path = out_dir / f"{result['sha']}{suffix}.json"
    path.write_text(json.dumps(result, indent=2) + "\n")

    if not args.quiet:
        print(f"bench {result['sha']}{' (working tree dirty)' if result['dirty'] else ''}")
        for s in result["signals"]:
            if "skipped" in s:
                print(f"  {s['id']} {s['name']:<22} skipped — {s['skipped'][:70]}")
            elif s.get("gate"):
                state = "PASS" if s["passed"] else "FAIL"
                print(f"  {s['id']} {s['name']:<22} {state}  ({s['identical']}/{s['maps']} maps)")
            else:
                direction = "lower" if s.get("lower_is_better") else "higher"
                print(f"  {s['id']} {s['name']:<22} {s['score']}  ({direction} is better)")
        if result["protected_paths_ok"] is False:
            print("\n  PROTECTED PATHS CHANGED:")
            for p in result["protected_problems"]:
                print(f"    {p}")
        print(f"\nwrote {path.relative_to(root)}")

    # A red gate is a failure of the run, not a low score.
    return 0 if result["gate_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
