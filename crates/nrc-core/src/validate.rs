//! Geometry and format validators.
//!
//! **Everything here is game-agnostic on purpose.** Not one validator in this file knows
//! which game it is serving, and the §7.4 seam lint fails the build if a game-specific
//! string appears outside `profiles/` and `corpus/`. Entity ontology, gametypes, spawn
//! rules and movement constants are *data*, checked by the Python layer against a profile
//! YAML. What lives here is true of any brush-based idTech map: a brush that encloses no
//! volume is broken in Quake 3 and in Urban Terror alike.
//!
//! Every finding carries a [`Finding::rule_source`] and a [`Confidence`], as §8.2 requires,
//! and none of them mutates anything. A fix is a *suggestion*, never applied.

use crate::exact::IVec3;
use crate::math::MAX_WORLD_COORD;
use crate::model::{Entity, Map, Primitive};
use crate::winding::{brush_geometry, duplicate_plane_pairs, face_area, Degeneracy};

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Severity {
    Info,
    Warning,
    Error,
}

impl Severity {
    pub fn as_str(self) -> &'static str {
        match self {
            Severity::Info => "info",
            Severity::Warning => "warning",
            Severity::Error => "error",
        }
    }
}

/// How much we trust a rule.
///
/// `Verified` means confirmed against upstream source or observed in real maps.
/// `Unverified` means it came from documentation alone — and per §7, unverified rules must
/// never produce a hard failure. This project was bitten by exactly that: three of the
/// "verified" Urban Terror spawn rules in the original design turned out to be wrong when
/// checked against the shipped gamepack (see `docs/spec-corrections.md`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Confidence {
    Verified,
    Unverified,
}

impl Confidence {
    pub fn as_str(self) -> &'static str {
        match self {
            Confidence::Verified => "verified",
            Confidence::Unverified => "unverified",
        }
    }
}

/// Where in the map a finding applies.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Location {
    pub entity: Option<usize>,
    pub primitive: Option<usize>,
    pub face: Option<usize>,
    /// `classname` of the containing entity, for a message a human can act on without
    /// counting braces in a text editor.
    pub classname: Option<String>,
}

impl std::fmt::Display for Location {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let mut parts = Vec::new();
        if let Some(e) = self.entity {
            parts.push(match &self.classname {
                Some(c) => format!("entity {e} ({c})"),
                None => format!("entity {e}"),
            });
        }
        if let Some(p) = self.primitive {
            parts.push(format!("brush {p}"));
        }
        if let Some(x) = self.face {
            parts.push(format!("face {x}"));
        }
        if parts.is_empty() {
            f.write_str("map")
        } else {
            f.write_str(&parts.join(", "))
        }
    }
}

#[derive(Clone, Debug)]
pub struct Finding {
    pub severity: Severity,
    /// Stable machine-readable identifier. Renaming one is a breaking change for anything
    /// that filters findings, so treat these like an API.
    pub code: &'static str,
    pub message: String,
    pub location: Location,
    pub rule_source: &'static str,
    pub confidence: Confidence,
}

#[derive(Clone, Debug, Default)]
pub struct Report {
    pub findings: Vec<Finding>,
}

impl Report {
    pub fn count(&self, s: Severity) -> usize {
        self.findings.iter().filter(|f| f.severity == s).count()
    }

    pub fn has_errors(&self) -> bool {
        self.count(Severity::Error) > 0
    }

    /// Findings, worst first, then grouped by code so a report reads coherently.
    pub fn sorted(&self) -> Vec<&Finding> {
        let mut v: Vec<&Finding> = self.findings.iter().collect();
        v.sort_by(|a, b| b.severity.cmp(&a.severity).then(a.code.cmp(b.code)));
        v
    }
}

/// Tunable thresholds. Defaults follow §4.1's post-conditions.
#[derive(Clone, Copy, Debug)]
pub struct Thresholds {
    /// Authoring grid that vertices are expected to land on.
    pub grid: i64,
    /// Below this a brush may be collapsed by the compiler.
    pub min_thickness_error: f64,
    /// Below this a brush is legal but fragile.
    pub min_thickness_warning: f64,
    /// Faces smaller than this contribute nothing but a potential sliver.
    pub min_face_area: f64,
}

impl Default for Thresholds {
    fn default() -> Self {
        Self {
            grid: 1,
            min_thickness_error: 1.0,
            min_thickness_warning: 2.0,
            min_face_area: 1.0,
        }
    }
}

const SRC_SPEC: &str = "nrc-mcp spec §4.1 post-conditions";
const SRC_Q3MAP2: &str = "q3map2 map.cpp RemoveDuplicateBrushPlanes";
const SRC_UPSTREAM_PATCH: &str = "netradiant-custom plugins/mapq3/plugin.cpp MapQ3API::parsePrimitive";
const SRC_WORLD: &str = "q3map2 MAX_WORLD_COORD";

/// Validate a whole map.
pub fn validate_map(map: &Map, t: &Thresholds) -> Report {
    let mut r = Report::default();

    if map.worldspawn().is_none() {
        r.findings.push(Finding {
            severity: Severity::Error,
            code: "MAP_NO_WORLDSPAWN",
            message: "the map has no worldspawn entity, so it cannot be compiled".into(),
            location: Location::default(),
            rule_source: SRC_SPEC,
            confidence: Confidence::Verified,
        });
    }

    for (ei, e) in map.entities.iter().enumerate() {
        validate_entity(&mut r, ei, e, t);
    }
    r
}

fn validate_entity(r: &mut Report, ei: usize, e: &Entity, t: &Thresholds) {
    let classname = Some(e.classname().to_string()).filter(|c| !c.is_empty());
    let at = |pi: Option<usize>, fi: Option<usize>| Location {
        entity: Some(ei),
        primitive: pi,
        face: fi,
        classname: classname.clone(),
    };

    for (pi, p) in e.prims.iter().enumerate() {
        match p {
            Primitive::Brush(b) => {
                // Out-of-bounds coordinates make every later check meaningless, so report
                // them first and plainly.
                for (fi, f) in b.faces.iter().enumerate() {
                    for pt in f.point_vecs() {
                        if !pt.is_finite()
                            || pt.x.abs() > MAX_WORLD_COORD
                            || pt.y.abs() > MAX_WORLD_COORD
                            || pt.z.abs() > MAX_WORLD_COORD
                        {
                            r.findings.push(Finding {
                                severity: Severity::Error,
                                code: "COORD_OUT_OF_BOUNDS",
                                message: format!(
                                    "plane point {pt} lies outside the +/-{MAX_WORLD_COORD:.0} \
                                     world limit"
                                ),
                                location: at(Some(pi), Some(fi)),
                                rule_source: SRC_WORLD,
                                confidence: Confidence::Verified,
                            });
                        }
                    }
                    if !f.extra.is_empty() {
                        r.findings.push(Finding {
                            severity: Severity::Info,
                            code: "FACE_UNMODELED_TOKENS",
                            message: format!(
                                "face carries {} token(s) this kernel does not model \
                                 ({}); they are preserved verbatim but not understood",
                                f.extra.len(),
                                f.extra.join(" ")
                            ),
                            location: at(Some(pi), Some(fi)),
                            rule_source: SRC_SPEC,
                            confidence: Confidence::Verified,
                        });
                    }
                }

                let (same, mirrored) = duplicate_plane_pairs(b);
                for (i, j) in same {
                    r.findings.push(Finding {
                        severity: Severity::Warning,
                        code: "BRUSH_DUPLICATE_PLANE",
                        message: format!(
                            "faces {i} and {j} lie on the same plane facing the same way; \
                             the compiler discards one"
                        ),
                        location: at(Some(pi), None),
                        rule_source: SRC_Q3MAP2,
                        confidence: Confidence::Verified,
                    });
                }
                for (i, j) in mirrored {
                    r.findings.push(Finding {
                        severity: Severity::Error,
                        code: "BRUSH_MIRRORED_PLANE",
                        message: format!(
                            "faces {i} and {j} are the same plane facing opposite ways; \
                             the compiler rejects the whole brush"
                        ),
                        location: at(Some(pi), None),
                        rule_source: SRC_Q3MAP2,
                        confidence: Confidence::Verified,
                    });
                }

                match brush_geometry(&b.faces) {
                    Err(d) => {
                        // An unrepresentable brush is a different class of problem from a
                        // broken one: we are declining to judge, not condemning.
                        let (sev, code) = match d {
                            Degeneracy::NotExactlyRepresentable(_) => {
                                (Severity::Warning, "BRUSH_NOT_EXACT")
                            }
                            Degeneracy::TooComplex(_) => (Severity::Warning, "BRUSH_TOO_COMPLEX"),
                            _ => (Severity::Error, "BRUSH_DEGENERATE"),
                        };
                        r.findings.push(Finding {
                            severity: sev,
                            code,
                            message: d.to_string(),
                            location: at(Some(pi), None),
                            rule_source: SRC_SPEC,
                            confidence: Confidence::Verified,
                        });
                    }
                    Ok(g) => {
                        for fi in g.redundant_faces() {
                            r.findings.push(Finding {
                                severity: Severity::Warning,
                                code: "BRUSH_REDUNDANT_PLANE",
                                message: format!(
                                    "face {fi} bounds no area; the brush would be identical \
                                     without it"
                                ),
                                location: at(Some(pi), Some(fi)),
                                rule_source: SRC_Q3MAP2,
                                confidence: Confidence::Verified,
                            });
                        }

                        let off = g.off_grid_vertices(t.grid);
                        if !off.is_empty() {
                            // One finding per brush, not per vertex: a brush knocked off
                            // the grid usually knocks several vertices off at once, and
                            // twelve identical findings tell a mapper nothing extra.
                            let sample = g.vertices[off[0]].to_vec3();
                            r.findings.push(Finding {
                                severity: Severity::Error,
                                code: "BRUSH_OFF_GRID",
                                message: format!(
                                    "{} of {} vertices are off the grid of {} (first: {})",
                                    off.len(),
                                    g.vertices.len(),
                                    t.grid,
                                    sample
                                ),
                                location: at(Some(pi), None),
                                rule_source: SRC_SPEC,
                                confidence: Confidence::Verified,
                            });
                        }

                        if let Some(th) = g.min_thickness() {
                            if th < t.min_thickness_error {
                                r.findings.push(Finding {
                                    severity: Severity::Error,
                                    code: "BRUSH_TOO_THIN",
                                    message: format!(
                                        "brush is {th} units thick; below {} the compiler \
                                         may collapse it",
                                        t.min_thickness_error
                                    ),
                                    location: at(Some(pi), None),
                                    rule_source: SRC_SPEC,
                                    confidence: Confidence::Verified,
                                });
                            } else if th < t.min_thickness_warning {
                                r.findings.push(Finding {
                                    severity: Severity::Warning,
                                    code: "BRUSH_THIN",
                                    message: format!("brush is only {th} units thick"),
                                    location: at(Some(pi), None),
                                    rule_source: SRC_SPEC,
                                    confidence: Confidence::Verified,
                                });
                            }
                        }

                        for (fi, fg) in g.faces.iter().enumerate() {
                            if let Some(fg) = fg {
                                if fg.contributes() {
                                    let a = face_area(&g, fg);
                                    if a < t.min_face_area {
                                        r.findings.push(Finding {
                                            severity: Severity::Warning,
                                            code: "FACE_TINY_AREA",
                                            message: format!(
                                                "face area is {a:.4} square units; slivers this \
                                                 small are a common source of sparkles and leaks"
                                            ),
                                            location: at(Some(pi), Some(fi)),
                                            rule_source: SRC_SPEC,
                                            confidence: Confidence::Verified,
                                        });
                                    }
                                }
                            }
                        }
                    }
                }
            }

            Primitive::Patch(p) => {
                if !p.dimensions_consistent() {
                    r.findings.push(Finding {
                        severity: Severity::Error,
                        code: "PATCH_DIMENSIONS_INCONSISTENT",
                        message: format!(
                            "{} declares {}x{} control points but contains {} row(s)",
                            p.kind,
                            p.width(),
                            p.height(),
                            p.rows.len()
                        ),
                        location: at(Some(pi), None),
                        rule_source: SRC_SPEC,
                        confidence: Confidence::Verified,
                    });
                }
                // Upstream will write a patchDef3 into a Quake 3 map when fixed
                // subdivisions are enabled, but MapQ3API::parsePrimitive accepts only
                // patchDef2 — so the map cannot be reopened, and q3map2 cannot read it
                // either. Worth catching before someone loses an evening's work.
                if p.kind == "patchDef3" {
                    r.findings.push(Finding {
                        severity: Severity::Error,
                        code: "PATCH_DEF3_UNREADABLE",
                        message: "patchDef3 in a Quake 3 map cannot be reopened by the editor \
                                  or read by q3map2; disable fixed subdivisions and re-save"
                            .into(),
                        location: at(Some(pi), None),
                        rule_source: SRC_UPSTREAM_PATCH,
                        confidence: Confidence::Verified,
                    });
                }
                for pt in p.control_points() {
                    if !pt.is_finite()
                        || pt.x.abs() > MAX_WORLD_COORD
                        || pt.y.abs() > MAX_WORLD_COORD
                        || pt.z.abs() > MAX_WORLD_COORD
                    {
                        r.findings.push(Finding {
                            severity: Severity::Error,
                            code: "COORD_OUT_OF_BOUNDS",
                            message: format!(
                                "patch control point {pt} lies outside the \
                                 +/-{MAX_WORLD_COORD:.0} world limit"
                            ),
                            location: at(Some(pi), None),
                            rule_source: SRC_WORLD,
                            confidence: Confidence::Verified,
                        });
                        break;
                    }
                }
            }

            Primitive::Raw(raw) => {
                r.findings.push(Finding {
                    severity: Severity::Info,
                    code: "PRIMITIVE_NOT_UNDERSTOOD",
                    message: format!(
                        "primitive block `{}` is preserved verbatim but cannot be analysed",
                        raw.keyword
                    ),
                    location: at(Some(pi), None),
                    rule_source: SRC_SPEC,
                    confidence: Confidence::Verified,
                });
            }
        }
    }
}

/// Count how many of a map's brush vertices land on a given grid.
///
/// Used by reporting rather than validation: "97% on a grid of 8" is a more useful thing
/// to tell a mapper about an inherited map than four thousand individual findings.
pub fn grid_alignment(map: &Map, grid: i64) -> (usize, usize) {
    let mut on = 0;
    let mut total = 0;
    for b in map.all_brushes() {
        if let Ok(g) = brush_geometry(&b.faces) {
            total += g.vertices.len();
            on += g.vertices.len() - g.off_grid_vertices(grid).len();
        }
    }
    (on, total)
}

/// Whether every plane point in the map is an exact integer within world bounds.
pub fn all_points_exact(map: &Map) -> bool {
    map.all_brushes().all(|b| {
        b.faces
            .iter()
            .all(|f| f.point_vecs().iter().all(|p| IVec3::try_from_vec3(*p).is_some()))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::parse::parse_map;

    /// Six face lines of an axis-aligned box from (0,0,0) to (s,s,h).
    ///
    /// Point order matters and is easy to get backwards: with q3's convention
    /// (`n = cross(c - a, b - a)`, solid half-space `n · p <= d`) the triples below give
    /// outward normals. Reversing any of them turns that face inside out, and the brush
    /// then encloses nothing — which is what a first attempt at this helper did.
    fn box_faces(s: i64, h: i64) -> String {
        let t = "a/b 0 0 0 0.5 0.5 0 0 0";
        format!(
            "( 0 0 {h} ) ( 0 1 {h} ) ( 1 0 {h} ) {t}\n\
             ( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) {t}\n\
             ( 0 0 0 ) ( 0 0 1 ) ( 1 0 0 ) {t}\n\
             ( 0 {s} 0 ) ( 1 {s} 0 ) ( 0 {s} 1 ) {t}\n\
             ( 0 0 0 ) ( 0 1 0 ) ( 0 0 1 ) {t}\n\
             ( {s} 0 0 ) ( {s} 0 1 ) ( {s} 1 0 ) {t}\n"
        )
    }

    /// An axis-aligned box brush from (0,0,0) to (s,s,h), in axial format.
    fn box_brush(s: i64, h: i64) -> String {
        format!("{{\n{}}}\n", box_faces(s, h))
    }

    fn world(prims: &str) -> Map {
        parse_map(&format!("{{\n\"classname\" \"worldspawn\"\n{prims}}}\n")).unwrap()
    }

    fn codes(r: &Report) -> Vec<&str> {
        let mut c: Vec<&str> = r.findings.iter().map(|f| f.code).collect();
        c.sort_unstable();
        c.dedup();
        c
    }

    #[test]
    fn a_clean_box_produces_no_findings() {
        let m = world(&box_brush(64, 64));
        let r = validate_map(&m, &Thresholds::default());
        assert!(r.findings.is_empty(), "unexpected findings: {:#?}", r.findings);
        assert!(!r.has_errors());
    }

    #[test]
    fn a_missing_worldspawn_is_an_error() {
        let m = parse_map("{\n\"classname\" \"point_entity_a\"\n}\n").unwrap();
        let r = validate_map(&m, &Thresholds::default());
        assert!(codes(&r).contains(&"MAP_NO_WORLDSPAWN"));
        assert!(r.has_errors());
    }

    #[test]
    fn a_thin_brush_warns_then_errors_as_it_gets_thinner() {
        let t = Thresholds::default();
        let warn = validate_map(&world(&box_brush(64, 1)), &t);
        assert!(codes(&warn).contains(&"BRUSH_THIN"), "{:?}", codes(&warn));
        assert!(!warn.has_errors());

        // A sub-unit brush cannot be expressed on the integer grid at all, so the
        // stronger finding is that it is off-grid — which is the honest report.
        let mut t2 = t;
        t2.min_thickness_error = 8.0;
        t2.min_thickness_warning = 16.0;
        let err = validate_map(&world(&box_brush(64, 4)), &t2);
        assert!(codes(&err).contains(&"BRUSH_TOO_THIN"), "{:?}", codes(&err));
        assert!(err.has_errors());
    }

    #[test]
    fn off_grid_geometry_is_an_error_not_a_warning() {
        // §3.2: "Off-grid vertices are a validation error, not a warning."
        let mut t = Thresholds::default();
        t.grid = 16;
        let r = validate_map(&world(&box_brush(8, 8)), &t);
        let f = r
            .findings
            .iter()
            .find(|f| f.code == "BRUSH_OFF_GRID")
            .expect("should flag off-grid");
        assert_eq!(f.severity, Severity::Error);
        assert!(f.message.contains("grid of 16"), "{}", f.message);
    }

    #[test]
    fn a_mirrored_plane_pair_is_an_error() {
        let brush = "{\n\
            ( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 1 0 ) ( 1 0 0 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 1 0 0 ) ( 0 0 1 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 8 0 ) ( 0 8 1 ) ( 1 8 0 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            }\n";
        let r = validate_map(&world(brush), &Thresholds::default());
        let f = r
            .findings
            .iter()
            .find(|f| f.code == "BRUSH_MIRRORED_PLANE")
            .expect("should flag the mirrored pair");
        assert_eq!(f.severity, Severity::Error);
    }

    #[test]
    fn a_redundant_plane_warns_and_names_the_face() {
        // A seventh plane, x + y + z = 192, touches the 64-cube only at the corner
        // (64,64,64). It bounds no area, so the brush is identical without it.
        let brush = format!(
            "{{\n{}( 64 64 64 ) ( 64 128 0 ) ( 128 64 0 ) a/b 0 0 0 0.5 0.5 0 0 0\n}}\n",
            box_faces(64, 64)
        );
        let r = validate_map(&world(&brush), &Thresholds::default());
        let f = r
            .findings
            .iter()
            .find(|f| f.code == "BRUSH_REDUNDANT_PLANE")
            .unwrap_or_else(|| panic!("codes were {:?}", codes(&r)));
        assert_eq!(f.severity, Severity::Warning);
        assert_eq!(f.location.face, Some(6), "should name the seventh face");
    }

    #[test]
    fn a_degenerate_brush_is_an_error_with_a_reason() {
        // Three faces cannot bound a volume.
        let brush = "{\n\
            ( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 1 0 ) ( 0 0 1 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 0 1 ) ( 1 0 0 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            }\n";
        let r = validate_map(&world(brush), &Thresholds::default());
        let f = r
            .findings
            .iter()
            .find(|f| f.code == "BRUSH_DEGENERATE")
            .expect("should flag it");
        assert!(f.message.contains("at least 4"), "{}", f.message);
    }

    #[test]
    fn off_grid_input_declines_to_judge_rather_than_condemning() {
        // A brush with fractional plane points cannot be evaluated exactly. That is a
        // warning about our own reach, not an accusation that the brush is broken.
        let brush = "{\n\
            ( 0 0 0.5 ) ( 1 0 0.5 ) ( 0 1 0.5 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 1 0 ) ( 1 0 0 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 1 0 0 ) ( 0 0 1 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 8 0 ) ( 0 8 1 ) ( 1 8 0 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            }\n";
        let r = validate_map(&world(brush), &Thresholds::default());
        let f = r
            .findings
            .iter()
            .find(|f| f.code == "BRUSH_NOT_EXACT")
            .expect("should decline to judge");
        assert_eq!(f.severity, Severity::Warning);
        assert!(!r.has_errors(), "must not claim the brush is broken");
    }

    #[test]
    fn out_of_bounds_coordinates_are_errors() {
        let brush = "{\n\
            ( 0 0 999999 ) ( 1 0 999999 ) ( 0 1 999999 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 1 0 ) ( 1 0 0 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 1 0 0 ) ( 0 0 1 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 8 0 ) ( 0 8 1 ) ( 1 8 0 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            }\n";
        let r = validate_map(&world(brush), &Thresholds::default());
        assert!(codes(&r).contains(&"COORD_OUT_OF_BOUNDS"));
    }

    #[test]
    fn patch_dimension_mismatch_is_an_error() {
        let patch = "{\npatchDef2\n{\nx/y\n( 3 2 0 0 0 )\n(\n\
                     ( ( 0 0 0 0 0 ) ( 0 0 0 0 0 ) )\n)\n}\n}\n";
        let r = validate_map(&world(patch), &Thresholds::default());
        let f = r
            .findings
            .iter()
            .find(|f| f.code == "PATCH_DIMENSIONS_INCONSISTENT")
            .expect("should flag it");
        assert!(f.message.contains("3x2"), "{}", f.message);
    }

    #[test]
    fn patchdef3_in_a_quake3_map_is_flagged_as_unreadable() {
        let patch = "{\npatchDef3\n{\nx/y\n( 2 2 4 4 0 0 0 )\n(\n\
                     ( ( 0 0 0 0 0 ) ( 0 0 0 0 0 ) )\n( ( 0 0 0 0 0 ) ( 0 0 0 0 0 ) )\n)\n}\n}\n";
        let r = validate_map(&world(patch), &Thresholds::default());
        let f = r
            .findings
            .iter()
            .find(|f| f.code == "PATCH_DEF3_UNREADABLE")
            .expect("should flag it");
        assert_eq!(f.severity, Severity::Error);
        assert_eq!(f.confidence, Confidence::Verified);
    }

    #[test]
    fn an_unrecognized_primitive_is_reported_as_unanalysable_not_broken() {
        let r = validate_map(&world("{\nfutureDef\n{\n( 0 0 0 )\n}\n}\n"), &Thresholds::default());
        let f = r
            .findings
            .iter()
            .find(|f| f.code == "PRIMITIVE_NOT_UNDERSTOOD")
            .expect("should mention it");
        assert_eq!(f.severity, Severity::Info);
        assert!(!r.has_errors());
    }

    #[test]
    fn findings_report_a_location_a_human_can_act_on() {
        let mut t = Thresholds::default();
        t.grid = 16;
        let r = validate_map(&world(&box_brush(8, 8)), &t);
        let f = &r.findings[0];
        assert_eq!(f.location.entity, Some(0));
        assert_eq!(f.location.classname.as_deref(), Some("worldspawn"));
        assert_eq!(f.location.to_string(), "entity 0 (worldspawn), brush 0");
    }

    #[test]
    fn sorting_puts_errors_first() {
        let mut t = Thresholds::default();
        t.grid = 16;
        t.min_thickness_warning = 16.0;
        let r = validate_map(&world(&box_brush(8, 8)), &t);
        let s = r.sorted();
        assert!(s.len() >= 2, "expected several findings, got {:?}", codes(&r));
        assert_eq!(s[0].severity, Severity::Error);
    }

    #[test]
    fn grid_alignment_reports_a_ratio() {
        let m = world(&box_brush(64, 64));
        assert_eq!(grid_alignment(&m, 64), (8, 8));
        // On a 128 grid only the corner at the origin still lands on it.
        assert_eq!(grid_alignment(&m, 128), (1, 8));
        assert!(all_points_exact(&m));
    }
}
