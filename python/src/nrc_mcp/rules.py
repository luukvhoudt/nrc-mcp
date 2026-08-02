"""A declarative rule engine for game-specific validation.

Every rule this module can evaluate is **data in a profile**, never code here. That is not
tidiness: `tools/seam_lint.py` fails the build if a game-specific string appears in
`python/src`, so a validator that mentioned a classname would not compile the project. The
constraint forces the right design — adding a rule means editing YAML, and a second game
means a second YAML.

Each rule carries a `confidence`. Only `verified` rules may produce an `error`; anything
`unverified` is downgraded to `info` no matter what the profile asks for. That mechanism
exists because it was needed: three of the four Urban Terror spawn rules the design document
called verified turned out to be wrong when checked against the shipped gamepack, and one
would have failed correct maps (`docs/spec-corrections.md`).

# Check types

`require_any`
    At least one entity from `classnames` must exist.
`count_exact`
    `classname` must appear exactly `count` times.
`count_min` / `count_max`
    Bounds on the number of entities of `classname`.
`key_in_enum`
    Entities of `classname` must have `key` in `values`.
`key_required`
    Entities of `classname` must have `key` at all.
`distinct_group_per_team`
    For entities of `classname` selected by a gametype list, no `group_key` value may be
    shared between two different `team_key` values.
`group_size`
    Each `group_key` bucket should hold `count` entities.
`min_spacing`
    Entities sharing a `group_key` must be at least `distance` apart.
`forbid_gametype`
    Entities of `classname` must not list `gametype` in `gametype_key`.

Every check may carry `applies_when: {classname_present: X}` so a rule about one gametype
stays silent on maps that do not use it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from . import profiles

SEVERITIES = ("info", "warning", "error")


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    confidence: str
    rule_source: str
    entities: list[int] = field(default_factory=list)
    fix_hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "confidence": self.confidence,
            "rule_source": self.rule_source,
            "entities": self.entities,
            "fix_hint": self.fix_hint,
        }


# ---------------------------------------------------------------------------
# Entity helpers
# ---------------------------------------------------------------------------


def _keys(entity: dict) -> dict[str, str]:
    """Entity keys as a mapping, first occurrence winning.

    The kernel hands over ordered pairs because `.map` key order is meaningful and duplicates
    occur; rule evaluation does not care about order, and taking the first duplicate matches
    what the engine's own lookup does.
    """
    out: dict[str, str] = {}
    for k, v in entity.get("keys") or []:
        out.setdefault(k, v)
    return out


def _of_class(entities: list[dict], classname: str) -> list[dict]:
    return [e for e in entities if e.get("classname") == classname]


def _gametype_list(value: str) -> set[str]:
    """Parse a gametype key value.

    The convention is a comma-separated list, but real maps use spaces and semicolons too, so
    all three separate. Tokens are kept as strings: the profile states ids as integers and a
    map states them as text, and comparing normalized strings avoids inventing a numeric
    parse that could silently drop a malformed entry.
    """
    out: set[str] = set()
    for token in value.replace(";", ",").replace(" ", ",").split(","):
        token = token.strip()
        if token:
            out.add(token)
    return out


def _origin(entity: dict) -> tuple[float, float, float] | None:
    o = entity.get("origin")
    if isinstance(o, (list, tuple)) and len(o) == 3:
        try:
            return (float(o[0]), float(o[1]), float(o[2]))
        except (TypeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _applies(rule: dict, entities: list[dict]) -> bool:
    cond = rule.get("applies_when")
    if not isinstance(cond, dict):
        return True
    present = cond.get("classname_present")
    if present and not _of_class(entities, str(present)):
        return False
    absent = cond.get("classname_absent")
    return not (absent and _of_class(entities, str(absent)))


def _check_require_any(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    names = [str(n) for n in p.get("classnames") or []]
    found = {n: len(_of_class(entities, n)) for n in names}
    total = sum(found.values())
    return total > 0, {"found": total, "detail": found, "expected": " or ".join(names)}


def _check_count(p: dict, entities: list[dict], mode: str) -> tuple[bool, dict]:
    cls = str(p.get("classname", ""))
    matches = _of_class(entities, cls)
    n = len(matches)
    want = int(p.get("count", 0))
    ok = {"exact": n == want, "min": n >= want, "max": n <= want}[mode]
    return ok, {
        "found": n,
        "expected": want,
        "classname": cls,
        "entities": [e["index"] for e in matches],
    }


def _check_key_in_enum(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    cls = str(p.get("classname", ""))
    key = str(p.get("key", ""))
    allowed = {str(v) for v in p.get("values") or []}
    bad = []
    for e in _of_class(entities, cls):
        v = _keys(e).get(key)
        if v is not None and str(v) not in allowed:
            bad.append((e["index"], v))
    return not bad, {
        "found": len(bad),
        "expected": sorted(allowed),
        "classname": cls,
        "key": key,
        "entities": [i for i, _ in bad],
        "detail": dict(bad[:8]),
    }


def _check_key_required(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    cls = str(p.get("classname", ""))
    key = str(p.get("key", ""))
    missing = [e["index"] for e in _of_class(entities, cls) if key not in _keys(e)]
    return not missing, {
        "found": len(missing),
        "classname": cls,
        "key": key,
        "entities": missing,
    }


def _selected_by_gametype(p: dict, entities: list[dict]) -> list[dict]:
    cls = str(p.get("classname", ""))
    gt_key = str(p.get("gametype_key", ""))
    gt = str(p.get("gametype", ""))
    out = []
    for e in _of_class(entities, cls):
        v = _keys(e).get(gt_key)
        if v is not None and gt in _gametype_list(v):
            out.append(e)
    return out


def _check_distinct_group_per_team(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    """No group value may be shared by two different teams.

    This is the corrected Team Survivor rule. The design document said TS needs *dedicated
    spawn entities* and could not share a gametype key — which the gamepack contradicts. What
    it actually requires is that each team's spawns sit in *separate groups*.
    """
    group_key = str(p.get("group_key", ""))
    team_key = str(p.get("team_key", ""))
    by_group: dict[str, set[str]] = {}
    index_by_group: dict[str, list[int]] = {}
    for e in _selected_by_gametype(p, entities):
        k = _keys(e)
        g = k.get(group_key)
        t = k.get(team_key)
        if g is None or t is None:
            continue
        by_group.setdefault(g, set()).add(t)
        index_by_group.setdefault(g, []).append(e["index"])

    shared = {g: sorted(ts) for g, ts in by_group.items() if len(ts) > 1}
    bad_entities = [i for g in shared for i in index_by_group[g]]
    return not shared, {
        "found": len(shared),
        "detail": shared,
        "entities": bad_entities,
        "expected": "one team per group",
    }


def _check_forbid_gametype(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    matches = _selected_by_gametype(p, entities)
    return not matches, {
        "found": len(matches),
        "classname": str(p.get("classname", "")),
        "entities": [e["index"] for e in matches],
        "expected": f"not listed for gametype {p.get('gametype')}",
    }


def _groups(p: dict, entities: list[dict]) -> dict[str, list[dict]]:
    cls = str(p.get("classname", ""))
    group_key = str(p.get("group_key", ""))
    out: dict[str, list[dict]] = {}
    for e in _of_class(entities, cls):
        g = _keys(e).get(group_key)
        if g is not None:
            out.setdefault(g, []).append(e)
    return out


def _check_group_size(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    want = int(p.get("count", 0))
    sizes = {g: len(es) for g, es in _groups(p, entities).items()}
    wrong = {g: n for g, n in sizes.items() if n != want}
    return not wrong, {
        "found": len(wrong),
        "expected": want,
        "detail": wrong,
        "entities": [],
    }


def _check_min_spacing(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    want = float(p.get("distance", 0))
    close: list[tuple[int, int, float]] = []
    for _, members in _groups(p, entities).items():
        pts = [(e["index"], _origin(e)) for e in members]
        pts = [(i, o) for i, o in pts if o is not None]
        for a in range(len(pts)):
            for b in range(a + 1, len(pts)):
                (ia, oa), (ib, ob) = pts[a], pts[b]
                d = math.dist(oa, ob)
                if d < want:
                    close.append((ia, ib, round(d, 2)))
    return not close, {
        "found": len(close),
        "expected": want,
        "detail": [f"{a} and {b} are {d} apart" for a, b, d in close[:8]],
        "entities": sorted({i for a, b, _ in close for i in (a, b)}),
    }


CHECKS = {
    "require_any": lambda p, e: _check_require_any(p, e),
    "count_exact": lambda p, e: _check_count(p, e, "exact"),
    "count_min": lambda p, e: _check_count(p, e, "min"),
    "count_max": lambda p, e: _check_count(p, e, "max"),
    "key_in_enum": lambda p, e: _check_key_in_enum(p, e),
    "key_required": lambda p, e: _check_key_required(p, e),
    "distinct_group_per_team": lambda p, e: _check_distinct_group_per_team(p, e),
    "forbid_gametype": lambda p, e: _check_forbid_gametype(p, e),
    "group_size": lambda p, e: _check_group_size(p, e),
    "min_spacing": lambda p, e: _check_min_spacing(p, e),
}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def _effective_severity(rule: dict) -> str:
    """Severity after the confidence clamp.

    An unverified rule can never fail a build. §7 requires it, and the alternative has already
    cost this project once.
    """
    asked = str(rule.get("severity", "warning")).lower()
    if asked not in SEVERITIES:
        asked = "warning"
    if str(rule.get("confidence", "unverified")).lower() != "verified":
        return "info"
    return asked


def _format(template: str, facts: dict) -> str:
    try:
        return template.format(**facts)
    except (KeyError, IndexError, ValueError):
        # A profile typo in a message template must not break validation. Report the
        # template plus the facts so the mistake is obvious and fixable.
        return f"{template}  (facts: {facts})"


def evaluate(profile_id: str, entities: list[dict]) -> list[Finding]:
    """Run every rule in the profile against a map's entities."""
    data = profiles.load(profile_id)
    rules = data.get("rules")
    findings: list[Finding] = []
    if not isinstance(rules, list):
        return findings

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        kind = str(rule.get("check", ""))
        fn = CHECKS.get(kind)
        code = str(rule.get("id", f"RULE_{kind.upper()}"))
        if fn is None:
            findings.append(
                Finding(
                    code="RULE_UNKNOWN_CHECK",
                    severity="info",
                    message=(
                        f"rule {code} asks for check {kind!r}, which this engine does not "
                        f"implement; known checks: {', '.join(sorted(CHECKS))}"
                    ),
                    confidence="verified",
                    rule_source="nrc_mcp.rules",
                )
            )
            continue

        params = rule.get("params") if isinstance(rule.get("params"), dict) else {}
        if not _applies(rule, entities):
            continue
        try:
            ok, facts = fn(params, entities)
        except (TypeError, ValueError) as e:
            findings.append(
                Finding(
                    code="RULE_MALFORMED",
                    severity="info",
                    message=f"rule {code} could not be evaluated: {e}",
                    confidence="verified",
                    rule_source="nrc_mcp.rules",
                )
            )
            continue
        if ok:
            continue

        findings.append(
            Finding(
                code=code,
                severity=_effective_severity(rule),
                message=_format(str(rule.get("message", code)), facts),
                confidence=str(rule.get("confidence", "unverified")),
                rule_source=str(rule.get("rule_source", f"profile {profile_id}")),
                entities=[int(i) for i in facts.get("entities") or []][:64],
                fix_hint=str(rule.get("fix_hint", "")),
            )
        )
    return findings


def unknown_classnames(profile_id: str, entities: list[dict]) -> list[Finding]:
    """Entities whose classname the profile does not define.

    A typo in a classname is silent in game — the entity simply does nothing — so it is worth
    reporting. Never an error: a map may legitimately use an entity from a newer game build
    than the profile was extracted from, and the profile, not the map, would be at fault.
    """
    known = {str(e.get("classname")) for e in profiles.entities(profile_id)}
    measurement = profiles.load(profile_id).get("measurement_entities") or []
    if isinstance(measurement, list):
        known |= {str(m.get("classname")) for m in measurement if isinstance(m, dict)}

    unknown: dict[str, list[int]] = {}
    for e in entities:
        cn = e.get("classname") or ""
        if cn and cn not in known:
            unknown.setdefault(cn, []).append(e["index"])

    return [
        Finding(
            code="ENTITY_CLASSNAME_UNKNOWN",
            severity="warning",
            message=(
                f"{len(idx)} entity/entities use classname {cn!r}, which profile "
                f"{profile_id} does not define — a typo here is silent in game, but the "
                f"profile may simply be older than the map"
            ),
            confidence="verified",
            rule_source=f"profile {profile_id} entity list",
            entities=idx[:64],
        )
        for cn, idx in sorted(unknown.items())
    ]


def summarize(findings: list[Finding]) -> dict[str, int]:
    return {s: sum(1 for f in findings if f.severity == s) for s in SEVERITIES}
