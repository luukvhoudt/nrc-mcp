"""Sidecar persistence for Solid IR trees (§4.4).

> Keep the IR tree persisted alongside the `.map` in a sidecar (`mapname.solids.json`) so
> parametric intent survives across sessions. The `.map` stays canonical and hand-editable;
> the sidecar is advisory and re-derivable.

That ordering is the whole design. The `.map` is the truth: if the sidecar disappears, nothing
is lost but the ability to re-parameterize, and if the two disagree the `.map` wins. So this
module never writes a `.map`, never refuses to work without a sidecar, and stores enough
provenance to tell when a record has gone stale.

The parametric edit `solid_edit_param` enables is the real payoff — "make that corridor 32
units wider" becomes one field change and a recompile, instead of moving brush faces by hand.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

SIDECAR_VERSION = 1


class SolidStoreError(RuntimeError):
    pass


def sidecar_path(map_path: str | Path) -> Path:
    """`foo.map` -> `foo.solids.json`."""
    p = Path(map_path)
    return p.with_suffix(".solids.json")


def load(map_path: str | Path) -> dict[str, Any]:
    """Read the sidecar, or an empty store if there is none."""
    p = sidecar_path(map_path)
    if not p.is_file():
        return {"version": SIDECAR_VERSION, "solids": {}}
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        # Never fatal: the map is still usable, only the parametric history is lost. Saying so
        # beats refusing to open a map because an advisory file got corrupted.
        raise SolidStoreError(
            f"{p} is unreadable ({e}). The .map itself is unaffected — delete or fix the "
            f"sidecar to carry on; only the ability to re-edit parameters is lost."
        ) from e
    if not isinstance(data, dict) or not isinstance(data.get("solids"), dict):
        raise SolidStoreError(f"{p} is not a solids sidecar")
    return data


def save(map_path: str | Path, store: dict[str, Any]) -> Path:
    p = sidecar_path(map_path)
    store["version"] = SIDECAR_VERSION
    p.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n")
    return p


def put(
    map_path: str | Path,
    name: str,
    ir: dict,
    *,
    brushes: int = 0,
    notes: str = "",
) -> dict[str, Any]:
    """Record an IR tree under `name`, replacing any previous version.

    The previous version is kept as `superseded`, one deep. Enough to undo a bad parameter
    change, without turning an advisory file into a version-control system.
    """
    if not name or not name.replace("_", "").replace("-", "").isalnum():
        raise SolidStoreError(
            f"solid name {name!r} should be alphanumeric with dashes or underscores, so it can "
            f"be used in a filename and a brush comment"
        )
    store = load(map_path)
    previous = store["solids"].get(name)
    entry: dict[str, Any] = {
        "ir": ir,
        "brushes": brushes,
        "notes": notes,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if previous is not None:
        entry["superseded"] = {k: previous[k] for k in ("ir", "updated") if k in previous}
    store["solids"][name] = entry
    save(map_path, store)
    return entry


def get(map_path: str | Path, name: str) -> dict[str, Any]:
    store = load(map_path)
    entry = store["solids"].get(name)
    if entry is None:
        known = sorted(store["solids"])
        raise SolidStoreError(
            f"no solid named {name!r} recorded for this map; known: {known or 'none'}"
        )
    return entry


def names(map_path: str | Path) -> list[str]:
    try:
        return sorted(load(map_path)["solids"])
    except SolidStoreError:
        return []


def remove(map_path: str | Path, name: str) -> bool:
    store = load(map_path)
    if name not in store["solids"]:
        return False
    del store["solids"][name]
    save(map_path, store)
    return True


# ---------------------------------------------------------------------------
# Parametric editing
# ---------------------------------------------------------------------------


def _split(path: str) -> list[str | int]:
    """`parts[1].thickness` -> `['parts', 1, 'thickness']`."""
    out: list[str | int] = []
    for chunk in path.replace("]", "").split("."):
        for i, piece in enumerate(chunk.split("[")):
            if not piece:
                continue
            if i == 0:
                out.append(piece)
            else:
                try:
                    out.append(int(piece))
                except ValueError as e:
                    raise SolidStoreError(f"{piece!r} is not a list index in path {path!r}") from e
    return out


def edit_param(ir: dict, path: str, value: Any) -> dict:
    """Return a copy of `ir` with the field at `path` replaced.

    `path` is dotted with bracket indices, e.g. `from.solid.max[0]` or `cut[0].min`.

    Returns a *copy*: an in-place edit would leave a half-modified tree behind if the path
    turned out to be wrong halfway down, and the caller would have no way to recover the
    original.
    """
    steps = _split(path)
    if not steps:
        raise SolidStoreError("an empty path cannot be edited")

    new = json.loads(json.dumps(ir))  # deep copy through a format the IR already lives in
    node: Any = new
    for i, step in enumerate(steps[:-1]):
        walked = ".".join(str(s) for s in steps[: i + 1])
        try:
            node = node[step]
        except (KeyError, IndexError, TypeError) as e:
            raise SolidStoreError(
                f"path {path!r} does not exist: failed at {walked!r} "
                f"({type(e).__name__}). Available here: {_available(node)}"
            ) from e

    last = steps[-1]
    try:
        if isinstance(last, int):
            if not isinstance(node, list):
                raise SolidStoreError(f"{path!r} indexes a {type(node).__name__}, not a list")
            node[last] = value
        else:
            if not isinstance(node, dict):
                raise SolidStoreError(f"{path!r} names a field on a {type(node).__name__}")
            if last not in node:
                raise SolidStoreError(f"{path!r} does not exist; available: {_available(node)}")
            node[last] = value
    except IndexError as e:
        raise SolidStoreError(f"{path!r} is out of range") from e
    return new


def _available(node: Any) -> str:
    if isinstance(node, dict):
        return ", ".join(sorted(node))
    if isinstance(node, list):
        return f"indices 0..{len(node) - 1}"
    return type(node).__name__


def describe(ir: dict, depth: int = 0) -> list[str]:
    """A readable outline of an IR tree, for `solid_inspect`.

    Lines rather than a nested structure: an agent reading its own tree wants to see the shape
    and the parameter names at a glance, and indentation carries that better than JSON does.
    """
    if not isinstance(ir, dict):
        return [f"{'  ' * depth}<not a node: {type(ir).__name__}>"]
    op = ir.get("op", "?")
    scalars = {
        k: v
        for k, v in ir.items()
        if k != "op"
        and not isinstance(v, (dict, list))
        or (isinstance(v, list) and all(not isinstance(x, dict) for x in v))
    }
    summary = " ".join(f"{k}={json.dumps(v)}" for k, v in sorted(scalars.items()))
    lines = [f"{'  ' * depth}{op}" + (f"  {summary}" if summary else "")]
    for key, value in ir.items():
        if isinstance(value, dict) and "op" in value:
            lines.append(f"{'  ' * (depth + 1)}{key}:")
            lines += describe(value, depth + 2)
        elif isinstance(value, list):
            kids = [v for v in value if isinstance(v, dict)]
            for i, v in enumerate(kids):
                lines.append(f"{'  ' * (depth + 1)}{key}[{i}]:")
                lines += describe(v, depth + 2)
    return lines
