"""Game profiles — the one layer that knows which game we are serving (§7.4).

Everything game-specific lives in `profiles/*.yaml` as **data**: entity ontology, gametype
ids, spawn rules, movement constants, packaging conventions. No module outside this one
loads them, no module anywhere hardcodes their contents, and `tools/seam_lint.py` fails the
build if a game-specific string appears in code.

Every rule carries a `confidence`. Only `verified` rules — confirmed against a shipped
gamepack, upstream source, or real map files — may produce a hard failure. That distinction
is not bureaucracy: three of the spec's supposedly-verified spawn rules turned out to be
wrong when checked against the actual gamepack, and one of them would have failed correct
maps. See `docs/spec-corrections.md`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from .kernel import repo_root


class ProfileError(RuntimeError):
    pass


def profiles_dir() -> Path:
    return repo_root() / "profiles"


def available() -> list[str]:
    d = profiles_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.yaml"))


@lru_cache(maxsize=8)
def load(profile_id: str) -> dict[str, Any]:
    """Load a profile by id, e.g. the value of `NRC_PROFILE`."""
    try:
        import yaml
    except ImportError as e:  # pragma: no cover - environment problem, not logic
        raise ProfileError(
            "pyyaml is required to read profiles. Run: mise run py:sync"
        ) from e

    path = profiles_dir() / f"{profile_id}.yaml"
    if not path.is_file():
        raise ProfileError(
            f"no profile {profile_id!r} in {profiles_dir()}; available: {available() or 'none'}"
        )
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ProfileError(f"{path} is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ProfileError(f"{path} should contain a mapping at the top level")
    return data


def entities(profile_id: str) -> list[dict[str, Any]]:
    data = load(profile_id)
    ents = data.get("entities") or []
    return [e for e in ents if isinstance(e, dict)]


def verified_entities(profile_id: str) -> list[dict[str, Any]]:
    return [e for e in entities(profile_id) if e.get("confidence") == "verified"]


def gametypes(profile_id: str) -> list[dict[str, Any]]:
    data = load(profile_id)
    return [g for g in (data.get("gametypes") or []) if isinstance(g, dict)]


def movement(profile_id: str) -> dict[str, Any]:
    """Player dimensions and movement constants, if the profile states them.

    Read from data, never hardcoded — §7.4 names "physics constants hardcoded into
    `movement_check`" as the most likely way game specifics leak into code.
    """
    data = load(profile_id)
    out = {}
    for key in ("movement", "units", "measurements"):
        section = data.get(key)
        if isinstance(section, dict):
            out[key] = section
    return out


def summary(profile_id: str) -> dict[str, Any]:
    """A compact overview, for the profile MCP resource."""
    data = load(profile_id)
    ents = entities(profile_id)
    verified = verified_entities(profile_id)
    unverified_section = data.get("unverified") or []

    by_kind: dict[str, int] = {}
    for e in ents:
        by_kind[str(e.get("kind", "unknown"))] = by_kind.get(str(e.get("kind", "unknown")), 0) + 1

    return {
        "profile": data.get("profile", {}),
        "entity_count": len(ents),
        "verified_entity_count": len(verified),
        "entities_by_kind": by_kind,
        "gametypes": gametypes(profile_id),
        "movement": movement(profile_id),
        "unverified_count": len(unverified_section) if isinstance(unverified_section, list) else 0,
        "sections": sorted(data.keys()),
        "note": (
            "Only `confidence: verified` entries may produce hard validation failures. "
            "See docs/spec-corrections.md for rules that documentation got wrong."
        ),
    }
