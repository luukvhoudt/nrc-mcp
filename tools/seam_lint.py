#!/usr/bin/env python3
"""Portability seam lint (§7.4): fail if game-specific strings leak into code.

The design commitment is not "a multi-game abstraction exists" — it is *"the seam exists
and is enforced"*. Urban Terror is the first profile, and everything about it is data in
``profiles/`` and ``corpus/``. Code must not know which game it is serving.

This is cheap and mechanical, and it catches the drift on the day it happens rather than
two years later, which is the entire argument for having it. The specific leaks §7.4
predicts, and this lint is aimed at:

- physics constants hardcoded into movement checks instead of read from the profile,
- entity classnames appearing in validator *code* rather than profile *data*,
- unit conversions baked into the asset-brief generator,
- game-specific shader assumptions inside the generic shader auditor.

**The forbidden vocabulary is derived from the profile itself**, not from a hand-maintained
list. Add an entity to ``profiles/urt43.yaml`` and it is automatically forbidden in code
from that moment — the lint cannot fall behind the thing it is guarding.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Directories that must stay game-agnostic.
CODE_DIRS = ["crates", "python/src", "tools"]

# Where game-specific vocabulary is allowed to live.
#
# `mise.toml` and `mise.local.toml` are configuration, not code: naming a game install path
# is exactly their job. `docs/` is prose. Tests that exercise profile *loading* legitimately
# name a profile id, so `NRC_PROFILE`-style identifiers are handled by MIN_LENGTH and the
# allowlist below rather than by exempting whole test files.
ALLOWED_PATHS = {"mise.toml", "mise.local.toml"}
ALLOWED_DIRS = {"docs", "profiles", "corpus", "bench", "vendor", "target", ".venv", ".git"}

# Tokens short or generic enough to produce false positives regardless of profile.
IGNORE_TOKENS = {
    "angle", "angles", "team", "group", "type", "name", "color", "health", "axis",
    "light", "origin", "target", "targetname", "model", "speed", "wait", "count",
    "message", "music", "style", "shards", "only", "notfree", "notteam", "spawnflags",
    "worldspawn", "classname", "func_door", "func_static",
}

# Identifiers that are game-specific by nature and must never appear in code, independent
# of what the profile happens to contain today.
ALWAYS_FORBIDDEN = [
    r"\bq3ut4\b",
    r"\burban\s*terror\b",
    r"\bUrbanTerror\d*\b",
    r"\but4_",
    r"\binfo_ut_",
    r"\but_jump",
    r"\bg_gametype\b",
]

MIN_LENGTH = 6


def repo_root() -> Path:
    return Path(os.environ.get("NRC_ROOT") or Path(__file__).resolve().parent.parent)


def load_profile_vocabulary(root: Path) -> set[str]:
    """Classnames and game identifiers from every profile, as forbidden-in-code tokens.

    Parsed with a deliberately small regex rather than a YAML library so the lint has no
    dependencies and runs even when the Python environment is not yet synced — a lint that
    only works after a successful install is a lint that gets skipped.
    """
    vocab: set[str] = set()
    prof_dir = root / "profiles"
    if not prof_dir.is_dir():
        return vocab
    for f in sorted(prof_dir.rglob("*.yaml")) + sorted(prof_dir.rglob("*.yml")):
        text = f.read_text(errors="replace")
        for m in re.finditer(r"^\s*(?:-\s*)?classname:\s*['\"]?([A-Za-z0-9_]+)", text, re.M):
            vocab.add(m.group(1))
        for key in ("basegame", "game"):
            for m in re.finditer(rf"^\s*{key}:\s*['\"]?([A-Za-z0-9_]+)", text, re.M):
                vocab.add(m.group(1))
    return {
        v for v in vocab
        if len(v) >= MIN_LENGTH and v.lower() not in IGNORE_TOKENS
    }


def should_scan(p: Path, root: Path) -> bool:
    if p.suffix.lower() not in {".rs", ".py", ".toml", ".pyi"}:
        return False
    rel = p.relative_to(root)
    if str(rel) in ALLOWED_PATHS:
        return False
    return not any(part in ALLOWED_DIRS for part in rel.parts)


COMMENT_PREFIXES = ("//", "///", "//!", "#", "*")
DOCSTRING_DELIMS = ('"""', "'''")


def code_lines(path: Path) -> list[tuple[int, str]]:
    """Lines that can actually affect behaviour.

    Comments and Python docstrings are excluded, on purpose. The leak this lint exists to
    prevent is a game-specific value that *code depends on* — a classname branched on, a
    physics constant baked into a formula. A comment recording that a design decision came
    from a particular real map is provenance worth keeping, and treating it as a violation
    would just teach people to delete the provenance.
    """
    out: list[tuple[int, str]] = []
    in_doc: str | None = None
    for lineno, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
        line = raw.strip()

        if in_doc is not None:
            if in_doc in line:
                in_doc = None
            continue
        if path.suffix in {".py", ".pyi"}:
            for d in DOCSTRING_DELIMS:
                if line.startswith(d) or line.startswith(("r" + d, 'f' + d, 'b' + d)):
                    body = line.split(d, 1)[1]
                    if d not in body:
                        in_doc = d
                    break
            else:
                if line.startswith(COMMENT_PREFIXES):
                    continue
                out.append((lineno, raw))
            continue

        if line.startswith(COMMENT_PREFIXES):
            continue
        # Strip a trailing comment so `let x = 1; // urban terror` is not a hit.
        code = line.split("//", 1)[0] if "//" in line else line
        out.append((lineno, code))
    return out


def scan(root: Path, vocab: set[str], verbose: bool) -> list[tuple[Path, int, str, str]]:
    patterns: list[tuple[str, re.Pattern[str]]] = [
        (tok, re.compile(rf"\b{re.escape(tok)}\b")) for tok in sorted(vocab)
    ]
    always = [(pat, re.compile(pat, re.I)) for pat in ALWAYS_FORBIDDEN]

    hits: list[tuple[Path, int, str, str]] = []
    files = 0
    for d in CODE_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or not should_scan(p, root):
                continue
            files += 1
            for lineno, line in code_lines(p):
                if "seam-lint: allow" in line:
                    continue
                for pat, rx in always:
                    if rx.search(line):
                        hits.append((p.relative_to(root), lineno, pat, line.strip()[:120]))
                for tok, rx in patterns:
                    if rx.search(line):
                        hits.append((p.relative_to(root), lineno, tok, line.strip()[:120]))
    if verbose:
        print(f"  scanned {files} file(s) in {', '.join(CODE_DIRS)}")
        print(f"  {len(vocab)} profile-derived token(s) + "
              f"{len(ALWAYS_FORBIDDEN)} always-forbidden pattern(s)")
        print("  comments and docstrings excluded: the rule guards behaviour, not prose")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--list-vocabulary", action="store_true",
                    help="print the derived forbidden vocabulary and exit")
    args = ap.parse_args()

    root = repo_root()
    vocab = load_profile_vocabulary(root)

    if args.list_vocabulary:
        for v in sorted(vocab):
            print(v)
        return 0

    if not vocab:
        print("seam lint: no profile vocabulary found under profiles/ — nothing to enforce "
              "yet. This is expected only before the first profile is written.")

    hits = scan(root, vocab, args.verbose)
    if not hits:
        print("seam lint: clean — no game-specific strings in code")
        return 0

    print(f"seam lint: {len(hits)} leak(s) — game-specific vocabulary belongs in "
          f"profiles/ or corpus/, not in code (§7.4)\n")
    for path, lineno, tok, line in hits:
        print(f"  {path}:{lineno}: {tok!r}")
        print(f"    {line}")
    print("\nFix by moving the value into profiles/ and reading it as data. If a line is a "
          "false positive, append `# seam-lint: allow` with a reason.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
