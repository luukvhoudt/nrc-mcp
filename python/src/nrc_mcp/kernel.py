"""Bridge to the Rust geometry kernel.

Isolated in one module on purpose. The kernel is reachable two ways — the `nrc_py`
extension module (in-process, what the server uses) and the `nrc` binary (out-of-process,
what mise tasks and CI use) — and callers should not care which. If the extension is ever
replaced, this file is the only one that changes.

A missing extension is by far the most likely first-run problem, so the error says exactly
what to run rather than surfacing a bare `ModuleNotFoundError`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path


class KernelUnavailable(RuntimeError):
    """The compiled kernel is not importable."""


def repo_root() -> Path:
    env = os.environ.get("NRC_ROOT")
    if env:
        return Path(env)
    # python/src/nrc_mcp/kernel.py -> repo root
    return Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def kernel():
    """The `nrc_py` extension module."""
    root = repo_root()
    # `mise run kernel:build` copies the cdylib here; add it to the path so the server
    # works from a source checkout without an install step.
    candidate = root / "python" / "src"
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))
    try:
        import nrc_py  # type: ignore[import-not-found]
    except ImportError as e:
        raise KernelUnavailable(
            "the geometry kernel extension is not built.\n"
            "  Run: mise run kernel:build\n"
            f"  Looked for nrc_py.so in {candidate}\n"
            f"  Underlying error: {e}"
        ) from e
    return nrc_py


@lru_cache(maxsize=1)
def nrc_binary() -> Path:
    """The `nrc` CLI, for operations a mise task should be able to reproduce."""
    root = repo_root()
    for rel in ("target/release/nrc", "target/debug/nrc"):
        p = root / rel
        if p.is_file():
            return p
    raise KernelUnavailable(
        "the `nrc` binary is not built.\n  Run: mise run kernel:build"
    )


def load_map(path: str | Path):
    """Load a `.map`, raising a message a human can act on."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"{p} does not exist")
    return kernel().Map.load(str(p))


def parse_map(source: str):
    return kernel().Map.parse(source)


def run_mise_task(task: str, args: list[str] | None = None, *, timeout: int = 1800) -> dict:
    """Run a mise task.

    §1.2's architectural rule: the server never shells out to a raw command, it calls
    `mise run <task> -- <args>`. The payoff is that anything the agent did is a task name
    plus arguments — a line a human can paste into their own shell and get the same result.
    """
    cmd = ["mise", "run", task]
    if args:
        cmd += ["--", *args]
    proc = subprocess.run(
        cmd,
        cwd=repo_root(),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return {
        "task": task,
        "args": args or [],
        "command": " ".join(cmd),
        "returncode": proc.returncode,
        "ok": proc.returncode == 0,
        # Trim: q3map2 emits thousands of shader-loading lines that would swamp a reply.
        "stdout": _tail(proc.stdout, 8000),
        "stderr": _tail(proc.stderr, 4000),
    }


def _tail(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return f"...[{len(text) - limit} bytes trimmed]...\n" + text[-limit:]
