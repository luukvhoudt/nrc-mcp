"""mise task discovery — the agent's action surface (§1.2).

`mise tasks --json` is exposed as an MCP resource, so the agent enumerates what the project
can do instead of relying on hardcoded tool wrappers. Adding a file task under
`mise-tasks/` therefore grants a new ability with zero server code change, which is what
makes the task list the safest substrate for the self-optimization loop to mutate (§11).

Task names are treated as a public API: renaming one is a breaking change to the agent's
action surface (§1.4).
"""

from __future__ import annotations

import json
import subprocess
from functools import lru_cache

from .kernel import repo_root

# Tasks that mutate user data or take a long time. Surfaced so the agent can warn before
# invoking one, and so `task_run` can refuse without an explicit acknowledgement.
DESTRUCTIVE_PREFIXES = ("pack", "install", "selfdev:merge", "normalize")
SLOW_PREFIXES = ("vendor:", "compile:final", "compile:quality", "bench")


def _classify(name: str) -> dict[str, bool]:
    return {
        "mutates_user_data": name.startswith(DESTRUCTIVE_PREFIXES),
        "slow": name.startswith(SLOW_PREFIXES),
    }


@lru_cache(maxsize=1)
def list_tasks() -> list[dict]:
    """Every mise task, with its description and hints."""
    proc = subprocess.run(
        ["mise", "tasks", "--json"],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"`mise tasks --json` failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    raw = json.loads(proc.stdout or "[]")

    out: list[dict] = []
    for t in raw:
        name = t.get("name", "")
        entry = {
            "name": name,
            "description": t.get("description", ""),
            "source": t.get("source", ""),
            "depends": t.get("depends", []),
            **_classify(name),
        }
        out.append(entry)
    out.sort(key=lambda t: t["name"])
    return out


def task_names() -> set[str]:
    return {t["name"] for t in list_tasks()}


def describe_tasks() -> str:
    """A compact grouped listing, which reads better than raw JSON for a model."""
    tasks = list_tasks()
    groups: dict[str, list[dict]] = {}
    for t in tasks:
        prefix = t["name"].split(":")[0] if ":" in t["name"] else "(top level)"
        groups.setdefault(prefix, []).append(t)

    lines = [f"{len(tasks)} mise tasks — the project's only build/run interface.", ""]
    for prefix in sorted(groups):
        lines.append(f"## {prefix}")
        for t in groups[prefix]:
            flags = []
            if t["mutates_user_data"]:
                flags.append("MUTATES USER DATA")
            if t["slow"]:
                flags.append("slow")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            lines.append(f"  {t['name']:<24} {t['description']}{suffix}")
        lines.append("")
    return "\n".join(lines)
