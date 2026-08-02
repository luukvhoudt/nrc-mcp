#!/usr/bin/env python3
"""Mirror `contrib/mcpbridge` into a netradiant-custom checkout, ready for a PR (§9.3).

The plugin is maintained *here* and mirrored *there*. That direction matters and follows §9.3:
ship the external tool first, keep the plugin as a maintained downstream patch, and open a
Discussion before a PR. If the maintainer declines it, this repository still holds the source and
nothing is lost — only live editor sync.

What this does:

1. Copies the five plugin files into `contrib/mcpbridge/` in the target checkout.
2. Applies the one Makefile hunk, idempotently, in the documented position.
3. Reports the diffstat against the §10.2 readiness criteria — core files touched (must be zero),
   new dependencies (zero), and lines added.

It deliberately does **not** commit or push. A change destined for someone else's repository
should be read by a human before it becomes a commit.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN_FILES = ("mcpbridge.cpp", "mcpbridge.h", "mcpbridge.def", "json.h", "README.md")

#: Inserted after the sunplug recipe. Appending to `binaries-radiant-plugins` rather than editing
#: the list keeps the whole change to one contiguous block, so the default build is byte-identical
#: to before and the diff a maintainer reads is as small as it can be.
MAKEFILE_HUNK = """
# opt-in: exposes editor control on a loopback socket, see contrib/mcpbridge/README.md
MCPBRIDGE ?= no
ifeq ($(MCPBRIDGE),yes)
binaries-radiant-plugins: $(INSTALLDIR)/plugins/mcpbridge.$(DLL)
$(INSTALLDIR)/plugins/mcpbridge.$(DLL): LIBS_EXTRA := $(LIBS_GLIB) $(LIBS_QTWIDGETS)
$(INSTALLDIR)/plugins/mcpbridge.$(DLL): CPPFLAGS_EXTRA := $(CPPFLAGS_GLIB) $(CPPFLAGS_QTWIDGETS) -Ilibs -Iinclude -DMCPBRIDGE_ENABLED
$(INSTALLDIR)/plugins/mcpbridge.$(DLL): \\
\tcontrib/mcpbridge/mcpbridge.o \\

endif
"""

ANCHOR = "$(INSTALLDIR)/plugins/terrain_generator.$(DLL): LIBS_EXTRA"
MARKER = "MCPBRIDGE ?= no"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def run(cmd: list[str], cwd: Path) -> str:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    return p.stdout if p.returncode == 0 else ""


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--target",
        type=Path,
        default=Path.home() / "projects" / "netradiant-custom",
        help="a netradiant-custom checkout to mirror into",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = repo_root() / "contrib" / "mcpbridge"
    if not src.is_dir():
        print(f"nothing to sync: {src} does not exist", file=sys.stderr)
        return 2
    target = args.target
    if not (target / "Makefile").is_file() or not (target / "contrib").is_dir():
        print(
            f"{target} does not look like a netradiant-custom checkout (no Makefile and "
            f"contrib/). Pass --target.",
            file=sys.stderr,
        )
        return 2

    dest = target / "contrib" / "mcpbridge"
    copied = []
    for name in PLUGIN_FILES:
        s = src / name
        if not s.is_file():
            print(f"warning: {name} is missing from {src}", file=sys.stderr)
            continue
        if not args.dry_run:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(s, dest / name)
        copied.append(name)

    makefile = target / "Makefile"
    text = makefile.read_text()
    if MARKER in text:
        hunk = "already present"
    elif ANCHOR not in text:
        print(
            f"could not find the insertion anchor in {makefile}. Upstream has moved the "
            f"plugin recipes; apply contrib/mcpbridge/README.md's hunk by hand and update "
            f"ANCHOR in this script.",
            file=sys.stderr,
        )
        return 1
    else:
        if not args.dry_run:
            makefile.write_text(text.replace(ANCHOR, MAKEFILE_HUNK.lstrip("\n") + "\n" + ANCHOR, 1))
        hunk = "applied"

    print(f"target:   {target}")
    print(f"copied:   {len(copied)} file(s) -> contrib/mcpbridge/")
    print(f"makefile: {hunk}")

    # The §10.2 readiness criteria, measured rather than asserted.
    status = run(["git", "status", "--porcelain"], target)
    changed = [ln[3:] for ln in status.splitlines() if ln.strip()]
    outside = [f for f in changed if not f.startswith("contrib/mcpbridge/") and f != "Makefile"]
    # `git diff HEAD` sees only tracked files, so on a first sync it would report the eleven-line
    # Makefile hunk and call the plugin free. Count the new files as well — the number a
    # maintainer sees in the PR is what §10.2 is about.
    numstat = run(["git", "diff", "--numstat", "HEAD"], target)
    added = sum(
        int(ln.split("\t")[0]) for ln in numstat.splitlines() if ln.split("\t")[0].isdigit()
    )
    untracked = [ln[3:] for ln in status.splitlines() if ln.startswith("?? ") and "mcpbridge" in ln]
    for rel in untracked:
        path = target / rel
        files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        for f in files:
            added += len(f.read_text(errors="replace").splitlines())

    print()
    print("readiness (§10.2):")
    print(f"  files touched outside contrib/mcpbridge/ + Makefile: {len(outside)} (target 0)")
    if outside:
        for f in outside[:10]:
            print(f"    {f}")
    print(f"  lines added to tracked files: {added} (target < 900)")
    if added >= 900:
        print(
            "    over target — docs/editor-bridge.md gives the pruning order driven by the "
            "usage counters, and §10.1's hard rule is that any RPC method with zero real-session "
            "usage is cut before submission"
        )
    print()
    print("nothing was committed. Read the diff, then commit and push in that checkout.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
