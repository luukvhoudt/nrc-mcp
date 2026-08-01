#!/usr/bin/env python3
"""Drive q3map2, including across the WSL/Windows boundary.

Every compile in this project goes through here rather than through a raw command line,
because on this class of machine the compiler is not a peer of the rest of the toolchain.
The kernel, the MCP server and the repo live in WSL; Urban Terror, NetRadiant-custom and
therefore ``q3map2.exe`` live on the Windows side. Three consequences, all handled here so
that no caller has to think about them:

**Paths must be translated.** ``q3map2.exe`` cannot read ``/home/...``. Arguments become
Windows paths via ``wslpath -w``.

**UNC paths do not work.** ``\\\\wsl.localhost\\...`` is accepted by Windows but q3map2
treats it as *relative* and prefixes its own working directory, producing a
"Script file ... was not found" that names a path which is half cwd and half argument.
Verified empirically, not assumed. So a map living in WSL is **staged** into a directory
on the Windows filesystem, compiled there, and its artefacts copied back.

**Staging is also the safe default.** q3map2 writes ``.bsp``/``.prt``/``.srf`` next to the
``.map``. Compiling in a staging directory keeps build output out of the repo and out of
the user's game installation, which is somebody's actual Urban Terror install.

Argument order is not free either: ``-json`` and ``-pk3`` must be the **first** argument
(``main.cpp`` uses ``takeFront``) and the map file must be **last** (``takeBack``).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Stage flag sets. Sequences run in order, each a separate q3map2 invocation, because
# q3map2 is one stage per process.
#
# `-maxshaderinfo` is deliberately absent: it existed briefly upstream and was removed
# ("refactor to work without -maxshaderinfo limit"), so passing it is an error now.
PRESETS: dict[str, list[list[str]]] = {
    "draft": [["-bsp", "-meta"]],
    "iterate": [
        ["-bsp", "-meta"],
        ["-vis", "-fast"],
        ["-light", "-fast", "-samples", "1", "-bounce", "0"],
    ],
    "quality": [
        ["-bsp", "-meta"],
        ["-vis"],
        ["-light", "-fast", "-filter", "-samples", "2", "-bounce", "4",
         "-patchshadows", "-nobouncestore"],
    ],
    "final": [
        ["-bsp", "-meta"],
        ["-vis", "-saveprt"],
        ["-light", "-filter", "-samples", "3", "-bounce", "8",
         "-patchshadows", "-dirty", "-nobouncestore"],
    ],
}

# Files q3map2 may produce beside the .map, worth copying back from staging.
ARTIFACT_SUFFIXES = (".bsp", ".prt", ".srf", ".lin", ".pk3", ".json")


class Q3Map2Error(RuntimeError):
    pass


@dataclass
class StageResult:
    flags: list[str]
    returncode: int
    seconds: float
    # q3map2 is chatty; keep the lines that matter rather than megabytes of shader loading.
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tail: list[str] = field(default_factory=list)


def is_wsl() -> bool:
    if os.environ.get("NRC_Q3MAP2_MODE") == "windows":
        return True
    if os.environ.get("NRC_Q3MAP2_MODE") == "native":
        return False
    return "microsoft" in Path("/proc/version").read_text().lower() if Path("/proc/version").exists() else False


def to_windows_path(p: Path) -> str:
    """Translate a path for the Windows binary, or return it unchanged when native."""
    if not is_wsl():
        return str(p)
    out = subprocess.run(
        ["wslpath", "-w", str(p)], capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        raise Q3Map2Error(f"wslpath failed for {p}: {out.stderr.strip()}")
    return out.stdout.strip()


def find_q3map2() -> Path:
    raw = os.environ.get("Q3MAP2")
    if not raw:
        raise Q3Map2Error(
            "Q3MAP2 is unset. Point it at a q3map2 binary in mise.local.toml, or run "
            "`mise run vendor:build-tools` to build one."
        )
    p = Path(raw)
    if not p.exists():
        raise Q3Map2Error(f"Q3MAP2={p} does not exist")
    return p


def find_windows_workdir() -> Path | None:
    """A staging directory on the Windows filesystem, or None if we do not need one."""
    if not is_wsl():
        return None
    override = os.environ.get("NRC_WIN_WORKDIR")
    if override:
        d = Path(override)
        d.mkdir(parents=True, exist_ok=True)
        return d

    candidates: list[Path] = []
    users = Path("/mnt/c/Users")
    if users.is_dir():
        skip = {"All Users", "Default", "Default User", "Public", "desktop.ini"}
        for u in sorted(users.iterdir()):
            if u.name in skip or not u.is_dir():
                continue
            candidates.append(u / "AppData" / "Local" / "Temp" / "nrc-mcp")
    candidates.append(Path("/mnt/c/Windows/Temp/nrc-mcp"))

    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            probe = c / ".write-test"
            probe.write_text("ok")
            probe.unlink()
            return c
        except OSError:
            continue
    raise Q3Map2Error(
        "no writable directory found on the Windows filesystem for staging. Set "
        "NRC_WIN_WORKDIR in mise.local.toml to a path under /mnt/c that you can write to."
    )


def needs_staging(map_path: Path) -> bool:
    """True when the map lives somewhere the Windows binary cannot address."""
    if not is_wsl():
        return False
    # Anything under a DrvFs mount already has a drive-letter path.
    return not str(map_path.resolve()).startswith("/mnt/")


def general_flags() -> list[str]:
    """Build the general flags from environment only.

    No game name, mod directory or install path is hardcoded here. That is the §7.4 seam:
    this file must stay game-agnostic, and `tools/seam_lint.py` fails the build if a
    game-specific string appears in it. `NRC_FS_GAME` unset simply means no `-fs_game`,
    which is the correct game-neutral behaviour rather than a guess.
    """
    flags = ["-game", os.environ.get("NRC_Q3MAP2_GAME", "quake3")]

    base = os.environ.get("NRC_FS_BASEPATH") or os.environ.get("URT_BASEPATH")
    if base:
        bp = Path(base)
        if bp.is_dir():
            flags += ["-fs_basepath", to_windows_path(bp)]
        else:
            print(
                f"warning: game basepath {bp} is not a directory; shaders will not resolve",
                file=sys.stderr,
            )
    game_dir = os.environ.get("NRC_FS_GAME")
    if game_dir:
        flags += ["-fs_game", game_dir]
    return flags


def classify(line: str) -> str | None:
    low = line.lower()
    if low.startswith("************ error") or low.startswith("error:"):
        return "error"
    if "warning" in low:
        return "warning"
    return None


def run_stage(exe: Path, general: list[str], flags: list[str], win_map: str,
              verbose: bool) -> StageResult:
    # `-json`/`-pk3` are consumed with takeFront and must lead; the map file is taken with
    # takeBack and must trail. Everything else sits between.
    lead: list[str] = []
    rest = list(flags)
    if rest and rest[0] in ("-json", "-pk3", "-repack"):
        lead = [rest.pop(0)]

    cmd = [str(exe), *lead, *general, *rest, win_map]
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", check=False)
    elapsed = time.monotonic() - t0

    out = (proc.stdout or "") + (proc.stderr or "")
    lines = out.splitlines()
    res = StageResult(flags=flags, returncode=proc.returncode, seconds=elapsed)
    for ln in lines:
        kind = classify(ln)
        if kind == "error":
            res.errors.append(ln.strip())
        elif kind == "warning":
            res.warnings.append(ln.strip())
    res.tail = [ln for ln in lines[-25:] if ln.strip()]

    if verbose:
        print(out, file=sys.stderr)
    return res


def compile_map(map_path: Path, stages: list[list[str]], *, stage_dir: Path | None,
                verbose: bool, keep: bool) -> dict:
    exe = find_q3map2()
    map_path = map_path.resolve()
    if not map_path.is_file():
        raise Q3Map2Error(f"{map_path} does not exist")

    staged = False
    work_map = map_path
    workdir: Path | None = None

    if needs_staging(map_path):
        root = stage_dir or find_windows_workdir()
        if root is None:
            raise Q3Map2Error("staging required but no Windows work directory available")
        workdir = root / map_path.stem
        workdir.mkdir(parents=True, exist_ok=True)
        work_map = workdir / map_path.name
        shutil.copyfile(map_path, work_map)
        staged = True

    general = general_flags()
    win_map = to_windows_path(work_map)

    results: list[StageResult] = []
    for flags in stages:
        r = run_stage(exe, general, flags, win_map, verbose)
        results.append(r)
        if r.returncode != 0:
            break

    # Collect artefacts before any cleanup, and copy them out of staging so a compile is
    # reproducible from the repo without reaching into a Windows temp directory.
    artifacts: list[str] = []
    out_dir = Path(os.environ.get("NRC_ROOT", ".")) / "out" / map_path.stem
    for suffix in ARTIFACT_SUFFIXES:
        produced = work_map.with_suffix(suffix)
        if produced.exists():
            if staged:
                out_dir.mkdir(parents=True, exist_ok=True)
                dest = out_dir / produced.name
                shutil.copyfile(produced, dest)
                artifacts.append(str(dest))
            else:
                artifacts.append(str(produced))

    # `-json` unpacks the BSP into a *directory* of per-lump files beside the input
    # (Brushes.json, DrawSurfaces.json, planes.json, entities.json, …), not a single file.
    # That directory is the whole point of the flag, so it has to come back too.
    lump_dir = work_map.with_suffix("")
    if lump_dir.is_dir():
        if staged:
            dest_dir = out_dir / lump_dir.name
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(lump_dir, dest_dir)
            artifacts.append(str(dest_dir))
        else:
            artifacts.append(str(lump_dir))

    if staged and workdir and not keep:
        shutil.rmtree(workdir, ignore_errors=True)

    ok = all(r.returncode == 0 for r in results) and bool(results)
    return {
        "map": str(map_path),
        "ok": ok,
        "staged": staged,
        "q3map2": str(exe),
        "windows_mode": is_wsl(),
        "artifacts": artifacts,
        "total_seconds": round(sum(r.seconds for r in results), 3),
        "stages": [
            {
                "flags": r.flags,
                "returncode": r.returncode,
                "seconds": round(r.seconds, 3),
                "error_count": len(r.errors),
                "warning_count": len(r.warnings),
                "errors": r.errors[:20],
                "warnings": r.warnings[:20],
                "tail": r.tail if r.returncode != 0 else r.tail[-6:],
            }
            for r in results
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--preset", choices=sorted(PRESETS), help="named compile preset (§6.1)")
    g.add_argument("--json-unpack", action="store_true",
                   help="dump a .bsp to JSON (note: q3map2 never parses -unpack; unpack "
                        "is the default for -json, so we do not pass it)")
    g.add_argument("--flags", nargs=argparse.REMAINDER,
                   help="raw q3map2 flags for one stage, for experiments")
    ap.add_argument("--stage-dir", type=Path, default=None,
                    help="override the Windows staging directory")
    ap.add_argument("--keep-staging", action="store_true",
                    help="leave the staging directory in place for inspection")
    ap.add_argument("-v", "--verbose", action="store_true", help="stream q3map2 output")
    ap.add_argument("target", type=Path, help="the .map (or .bsp for --json-unpack)")
    args = ap.parse_args()

    if args.preset:
        stages = PRESETS[args.preset]
    elif args.json_unpack:
        stages = [["-json"]]
    else:
        stages = [list(args.flags or [])]
        if not stages[0]:
            ap.error("--flags needs at least one flag")

    try:
        result = compile_map(args.target, stages, stage_dir=args.stage_dir,
                             verbose=args.verbose, keep=args.keep_staging)
    except Q3Map2Error as e:
        print(f"q3map2: {e}", file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2))
    if not result["ok"]:
        for s in result["stages"]:
            if s["returncode"] != 0:
                print(f"\nq3map2 failed on {' '.join(s['flags'])}:", file=sys.stderr)
                for ln in s["errors"] or s["tail"]:
                    print(f"  {ln}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
