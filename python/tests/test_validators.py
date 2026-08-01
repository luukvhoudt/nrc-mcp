"""Validator behaviour, including the guarantees the design leans on.

Two of these are about *discipline* rather than geometry: that unverified rules cannot
produce hard failures, and that every finding is traceable to a source. Both exist because
the design document's "verified" Urban Terror spawn rules included one that was wrong in a
way that would have failed correct maps (`docs/spec-corrections.md`).
"""

from __future__ import annotations

import pytest

from nrc_mcp import kernel

FACES_BOX = [
    "( 0 0 64 ) ( 0 1 64 ) ( 1 0 64 ) a/b 0 0 0 0.5 0.5 0 0 0",
    "( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) a/b 0 0 0 0.5 0.5 0 0 0",
    "( 0 0 0 ) ( 0 0 1 ) ( 1 0 0 ) a/b 0 0 0 0.5 0.5 0 0 0",
    "( 0 64 0 ) ( 1 64 0 ) ( 0 64 1 ) a/b 0 0 0 0.5 0.5 0 0 0",
    "( 0 0 0 ) ( 0 1 0 ) ( 0 0 1 ) a/b 0 0 0 0.5 0.5 0 0 0",
    "( 64 0 0 ) ( 64 0 1 ) ( 64 1 0 ) a/b 0 0 0 0.5 0.5 0 0 0",
]


def world(*prims: str) -> str:
    return '{\n"classname" "worldspawn"\n' + "".join(prims) + "}\n"


def brush(faces: list[str]) -> str:
    return "{\n" + "\n".join(faces) + "\n}\n"


@pytest.fixture(scope="module")
def k():
    try:
        return kernel.kernel()
    except kernel.KernelUnavailable as e:
        pytest.skip(str(e))


def codes(k, src: str, **kw) -> set[str]:
    return {f["code"] for f in k.Map.parse(src).validate(severity_min="info", **kw)["findings"]}


def test_missing_worldspawn(k):
    assert "MAP_NO_WORLDSPAWN" in codes(k, '{\n"classname" "point_entity_a"\n}\n')


def test_mirrored_plane_is_an_error(k):
    src = world(brush([
        "( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) a/b 0 0 0 0.5 0.5 0 0 0",
        "( 0 0 0 ) ( 0 1 0 ) ( 1 0 0 ) a/b 0 0 0 0.5 0.5 0 0 0",
        "( 0 0 0 ) ( 0 0 1 ) ( 1 0 0 ) a/b 0 0 0 0.5 0.5 0 0 0",
        "( 0 8 0 ) ( 1 8 0 ) ( 0 8 1 ) a/b 0 0 0 0.5 0.5 0 0 0",
    ]))
    v = k.Map.parse(src).validate(severity_min="info")
    f = next(x for x in v["findings"] if x["code"] == "BRUSH_MIRRORED_PLANE")
    assert f["severity"] == "error"


def test_duplicate_plane_written_from_different_points(k):
    extra = "( 512 -256 64 ) ( 512 -255 64 ) ( 513 -256 64 ) a/b 0 0 0 0.5 0.5 0 0 0"
    assert "BRUSH_DUPLICATE_PLANE" in codes(k, world(brush([*FACES_BOX, extra])))


def test_redundant_plane_touching_one_corner(k):
    extra = "( 64 64 64 ) ( 64 128 0 ) ( 128 64 0 ) a/b 0 0 0 0.5 0.5 0 0 0"
    assert "BRUSH_REDUNDANT_PLANE" in codes(k, world(brush([*FACES_BOX, extra])))


def test_out_of_bounds_coordinate(k):
    bad = ["( 0 0 999999 ) ( 0 1 999999 ) ( 1 0 999999 ) a/b 0 0 0 0.5 0.5 0 0 0", *FACES_BOX[1:]]
    assert "COORD_OUT_OF_BOUNDS" in codes(k, world(brush(bad)))


def test_collinear_face_is_distinguished_from_off_grid(k):
    collinear = ["( 0 0 0 ) ( 8 0 0 ) ( 16 0 0 ) a/b 0 0 0 0.5 0.5 0 0 0", *FACES_BOX[1:]]
    f = next(
        x for x in k.Map.parse(world(brush(collinear))).validate(severity_min="info")["findings"]
        if x["code"] == "BRUSH_DEGENERATE"
    )
    assert "collinear" in f["message"]

    off = ["( 0 0 0.5 ) ( 0 1 0.5 ) ( 1 0 0.5 ) a/b 0 0 0 0.5 0.5 0 0 0", *FACES_BOX[1:]]
    v = k.Map.parse(world(brush(off))).validate(severity_min="info")
    assert "BRUSH_NOT_EXACT" in {x["code"] for x in v["findings"]}
    # Declining to judge must not be reported as the brush being broken.
    assert v["summary"]["error"] == 0


def test_patchdef3_in_a_quake3_map_is_flagged(k):
    patch = (
        "{\npatchDef3\n{\nx/y\n( 2 2 4 4 0 0 0 )\n(\n"
        "( ( 0 0 0 0 0 ) ( 0 0 0 0 0 ) )\n( ( 0 0 0 0 0 ) ( 0 0 0 0 0 ) )\n)\n}\n}\n"
    )
    v = k.Map.parse(world(patch)).validate(severity_min="info")
    f = next(x for x in v["findings"] if x["code"] == "PATCH_DEF3_UNREADABLE")
    assert f["severity"] == "error"
    assert f["confidence"] == "verified"


def test_unknown_primitive_is_informational_not_an_error(k):
    v = k.Map.parse(world("{\nfutureDef\n{\n( 0 0 0 )\n}\n}\n")).validate(severity_min="info")
    f = next(x for x in v["findings"] if x["code"] == "PRIMITIVE_NOT_UNDERSTOOD")
    assert f["severity"] == "info"
    assert v["summary"]["error"] == 0


def test_every_finding_is_traceable(k):
    """§8.2: every finding carries a rule_source and a confidence. No exceptions."""
    src = world(brush(FACES_BOX))
    v = k.Map.parse(src).validate(grid=128, severity_min="info")
    assert v["findings"], "expected at least one finding to inspect"
    for f in v["findings"]:
        assert f["rule_source"], f
        assert f["confidence"] in {"verified", "unverified"}, f
        assert f["message"], f
        assert f["location"], f


def test_unverified_rules_never_produce_errors(k):
    """A hard failure must always be backed by something we actually checked.

    Enforced across the whole corpus rather than a sample, because the moment an unverified
    rule can fail a build, the confidence field has stopped meaning anything.
    """
    corpus = kernel.repo_root() / "corpus"
    maps = sorted(corpus.rglob("*.map"))
    if not maps:
        pytest.skip("no corpus; run `mise run corpus:gen`")
    for path in maps:
        v = k.Map.load(str(path)).validate(severity_min="error")
        for f in v["findings"]:
            assert f["confidence"] == "verified", (
                f"{path.name}: {f['code']} is an error but only {f['confidence']}"
            )


def test_degenerate_corpus_is_actually_caught(k):
    """The synthetic degenerate maps exist to be caught; check that they are.

    A validator suite that passes everything is indistinguishable from no validator suite,
    so this asserts each deliberately-broken map produces the finding it was built for.
    """
    d = kernel.repo_root() / "corpus" / "synthetic" / "degenerate"
    if not d.is_dir():
        pytest.skip("no synthetic corpus; run `mise run corpus:gen`")

    expected = {
        "too_few_faces.map": "BRUSH_DEGENERATE",
        "mirrored_plane.map": "BRUSH_MIRRORED_PLANE",
        "duplicate_plane.map": "BRUSH_DUPLICATE_PLANE",
        "redundant_plane.map": "BRUSH_REDUNDANT_PLANE",
        "off_grid.map": "BRUSH_NOT_EXACT",
        "out_of_bounds.map": "COORD_OUT_OF_BOUNDS",
        "collinear_face.map": "BRUSH_DEGENERATE",
        "no_worldspawn.map": "MAP_NO_WORLDSPAWN",
        "patch_dims_mismatch.map": "PATCH_DIMENSIONS_INCONSISTENT",
        "patch_def3_unreadable.map": "PATCH_DEF3_UNREADABLE",
        "unknown_primitive.map": "PRIMITIVE_NOT_UNDERSTOOD",
    }
    for name, want in expected.items():
        p = d / name
        if not p.is_file():
            pytest.fail(f"{name} is missing from the generated degenerate corpus")
        found = {f["code"] for f in k.Map.load(str(p)).validate(severity_min="info")["findings"]}
        assert want in found, f"{name}: expected {want}, got {sorted(found)}"
