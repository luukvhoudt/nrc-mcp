//! Convex polytopes as exact integer half-space intersections.
//!
//! This is the one representation the Solid IR reduces everything to, and the reason §4.1's
//! central promise holds: a polytope *is* an intersection of half-spaces, so it cannot be
//! non-convex. Invalid geometry is not expressible, rather than checked for and rejected.
//!
//! Planes are [`IPlane`], so plane identity, emptiness and side tests are exact integer
//! arithmetic with no epsilon anywhere.

use nrc_core::exact::{ivec3, IPlane, IVec3, RatVec3, Sign};
use nrc_core::math::{Aabb, Vec3};
use nrc_core::winding::{geometry_from_planes, BrushGeometry, Degeneracy};

/// A convex polytope: the intersection of the half-spaces `n · p <= d` of its planes.
///
/// Plane order is preserved because it is meaningful to callers — primitive builders emit
/// faces in a documented order, and `hollow`'s `open_faces` indexes into it — but it is *not*
/// part of the polytope's identity. Intersection is commutative, so two polytopes with the
/// same plane set are the same shape however they were built. `PartialEq` reflects that;
/// deriving it would have made a merged box unequal to the box it reassembles.
#[derive(Clone, Debug)]
pub struct Polytope {
    planes: Vec<IPlane>,
}

fn plane_key(p: &IPlane) -> (i128, i128, i128, i128) {
    (p.nx, p.ny, p.nz, p.d)
}

impl PartialEq for Polytope {
    fn eq(&self, other: &Self) -> bool {
        if self.planes.len() != other.planes.len() {
            return false;
        }
        let mut a: Vec<_> = self.planes.iter().map(plane_key).collect();
        let mut b: Vec<_> = other.planes.iter().map(plane_key).collect();
        a.sort_unstable();
        b.sort_unstable();
        a == b
    }
}

impl Eq for Polytope {}

impl Polytope {
    /// Build from planes, dropping exact duplicates.
    ///
    /// Duplicates are dropped rather than kept because they are the commonest by-product of
    /// CSG — a subtraction re-adds planes the input already had — and because q3map2 would
    /// discard them anyway. Exact plane identity makes this reliable rather than approximate.
    pub fn from_planes(planes: impl IntoIterator<Item = IPlane>) -> Polytope {
        let mut out: Vec<IPlane> = Vec::new();
        for p in planes {
            if !out.contains(&p) {
                out.push(p);
            }
        }
        Polytope { planes: out }
    }

    pub fn planes(&self) -> &[IPlane] {
        &self.planes
    }

    pub fn len(&self) -> usize {
        self.planes.len()
    }

    pub fn is_empty(&self) -> bool {
        self.planes.is_empty()
    }

    /// Add a half-space, keeping the result convex by construction.
    pub fn clipped_by(&self, plane: IPlane) -> Polytope {
        let mut planes = self.planes.clone();
        if !planes.contains(&plane) {
            planes.push(plane);
        }
        Polytope { planes }
    }

    /// True if any two planes are exact mirrors of each other, which encloses no volume.
    ///
    /// Worth a cheap dedicated check: q3map2 rejects a brush outright for this, and it is the
    /// commonest way a CSG result degenerates.
    pub fn has_mirrored_pair(&self) -> bool {
        for (i, a) in self.planes.iter().enumerate() {
            for b in &self.planes[i + 1..] {
                if *a == b.flipped() {
                    return true;
                }
            }
        }
        false
    }

    /// Derive exact geometry, or say why it has none.
    pub fn geometry(&self) -> Result<BrushGeometry, Degeneracy> {
        geometry_from_planes(&self.planes)
    }

    /// True if this polytope encloses a volume.
    ///
    /// The exact hull decides it: four or more vertices that satisfy every half-space. A
    /// polytope that only touches at a point, an edge or a plane is *not* solid, and this is
    /// what stops a CSG decomposition emitting slivers.
    pub fn is_solid(&self) -> bool {
        match self.geometry() {
            Ok(g) => {
                // Four vertices can still be coplanar (a flattened box), which bounds no
                // volume. A real solid needs one vertex off the plane of three others.
                g.vertices.len() >= 4 && !all_coplanar(&g.vertices)
            }
            Err(_) => false,
        }
    }

    pub fn vertices(&self) -> Vec<RatVec3> {
        self.geometry().map(|g| g.vertices).unwrap_or_default()
    }

    pub fn bounds(&self) -> Aabb {
        self.geometry().map(|g| g.bounds()).unwrap_or(Aabb::EMPTY)
    }

    /// Exact side test for a point.
    pub fn contains(&self, p: IVec3) -> bool {
        self.planes
            .iter()
            .all(|pl| matches!(pl.side_of(p), Sign::Negative | Sign::Zero))
    }

    /// Exact side test for a rational point, as produced by plane intersection.
    pub fn contains_rat(&self, p: &RatVec3) -> bool {
        self.planes
            .iter()
            .all(|pl| matches!(pl.side_of_rat(p), Sign::Negative | Sign::Zero))
    }

    /// Planes that bound no area, i.e. that could be removed without changing the shape.
    pub fn redundant_planes(&self) -> Vec<usize> {
        self.geometry()
            .map(|g| g.redundant_faces())
            .unwrap_or_default()
    }

    /// Drop planes that do not contribute a face.
    ///
    /// Emitting these as brush faces would produce sliver faces the compiler then has to
    /// strip, and they make a hand-inspected result look wrong even when it is not.
    pub fn simplified(&self) -> Polytope {
        match self.geometry() {
            Ok(g) => {
                let redundant = g.redundant_faces();
                Polytope {
                    planes: self
                        .planes
                        .iter()
                        .enumerate()
                        .filter(|(i, _)| !redundant.contains(i))
                        .map(|(_, p)| *p)
                        .collect(),
                }
            }
            Err(_) => self.clone(),
        }
    }

    /// Approximate volume, for ranking decomposition results.
    ///
    /// Floating point on purpose: this drives "is this piece worth keeping" and "did merging
    /// help", never a validity decision. An exact rational volume would cost far more than the
    /// question is worth.
    pub fn volume(&self) -> f64 {
        let Ok(g) = self.geometry() else { return 0.0 };
        if g.vertices.len() < 4 {
            return 0.0;
        }
        let pts: Vec<Vec3> = g.vertices.iter().map(|v| v.to_vec3()).collect();
        let origin = pts[0];
        let mut total = 0.0;
        for face in g.faces.iter().flatten() {
            if face.vertices.len() < 3 {
                continue;
            }
            // Fan the face into triangles and sum signed tetrahedron volumes from `origin`.
            let a = g.vertices[face.vertices[0]].to_vec3() - origin;
            for w in face.vertices.windows(2).skip(1) {
                let b = g.vertices[w[0]].to_vec3() - origin;
                let c = g.vertices[w[1]].to_vec3() - origin;
                total += a.dot(b.cross(c)) / 6.0;
            }
        }
        total.abs()
    }

    /// Smallest distance between opposing faces, or `None` if there is no opposing pair.
    pub fn min_thickness(&self) -> Option<f64> {
        self.geometry().ok().and_then(|g| g.min_thickness())
    }

    /// Translate by an integer offset.
    ///
    /// Exact: a half-space `n · p <= d` moved by `t` becomes `n · p <= d + n · t`, and every
    /// term stays integral. No vertex is recomputed, so nothing drifts.
    pub fn translated(&self, by: IVec3) -> Polytope {
        Polytope {
            planes: self
                .planes
                .iter()
                .map(|p| IPlane {
                    nx: p.nx,
                    ny: p.ny,
                    nz: p.nz,
                    d: p.d + p.nx * by.x as i128 + p.ny * by.y as i128 + p.nz * by.z as i128,
                })
                .collect(),
        }
    }

    /// Mirror across an axis-aligned plane.
    ///
    /// Substituting `x -> 2k - x` into `n · p <= d` gives a negated component and
    /// `d - 2k·nₐ`, so this is exact too. Handedness flips, which does not matter because a
    /// polytope is stored as half-spaces rather than as wound faces.
    pub fn mirrored(&self, axis: crate::prim::Axis, at: i64) -> Polytope {
        let k = at as i128;
        Polytope {
            planes: self
                .planes
                .iter()
                .map(|p| match axis {
                    crate::prim::Axis::X => IPlane {
                        nx: -p.nx,
                        ny: p.ny,
                        nz: p.nz,
                        d: p.d - 2 * k * p.nx,
                    },
                    crate::prim::Axis::Y => IPlane {
                        nx: p.nx,
                        ny: -p.ny,
                        nz: p.nz,
                        d: p.d - 2 * k * p.ny,
                    },
                    crate::prim::Axis::Z => IPlane {
                        nx: p.nx,
                        ny: p.ny,
                        nz: -p.nz,
                        d: p.d - 2 * k * p.nz,
                    },
                })
                .collect(),
        }
    }
}

/// Three non-collinear integer points on a plane, if any exist.
///
/// A `.map` face is written as three points, and integer ones keep the file exact and stable.
/// Whether they exist is a linear Diophantine question: `n · p = d` has an integer solution iff
/// `gcd(nx, ny, nz)` divides `d`. Planes built from integer points always qualify — the
/// reduction that makes an `IPlane` canonical preserves the original solution — but a plane
/// produced by *insetting* (as `hollow` does) can fail, since shifting `d` by a rounded
/// distance can land it off the lattice.
///
/// Returning `None` in that case is deliberate: the caller then writes the exact rational
/// vertices as decimals, which the format allows, rather than nudging the plane to make it fit.
pub fn integer_plane_points(plane: &IPlane) -> Option<[IVec3; 3]> {
    let n = [plane.nx, plane.ny, plane.nz];
    let base = lattice_point(n, plane.d)?;

    // Two independent integer vectors lying in the plane. At least two of these three are
    // independent whenever the normal is non-zero.
    let candidates = [[n[1], -n[0], 0], [0, n[2], -n[1]], [n[2], 0, -n[0]]];
    let mut basis: Vec<[i128; 3]> = Vec::new();
    for c in candidates {
        if c == [0, 0, 0] {
            continue;
        }
        if basis.len() == 1 {
            let b = basis[0];
            // Independent if the cross product is non-zero.
            let cross = [
                b[1] * c[2] - b[2] * c[1],
                b[2] * c[0] - b[0] * c[2],
                b[0] * c[1] - b[1] * c[0],
            ];
            if cross == [0, 0, 0] {
                continue;
            }
        }
        basis.push(c);
        if basis.len() == 2 {
            break;
        }
    }
    if basis.len() < 2 {
        return None;
    }

    let to_ivec = |v: [i128; 3]| -> Option<IVec3> {
        Some(ivec3(
            i64::try_from(v[0]).ok()?,
            i64::try_from(v[1]).ok()?,
            i64::try_from(v[2]).ok()?,
        ))
    };
    let add = |a: [i128; 3], b: [i128; 3]| [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
    let p0 = to_ivec(base)?;
    let p1 = to_ivec(add(base, basis[0]))?;
    let p2 = to_ivec(add(base, basis[1]))?;

    // Orient the triple. Three points on a plane describe it either way round, and which way
    // depends on the arbitrary order the in-plane basis came out in. Getting it wrong turns the
    // face inside out, the brush then encloses nothing, and the map silently contains a hole —
    // so re-derive the plane from the points and swap if it came out flipped.
    let derived = IPlane::from_points(p0, p1, p2)?;
    if derived == *plane {
        Some([p0, p1, p2])
    } else if derived == plane.flipped() {
        Some([p0, p2, p1])
    } else {
        // The points are on the plane but scale it differently, which should be impossible for
        // a reduced plane. Refuse rather than emit a face that is subtly not the one asked for.
        None
    }
}

/// One integer point satisfying `n · p = d`, or `None` if the lattice misses the plane.
fn lattice_point(n: [i128; 3], d: i128) -> Option<[i128; 3]> {
    // Single-component shortcut, which covers every axis-aligned plane.
    for i in 0..3 {
        if n[i] != 0 && d % n[i] == 0 {
            let mut p = [0i128; 3];
            p[i] = d / n[i];
            return Some(p);
        }
    }
    // Otherwise solve over a pair of components with the extended Euclidean algorithm.
    for i in 0..3 {
        for j in (i + 1)..3 {
            if n[i] == 0 || n[j] == 0 {
                continue;
            }
            let (g, x, y) = egcd(n[i], n[j]);
            if g != 0 && d % g == 0 {
                let k = d / g;
                let mut p = [0i128; 3];
                p[i] = x * k;
                p[j] = y * k;
                return Some(p);
            }
        }
    }
    None
}

/// Extended Euclidean algorithm: returns `(g, x, y)` with `a*x + b*y = g = gcd(a, b)`.
fn egcd(a: i128, b: i128) -> (i128, i128, i128) {
    if b == 0 {
        return (a.abs(), if a < 0 { -1 } else { 1 }, 0);
    }
    let (g, x, y) = egcd(b, a % b);
    (g, y, x - (a / b) * y)
}

fn all_coplanar(vertices: &[RatVec3]) -> bool {
    if vertices.len() < 4 {
        return true;
    }
    // Find three points defining a plane, then test the rest against it. Exact throughout:
    // "are these four corners flat?" is precisely the question a float test gets wrong.
    let pts: Vec<Vec3> = vertices.iter().map(|v| v.to_vec3()).collect();
    for i in 2..pts.len() {
        let Some(plane) = nrc_core::math::Plane::from_points(pts[0], pts[1], pts[i]) else {
            continue;
        };
        return pts
            .iter()
            .all(|p| plane.distance_to(*p).abs() < nrc_core::math::DIST_EPSILON);
    }
    true
}

/// An axis-aligned box from integer corners. The most-used primitive by a wide margin.
pub fn box_polytope(min: IVec3, max: IVec3) -> Option<Polytope> {
    if min.x >= max.x || min.y >= max.y || min.z >= max.z {
        return None;
    }
    Some(Polytope::from_planes([
        IPlane {
            nx: -1,
            ny: 0,
            nz: 0,
            d: -(min.x as i128),
        },
        IPlane {
            nx: 1,
            ny: 0,
            nz: 0,
            d: max.x as i128,
        },
        IPlane {
            nx: 0,
            ny: -1,
            nz: 0,
            d: -(min.y as i128),
        },
        IPlane {
            nx: 0,
            ny: 1,
            nz: 0,
            d: max.y as i128,
        },
        IPlane {
            nx: 0,
            ny: 0,
            nz: -1,
            d: -(min.z as i128),
        },
        IPlane {
            nx: 0,
            ny: 0,
            nz: 1,
            d: max.z as i128,
        },
    ]))
}

/// A plane through three integer points, oriented so `interior` is inside it.
///
/// Primitive builders know which side is solid but not which winding produces it, and getting
/// that backwards turns a solid inside out. Passing an interior point removes the guesswork.
pub fn plane_facing_away_from(a: IVec3, b: IVec3, c: IVec3, interior: IVec3) -> Option<IPlane> {
    let p = IPlane::from_points(a, b, c)?;
    match p.side_of(interior) {
        Sign::Negative => Some(p),
        Sign::Positive => Some(p.flipped()),
        // The interior point lies on the plane, so it cannot say which side is inside.
        Sign::Zero | Sign::Indeterminate => None,
    }
}

/// A solid: one or more convex polytopes whose union is the shape.
///
/// Overlap between members is allowed and normal — Quake brushes may overlap, and forbidding
/// it would mean splitting geometry for no benefit. What matters is that each member is
/// individually convex and solid.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Solid {
    pub parts: Vec<Polytope>,
}

impl Solid {
    pub fn new(parts: Vec<Polytope>) -> Solid {
        Solid { parts }
    }

    pub fn single(p: Polytope) -> Solid {
        Solid { parts: vec![p] }
    }

    pub fn is_empty(&self) -> bool {
        self.parts.is_empty()
    }

    pub fn len(&self) -> usize {
        self.parts.len()
    }

    /// Drop parts that enclose no volume.
    pub fn solid_parts_only(self) -> Solid {
        Solid {
            parts: self.parts.into_iter().filter(Polytope::is_solid).collect(),
        }
    }

    pub fn bounds(&self) -> Aabb {
        self.parts
            .iter()
            .fold(Aabb::EMPTY, |a, p| a.union(p.bounds()))
    }

    pub fn volume(&self) -> f64 {
        // Sum, not union: parts may overlap, so this over-counts. Only used for ranking.
        self.parts.iter().map(Polytope::volume).sum()
    }

    pub fn contains(&self, p: IVec3) -> bool {
        self.parts.iter().any(|q| q.contains(p))
    }

    pub fn simplified(self) -> Solid {
        Solid {
            parts: self.parts.iter().map(Polytope::simplified).collect(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use nrc_core::exact::ivec3;
    use nrc_core::math::vec3;

    fn unit_box(s: i64) -> Polytope {
        box_polytope(ivec3(0, 0, 0), ivec3(s, s, s)).unwrap()
    }

    #[test]
    fn a_box_is_solid_with_six_planes_and_eight_vertices() {
        let b = unit_box(64);
        assert_eq!(b.len(), 6);
        assert!(b.is_solid());
        assert_eq!(b.vertices().len(), 8);
        assert_eq!(b.bounds().min, vec3(0.0, 0.0, 0.0));
        assert_eq!(b.bounds().max, vec3(64.0, 64.0, 64.0));
        // Volume is floating point on purpose (it only ranks results), so compare loosely.
        assert!((b.volume() - 64.0 * 64.0 * 64.0).abs() < 1e-6);
        assert_eq!(b.min_thickness(), Some(64.0));
    }

    #[test]
    fn a_degenerate_box_is_refused_at_construction() {
        assert!(box_polytope(ivec3(0, 0, 0), ivec3(0, 64, 64)).is_none());
        assert!(box_polytope(ivec3(64, 0, 0), ivec3(0, 64, 64)).is_none());
    }

    #[test]
    fn duplicate_planes_are_dropped_on_construction() {
        let p = IPlane {
            nx: 1,
            ny: 0,
            nz: 0,
            d: 0,
        };
        assert_eq!(Polytope::from_planes([p, p, p]).len(), 1);
    }

    #[test]
    fn containment_is_exact_including_the_boundary() {
        let b = unit_box(64);
        assert!(b.contains(ivec3(32, 32, 32)));
        assert!(b.contains(ivec3(0, 0, 0)), "a boundary point is inside");
        assert!(b.contains(ivec3(64, 64, 64)));
        assert!(!b.contains(ivec3(65, 32, 32)));
        assert!(!b.contains(ivec3(-1, 32, 32)));
    }

    #[test]
    fn a_flattened_box_is_not_solid() {
        // Four coplanar corners bound no volume, and must not be mistaken for a thin brush.
        let flat = Polytope::from_planes([
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
        ]);
        assert!(!flat.is_solid());
        assert_eq!(flat.volume(), 0.0);
    }

    #[test]
    fn a_one_unit_slab_is_solid() {
        // Thin but real: the boundary between "thin" and "not a solid" must sit below one
        // unit, or the IR would refuse legitimate trim geometry.
        let slab = box_polytope(ivec3(0, 0, 0), ivec3(64, 64, 1)).unwrap();
        assert!(slab.is_solid());
        assert_eq!(slab.min_thickness(), Some(1.0));
    }

    #[test]
    fn mirrored_planes_are_detected_cheaply() {
        let up = IPlane {
            nx: 0,
            ny: 0,
            nz: 1,
            d: 0,
        };
        assert!(Polytope::from_planes([up, up.flipped()]).has_mirrored_pair());
        assert!(!unit_box(64).has_mirrored_pair());
    }

    #[test]
    fn clipping_adds_a_halfspace_and_stays_convex() {
        let cut = unit_box(64).clipped_by(IPlane {
            nx: 1,
            ny: 1,
            nz: 0,
            d: 64,
        });
        assert_eq!(cut.len(), 7);
        assert!(cut.is_solid());
        assert!(cut.volume() < unit_box(64).volume());
        assert!(
            !cut.contains(ivec3(60, 60, 32)),
            "the clipped corner is gone"
        );
        assert!(cut.contains(ivec3(10, 10, 32)));
    }

    #[test]
    fn simplify_removes_a_plane_that_bounds_nothing() {
        // A plane touching only one corner contributes no face.
        let with_extra = unit_box(64).clipped_by(IPlane {
            nx: 1,
            ny: 1,
            nz: 1,
            d: 192,
        });
        assert_eq!(with_extra.len(), 7);
        assert_eq!(with_extra.redundant_planes(), vec![6]);
        let s = with_extra.simplified();
        assert_eq!(s.len(), 6);
        assert_eq!(s.volume(), unit_box(64).volume());
    }

    #[test]
    fn plane_orientation_follows_the_interior_point() {
        let a = ivec3(0, 0, 0);
        let b = ivec3(64, 0, 0);
        let c = ivec3(0, 64, 0);
        let inside = ivec3(16, 16, -32); // below the plane
        let p = plane_facing_away_from(a, b, c, inside).unwrap();
        assert_eq!(p.side_of(inside), Sign::Negative);
        // Asking from the other side must flip it.
        let q = plane_facing_away_from(a, b, c, ivec3(16, 16, 32)).unwrap();
        assert_eq!(q, p.flipped());
        // An interior point on the plane cannot decide, and says so.
        assert!(plane_facing_away_from(a, b, c, ivec3(16, 16, 0)).is_none());
    }

    #[test]
    fn a_solid_of_two_boxes_reports_the_union_bounds() {
        let s = Solid::new(vec![
            box_polytope(ivec3(0, 0, 0), ivec3(64, 64, 64)).unwrap(),
            box_polytope(ivec3(128, 0, 0), ivec3(192, 64, 64)).unwrap(),
        ]);
        assert_eq!(s.len(), 2);
        assert_eq!(s.bounds().max, vec3(192.0, 64.0, 64.0));
        assert!(s.contains(ivec3(150, 32, 32)));
        assert!(!s.contains(ivec3(100, 32, 32)));
    }

    #[test]
    fn empty_parts_are_dropped() {
        let empty = Polytope::from_planes([
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
            },
        ]);
        let s = Solid::new(vec![unit_box(64), empty]).solid_parts_only();
        assert_eq!(s.len(), 1);
    }
}
