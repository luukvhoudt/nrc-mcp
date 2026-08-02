#!/usr/bin/env python3
"""Watch upstream for drift that breaks an out-of-tree plugin (§10.1).

§13: "Upstream drift breaks out-of-tree plugins silently. The interface-hash watcher in §10.1
exists because a changed signature in `include/` will compile-fail at the worst possible moment
otherwise. Run it nightly from day one, not from phase 8."

So this hashes the *declarations the bridge actually binds to*, not whole files. A whole-file hash
fires on every comment change and is therefore ignored within a week; a per-declaration hash fires
only when something the plugin calls has moved, which is the signal worth waking up for.

Four feeds, per §10.1:

1. **Interface signatures** — the declarations in `include/` that `contrib/mcpbridge` uses.
2. **`contrib/` structure** — new plugins mean new conventions to match.
3. **Build plumbing** — `Makefile` and its `.conf` files, where the plugin's one hunk lives.
4. **Compiler flags** — new flags are new optimizer capabilities (§6), so this feed pays for
   itself even if the PR never happens. Read from `help.cpp`, **not** from
   `docs/changelog-custom.txt`: the changelog is prose, covers a fraction of the flags, and
   documents flags that were later removed with no removal marker. See docs/spec-corrections.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

#: Headers the bridge binds to. Anything not listed here can change freely as far as we care.
WATCHED_HEADERS = (
    "qerplugin.h",
    "iscenegraph.h",
    "iselection.h",
    "iundo.h",
    "ientity.h",
    "ibrush.h",
    "ipatch.h",
    "icamera.h",
    "imap.h",
    "ifilter.h",
    "ieclass.h",
    "ireference.h",
    "mapfile.h",
    "preferencesystem.h",
    "modulesystem.h",
)

#: A declaration: a return type, a name, and a parameter list. Deliberately loose — this needs to
#: notice a changed signature, not to parse C++.
DECL_RE = re.compile(
    r"^[ \t]*(?:virtual\s+|static\s+|inline\s+|explicit\s+)*"
    r"([A-Za-z_][\w:<>,*&\s]*?)\s+"
    r"([A-Za-z_]\w*)\s*"
    r"\(([^;{)]*)\)\s*(?:const)?\s*(?:=\s*0)?\s*[;{]",
    re.M,
)

FLAG_RE = re.compile(r'\{\s*"(-[A-Za-z0-9_]+)"')


def repo_root() -> Path:
    return Path(os.environ.get("NRC_ROOT") or Path(__file__).resolve().parent.parent)


def upstream_dir(root: Path) -> Path:
    return Path(os.environ.get("NRC_SRC") or (root / "vendor" / "netradiant-custom"))


def git(args: list[str], cwd: Path) -> tuple[int, str]:
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def normalize(text: str) -> str:
    """Collapse whitespace so reformatting alone does not look like a change."""
    return re.sub(r"\s+", " ", text).strip()


def header_signatures(src: Path) -> dict[str, dict[str, str]]:
    """Per header, a hash per declaration name.

    Keyed by name rather than by position, so adding a method does not shift every hash after it —
    that is the difference between a watcher that reports one new declaration and one that reports
    forty changes and gets muted.
    """
    out: dict[str, dict[str, str]] = {}
    inc = src / "include"
    if not inc.is_dir():
        return out
    for name in WATCHED_HEADERS:
        f = inc / name
        if not f.is_file():
            out[name] = {"__missing__": "absent"}
            continue
        text = f.read_text(errors="replace")
        decls: dict[str, str] = {}
        for m in DECL_RE.finditer(text):
            ret, fn, params = m.group(1), m.group(2), m.group(3)
            if fn in ("if", "for", "while", "switch", "return", "sizeof", "defined"):
                continue
            # Normalize the parts separately: collapsing the whole string still leaves
            # "( int x )" different from "(int x)", which would make reformatting look like a
            # signature change and get the watcher muted within a week.
            sig = f"{normalize(ret)} {fn}({normalize(params)})"
            key = fn
            # Overloads share a name, so fold them into one hash for that name.
            decls[key] = hashlib.sha256((decls.get(key, "") + "|" + sig).encode()).hexdigest()[:16]
        out[name] = decls
    return out


def contrib_shape(src: Path) -> dict[str, int]:
    d = src / "contrib"
    if not d.is_dir():
        return {}
    return {
        p.name: sum(1 for f in p.rglob("*") if f.is_file())
        for p in sorted(d.iterdir())
        if p.is_dir()
    }


def build_plumbing(src: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in ("Makefile", "Makefile.conf", "mingw-Makefile.conf", "msys2-Makefile.conf"):
        f = src / name
        out[name] = hashlib.sha256(f.read_bytes()).hexdigest()[:16] if f.is_file() else "absent"
    return out


def compiler_flags(src: Path) -> list[str]:
    """Flags as the compiler's own help text lists them.

    `help.cpp` is the real inventory: 267 `{ "-flag", "description" }` literals, and it is what
    `q3map2 -help` prints. It can still drift from what is actually parsed — `-unpack` appears here
    and is never consumed — so a new flag is a lead to check, not a fact.
    """
    f = src / "tools" / "quake3" / "q3map2" / "help.cpp"
    if not f.is_file():
        return []
    return sorted(set(FLAG_RE.findall(f.read_text(errors="replace"))))


def snapshot(src: Path) -> dict[str, Any]:
    code, head = git(["git", "rev-parse", "HEAD"], src)
    _, date = git(["git", "log", "-1", "--format=%cI"], src)
    return {
        "head": head.strip() if code == 0 else "unknown",
        "head_date": date.strip(),
        "signatures": header_signatures(src),
        "contrib": contrib_shape(src),
        "build": build_plumbing(src),
        "flags": compiler_flags(src),
    }


def diff_snapshots(old: dict, new: dict) -> dict[str, Any]:
    report: dict[str, Any] = {
        "head_changed": old.get("head") != new.get("head"),
        "old_head": old.get("head"),
        "new_head": new.get("head"),
        "signature_changes": [],
        "contrib_changes": [],
        "build_changes": [],
        "new_flags": [],
        "removed_flags": [],
    }

    for header, decls in new.get("signatures", {}).items():
        before = old.get("signatures", {}).get(header, {})
        for fn, h in decls.items():
            if fn not in before:
                report["signature_changes"].append(f"{header}: new declaration {fn}")
            elif before[fn] != h:
                report["signature_changes"].append(
                    f"{header}: {fn} CHANGED — this is the case that silently breaks the plugin"
                )
        for fn in before:
            if fn not in decls:
                report["signature_changes"].append(f"{header}: {fn} REMOVED")

    for name, count in new.get("contrib", {}).items():
        if name not in old.get("contrib", {}):
            report["contrib_changes"].append(f"new plugin {name} ({count} files)")
    for name in old.get("contrib", {}):
        if name not in new.get("contrib", {}):
            report["contrib_changes"].append(f"plugin {name} removed")

    for name, h in new.get("build", {}).items():
        if old.get("build", {}).get(name) != h:
            report["build_changes"].append(name)

    old_flags = set(old.get("flags", []))
    new_flags = set(new.get("flags", []))
    report["new_flags"] = sorted(new_flags - old_flags)
    report["removed_flags"] = sorted(old_flags - new_flags)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--state", type=Path, default=Path("docs/upstream-state.json"))
    ap.add_argument("--fetch", action="store_true", help="fetch upstream before comparing")
    ap.add_argument("--baseline", action="store_true", help="record the current state and stop")
    args = ap.parse_args()

    root = repo_root()
    src = upstream_dir(root)
    if not (src / "include").is_dir():
        print(
            f"{src} is not a netradiant-custom checkout. Run `mise run vendor:clone`, or set "
            f"NRC_SRC.",
            file=sys.stderr,
        )
        return 2

    if args.fetch:
        code, out = git(["git", "fetch", "--depth", "50", "origin"], src)
        if code != 0:
            print(
                f"fetch failed (continuing with the local checkout): {out.strip()[:200]}",
                file=sys.stderr,
            )

    now = snapshot(src)
    state_path = root / args.state
    state_path.parent.mkdir(parents=True, exist_ok=True)

    if args.baseline or not state_path.is_file():
        state_path.write_text(json.dumps(now, indent=2, sort_keys=True) + "\n")
        n = sum(len(d) for d in now["signatures"].values())
        print(f"baseline recorded: {now['head'][:12]}, {n} declarations, {len(now['flags'])} flags")
        print(f"wrote {state_path.relative_to(root)}")
        return 0

    old = json.loads(state_path.read_text())
    report = diff_snapshots(old, now)

    breaking = [c for c in report["signature_changes"] if "CHANGED" in c or "REMOVED" in c]
    print(f"upstream: {report['old_head'][:12]} -> {report['new_head'][:12]}")
    if not report["head_changed"]:
        print("  no new commits")

    if breaking:
        print(
            f"\n  {len(breaking)} BREAKING interface change(s) — the plugin will fail to compile:"
        )
        for c in breaking[:20]:
            print(f"    {c}")
    added = [c for c in report["signature_changes"] if "new declaration" in c]
    if added:
        print(f"\n  {len(added)} new declaration(s) (additive, harmless):")
        for c in added[:10]:
            print(f"    {c}")
    for key, label in (
        ("contrib_changes", "contrib/ structure"),
        ("build_changes", "build plumbing"),
        ("new_flags", "new compiler flags — each one is a potential optimizer capability (§6)"),
        ("removed_flags", "removed compiler flags — stop passing these"),
    ):
        if report[key]:
            print(f"\n  {label}:")
            for c in report[key][:20]:
                print(f"    {c}")

    if not any(
        report[k]
        for k in (
            "signature_changes",
            "contrib_changes",
            "build_changes",
            "new_flags",
            "removed_flags",
        )
    ):
        print("  nothing the bridge or the optimizer depends on has moved")

    # Only advance the baseline when there is nothing breaking, so a breaking change keeps being
    # reported until somebody deals with it rather than being silently absorbed.
    if not breaking:
        state_path.write_text(json.dumps(now, indent=2, sort_keys=True) + "\n")
        print(f"\nbaseline advanced to {now['head'][:12]}")
    else:
        print(
            "\nbaseline NOT advanced: a breaking change stays reported until it is dealt with, "
            "rather than being absorbed into the new normal."
        )
    return 1 if breaking else 0


if __name__ == "__main__":
    sys.exit(main())
