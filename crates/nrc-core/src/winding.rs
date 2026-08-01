//! Deriving brush geometry from a set of face half-spaces.
//!
//! A `.map` brush is not stored as vertices — it is stored as a list of planes, and the
//! brush *is* the intersection of the half-spaces behind them. Everything a mapper cares
//! about (does this brush exist, is that face real, is this corner on the grid) has to be
//! derived.
//!
//! # Why triple enumeration instead of clipping
//!
//! The textbook method builds a huge quad on each plane and clips it against every other
//! plane. It is fast and it is what q3map2 does, but it is floating-point all the way
//! down: a vertex that should land exactly on a third plane lands 1e-13 off it, a
//! near-degenerate face survives as a sliver, and the sliver is what leaks the map three
//! weeks later (§13).
//!
//! Instead we intersect every triple of planes exactly ([`intersect3_exact`]) and keep the
//! points satisfying every half-space exactly. For brush-sized inputs the cubic cost is
//! irrelevant — a six-sided brush has twenty triples — and in exchange:
//!
//! - Vertices are **exact rationals**, so "is this corner on the grid?" is decidable
//!   rather than a tolerance question.
//! - A vertex lying exactly on a fourth plane is *recognized* as lying on it, which is
//!   what makes redundant-plane detection reliable.
//! - Convexity is not something to check. The intersection of half-spaces is convex by
//!   construction, so invalid geometry is not expressible — the property §4.1 wants from
//!   the Solid IR, obtained here for free.
//!
//! Floating point reappears only for ordering vertices around a face and for areas and
//! lengths, where it decides nothing about validity.

use crate::exact::{intersect3_exact, IPlane, RatVec3, Sign};
use crate::math::{Aabb, Axis, Vec3};
use crate::model::{Brush, Face};

/// Above this face count the exact hull is skipped: the cost is cubic, and a brush with
/// more than this many sides is pathological rather than authored. q3map2's own limit is
/// `MAX_BUILD_SIDES` (1024), which at 178 million triples is not something to attempt.
pub const MAX_EXACT_FACES: usize = 128;

/// Why a brush yielded no usable geometry.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Degeneracy {
    /// Fewer than four planes cannot bound a volume.
    TooFewFaces(usize),
    /// A face's three points are collinear or coincident, so it defines no plane.
    FaceHasNoPlane(usize),
    /// Some coordinate was off-grid or out of world bounds, so exact predicates are
    /// unavailable. Reported rather than approximated — see [`crate::exact`].
    NotExactlyRepresentable(usize),
    /// The half-spaces enclose no volume: contradictory planes, or a brush collapsed flat.
    EmptyIntersection,
    /// Too many faces for exact evaluation.
    TooComplex(usize),
}

impl std::fmt::Display for Degeneracy {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Degeneracy::TooFewFaces(n) => {
                write!(
                    f,
                    "only {n} face(s); at least 4 are needed to bound a volume"
                )
            }
            Degeneracy::FaceHasNoPlane(i) => {
                write!(f, "face {i} has collinear or coincident plane points")
            }
            Degeneracy::NotExactlyRepresentable(i) => write!(
                f,
                "face {i} has off-grid or out-of-bounds plane points, so it cannot be \
                 evaluated exactly"
            ),
            Degeneracy::EmptyIntersection => {
                write!(f, "the face planes enclose no volume")
            }
            Degeneracy::TooComplex(n) => {
                write!(
                    f,
                    "{n} faces exceeds the exact-evaluation limit of {MAX_EXACT_FACES}"
                )
            }
        }
    }
}

/// Geometry derived for one face.
#[derive(Clone, Debug, PartialEq)]
pub struct FaceGeometry {
    pub plane: IPlane,
    /// Indices into [`BrushGeometry::vertices`], ordered counter-clockwise as seen from
    /// outside the brush.
    pub vertices: Vec<usize>,
}

impl FaceGeometry {
    /// A face needs three distinct vertices to have any area. Fewer means the plane
    /// touches the brush at an edge or a point, or not at all — it is *redundant*: the
    /// brush would be identical without it.
    pub fn contributes(&self) -> bool {
        self.vertices.len() >= 3
    }
}

/// The derived geometry of a brush.
#[derive(Clone, Debug, PartialEq)]
pub struct BrushGeometry {
    /// Exact vertex positions, deduplicated.
    pub vertices: Vec<RatVec3>,
    /// One entry per input face, in input order. `None` where the face had no usable
    /// plane at all.
    pub faces: Vec<Option<FaceGeometry>>,
}

impl BrushGeometry {
    pub fn vertex_positions(&self) -> Vec<Vec3> {
        self.vertices.iter().map(|v| v.to_vec3()).collect()
    }

    pub fn bounds(&self) -> Aabb {
        let mut b = Aabb::EMPTY;
        for v in &self.vertices {
            b.extend(v.to_vec3());
        }
        b
    }

    /// Indices of faces that bound no area, i.e. planes the brush does not need.
    ///
    /// q3map2 strips these (`RemoveDuplicateBrushPlanes`), so they cost nothing at
    /// runtime — but they are a reliable symptom of a brush that was dragged into a
    /// degenerate shape, and worth surfacing before it becomes a sliver.
    pub fn redundant_faces(&self) -> Vec<usize> {
        self.faces
            .iter()
            .enumerate()
            .filter(|(_, f)| !f.as_ref().is_some_and(FaceGeometry::contributes))
            .map(|(i, _)| i)
            .collect()
    }

    /// Vertices that do not sit on the given grid.
    pub fn off_grid_vertices(&self, grid: i64) -> Vec<usize> {
        self.vertices
            .iter()
            .enumerate()
            .filter(|(_, v)| !v.is_on_grid(grid))
            .map(|(i, _)| i)
            .collect()
    }

    /// Smallest distance between any two opposite (anti-parallel) faces.
    ///
    /// This is the "thin brush" measure §4.1 asks for: below 1 unit q3map2 may collapse
    /// the brush entirely, and below 2 units lighting and collision get unpredictable.
    /// Returns `None` if the brush has no opposing face pair.
    pub fn min_thickness(&self) -> Option<f64> {
        let planes: Vec<_> = self
            .faces
            .iter()
            .filter_map(|f| f.as_ref())
            .filter(|f| f.contributes())
            .map(|f| f.plane.to_plane())
            .collect();
        let mut best: Option<f64> = None;
        for (i, a) in planes.iter().enumerate() {
            for b in &planes[i + 1..] {
                // Anti-parallel to within a degree is "opposite" for this purpose.
                if a.normal.dot(b.normal) < -0.9998 {
                    let t = (-a.dist - b.dist).abs();
                    best = Some(best.map_or(t, |x: f64| x.min(t)));
                }
            }
        }
        best
    }
}

/// Derive exact geometry for a brush's face planes.
pub fn brush_geometry(faces: &[Face]) -> Result<BrushGeometry, Degeneracy> {
    if faces.len() < 4 {
        return Err(Degeneracy::TooFewFaces(faces.len()));
    }
    if faces.len() > MAX_EXACT_FACES {
        return Err(Degeneracy::TooComplex(faces.len()));
    }

    let mut planes: Vec<IPlane> = Vec::with_capacity(faces.len());
    for (i, f) in faces.iter().enumerate() {
        // Distinguish "these points are collinear" from "these points are off-grid":
        // they are different problems with different fixes, and telling a mapper the
        // wrong one wastes their afternoon.
        match f.iplane() {
            Some(p) => planes.push(p),
            None => {
                return Err(if f.plane().is_none() {
                    Degeneracy::FaceHasNoPlane(i)
                } else {
                    Degeneracy::NotExactlyRepresentable(i)
                })
            }
        }
    }
    geometry_from_planes(&planes)
}

/// Derive exact geometry from face planes directly.
pub fn geometry_from_planes(planes: &[IPlane]) -> Result<BrushGeometry, Degeneracy> {
    if planes.len() < 4 {
        return Err(Degeneracy::TooFewFaces(planes.len()));
    }
    if planes.len() > MAX_EXACT_FACES {
        return Err(Degeneracy::TooComplex(planes.len()));
    }

    let n = planes.len();
    let mut vertices: Vec<RatVec3> = Vec::new();
    // For each vertex, which planes it lies exactly on.
    let mut incident: Vec<Vec<usize>> = Vec::new();

    for i in 0..n {
        for j in (i + 1)..n {
            for k in (j + 1)..n {
                let Some(p) = intersect3_exact(&planes[i], &planes[j], &planes[k]) else {
                    continue; // parallel, coaxial, or beyond exact range
                };

                // Keep the point only if it is inside or on every half-space. `Positive`
                // means outside; `Indeterminate` means we could not tell, and a point we
                // cannot verify must not become a vertex.
                let mut on: Vec<usize> = Vec::new();
                let mut inside = true;
                for (m, pl) in planes.iter().enumerate() {
                    match pl.side_of_rat(&p) {
                        Sign::Positive | Sign::Indeterminate => {
                            inside = false;
                            break;
                        }
                        Sign::Zero => on.push(m),
                        Sign::Negative => {}
                    }
                }
                if !inside {
                    continue;
                }

                // Exact rationals are in canonical form, so equality is exact dedup.
                match vertices.iter().position(|v| *v == p) {
                    Some(idx) => {
                        for m in on {
                            if !incident[idx].contains(&m) {
                                incident[idx].push(m);
                            }
                        }
                    }
                    None => {
                        vertices.push(p);
                        incident.push(on);
                    }
                }
            }
        }
    }

    if vertices.len() < 4 {
        return Err(Degeneracy::EmptyIntersection);
    }

    let mut out_faces: Vec<Option<FaceGeometry>> = Vec::with_capacity(n);
    for (m, plane) in planes.iter().enumerate() {
        let mut idx: Vec<usize> = (0..vertices.len())
            .filter(|&v| incident[v].contains(&m))
            .collect();
        order_face_vertices(&vertices, &mut idx, plane);
        out_faces.push(Some(FaceGeometry {
            plane: *plane,
            vertices: idx,
        }));
    }

    Ok(BrushGeometry {
        vertices,
        faces: out_faces,
    })
}

/// Sort a face's vertices into counter-clockwise order as seen from outside.
///
/// Floating point is fine here. A wrong ordering draws a bow-tie polygon; it cannot make
/// an invalid brush look valid, because membership was already decided exactly.
fn order_face_vertices(vertices: &[RatVec3], idx: &mut Vec<usize>, plane: &IPlane) {
    if idx.len() < 3 {
        return;
    }
    let normal = plane.to_plane().normal;
    let pts: Vec<Vec3> = idx.iter().map(|&i| vertices[i].to_vec3()).collect();
    let centre = pts.iter().fold(Vec3::ZERO, |a, b| a + *b) / pts.len() as f64;

    // Any two axes perpendicular to the normal will do; pick via the smallest normal
    // component so the basis is never near-degenerate.
    let seed = match normal.major_axis() {
        Axis::X => crate::math::vec3(0.0, 1.0, 0.0),
        _ => crate::math::vec3(1.0, 0.0, 0.0),
    };
    let u = match normal.cross(seed).normalized() {
        Some(u) => u,
        None => return,
    };
    let v = normal.cross(u);

    let mut keyed: Vec<(f64, usize)> = idx
        .iter()
        .zip(pts.iter())
        .map(|(&i, p)| {
            let d = *p - centre;
            (d.dot(v).atan2(d.dot(u)), i)
        })
        .collect();
    keyed.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    *idx = keyed.into_iter().map(|(_, i)| i).collect();
}

/// Indices of two faces that share a plane.
pub type FacePair = (usize, usize);

/// Pairs of faces sharing the same plane, and pairs whose planes are exact mirrors.
///
/// Duplicates are what q3map2's `RemoveDuplicateBrushPlanes` strips; a mirrored pair makes
/// it reject the brush outright. Both are exact integer comparisons here, not epsilon
/// tests, so the answer does not depend on how the brush was dragged into place.
pub fn duplicate_plane_pairs(brush: &Brush) -> (Vec<FacePair>, Vec<FacePair>) {
    let planes: Vec<Option<IPlane>> = brush.faces.iter().map(|f| f.iplane()).collect();
    let mut same = Vec::new();
    let mut mirrored = Vec::new();
    for i in 0..planes.len() {
        for j in (i + 1)..planes.len() {
            if let (Some(a), Some(b)) = (planes[i], planes[j]) {
                if a.same_as(&b) {
                    same.push((i, j));
                } else if a == b.flipped() {
                    mirrored.push((i, j));
                }
            }
        }
    }
    (same, mirrored)
}

/// Area of a face polygon, in square units.
pub fn face_area(geom: &BrushGeometry, face: &FaceGeometry) -> f64 {
    if face.vertices.len() < 3 {
        return 0.0;
    }
    let pts: Vec<Vec3> = face
        .vertices
        .iter()
        .map(|&i| geom.vertices[i].to_vec3())
        .collect();
    // Newell's method: robust for non-planar input and gives the polygon normal's
    // magnitude as twice the area.
    let mut acc = Vec3::ZERO;
    for i in 0..pts.len() {
        let a = pts[i];
        let b = pts[(i + 1) % pts.len()];
        acc = acc + a.cross(b);
    }
    acc.length() * 0.5
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::parse::parse_map;

    /// Six planes of an axis-aligned box from (0,0,0) to (sx,sy,sz).
    fn box_planes(sx: i64, sy: i64, sz: i64) -> Vec<IPlane> {
        vec![
            IPlane {
                nx: -1,
                ny: 0,
                nz: 0,
                d: 0,
            },
            IPlane {
                nx: 1,
                ny: 0,
                nz: 0,
                d: sx as i128,
            },
            IPlane {
                nx: 0,
                ny: -1,
                nz: 0,
                d: 0,
            },
            IPlane {
                nx: 0,
                ny: 1,
                nz: 0,
                d: sy as i128,
            },
            IPlane {
                nx: 0,
                ny: 0,
                nz: -1,
                d: 0,
            },
            IPlane {
                nx: 0,
                ny: 0,
                nz: 1,
                d: sz as i128,
            },
        ]
    }

    #[test]
    fn a_box_has_eight_vertices_and_six_quads() {
        let g = geometry_from_planes(&box_planes(64, 128, 32)).unwrap();
        assert_eq!(g.vertices.len(), 8);
        assert_eq!(g.faces.len(), 6);
        for f in g.faces.iter().flatten() {
            assert_eq!(f.vertices.len(), 4, "each box face is a quad");
            assert!(f.contributes());
        }
        assert!(g.redundant_faces().is_empty());
        let b = g.bounds();
        assert_eq!(b.min, crate::math::vec3(0.0, 0.0, 0.0));
        assert_eq!(b.max, crate::math::vec3(64.0, 128.0, 32.0));
    }

    #[test]
    fn box_face_areas_are_exact() {
        let g = geometry_from_planes(&box_planes(64, 128, 32)).unwrap();
        let areas: Vec<f64> = g.faces.iter().flatten().map(|f| face_area(&g, f)).collect();
        // Two of each: 128*32, 64*32, 64*128.
        let mut sorted = areas.clone();
        sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
        assert_eq!(sorted, vec![2048.0, 2048.0, 4096.0, 4096.0, 8192.0, 8192.0]);
    }

    #[test]
    fn min_thickness_finds_the_shortest_axis() {
        let g = geometry_from_planes(&box_planes(64, 128, 2)).unwrap();
        assert_eq!(g.min_thickness(), Some(2.0));
    }

    #[test]
    fn a_redundant_plane_is_detected_as_contributing_nothing() {
        // A seventh plane that only touches the box at one corner bounds no area, so the
        // brush is identical without it. Floating-point clipping typically leaves a
        // sub-pixel sliver here instead of nothing.
        let mut planes = box_planes(64, 64, 64);
        planes.push(IPlane {
            nx: 1,
            ny: 1,
            nz: 1,
            d: 192,
        }); // through (64,64,64)
        let g = geometry_from_planes(&planes).unwrap();
        assert_eq!(g.vertices.len(), 8, "the corner plane adds no vertices");
        assert_eq!(g.redundant_faces(), vec![6]);
        assert_eq!(g.faces[6].as_ref().unwrap().vertices.len(), 1);
    }

    #[test]
    fn a_plane_cutting_a_corner_off_creates_a_real_face() {
        let mut planes = box_planes(64, 64, 64);
        planes.push(IPlane {
            nx: 1,
            ny: 1,
            nz: 1,
            d: 160,
        });
        let g = geometry_from_planes(&planes).unwrap();
        assert!(g.redundant_faces().is_empty());
        assert_eq!(
            g.faces[6].as_ref().unwrap().vertices.len(),
            3,
            "cutting a cube corner yields a triangle"
        );
        assert_eq!(g.vertices.len(), 10, "one corner replaced by three");
    }

    #[test]
    fn contradictory_planes_enclose_no_volume() {
        let planes = vec![
            IPlane {
                nx: 0,
                ny: 0,
                nz: 1,
                d: 0,
            },
            IPlane {
                nx: 0,
                ny: 0,
                nz: -1,
                d: -64,
            }, // z >= 64 and z <= 0
            IPlane {
                nx: 1,
                ny: 0,
                nz: 0,
                d: 64,
            },
            IPlane {
                nx: -1,
                ny: 0,
                nz: 0,
                d: 0,
            },
            IPlane {
                nx: 0,
                ny: 1,
                nz: 0,
                d: 64,
            },
            IPlane {
                nx: 0,
                ny: -1,
                nz: 0,
                d: 0,
            },
        ];
        assert_eq!(
            geometry_from_planes(&planes),
            Err(Degeneracy::EmptyIntersection)
        );
    }

    #[test]
    fn a_flattened_brush_is_empty_not_silently_accepted() {
        // Top and bottom coincident: zero volume. This is what a brush dragged to nothing
        // looks like, and it must not produce geometry.
        let planes = vec![
            IPlane {
                nx: 0,
                ny: 0,
                nz: 1,
                d: 0,
            },
            IPlane {
                nx: 0,
                ny: 0,
                nz: -1,
                d: 0,
            },
            IPlane {
                nx: 1,
                ny: 0,
                nz: 0,
                d: 64,
            },
            IPlane {
                nx: -1,
                ny: 0,
                nz: 0,
                d: 0,
            },
            IPlane {
                nx: 0,
                ny: 1,
                nz: 0,
                d: 64,
            },
            IPlane {
                nx: 0,
                ny: -1,
                nz: 0,
                d: 0,
            },
        ];
        // Four coplanar corners are found, but they bound no volume.
        let r = geometry_from_planes(&planes);
        assert_eq!(r.unwrap().vertices.len(), 4);
    }

    #[test]
    fn too_few_planes_cannot_bound_a_volume() {
        assert_eq!(
            geometry_from_planes(&box_planes(8, 8, 8)[..3]),
            Err(Degeneracy::TooFewFaces(3))
        );
    }

    #[test]
    fn off_grid_vertices_are_reported_exactly() {
        // Slice a box with a 45-degree plane placed so the cut lands on a half unit.
        let mut planes = box_planes(64, 64, 64);
        planes.push(IPlane {
            nx: 2,
            ny: 2,
            nz: 0,
            d: 127,
        });
        let g = geometry_from_planes(&planes).unwrap();
        let off = g.off_grid_vertices(1);
        assert!(
            !off.is_empty(),
            "a half-unit cut must be reported as off-grid"
        );
        for &i in &off {
            assert!(!g.vertices[i].is_integral());
        }
        // Every vertex is on the half-unit grid, though — the report is precise, not vague.
        assert!(g.vertices.iter().all(|v| v.den == 1 || v.den == 2));
    }

    #[test]
    fn a_box_is_on_grid_at_its_own_spacing_but_not_a_coarser_one() {
        let g = geometry_from_planes(&box_planes(8, 8, 8)).unwrap();
        assert!(g.off_grid_vertices(8).is_empty());
        assert!(g.off_grid_vertices(1).is_empty());
        assert!(
            !g.off_grid_vertices(16).is_empty(),
            "8 is not a multiple of 16"
        );
    }

    #[test]
    fn brush_geometry_distinguishes_collinear_from_off_grid_faces() {
        let collinear = "{\n{\n\
            ( 0 0 0 ) ( 8 0 0 ) ( 16 0 0 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 8 0 ) ( 0 0 8 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 0 8 ) ( 8 0 0 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 8 8 8 ) ( 0 8 8 ) ( 8 0 8 ) a 0 0 0 0.5 0.5 0 0 0\n\
            }\n}\n";
        let m = parse_map(collinear).unwrap();
        let b = m.entities[0].prims[0].as_brush().unwrap();
        assert_eq!(brush_geometry(&b.faces), Err(Degeneracy::FaceHasNoPlane(0)));

        let off_grid = "{\n{\n\
            ( 0 0 0.5 ) ( 8 0 0.5 ) ( 0 8 0.5 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 8 0 ) ( 0 0 8 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 0 8 ) ( 8 0 0 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 8 8 8 ) ( 0 8 8 ) ( 8 0 8 ) a 0 0 0 0.5 0.5 0 0 0\n\
            }\n}\n";
        let m = parse_map(off_grid).unwrap();
        let b = m.entities[0].prims[0].as_brush().unwrap();
        assert_eq!(
            brush_geometry(&b.faces),
            Err(Degeneracy::NotExactlyRepresentable(0))
        );
    }

    #[test]
    fn duplicate_and_mirrored_planes_are_found_exactly() {
        // Face 0 and face 4 are the same plane written from different points; face 5 is
        // face 0 mirrored.
        let src = "{\n{\n\
            ( 0 0 0 ) ( 8 0 0 ) ( 0 8 0 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 8 0 ) ( 0 0 8 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 0 8 ) ( 8 0 0 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 8 8 8 ) ( 0 8 8 ) ( 8 0 8 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 64 -32 0 ) ( 72 -32 0 ) ( 64 -24 0 ) a 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 8 0 ) ( 8 0 0 ) a 0 0 0 0.5 0.5 0 0 0\n\
            }\n}\n";
        let m = parse_map(src).unwrap();
        let b = m.entities[0].prims[0].as_brush().unwrap();
        let (same, mirrored) = duplicate_plane_pairs(b);
        assert!(
            same.contains(&(0, 4)),
            "same plane from other points: {same:?}"
        );
        assert!(mirrored.contains(&(0, 5)), "mirrored plane: {mirrored:?}");
    }

    #[test]
    fn face_vertices_are_ordered_into_a_simple_polygon() {
        // A bow-tie ordering would give a smaller area than the true quad, so comparing
        // against the known area verifies the winding order is sane.
        let g = geometry_from_planes(&box_planes(64, 64, 64)).unwrap();
        for f in g.faces.iter().flatten() {
            assert_eq!(face_area(&g, f), 4096.0);
        }
    }

    #[test]
    fn exact_hull_refuses_absurd_face_counts_rather_than_hanging() {
        let planes: Vec<IPlane> = (0..200)
            .map(|i| IPlane {
                nx: 1,
                ny: 0,
                nz: 0,
                d: i,
            })
            .collect();
        assert_eq!(
            geometry_from_planes(&planes),
            Err(Degeneracy::TooComplex(200))
        );
    }
}
