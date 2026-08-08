#!/usr/bin/env python3
"""Tier 2: a model in the loop, driving this MCP, graded by the tape invariants.

The tapes (`tools/tapes.py`) test the surface with the model removed. That is most of the
value and none of the cost, but it leaves one question open: **would a model, given only
this MCP and a natural-language brief, choose the right calls?** Nothing else in this
repository can answer that, and the `ut4_dofa` session is the evidence that the answer
matters — the tools were correct and the session still produced an unplayable map.

So this harness gives a model a brief, a fixture and the real MCP server over stdio, lets it
work, and then grades the *artefact* with the same invariants the tapes use. No model judges
anything. The grader is `nrc_mcp.playspace`, which measures.

**This costs money and is never part of `mise run test`.** It is opt-in and budgeted; `--run`
has to be asked for by name.

    mise run bench:tier2 -- --check                 # no API calls: wiring, fixtures, grading
    mise run bench:tier2 -- --estimate              # + count_tokens on the fixed prefix
    mise run bench:tier2 -- --run --scenario seal-the-roofs
    mise run bench:tier2 -- --run                   # every scenario

Cost control, in order of how much it saves:

- **Small fixtures.** A 9-brush plaza with one building, not a city. `seal-the-roofs`
  reproduces the trap that motivated all of this at roughly a fortieth of the size, and a
  trap does not need a city to be a trap.
- **A cached prefix.** The tool schemas are ~48 stable definitions and they are the bulk of
  every request. `cache_control` on the last one means later turns read them at a tenth of
  the price. `usage.cache_read_input_tokens` is reported per turn so a silent invalidation
  shows up instead of quietly costing full price.
- **A deterministic grader.** An LLM judge would roughly double the spend and add a second
  thing that can be wrong. The invariants are arithmetic over a voxel grid.
- **Hard caps.** `--max-turns` and `--max-output-tokens` per scenario, enforced client-side.
  A scenario stopped by a cap is reported as `budget_exhausted`, never as a failure — those
  are different results and conflating them would make the suite lie about capability.

The caps are client-side rather than the API's task budgets, which keeps this off a beta
surface at the cost of the model not knowing it is running out. For a graded artefact that
trade is fine; if you want the model to wrap up gracefully instead of being cut off, that is
what `output_config.task_budget` is for.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


def repo_root() -> Path:
    return Path(os.environ.get("NRC_ROOT") or Path(__file__).resolve().parent.parent)


sys.path.insert(0, str(repo_root() / "python" / "src"))

from nrc_mcp import playspace  # noqa: E402

SCENARIO_DIR = repo_root() / "bench" / "scenarios"
RESULT_DIR = repo_root() / "bench" / "results"
WORK_NAME = "work.map"

#: Opus 5. Overridable, but not silently: the report records which model produced it, because
#: a score without a model beside it cannot be compared to anything.
DEFAULT_MODEL = "claude-opus-5"

#: Enough room for thinking and a long answer. On Opus 5 thinking is on by default and
#: `max_tokens` caps thinking plus text together, so a tight value truncates mid-edit.
DEFAULT_MAX_TOKENS = 32000

#: The surface is large and the tasks are small; this is not where quality is won.
DEFAULT_EFFORT = "medium"

SYSTEM = """You are working on a level for a Quake-3-engine game through the nrc-mcp tool \
surface. Every tool call operates on real files.

Work only through the tools you have been given. Do not ask for confirmation — nobody is \
watching, and a question ends the run with the task unfinished.

Two things about this surface are worth knowing before you start:

- `map_save` compares what you are about to write against what is on disk and refuses a \
write that destroys playable space. If it refuses, read the findings and fix the cause. \
`acknowledge_regression=true` exists for when you genuinely meant to remove that space, not \
as a way past a message you did not read.
- Sculpting blind fails. Render, or measure with the analysis tools, before and after you \
change something.

Finish the task, then stop."""


class Tier2Error(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def load_scenarios(only: str | None = None) -> list[dict]:
    out = []
    for path in sorted(SCENARIO_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        data.setdefault("name", path.stem)
        if only and data["name"] != only:
            continue
        out.append(data)
    if only and not out:
        raise Tier2Error(f"no scenario named {only!r} in {SCENARIO_DIR}")
    return out


def prepare_workspace(scenario: dict) -> tuple[Path, Path | None]:
    """A throwaway directory holding the fixture, plus a pristine copy to grade against."""
    workspace = Path(tempfile.mkdtemp(prefix=f"nrc-tier2-{scenario['name']}-"))
    baseline: Path | None = None
    fixture = scenario.get("fixture")
    if fixture:
        src = Path(fixture)
        if not src.is_absolute():
            src = repo_root() / src
        if not src.is_file():
            raise Tier2Error(f"fixture not found: {src}")
        shutil.copyfile(src, workspace / WORK_NAME)
        (workspace / WORK_NAME).chmod(0o644)
        baseline = workspace / "baseline.map"
        shutil.copyfile(src, baseline)
        baseline.chmod(0o644)
    return workspace, baseline


def grade(scenario: dict, workspace: Path, baseline: Path | None, *, allow_compile: bool) -> dict:
    """Run the scenario's invariants over whatever the model left behind."""
    current = workspace / WORK_NAME
    check = playspace.Check(
        workspace=workspace,
        baseline=baseline if baseline and baseline.is_file() else None,
        current=current if current.is_file() else None,
        profile_id=scenario.get("profile"),
        cell=float(scenario.get("cell") or playspace.DEFAULT_DIFF_CELL),
        allow_compile=allow_compile,
    )
    outcomes = [
        playspace.run_invariant(spec["name"], check, **(spec.get("args") or {})).as_dict()
        for spec in scenario.get("invariants") or []
    ]
    return {
        "passed": all(o["ok"] for o in outcomes),
        "invariants": outcomes,
    }


# ---------------------------------------------------------------------------
# The MCP connection
# ---------------------------------------------------------------------------


async def _open_server():
    """Start the real server the way a client would, and hand back a live session."""
    from mcp import ClientSession, StdioServerParameters  # noqa: PLC0415
    from mcp.client.stdio import stdio_client  # noqa: PLC0415

    params = StdioServerParameters(
        command="mise",
        args=["-C", str(repo_root()), "run", "mcp:serve"],
        env=dict(os.environ),
    )
    return stdio_client(params), ClientSession


def _tool_schemas(mcp_tools: Any, allowed: list[str] | None) -> list[dict]:
    """MCP tool definitions as Messages API tools, with the cache breakpoint on the last one.

    Order is the order the server lists them in, which is stable — the prefix has to be
    byte-identical between turns or the cache never reads.
    """
    schemas = []
    for tool in mcp_tools:
        if allowed and tool.name not in allowed:
            continue
        schemas.append(
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
            }
        )
    if schemas:
        schemas[-1] = {**schemas[-1], "cache_control": {"type": "ephemeral"}}
    return schemas


def _text_of(content: Any) -> str:
    """MCP tool results arrive as content blocks; the model wants one string."""
    parts = []
    for block in getattr(content, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        elif getattr(block, "type", "") == "image":
            parts.append("[image returned]")
    return "\n".join(parts) or "(no output)"


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


async def run_scenario(
    scenario: dict,
    *,
    model: str,
    effort: str,
    allow_compile: bool,
    verbose: bool,
) -> dict:
    """Drive one scenario to completion, a cap, or a refusal. Never raises for a model result."""
    import anthropic  # noqa: PLC0415
    from mcp import ClientSession  # noqa: PLC0415

    started = time.time()
    workspace, baseline = prepare_workspace(scenario)
    max_turns = int(scenario.get("max_turns") or 24)
    budget = int(scenario.get("max_output_tokens") or 120_000)

    client = anthropic.AsyncAnthropic()
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    turns = 0
    outcome = "completed"
    transcript: list[dict] = []

    stdio_ctx, _ = await _open_server()
    async with stdio_ctx as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listing = await session.list_tools()
        tools = _tool_schemas(listing.tools, scenario.get("allowed_tools"))

        prompt = scenario["prompt"].format(
            work=str(workspace / WORK_NAME), workspace=str(workspace)
        )
        messages: list[dict] = [{"role": "user", "content": prompt}]

        while True:
            if turns >= max_turns:
                outcome = "budget_exhausted:turns"
                break
            if usage["output"] >= budget:
                outcome = "budget_exhausted:tokens"
                break
            turns += 1

            response = await client.messages.create(
                model=model,
                max_tokens=int(scenario.get("max_tokens") or DEFAULT_MAX_TOKENS),
                system=SYSTEM,
                tools=tools,
                messages=messages,
                output_config={"effort": effort},
            )
            u = response.usage
            usage["input"] += u.input_tokens or 0
            usage["output"] += u.output_tokens or 0
            usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
            usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0

            # Opus 5's classifiers can decline; content is empty or partial when they do.
            if response.stop_reason == "refusal":
                outcome = "refused"
                break

            messages.append({"role": "assistant", "content": response.content})
            calls = [b for b in response.content if b.type == "tool_use"]
            if verbose:
                for block in response.content:
                    if block.type == "text" and block.text.strip():
                        print(f"      · {block.text.strip()[:160]}")
                for c in calls:
                    print(f"      → {c.name}({json.dumps(c.input)[:120]})")

            if not calls:
                outcome = (
                    "completed" if response.stop_reason == "end_turn" else str(response.stop_reason)
                )
                break

            results = []
            for call in calls:
                try:
                    got = await session.call_tool(call.name, call.input or {})
                    body, is_error = _text_of(got), bool(getattr(got, "isError", False))
                except Exception as e:  # noqa: BLE001 — a tool error is data for the model
                    body, is_error = f"{type(e).__name__}: {e}", True
                transcript.append({"turn": turns, "tool": call.name, "error": is_error})
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": body[:20000],
                        "is_error": is_error,
                    }
                )
            messages.append({"role": "user", "content": results})

    report = grade(scenario, workspace, baseline, allow_compile=allow_compile)
    shutil.rmtree(workspace, ignore_errors=True)

    return {
        "scenario": scenario["name"],
        "note": scenario.get("note", ""),
        "model": model,
        "effort": effort,
        "outcome": outcome,
        "turns": turns,
        "tool_calls": len(transcript),
        "usage": usage,
        "seconds": round(time.time() - started, 1),
        # A scenario that never finished has not been shown to fail at the task.
        "passed": report["passed"] and outcome == "completed",
        "invariants": report["invariants"],
        "tools_used": sorted({t["tool"] for t in transcript}),
    }


# ---------------------------------------------------------------------------
# The modes that cost nothing
# ---------------------------------------------------------------------------


async def check(scenarios: list[dict], *, allow_compile: bool) -> int:
    """Everything except the model: the server starts, the tools list, the grading runs.

    Worth having as its own mode. Most of what can break in this harness — a renamed tool, a
    fixture that moved, an invariant that no longer exists — breaks without any model being
    involved, and finding that out should not cost anything.
    """
    from mcp import ClientSession  # noqa: PLC0415

    failures = 0
    stdio_ctx, _ = await _open_server()
    async with stdio_ctx as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listing = await session.list_tools()
        names = {t.name for t in listing.tools}
        print(f"  server: {len(names)} tools over stdio")

        for scenario in scenarios:
            problems = []
            for name in scenario.get("allowed_tools") or []:
                if name not in names:
                    problems.append(
                        f"allowed_tools names {name!r}, which the server does not expose"
                    )
            for spec in scenario.get("invariants") or []:
                if spec["name"] not in playspace.available():
                    problems.append(f"unknown invariant {spec['name']!r}")
            if not scenario.get("prompt"):
                problems.append("no prompt")

            # Grade the untouched fixture. This is the control: an invariant that already
            # passes before the model has done anything is not measuring the task.
            try:
                workspace, baseline = prepare_workspace(scenario)
                report = grade(scenario, workspace, baseline, allow_compile=allow_compile)
                shutil.rmtree(workspace, ignore_errors=True)
                trivial = [o["name"] for o in report["invariants"] if o["ok"] and not o["skipped"]]
                if report["passed"]:
                    problems.append(
                        "every invariant already passes on the untouched fixture, so this "
                        "scenario cannot distinguish success from doing nothing"
                    )
            except Tier2Error as e:
                problems.append(str(e))
                trivial = []

            mark = "ok  " if not problems else "FAIL"
            print(
                f"  {mark} {scenario['name']:<24} {len(scenario.get('invariants') or [])} invariant(s)"
            )
            if trivial and not problems:
                print(f"         . already true before the run: {', '.join(trivial)}")
            for p in problems:
                print(f"         ! {p}")
            failures += bool(problems)
    return failures


async def estimate(scenarios: list[dict], *, model: str) -> int:
    """What the fixed part of every turn costs, before spending anything on a real one."""
    import anthropic  # noqa: PLC0415
    from mcp import ClientSession  # noqa: PLC0415

    client = anthropic.AsyncAnthropic()
    stdio_ctx, _ = await _open_server()
    async with stdio_ctx as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listing = await session.list_tools()
        for scenario in scenarios:
            tools = _tool_schemas(listing.tools, scenario.get("allowed_tools"))
            counted = await client.messages.count_tokens(
                model=model,
                system=SYSTEM,
                tools=tools,
                messages=[{"role": "user", "content": scenario["prompt"]}],
            )
            turns = int(scenario.get("max_turns") or 24)
            print(
                f"  {scenario['name']:<24} prefix {counted.input_tokens:>7,} tokens "
                f"× up to {turns} turns "
                f"(cached after the first, at a tenth of the price)"
            )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def report(results: list[dict]) -> None:
    for r in results:
        mark = "ok  " if r["passed"] else "FAIL"
        u = r["usage"]
        print(
            f"  {mark} {r['scenario']:<24} {r['outcome']:<22} "
            f"{r['turns']:>2} turns, {r['tool_calls']:>3} calls, "
            f"{u['output']:>6,} out / {u['cache_read']:>7,} cached, {r['seconds']:>5.0f}s"
        )
        for inv in r["invariants"]:
            if inv["skipped"]:
                continue
            print(f"         {'.' if inv['ok'] else '!'} {inv['name']}: {inv['detail']}")
        if r["tools_used"]:
            print(f"         tools: {', '.join(r['tools_used'])}")

    passed = sum(1 for r in results if r["passed"])
    out = sum(r["usage"]["output"] for r in results)
    read = sum(r["usage"]["cache_read"] for r in results)
    fresh = sum(r["usage"]["input"] for r in results)
    print()
    print(f"  {passed}/{len(results)} passed")
    print(f"  tokens: {fresh:,} input, {read:,} cache read, {out:,} output")
    if read == 0 and len(results) > 0:
        print("  NOTE: nothing was read from cache. The tool prefix is being invalidated.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="wiring and grading only; no API calls")
    mode.add_argument("--estimate", action="store_true", help="count tokens in the fixed prefix")
    mode.add_argument("--run", action="store_true", help="spend money: drive a model")
    ap.add_argument("--scenario", help="run only this one")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument(
        "--effort", default=DEFAULT_EFFORT, choices=["low", "medium", "high", "xhigh", "max"]
    )
    ap.add_argument("--compile", action="store_true", help="let invariants run q3map2")
    ap.add_argument("--out", help="write the full report as JSON here")
    ap.add_argument(
        "-v", "--verbose", action="store_true", help="print the model's calls as they happen"
    )
    args = ap.parse_args()

    try:
        scenarios = load_scenarios(args.scenario)
    except (Tier2Error, json.JSONDecodeError) as e:
        print(f"tier2: {e}", file=sys.stderr)
        return 2
    if not scenarios:
        print("tier2: no scenarios", file=sys.stderr)
        return 2

    if args.check:
        print(f"tier2 check ({len(scenarios)} scenario(s)) — no API calls")
        return 1 if asyncio.run(check(scenarios, allow_compile=args.compile)) else 0

    try:
        import anthropic  # noqa: F401,PLC0415
    except ImportError:
        print(
            "tier2: the anthropic SDK is not installed. It is an optional dependency, so that "
            "`mise run test` never needs it:\n    uv sync --extra tier2",
            file=sys.stderr,
        )
        return 2

    if args.estimate:
        print(f"tier2 estimate, model {args.model}")
        return asyncio.run(estimate(scenarios, model=args.model))

    print(f"tier2 run, model {args.model}, effort {args.effort} — THIS SPENDS TOKENS")
    results = []
    for scenario in scenarios:
        results.append(
            asyncio.run(
                run_scenario(
                    scenario,
                    model=args.model,
                    effort=args.effort,
                    allow_compile=args.compile,
                    verbose=args.verbose,
                )
            )
        )
    report(results)
    if args.out:
        Path(args.out).write_text(json.dumps({"results": results}, indent=2))
        print(f"  written: {args.out}")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
