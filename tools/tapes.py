#!/usr/bin/env python3
"""Scenario tapes: end-to-end tests of the MCP surface that cost no tokens.

A real session is a model choosing tools plus the tools doing the work. Only the second
half can be wrong in a way this repository can fix, and only the second half is cheap to
run — so it is tested on its own. A **tape** is the tool sequence a session produced, with
the model removed: a starting map, an ordered list of `{tool, args}`, and the properties
the result must have.

That is a real end-to-end test. It opens a map, sculpts, saves and packages through exactly
the functions the MCP server exposes, and then checks the artefacts. What it does not test
is whether a model would choose those calls. `bench/tapes/README.md` says so plainly, and
Tier 2 — a model in the loop, graded by these same invariants — is the thing that closes it.

Two ways a tape comes into being:

- written by hand, to pin a workflow that must keep working;
- recorded from a session, by pointing `NRC_MCP_TAPE` at a file, which turns an afternoon of
  real work into a regression test at no cost.

Run them::

    mise run test:tapes                  # all of them, no compiler
    mise run test:tapes -- --tape build-from-scratch
    NRC_TAPES_COMPILE=1 mise run test:tapes    # also compile and check the map seals

Exit codes: 0 all passed, 1 a tape failed, 2 the runner itself broke.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any


def repo_root() -> Path:
    return Path(os.environ.get("NRC_ROOT") or Path(__file__).resolve().parent.parent)


sys.path.insert(0, str(repo_root() / "python" / "src"))

from nrc_mcp import playspace  # noqa: E402
from nrc_mcp import server as srv  # noqa: E402

TAPE_DIR = repo_root() / "bench" / "tapes"

#: The map inside a tape's workspace. Tapes refer to it as `{work}`.
WORK_NAME = "work.map"


class TapeError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Running one tape
# ---------------------------------------------------------------------------


def _expand(value: Any, subs: dict[str, str]) -> Any:
    """Substitute `{work}`, `{workspace}` and `{repo}` anywhere in a tape's arguments."""
    if isinstance(value, str):
        return value.format(**subs) if "{" in value else value
    if isinstance(value, list):
        return [_expand(v, subs) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, subs) for k, v in value.items()}
    return value


def _dig(value: Any, path: str) -> Any:
    for part in [p for p in path.split(".") if p]:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.lstrip("-").isdigit():
            idx = int(part)
            value = value[idx] if -len(value) <= idx < len(value) else None
        else:
            return None
    return value


def _reset_session() -> None:
    """Tools share one module-level session, so a tape must not inherit the last one's.

    Recorded shapes need no resetting: they live in a sidecar beside the map, which for a
    tape means inside its own throwaway workspace.
    """
    srv.SESSION.map = None
    srv.SESSION.path = None
    srv.SESSION.grid = 8
    srv.SESSION.warnings = []
    srv.SESSION.opened_round_trip = None


def run_tape(tape: dict, *, workspace: Path, allow_compile: bool = False) -> dict:
    """Replay one tape and check its invariants. Never raises for a *tape* failure."""
    name = tape.get("name") or "unnamed"
    started = time.time()
    subs = {
        "work": str(workspace / WORK_NAME),
        "workspace": str(workspace),
        "repo": str(repo_root()),
    }

    baseline: Path | None = None
    fixture = tape.get("fixture")
    if fixture:
        src = Path(_expand(fixture, subs))
        if not src.is_absolute():
            src = repo_root() / src
        if not src.is_file():
            return _fail(name, started, f"fixture not found: {src}")
        shutil.copyfile(src, workspace / WORK_NAME)
        # A pristine copy, so the invariants can see what the map looked like before the
        # tape touched it even after the tape has overwritten the working file.
        baseline = workspace / "baseline.map"
        shutil.copyfile(src, baseline)

    _reset_session()
    steps: list[dict] = []
    results: dict[str, Any] = {}
    ok = True

    for i, step in enumerate(tape.get("steps") or []):
        tool = step.get("tool")
        label = step.get("as") or f"{i}:{tool}"
        args = _expand(step.get("args") or {}, subs)
        fn = getattr(srv, tool, None)
        if not callable(fn) or tool not in srv.TOOL_NAMES:
            steps.append({"step": label, "ok": False, "error": f"no such tool: {tool!r}"})
            ok = False
            break
        try:
            result = fn(**args)
        except Exception as e:  # noqa: BLE001 — a tool raising is a tape failure, not a crash
            steps.append({"step": label, "ok": False, "error": f"{type(e).__name__}: {e}"})
            ok = False
            break
        results[label] = result

        # `expect` is a map of dotted paths to expected values. A value may be a comparison
        # object like {"$gte": 4}; see playspace._matches.
        mismatches = []
        for path, want in (step.get("expect") or {}).items():
            got = _dig(result, path)
            if not playspace._matches(got, want):
                mismatches.append(f"{path} = {got!r}, want {want!r}")
        steps.append({"step": label, "ok": not mismatches, "mismatches": mismatches})
        if mismatches:
            ok = False
            if not step.get("continue_on_mismatch"):
                break

    current = workspace / WORK_NAME
    check = playspace.Check(
        workspace=workspace,
        baseline=baseline if baseline and baseline.is_file() else None,
        current=current if current.is_file() else None,
        results=results,
        profile_id=tape.get("profile"),
        cell=float(tape.get("cell") or playspace.DEFAULT_DIFF_CELL),
        allow_compile=allow_compile,
    )

    outcomes = []
    if ok:  # a broken tape makes every invariant meaningless, so do not pretend to run them
        for spec in tape.get("invariants") or []:
            outcome = playspace.run_invariant(
                spec["name"], check, **_expand(spec.get("args") or {}, subs)
            )
            outcomes.append(outcome.as_dict())
            if not outcome.ok:
                ok = False

    return {
        "tape": name,
        "note": tape.get("note", ""),
        "ok": ok,
        "seconds": round(time.time() - started, 2),
        "steps": steps,
        "invariants": outcomes,
    }


def _fail(name: str, started: float, message: str) -> dict:
    return {
        "tape": name,
        "ok": False,
        "seconds": round(time.time() - started, 2),
        "steps": [{"step": "setup", "ok": False, "error": message}],
        "invariants": [],
    }


# ---------------------------------------------------------------------------
# Discovery and reporting
# ---------------------------------------------------------------------------


def load_tapes(only: str | None = None) -> list[dict]:
    tapes = []
    for path in sorted(TAPE_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        data.setdefault("name", path.stem)
        if only and data["name"] != only:
            continue
        tapes.append(data)
    if only and not tapes:
        raise TapeError(f"no tape named {only!r} in {TAPE_DIR}")
    return tapes


def report(results: list[dict], *, verbose: bool) -> None:
    for r in results:
        mark = "ok  " if r["ok"] else "FAIL"
        print(f"  {mark} {r['tape']:<28} {r['seconds']:>6.2f}s")
        for step in r["steps"]:
            if step.get("ok") and not verbose:
                continue
            if step.get("error"):
                print(f"         ! {step['step']}: {step['error']}")
            for m in step.get("mismatches") or []:
                print(f"         ! {step['step']}: {m}")
            if verbose and step.get("ok"):
                print(f"         . {step['step']}")
        for inv in r["invariants"]:
            if inv["skipped"]:
                if verbose:
                    print(f"         - {inv['name']}: skipped ({inv['detail']})")
                continue
            if inv["ok"] and not verbose:
                continue
            symbol = "." if inv["ok"] else "!"
            print(f"         {symbol} {inv['name']}: {inv['detail']}")

    passed = sum(1 for r in results if r["ok"])
    skipped = sum(1 for r in results for i in r["invariants"] if i["skipped"])
    print()
    print(f"  tapes: {passed}/{len(results)} passed, {skipped} invariant(s) skipped")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tape", help="run only this tape")
    ap.add_argument("--compile", action="store_true", help="also run invariants needing q3map2")
    ap.add_argument("--json", action="store_true", help="machine-readable output only")
    ap.add_argument("-v", "--verbose", action="store_true", help="show passing steps too")
    ap.add_argument("--keep", action="store_true", help="keep each tape's workspace")
    args = ap.parse_args()

    allow_compile = args.compile or os.environ.get("NRC_TAPES_COMPILE") == "1"

    try:
        tapes = load_tapes(args.tape)
    except (TapeError, json.JSONDecodeError) as e:
        print(f"tapes: {e}", file=sys.stderr)
        return 2

    if not args.json:
        print(f"scenario tapes ({len(tapes)}), compiling {'on' if allow_compile else 'off'}")

    results = []
    for tape in tapes:
        workspace = Path(tempfile.mkdtemp(prefix=f"nrc-tape-{tape['name']}-"))
        try:
            results.append(run_tape(tape, workspace=workspace, allow_compile=allow_compile))
        except Exception:  # noqa: BLE001 — the runner breaking is exit 2, not a tape failure
            traceback.print_exc()
            return 2
        finally:
            if args.keep:
                print(f"    workspace kept: {workspace}")
            else:
                shutil.rmtree(workspace, ignore_errors=True)

    if args.json:
        print(json.dumps({"tapes": results}, indent=2))
    else:
        report(results, verbose=args.verbose)

    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
