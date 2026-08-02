#!/usr/bin/env python3
"""Protected paths: the things self-modification may never touch (§11.4).

> Protected paths are immutable to self-dev, hash-pinned and checked pre-merge: the
> differential harness, the fitness definitions and bench runner, the corpus, and every
> `confidence: verified` entry in the rule profile. **Without this the agent optimizes the ruler
> instead of the thing** — the single most likely failure mode.

That is the whole argument, and §13 restates it: "The agent will try to game its own fitness
function. This is not hypothetical; it is the default behaviour of any optimization loop with a
mutable objective."

So the protection is mechanical. Every protected file's SHA-256 is pinned in
`bench/protected.json`, and `--verify` fails if any of them moved. Updating a pin requires
`--repin`, which is not something the self-dev loop calls — it exists for a human who has decided
the change is legitimate.

The list includes the profile's verified rules, extracted rather than whole-file hashed, so a
profile can gain new unverified rules without a human in the loop while the verified ones stay
frozen.

The pin file itself cannot appear in its own hash list — a file cannot contain its own hash. Its
integrity comes from git instead: `verify` checks that `bench/protected.json` is unmodified
relative to `HEAD`, so tampering shows up as a working-tree change a human sees in the diff before
anything merges. That is exactly the "git-backed with automatic revert" guarantee §11.4 asks for,
applied to the one file that cannot be self-protecting.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

#: Files self-dev may never modify. Anything that measures, judges, or defines correctness.
PROTECTED_FILES = (
    # The gate itself, and the kernel invariants it checks.
    "tools/difftest.py",
    "crates/nrc-core/src/exact.rs",
    "crates/nrc-core/src/winding.rs",
    "crates/nrc-core/src/num.rs",
    # The ruler.
    "tools/bench.py",
    "tools/protected.py",
    "bench/fitness.json",
    "bench/labels.json",
    "bench/tasks.json",
    # The seam that keeps the design honest.
    "tools/seam_lint.py",
)

#: Directories whose contents must not change. The corpus is evidence, not code.
PROTECTED_TREES = ("corpus/synthetic/degenerate",)

#: Extracted rather than whole-file hashed, so a profile may gain unverified rules freely.
PROTECTED_PROFILE_SECTIONS = ("rules", "movement", "engine_limits")


class ProtectionError(RuntimeError):
    pass


def repo_root() -> Path:
    return Path(os.environ.get("NRC_ROOT") or Path(__file__).resolve().parent.parent)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _verified_rule_digest(root: Path) -> dict[str, str]:
    """A digest per profile of only its verified, hard-failing content.

    Whole-file hashing a 2700-line profile would mean every documentation fix needed a human
    re-pin, and the loop would learn to avoid touching profiles at all rather than to respect the
    verified ones. Hashing only what can fail a build keeps the incentive pointed the right way.
    """
    out: dict[str, str] = {}
    prof_dir = root / "profiles"
    if not prof_dir.is_dir():
        return out
    try:
        import yaml
    except ImportError:
        return out

    for f in sorted(prof_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text())
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        material: dict = {}
        for section in PROTECTED_PROFILE_SECTIONS:
            value = data.get(section)
            if section == "rules" and isinstance(value, list):
                material[section] = [
                    r for r in value if isinstance(r, dict) and r.get("confidence") == "verified"
                ]
            elif value is not None:
                material[section] = value
        out[f.name] = sha(json.dumps(material, sort_keys=True).encode())
    return out


def compute(root: Path | None = None) -> dict[str, dict[str, str]]:
    """Current hashes of everything protected."""
    root = root or repo_root()
    files: dict[str, str] = {}
    for rel in PROTECTED_FILES:
        p = root / rel
        # A protected file that does not exist yet is recorded as absent rather than skipped, so
        # its later appearance is visible and its deletion cannot pass unnoticed.
        files[rel] = sha(p.read_bytes()) if p.is_file() else "absent"

    trees: dict[str, str] = {}
    for rel in PROTECTED_TREES:
        d = root / rel
        if not d.is_dir():
            trees[rel] = "absent"
            continue
        h = hashlib.sha256()
        for f in sorted(d.rglob("*")):
            if f.is_file():
                h.update(f.relative_to(d).as_posix().encode())
                h.update(f.read_bytes())
        trees[rel] = h.hexdigest()

    return {"files": files, "trees": trees, "profiles": _verified_rule_digest(root)}


def pin_path(root: Path) -> Path:
    return root / "bench" / "protected.json"


def load_pins(root: Path) -> dict:
    p = pin_path(root)
    if not p.is_file():
        raise ProtectionError(
            f"{p} does not exist, so nothing is pinned. Run `python tools/protected.py --repin` "
            f"once, by hand, after reviewing what is about to be frozen."
        )
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ProtectionError(f"{p} is unreadable: {e}") from e


def _pin_file_modified(root: Path) -> bool:
    """True if the pin file differs from what is committed.

    The pin file cannot hash itself, so this is how tampering with it is caught: a self-dev run
    that rewrote the pins would leave the file dirty relative to HEAD, and that is visible.
    """
    import subprocess as sp

    rel = pin_path(root).relative_to(root).as_posix()
    # Only meaningful for a tracked file in a real repository, and the check must be scoped to
    # *this* root. Git searches upward for a repository, so running it in a synthetic tree can
    # otherwise answer about an enclosing one. An untracked pin file cannot differ from HEAD
    # anyway, and a guard that fires on every fresh checkout gets switched off.
    tracked = sp.run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", rel],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_CEILING_DIRECTORIES": str(root.parent)},
    )
    if tracked.returncode != 0:
        return False
    diff = sp.run(
        ["git", "-C", str(root), "diff", "--quiet", "HEAD", "--", rel],
        capture_output=True,
        text=True,
        check=False,
    )
    # Exit 1 means differences; 128 means no HEAD, which is not evidence of tampering.
    return diff.returncode == 1


def verify(root: Path | None = None) -> list[str]:
    """Return a list of violations. Empty means everything protected is unchanged."""
    root = root or repo_root()
    pins = load_pins(root)
    now = compute(root)
    problems: list[str] = []

    for kind in ("files", "trees", "profiles"):
        expected = pins.get(kind, {})
        actual = now.get(kind, {})
        for key, want in expected.items():
            got = actual.get(key, "missing")
            if got != want:
                problems.append(
                    f"{kind}/{key}: pinned {want[:12]} but found "
                    f"{got[:12] if got != 'missing' else 'nothing'}"
                )
        for key in actual:
            if key not in expected:
                problems.append(f"{kind}/{key}: not pinned — re-pin deliberately or remove it")

    if _pin_file_modified(root):
        problems.append(
            "bench/protected.json differs from HEAD — the pin file cannot hash itself, so a "
            "change to it is caught here. Commit it deliberately or revert it."
        )
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--verify", action="store_true", help="fail if anything protected changed")
    ap.add_argument("--list", action="store_true", help="show what is protected and why")
    ap.add_argument(
        "--repin",
        action="store_true",
        help="rewrite the pins. For a human who has reviewed the change; self-dev must never "
        "invoke this, and the pin file is itself protected so that re-pinning cannot launder "
        "an edit.",
    )
    args = ap.parse_args()
    root = repo_root()

    if args.list or not (args.verify or args.repin):
        print("Protected from self-modification (§11.4):\n")
        print("  files — the gate, the exact predicates, the ruler, the seam lint")
        for rel in PROTECTED_FILES:
            state = "" if (root / rel).is_file() else "   [absent]"
            print(f"    {rel}{state}")
        print("\n  trees — evidence, not code")
        for rel in PROTECTED_TREES:
            print(f"    {rel}")
        print(f"\n  profile sections — {', '.join(PROTECTED_PROFILE_SECTIONS)}")
        print("    only `confidence: verified` rules are frozen, so a profile may still gain")
        print("    unverified ones without a human in the loop")
        if not (args.verify or args.repin):
            return 0

    if args.repin:
        pin_path(root).parent.mkdir(parents=True, exist_ok=True)
        state = compute(root)
        state["note"] = (
            "Hash pins for paths self-dev may not modify (§11.4). Re-pinned only by a human who "
            "has reviewed the change; the loop cannot call --repin."
        )
        pin_path(root).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        n = len(state["files"]) + len(state["trees"]) + len(state["profiles"])
        print(f"pinned {n} item(s) to {pin_path(root).relative_to(root)}")
        return 0

    try:
        problems = verify(root)
    except ProtectionError as e:
        print(f"protected: {e}", file=sys.stderr)
        return 2

    if problems:
        print(f"PROTECTION VIOLATED — {len(problems)} item(s) changed:\n", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print(
            "\nThese paths define what correct means. A self-dev attempt that changes them is "
            "optimizing the ruler, which §11.4 names as the most likely failure mode. If the "
            "change is legitimate, a human re-pins it with --repin after reviewing it.",
            file=sys.stderr,
        )
        return 1

    print("protected paths unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
