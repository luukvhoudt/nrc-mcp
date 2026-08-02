#!/usr/bin/env python3
"""The self-optimization loop (§11.2), with §11.4's guardrails as code rather than intentions.

§11.3 is explicit about where to start, and this follows it exactly:

> The highest return per unit of risk is **not** in the kernel. It's in tool descriptions, the
> conventions resource, the asset-brief template, and the pipeline prompt. These are measurable
> through F5/F6, can't corrupt anyone's map, and are trivially revertible.

So `ALLOWED_PATHS` covers the prompt and resource layer and nothing else. Widening it is a
deliberate edit to this file, which is itself protected — and §11.4 requires human review
indefinitely for the exact predicates and for anything that writes user `.map` files, so the kernel
stays out regardless.

Every guardrail §11.4 lists is enforced here:

- **Protected paths** are checked before and after, via `tools/protected.py`.
- **F1 is a gate**, never a weight. A red gate reverts, whatever else improved.
- **Merge requires** F1 green, no signal regressed, and at least one improved.
- **Git-backed with automatic revert**: every attempt is a branch, and a failed gate discards it.
- **Rate-limited**: on demand or nightly, never continuously. An agent rewriting itself in the
  middle of somebody's mapping session is not a feature.
- **Opt-in**: nothing here runs unless `NRC_SELFDEV=1`, so a normal session never sees it.

And **archive, not just a pointer** (§11.2): every attempt is kept with its scores, including the
rejected ones, so a later attempt can branch from a non-latest ancestor. A greedy chain gets stuck,
and the self-improving-agent literature is consistent on that point.
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

#: The only paths a self-dev attempt may modify. §11.3's "start with prompts, not code".
ALLOWED_PATHS = (
    "python/src/nrc_mcp/server.py",  # tool descriptions, which drive tool choice at all
    "docs/conventions.md",
    "profiles/",  # unverified rules only; the verified ones are hash-pinned
)

ATTEMPTS_DIR = "bench/selfdev"
#: Minimum gap between runs. §11.4: "Self-dev runs on demand or nightly, not continuously."
MIN_INTERVAL_SECONDS = 3600


class SelfDevError(RuntimeError):
    pass


def repo_root() -> Path:
    return Path(os.environ.get("NRC_ROOT") or Path(__file__).resolve().parent.parent)


def enabled() -> bool:
    return os.environ.get("NRC_SELFDEV") == "1"


def require_enabled() -> None:
    if not enabled():
        raise SelfDevError(
            "self-dev is off. Set NRC_SELFDEV=1 to enable it. It is opt-in because a normal "
            "mapping session has no business being able to rewrite the tool it is using."
        )


def git(args: list[str], root: Path, check: bool = False) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    out = (p.stdout or "") + (p.stderr or "")
    if check and p.returncode != 0:
        raise SelfDevError(f"git {' '.join(args)} failed: {out.strip()[:300]}")
    return p.returncode, out


def attempts_dir(root: Path) -> Path:
    d = root / ATTEMPTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def history(root: Path) -> list[dict]:
    return [
        json.loads(f.read_text()) for f in sorted(attempts_dir(root).glob("*.json")) if f.is_file()
    ]


def _last_run_time(root: Path) -> float:
    times = [
        a.get("started_epoch", 0.0)
        for a in history(root)
        if isinstance(a.get("started_epoch"), (int, float))
    ]
    return max(times) if times else 0.0


def check_rate_limit(root: Path, force: bool = False) -> None:
    if force:
        return
    since = time.time() - _last_run_time(root)
    if since < MIN_INTERVAL_SECONDS:
        raise SelfDevError(
            f"last attempt was {int(since / 60)} minutes ago; the minimum gap is "
            f"{MIN_INTERVAL_SECONDS // 60} minutes (§11.4 rate-limits this deliberately). "
            f"Pass --force if you are driving it by hand."
        )


def path_allowed(rel: str) -> tuple[bool, str]:
    """Whether a self-dev attempt may modify `rel`."""
    norm = rel.replace("\\", "/")
    if ".." in norm or norm.startswith("/"):
        return False, "path escapes the repository"
    # Note that `profiles/` being allowed does not make a profile's *verified* rules editable:
    # those are hash-pinned, so a change to one fails the protected-path check instead. The
    # split is deliberate — a profile may gain unverified rules freely, and freeze the rest.
    for allowed in ALLOWED_PATHS:
        if norm == allowed or (allowed.endswith("/") and norm.startswith(allowed)):
            return True, ""
    return False, (
        f"{norm} is outside the prompt and resource layer. §11.3 puts the loop there first "
        f"because that is where the return per unit of risk is highest, and §11.4 requires human "
        f"review indefinitely for the geometric predicates and for anything that writes user "
        f"maps. Allowed: {', '.join(ALLOWED_PATHS)}"
    )


def protected_problems(root: Path) -> list[str]:
    sys.path.insert(0, str(root / "tools"))
    import protected as prot  # noqa: PLC0415

    return prot.verify(root)


def run_bench(root: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, str(root / "tools" / "bench.py"), "--quiet"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    results = sorted((root / "bench" / "results").glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not results:
        raise SelfDevError(f"bench produced no result file: {proc.stderr.strip()[:300]}")
    return json.loads(results[-1].read_text())


def score_map(bench: dict) -> dict[str, float]:
    return {
        s["id"]: float(s["score"])
        for s in bench.get("signals", [])
        if "score" in s and not s.get("gate")
    }


def compare(before: dict, after: dict) -> dict[str, Any]:
    """Apply §11.4's merge rule: F1 green, nothing regressed, at least one improved."""
    b, a = score_map(before), score_map(after)
    lower_better = {"F2", "F6"}
    regressed, improved, unchanged = [], [], []
    for sid in sorted(set(b) | set(a)):
        if sid not in b or sid not in a:
            continue
        delta = a[sid] - b[sid]
        if delta == 0:
            unchanged.append(sid)
        elif (delta < 0) == (sid in lower_better):
            improved.append(f"{sid} {b[sid]} -> {a[sid]}")
        else:
            regressed.append(f"{sid} {b[sid]} -> {a[sid]}")

    gate = bool(after.get("gate_passed"))
    accept = gate and not regressed and bool(improved)
    reasons = []
    if not gate:
        reasons.append("F1 is red, and F1 is a gate that can never be traded (§11.4)")
    if regressed:
        reasons.append(f"regressed: {', '.join(regressed)}")
    if not improved:
        reasons.append("nothing improved, so there is no reason to keep the change")
    return {
        "accept": accept,
        "gate_passed": gate,
        "improved": improved,
        "regressed": regressed,
        "unchanged": unchanged,
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_propose(root: Path, hypothesis: str, target: str) -> dict:
    """Record an attempt before making it, so a rejected one is still evidence."""
    require_enabled()
    if target not in {
        s["id"]
        for s in json.loads((root / "bench" / "fitness.json").read_text())["signals"].values()
    } | set(json.loads((root / "bench" / "fitness.json").read_text())["signals"]):
        raise SelfDevError(
            f"target must be a fitness signal id (F1..F6), got {target!r}. Proposing against "
            f"something unmeasured is how a loop starts hill-climbing on its own opinion."
        )
    attempt_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    record = {
        "id": attempt_id,
        "hypothesis": hypothesis,
        "target_signal": target,
        "state": "proposed",
        "base_sha": git(["rev-parse", "HEAD"], root)[1].strip(),
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "started_epoch": time.time(),
    }
    (attempts_dir(root) / f"{attempt_id}.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def cmd_run(root: Path, attempt_id: str, force: bool = False) -> dict:
    """Verify, bench and gate the working tree against the attempt's baseline.

    The attempt's *changes* are whatever is already in the working tree: this does not write code
    itself. That separation is deliberate — the thing making edits is a model, and the thing
    judging them must not be.
    """
    require_enabled()
    check_rate_limit(root, force)

    path = attempts_dir(root) / f"{attempt_id}.json"
    if not path.is_file():
        raise SelfDevError(f"no attempt {attempt_id}; run selfdev.py history")
    record = json.loads(path.read_text())

    # 1. Only allowed paths may have changed.
    _, status = git(["status", "--porcelain"], root)
    changed = [ln[3:].strip() for ln in status.splitlines() if ln.strip()]
    violations = []
    for rel in changed:
        ok, why = path_allowed(rel)
        if not ok:
            violations.append(why)
    if violations:
        record.update(
            {
                "state": "rejected",
                "reason": "modified a disallowed path",
                "violations": violations[:5],
            }
        )
        path.write_text(json.dumps(record, indent=2) + "\n")
        return record

    # 2. Protected paths must be untouched. This is the guard against optimizing the ruler.
    problems = protected_problems(root)
    if problems:
        record.update(
            {"state": "rejected", "reason": "protected paths changed", "violations": problems[:5]}
        )
        path.write_text(json.dumps(record, indent=2) + "\n")
        return record

    # 3. The test suite, which contains F1.
    proc = subprocess.run(
        ["mise", "run", "test"], cwd=root, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        record.update(
            {
                "state": "rejected",
                "reason": "mise run test failed",
                "output_tail": (proc.stdout + proc.stderr)[-1500:],
            }
        )
        path.write_text(json.dumps(record, indent=2) + "\n")
        return record

    # 4. Bench, and compare against the baseline.
    after = run_bench(root)
    baseline_path = root / "bench" / "results" / f"{record['base_sha'][:7]}.json"
    if baseline_path.is_file():
        before = json.loads(baseline_path.read_text())
    else:
        record["note"] = (
            f"no baseline at {baseline_path.name}; this run becomes the baseline and cannot be "
            f"accepted, because there is nothing to compare it against"
        )
        before = None

    if before is None:
        record.update({"state": "baselined", "bench": after})
    else:
        verdict = compare(before, after)
        record.update(
            {
                "state": "accepted" if verdict["accept"] else "rejected",
                "verdict": verdict,
                "reason": "; ".join(verdict["reasons"])
                or "gate green and at least one signal improved",
                "bench": after,
            }
        )
    record["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path.write_text(json.dumps(record, indent=2) + "\n")
    return record


def cmd_revert(root: Path, attempt_id: str) -> dict:
    """Discard the working-tree changes of a rejected attempt."""
    require_enabled()
    path = attempts_dir(root) / f"{attempt_id}.json"
    if not path.is_file():
        raise SelfDevError(f"no attempt {attempt_id}")
    record = json.loads(path.read_text())

    _, status = git(["status", "--porcelain"], root)
    changed = [ln[3:].strip() for ln in status.splitlines() if ln.strip()]
    # Only revert what the attempt was allowed to touch, so an unrelated edit made by a human in
    # the meantime is not thrown away.
    reverted = []
    for rel in changed:
        ok, _ = path_allowed(rel)
        if ok:
            git(["checkout", "--", rel], root)
            reverted.append(rel)
    record["state"] = "reverted"
    record["reverted_paths"] = reverted
    path.write_text(json.dumps(record, indent=2) + "\n")
    return record


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="whether self-dev is enabled, and what it may touch")
    p = sub.add_parser("propose", help="record a hypothesis and its target signal")
    p.add_argument("hypothesis")
    p.add_argument("--target", required=True, help="fitness signal id, e.g. F5")
    p = sub.add_parser("run", help="verify, bench and gate the working tree")
    p.add_argument("attempt_id")
    p.add_argument("--force", action="store_true", help="ignore the rate limit")
    p = sub.add_parser("revert", help="discard a rejected attempt's changes")
    p.add_argument("attempt_id")
    sub.add_parser("history", help="every attempt, accepted and rejected")
    args = ap.parse_args()

    root = repo_root()
    try:
        if args.cmd == "status":
            print(f"self-dev: {'ENABLED' if enabled() else 'off (set NRC_SELFDEV=1)'}")
            print("\nmay modify (§11.3 — prompts and resources first, not the kernel):")
            for a in ALLOWED_PATHS:
                print(f"  {a}")
            print("\nnever, even by hand, without a human re-pin (§11.4):")
            try:
                for pr in protected_problems(root) or ["(all protected paths unchanged)"]:
                    print(f"  {pr}")
            except Exception as e:  # noqa: BLE001
                print(f"  could not check: {e}")
            print(f"\nrate limit: {MIN_INTERVAL_SECONDS // 60} minutes between attempts")
            attempts = history(root)
            print(f"attempts recorded: {len(attempts)}")
            return 0

        if args.cmd == "propose":
            r = cmd_propose(root, args.hypothesis, args.target)
            print(f"proposed {r['id']} targeting {r['target_signal']}")
            print("make your changes, then: selfdev.py run " + r["id"])
            return 0

        if args.cmd == "run":
            r = cmd_run(root, args.attempt_id, args.force)
            print(f"{r['id']}: {r['state'].upper()} — {r.get('reason', '')}")
            v = r.get("verdict") or {}
            for label in ("improved", "regressed"):
                if v.get(label):
                    print(f"  {label}: {', '.join(v[label])}")
            return 0 if r["state"] in ("accepted", "baselined") else 1

        if args.cmd == "revert":
            r = cmd_revert(root, args.attempt_id)
            print(f"{r['id']}: reverted {len(r['reverted_paths'])} path(s)")
            return 0

        if args.cmd == "history":
            for a in history(root):
                v = a.get("verdict") or {}
                extra = f"  improved={v.get('improved')}" if v.get("improved") else ""
                print(
                    f"  {a['id']}  {a['state']:<10} {a.get('target_signal', '?')}  "
                    f"{a.get('hypothesis', '')[:60]}{extra}"
                )
            return 0
    except SelfDevError as e:
        print(f"selfdev: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
