#!/usr/bin/env python3
"""The differential round-trip harness — THE gate (§3.3).

Nothing else in this project is trustworthy until this is green, so it checks the claim at
two independent levels and reports them separately.

**Syntactic.** Load and re-serialize every map in the corpus and require the bytes to be
identical. This is the strong check: if the bytes match, no downstream behaviour can have
changed, and it runs in milliseconds over megabytes.

**Semantic.** Compile the original and the re-serialized copy with q3map2, unpack both
BSPs to JSON, and compare the lumps that describe geometry. This catches a class the
syntactic check cannot: a file we reproduce faithfully but *interpret* differently from the
compiler, and — once the kernel starts modifying maps — a change that is textually small
and geometrically catastrophic. It is skipped with a clear note, never silently, when
q3map2 is unavailable.

Lumps holding compiled lighting are excluded from the comparison: ``-light`` is not run
here, and vis/lightmap bytes vary with thread scheduling in ways that say nothing about the
kernel.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Lumps compared for semantic equality. Geometry and entities only — the things a .map
# actually determines.
COMPARED_LUMPS = [
    "entities.json",
    "shaders.json",
    "planes.json",
    "nodes.json",
    "leafs.json",
    "LeafSurfaces.json",
    "LeafBrushes.json",
    "models.json",
    "Brushes.json",
    "BrushSides.json",
    "DrawVert.json",
    "DrawSurfaces.json",
    "DrawIndexes.json",
    "fogs.json",
]

# Deliberately excluded, with the reason recorded so nobody "fixes" this later.
EXCLUDED_LUMPS = {
    "VisBytes.json": "vis is not run in a -bsp-only compile",
    "LightBytes.json": "lighting is not run; bytes are noise here",
    "GridPoints.json": "light grid is a lighting product",
}

# Fields dropped before comparing, per lump.
#
# `DrawVert.lightmap` is **uninitialized memory** in a `-bsp`-only compile. Verified by
# compiling one byte-identical input twice, single-threaded: 57 of 12986 verts differed, in
# no field but `lightmap`, holding values like `6.5e-43` and `-2.0e+21`. Lighting never ran,
# so those coordinates were never written and the BSP carries whatever was on the heap.
#
# This is an upstream defect, not non-determinism we introduced, and not something
# `-threads 1` fixes. Excluding the field rather than the whole lump keeps the parts a
# `.map` actually determines — position, normal, texture coordinates, colour — under test.
LUMP_FIELD_EXCLUSIONS: dict[str, list[str]] = {
    "DrawVert.json": ["lightmap"],
}


def strip_fields(value, fields: list[str]):
    """Recursively drop `fields` from every mapping inside `value`."""
    if isinstance(value, dict):
        return {k: strip_fields(v, fields) for k, v in value.items() if k not in fields}
    if isinstance(value, list):
        return [strip_fields(v, fields) for v in value]
    return value


@dataclass
class Result:
    name: str
    path: str
    syntactic_ok: bool
    detail: str = ""
    semantic: str = "skipped"  # ok | differs | skipped | error
    semantic_detail: str = ""
    brushes: int = 0
    patches: int = 0
    entities: int = 0
    lump_diffs: list[str] = field(default_factory=list)


def repo_root() -> Path:
    return Path(os.environ.get("NRC_ROOT") or Path(__file__).resolve().parent.parent)


def nrc_binary(root: Path) -> Path:
    for cand in (root / "target" / "release" / "nrc", root / "target" / "debug" / "nrc"):
        if cand.is_file():
            return cand
    raise SystemExit(
        "the `nrc` binary is missing — run `mise run kernel:build` first "
        "(looked in target/release and target/debug)"
    )


def collect_maps(corpus: Path, limit: int | None) -> list[Path]:
    maps = sorted(p for p in corpus.rglob("*.map") if p.is_file())
    return maps[:limit] if limit else maps


def run_syntactic(nrc: Path, maps: list[Path]) -> dict[str, Result]:
    """One `nrc roundtrip` call over the whole corpus."""
    proc = subprocess.run(
        [str(nrc), "roundtrip", "--quiet", *[str(m) for m in maps]],
        capture_output=True, text=True, check=False,
    )
    if not proc.stdout.strip():
        raise SystemExit(f"nrc roundtrip produced no output; stderr:\n{proc.stderr}")
    data = json.loads(proc.stdout)

    out: dict[str, Result] = {}
    for r in data["results"]:
        p = Path(r["file"])
        detail = ""
        if not r["ok"]:
            if "error" in r:
                detail = r["error"]
            else:
                fd = r.get("first_difference") or {}
                detail = (
                    f"line {fd.get('line')}: expected {fd.get('expected')!r}, "
                    f"got {fd.get('actual')!r}"
                )
        out[p.name] = Result(
            name=p.name,
            path=str(p),
            syntactic_ok=bool(r["ok"]),
            detail=detail,
            brushes=r.get("brushes", 0),
            patches=r.get("patches", 0),
            entities=r.get("entities", 0),
        )
    return out


def q3map2_available() -> tuple[bool, str]:
    raw = os.environ.get("Q3MAP2")
    if not raw:
        return False, "Q3MAP2 is unset (set it in mise.local.toml)"
    if not Path(raw).exists():
        return False, f"Q3MAP2={raw} does not exist"
    return True, raw


def compile_and_unpack(root: Path, map_path: Path, workdir: Path) -> Path:
    """Compile a .map and unpack its BSP, returning the lump directory."""
    env = dict(os.environ, NRC_ROOT=str(workdir))
    for stage in (["--preset", "draft"], ):
        proc = subprocess.run(
            [sys.executable, str(root / "tools" / "q3map2.py"), *stage, str(map_path)],
            capture_output=True, text=True, check=False, env=env,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"draft compile failed: {proc.stderr.strip()[:400]}")

    bsp = workdir / "out" / map_path.stem / f"{map_path.stem}.bsp"
    if not bsp.is_file():
        # A non-staged (native) compile leaves the bsp beside the map.
        bsp = map_path.with_suffix(".bsp")
    if not bsp.is_file():
        raise RuntimeError("compile produced no .bsp")

    proc = subprocess.run(
        [sys.executable, str(root / "tools" / "q3map2.py"), "--json-unpack", str(bsp)],
        capture_output=True, text=True, check=False, env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"json unpack failed: {proc.stderr.strip()[:400]}")

    lumps = workdir / "out" / bsp.stem / bsp.stem
    if not lumps.is_dir():
        lumps = bsp.with_suffix("")
    if not lumps.is_dir():
        raise RuntimeError("json unpack produced no lump directory")
    return lumps


def compare_lumps(a: Path, b: Path) -> list[str]:
    diffs: list[str] = []
    for name in COMPARED_LUMPS:
        fa, fb = a / name, b / name
        if not fa.exists() and not fb.exists():
            continue
        if fa.exists() != fb.exists():
            diffs.append(f"{name}: present in only one compile")
            continue
        try:
            ja = json.loads(fa.read_text())
            jb = json.loads(fb.read_text())
        except (OSError, json.JSONDecodeError) as e:
            diffs.append(f"{name}: unreadable ({e})")
            continue
        drop = LUMP_FIELD_EXCLUSIONS.get(name)
        if drop:
            ja = strip_fields(ja, drop)
            jb = strip_fields(jb, drop)
        if ja != jb:
            extra = ""
            if len(ja) != len(jb):
                extra = f" ({len(ja)} vs {len(jb)} entries)"
            diffs.append(f"{name}: differs{extra}")
    return diffs


def run_semantic(root: Path, nrc: Path, results: dict[str, Result], maps: list[Path],
                 limit: int) -> None:
    ok, why = q3map2_available()
    if not ok:
        for r in results.values():
            r.semantic = "skipped"
            r.semantic_detail = why
        print(f"  semantic: skipped — {why}")
        return

    # `corpus/synthetic/degenerate/` holds maps that are broken on purpose. They must still
    # round-trip — losing data is never acceptable — but q3map2 is entitled to reject them,
    # so compiling them would report a failure that is actually the expected outcome.
    def compilable(m: Path) -> bool:
        return "degenerate" not in m.parts and results[m.name].syntactic_ok

    for m in maps:
        if "degenerate" in m.parts:
            r = results[m.name]
            r.semantic = "skipped"
            r.semantic_detail = "degenerate by design; not compiled"

    chosen = [m for m in maps if compilable(m)][:limit]
    for m in chosen:
        r = results[m.name]
        with tempfile.TemporaryDirectory(prefix="nrc-difftest-") as td:
            work = Path(td)
            try:
                # Re-serialize through the kernel into a second file, then compile both.
                resaved = work / m.name
                shutil.copyfile(m, resaved)
                proc = subprocess.run(
                    [str(nrc), "normalize", "--write", str(resaved)],
                    capture_output=True, text=True, check=False,
                )
                if proc.returncode != 0:
                    raise RuntimeError(f"normalize failed: {proc.stderr.strip()[:200]}")

                a = compile_and_unpack(root, m, work / "orig")
                b = compile_and_unpack(root, resaved, work / "resaved")
                diffs = compare_lumps(a, b)
                if diffs:
                    r.semantic = "differs"
                    r.lump_diffs = diffs
                    r.semantic_detail = "; ".join(diffs[:4])
                else:
                    r.semantic = "ok"
            except (RuntimeError, OSError) as e:
                r.semantic = "error"
                r.semantic_detail = str(e)[:300]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", type=Path, default=None, help="corpus directory")
    ap.add_argument("--limit", type=int, default=None, help="only the first N maps")
    ap.add_argument("--semantic-limit", type=int, default=6,
                    help="how many maps to compile for the semantic check (0 disables)")
    ap.add_argument("--no-semantic", action="store_true",
                    help="syntactic check only")
    ap.add_argument("--json", type=Path, default=None, help="also write a JSON report here")
    args = ap.parse_args()

    root = repo_root()
    corpus = args.corpus or Path(os.environ.get("MAP_CORPUS") or root / "corpus")
    if not corpus.is_dir():
        raise SystemExit(f"corpus directory {corpus} does not exist")

    nrc = nrc_binary(root)
    maps = collect_maps(corpus, args.limit)
    if not maps:
        raise SystemExit(
            f"no .map files under {corpus} — run `mise run corpus:import` and "
            "`mise run corpus:gen`"
        )

    print(f"differential harness: {len(maps)} map(s) under {corpus}")
    results = run_syntactic(nrc, maps)

    syn_fail = [r for r in results.values() if not r.syntactic_ok]
    total_brushes = sum(r.brushes for r in results.values())
    total_patches = sum(r.patches for r in results.values())
    print(
        f"  syntactic: {len(results) - len(syn_fail)}/{len(results)} byte-identical "
        f"({total_brushes} brushes, {total_patches} patches)"
    )
    for r in syn_fail:
        print(f"    FAIL {r.name}: {r.detail}")

    if not args.no_semantic and args.semantic_limit > 0:
        run_semantic(root, nrc, results, maps, args.semantic_limit)
        checked = [r for r in results.values() if r.semantic in ("ok", "differs", "error")]
        good = [r for r in checked if r.semantic == "ok"]
        if checked:
            print(f"  semantic: {len(good)}/{len(checked)} compiled BSPs identical")
            for r in checked:
                if r.semantic != "ok":
                    print(f"    {r.semantic.upper()} {r.name}: {r.semantic_detail}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(
            {
                "corpus": str(corpus),
                "excluded_lumps": EXCLUDED_LUMPS,
                "results": [vars(r) for r in results.values()],
            }, indent=2) + "\n")
        print(f"  report: {args.json}")

    sem_bad = [r for r in results.values() if r.semantic in ("differs", "error")]
    if syn_fail or sem_bad:
        print("\nGATE FAILED — nothing downstream of the kernel should be trusted until "
              "this is green (§3.3).")
        return 1
    print("\nGATE GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
