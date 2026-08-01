//! Exact geometric predicates over the integer grid.
//!
//! §3.2 asks for exact predicates because "floating-point slop is how you produce
//! invisible micro-slivers that leak maps". There are two ways to get them: adaptive
//! floating-point expansion arithmetic (Shewchuk), or exact integer arithmetic over a
//! domain you control. This module takes the second route, for a reason worth stating
//! plainly.
//!
//! The spec also requires that **all authored vertices snap to a grid** and that
//! off-grid vertices are an error rather than a warning. That makes the authored domain
//! *integral*, and on integer coordinates bounded by `MAX_WORLD_COORD` every predicate
//! here reduces to a small determinant that fits in `i128` with room to spare — exactly,
//! with no epsilon and no error bound to get wrong.
//!
//! The honest consequence: when a coordinate is *not* representable on the integer grid
//! (an imported map with fractional vertices, say), these functions return
//! [`Sign::Indeterminate`] rather than guessing. Callers must then either snap first or
//! record the result as approximate. A predicate that admits it cannot decide is far
//! safer here than one that quietly picks a side — a wrong side is a sliver, and a
//! sliver is a leak three weeks later.
//!
//! What this buys, beyond not being wrong: **exact plane identity**. [`IPlane`] reduces
//! to a primitive integer 4-tuple, so "are these the same plane?" and "is this plane
//! redundant?" become integer equality instead of a two-epsilon comparison. Detecting
//! duplicate and redundant planes is most of what makes brush validation trustworthy.

use crate::math::{vec3, Vec3, MAX_WORLD_COORD};

/// The sign of an exact predicate, or an admission that it could not be evaluated.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Sign {
    Negative,
    Zero,
    Positive,
    /// Inputs were not exactly representable on the integer grid, or an intermediate
    /// value overflowed `i128`. Never returned for on-grid geometry within world bounds.
    Indeterminate,
}

impl Sign {
    fn of(v: i128) -> Sign {
        match v.cmp(&0) {
            std::cmp::Ordering::Less => Sign::Negative,
            std::cmp::Ordering::Equal => Sign::Zero,
            std::cmp::Ordering::Greater => Sign::Positive,
        }
    }

    pub fn is_zero(self) -> bool {
        self == Sign::Zero
    }

    pub fn is_known(self) -> bool {
        self != Sign::Indeterminate
    }

    pub fn negated(self) -> Sign {
        match self {
            Sign::Negative => Sign::Positive,
            Sign::Positive => Sign::Negative,
            other => other,
        }
    }
}

/// A point on the integer grid.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct IVec3 {
    pub x: i64,
    pub y: i64,
    pub z: i64,
}

pub const fn ivec3(x: i64, y: i64, z: i64) -> IVec3 {
    IVec3 { x, y, z }
}

impl IVec3 {
    pub fn to_vec3(self) -> Vec3 {
        vec3(self.x as f64, self.y as f64, self.z as f64)
    }

    /// Exact conversion, or `None` if the point is off-grid or out of world bounds.
    ///
    /// Both rejections matter: a fractional coordinate cannot be reasoned about
    /// exactly, and a coordinate beyond `MAX_WORLD_COORD` is already a map q3map2 will
    /// refuse, so treating it as valid input would only defer the error.
    pub fn try_from_vec3(v: Vec3) -> Option<IVec3> {
        let f = |c: f64| -> Option<i64> {
            if c.is_finite() && c.fract() == 0.0 && c.abs() <= MAX_WORLD_COORD {
                Some(c as i64)
            } else {
                None
            }
        };
        Some(ivec3(f(v.x)?, f(v.y)?, f(v.z)?))
    }

    fn sub(self, o: IVec3) -> [i128; 3] {
        [
            self.x as i128 - o.x as i128,
            self.y as i128 - o.y as i128,
            self.z as i128 - o.z as i128,
        ]
    }
}

/// Exact 3x3 determinant of rows `u`, `v`, `w`.
fn det3(u: [i128; 3], v: [i128; 3], w: [i128; 3]) -> Option<i128> {
    // Coordinate differences are bounded by 2^18 and each term is a product of three
    // of them, so the true magnitude cannot exceed ~2^56 for in-bounds input. The
    // checked arithmetic is for callers who reach these helpers with reduced plane
    // normals, where the operands are larger.
    let m = |a: i128, b: i128, c: i128, d: i128| -> Option<i128> {
        a.checked_mul(b)?.checked_sub(c.checked_mul(d)?)
    };
    let a = u[0].checked_mul(m(v[1], w[2], v[2], w[1])?)?;
    let b = u[1].checked_mul(m(v[0], w[2], v[2], w[0])?)?;
    let c = u[2].checked_mul(m(v[0], w[1], v[1], w[0])?)?;
    a.checked_sub(b)?.checked_add(c)
}

/// Exact `orient3d`: on which side of the plane through `a`, `b`, `c` does `d` lie?
///
/// `Zero` means exactly coplanar — the answer floating-point cannot give you and the
/// one that decides whether a face is planar or a brush has a redundant plane.
pub fn orient3d(a: IVec3, b: IVec3, c: IVec3, d: IVec3) -> Sign {
    match det3(b.sub(a), c.sub(a), d.sub(a)) {
        Some(v) => Sign::of(v),
        None => Sign::Indeterminate,
    }
}

/// `orient3d` on floating-point input, exact when the input happens to be on-grid.
pub fn orient3d_f(a: Vec3, b: Vec3, c: Vec3, d: Vec3) -> Sign {
    match (
        IVec3::try_from_vec3(a),
        IVec3::try_from_vec3(b),
        IVec3::try_from_vec3(c),
        IVec3::try_from_vec3(d),
    ) {
        (Some(a), Some(b), Some(c), Some(d)) => orient3d(a, b, c, d),
        _ => Sign::Indeterminate,
    }
}

/// A plane with integer coefficients, stored in primitive (gcd-reduced) form so that
/// plane identity is integer equality.
///
/// Convention matches [`crate::math::Plane`]: the solid half-space is `n · p <= d`, so
/// the normal points out of the brush.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct IPlane {
    pub nx: i128,
    pub ny: i128,
    pub nz: i128,
    pub d: i128,
}

impl IPlane {
    /// Plane through three integer points, reduced to primitive form.
    ///
    /// Mirrors q3map2's `PlaneFromPoints` (`cross(c - a, b - a)`) so that our idea of
    /// which way a face points is the compiler's idea, not merely a defensible one.
    /// Returns `None` when the points are collinear or coincident.
    pub fn from_points(a: IVec3, b: IVec3, c: IVec3) -> Option<IPlane> {
        let ba = b.sub(a);
        let ca = c.sub(a);
        // cross(ca, ba)
        let nx = ca[1].checked_mul(ba[2])?.checked_sub(ca[2].checked_mul(ba[1])?)?;
        let ny = ca[2].checked_mul(ba[0])?.checked_sub(ca[0].checked_mul(ba[2])?)?;
        let nz = ca[0].checked_mul(ba[1])?.checked_sub(ca[1].checked_mul(ba[0])?)?;
        if nx == 0 && ny == 0 && nz == 0 {
            return None; // collinear: no plane exists
        }
        let d = nx
            .checked_mul(a.x as i128)?
            .checked_add(ny.checked_mul(a.y as i128)?)?
            .checked_add(nz.checked_mul(a.z as i128)?)?;
        Some(IPlane { nx, ny, nz, d }.reduced())
    }

    /// Divide out the common factor of all four coefficients.
    ///
    /// Reducing `d` along with the normal (rather than the normal alone) keeps every
    /// coefficient integral, which is what lets equality be exact. The cost is that
    /// `nx..nz` is not a unit normal — irrelevant for sign predicates, and
    /// [`IPlane::to_plane`] normalizes when a float plane is wanted.
    fn reduced(self) -> IPlane {
        let g = gcd4(self.nx, self.ny, self.nz, self.d);
        if g <= 1 {
            return self;
        }
        IPlane {
            nx: self.nx / g,
            ny: self.ny / g,
            nz: self.nz / g,
            d: self.d / g,
        }
    }

    /// Exact side test: `Positive` is in front of the plane, i.e. outside the brush.
    pub fn side_of(&self, p: IVec3) -> Sign {
        let f = || -> Option<i128> {
            self.nx
                .checked_mul(p.x as i128)?
                .checked_add(self.ny.checked_mul(p.y as i128)?)?
                .checked_add(self.nz.checked_mul(p.z as i128)?)?
                .checked_sub(self.d)
        };
        match f() {
            Some(v) => Sign::of(v),
            None => Sign::Indeterminate,
        }
    }

    /// Exact side test for a rational point, as produced by [`intersect3_exact`].
    ///
    /// Multiplying through by the (positive) denominator keeps this in integers, so a
    /// vertex that lies *exactly* on another face's plane is reported as `Zero` rather
    /// than landing arbitrarily on one side. That distinction is the whole reason brush
    /// hull construction can be trusted: every real brush has vertices lying exactly on
    /// three or more of its own planes.
    pub fn side_of_rat(&self, p: &RatVec3) -> Sign {
        let f = || -> Option<i128> {
            self.nx
                .checked_mul(p.x)?
                .checked_add(self.ny.checked_mul(p.y)?)?
                .checked_add(self.nz.checked_mul(p.z)?)?
                .checked_sub(self.d.checked_mul(p.den)?)
        };
        match f() {
            Some(v) => Sign::of(v),
            None => Sign::Indeterminate,
        }
    }

    pub fn flipped(&self) -> IPlane {
        IPlane { nx: -self.nx, ny: -self.ny, nz: -self.nz, d: -self.d }
    }

    /// True if this is the same plane facing the same way — exact, no epsilon.
    pub fn same_as(&self, o: &IPlane) -> bool {
        self == o
    }

    /// True if this is the same plane facing either way. Two brush faces that are
    /// opposite in this sense enclose zero volume.
    pub fn same_plane_ignoring_facing(&self, o: &IPlane) -> bool {
        self == o || *self == o.flipped()
    }

    pub fn to_plane(self) -> crate::math::Plane {
        let n = vec3(self.nx as f64, self.ny as f64, self.nz as f64);
        let len = n.length();
        crate::math::Plane { normal: n / len, dist: self.d as f64 / len }
    }
}

/// A point with exact rational coordinates over a shared denominator.
///
/// The intersection of three integer planes is rational, not integral. Keeping it exact
/// lets us answer "is this vertex on the grid?" — the check §3.2 wants — without ever
/// rounding first and thereby destroying the evidence.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RatVec3 {
    pub x: i128,
    pub y: i128,
    pub z: i128,
    /// Always strictly positive.
    pub den: i128,
}

impl RatVec3 {
    pub fn to_vec3(self) -> Vec3 {
        let d = self.den as f64;
        vec3(self.x as f64 / d, self.y as f64 / d, self.z as f64 / d)
    }

    /// True if the point lies exactly on the integer grid.
    pub fn is_integral(&self) -> bool {
        self.x % self.den == 0 && self.y % self.den == 0 && self.z % self.den == 0
    }

    /// True if the point lies exactly on a grid of the given spacing.
    pub fn is_on_grid(&self, grid: i64) -> bool {
        if grid <= 0 {
            return self.is_integral();
        }
        let g = grid as i128 * self.den;
        // (x/den) is a multiple of grid  <=>  x is a multiple of grid*den.
        self.x % g == 0 && self.y % g == 0 && self.z % g == 0
    }

    pub fn to_ivec3(self) -> Option<IVec3> {
        if !self.is_integral() {
            return None;
        }
        let f = |v: i128| -> Option<i64> { i64::try_from(v / self.den).ok() };
        Some(ivec3(f(self.x)?, f(self.y)?, f(self.z)?))
    }
}

/// Exact intersection point of three integer planes, or `None` if they do not meet in a
/// single point (parallel or coaxial) or an intermediate value overflowed.
///
/// The overflow case is real but rare: it needs planes whose reduced normals are large,
/// which in practice means faces defined by wildly off-grid-ish point triples. Returning
/// `None` sends the caller to the float path with an approximate marker rather than
/// handing back a wrapped, catastrophically wrong vertex.
pub fn intersect3_exact(a: &IPlane, b: &IPlane, c: &IPlane) -> Option<RatVec3> {
    let na = [a.nx, a.ny, a.nz];
    let nb = [b.nx, b.ny, b.nz];
    let nc = [c.nx, c.ny, c.nz];

    let den = det3(na, nb, nc)?;
    if den == 0 {
        return None; // no unique intersection
    }
    // Cramer's rule: substitute the distance column for each coordinate column.
    let rows_with_d_in = |i: usize| -> ([i128; 3], [i128; 3], [i128; 3]) {
        let mut ra = na;
        let mut rb = nb;
        let mut rc = nc;
        ra[i] = a.d;
        rb[i] = b.d;
        rc[i] = c.d;
        (ra, rb, rc)
    };

    let mut num = [0i128; 3];
    for i in 0..3 {
        let (ra, rb, rc) = rows_with_d_in(i);
        num[i] = det3(ra, rb, rc)?;
    }

    // Normalize the sign so `den` is positive, keeping the `%` checks in `RatVec3`
    // meaningful regardless of plane ordering.
    let (mut x, mut y, mut z, mut den) = (num[0], num[1], num[2], den);
    if den < 0 {
        x = -x;
        y = -y;
        z = -z;
        den = -den;
    }
    let g = gcd4(x, y, z, den);
    if g > 1 {
        x /= g;
        y /= g;
        z /= g;
        den /= g;
    }
    Some(RatVec3 { x, y, z, den })
}

fn gcd(mut a: i128, mut b: i128) -> i128 {
    a = a.abs();
    b = b.abs();
    while b != 0 {
        let t = a % b;
        a = b;
        b = t;
    }
    a
}

fn gcd4(a: i128, b: i128, c: i128, d: i128) -> i128 {
    gcd(gcd(gcd(a, b), c), d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn orient3d_is_exact_on_coplanar_points() {
        let a = ivec3(0, 0, 0);
        let b = ivec3(64, 0, 0);
        let c = ivec3(0, 64, 0);
        assert_eq!(orient3d(a, b, c, ivec3(32, 32, 0)), Sign::Zero);
        assert_eq!(orient3d(a, b, c, ivec3(32, 32, 1)).is_zero(), false);
        assert_eq!(
            orient3d(a, b, c, ivec3(32, 32, 1)).negated(),
            orient3d(a, b, c, ivec3(32, 32, -1))
        );
    }

    #[test]
    fn orient3d_decides_a_near_coplanar_case_that_floats_cannot() {
        // A point one unit off a plane spanning most of the world. The float cross
        // product loses the last bits here; the integer determinant does not.
        let a = ivec3(-65536, -65536, 0);
        let b = ivec3(65536, -65536, 1);
        let c = ivec3(-65536, 65536, 0);
        assert_eq!(orient3d(a, b, c, ivec3(65536, 65536, 1)), Sign::Zero);
        assert_ne!(orient3d(a, b, c, ivec3(65535, 65536, 1)), Sign::Zero);
    }

    #[test]
    fn off_grid_input_is_indeterminate_not_guessed() {
        assert_eq!(
            orient3d_f(
                vec3(0.0, 0.0, 0.0),
                vec3(64.0, 0.0, 0.0),
                vec3(0.0, 64.0, 0.0),
                vec3(32.0, 32.0, 0.5)
            ),
            Sign::Indeterminate
        );
        // Out of world bounds is likewise refused rather than accepted.
        assert!(IVec3::try_from_vec3(vec3(1e9, 0.0, 0.0)).is_none());
        assert!(IVec3::try_from_vec3(vec3(f64::NAN, 0.0, 0.0)).is_none());
    }

    #[test]
    fn plane_identity_is_exact_regardless_of_defining_points() {
        // The same geometric plane described by two completely different point triples
        // must produce the same primitive coefficients. This is what makes redundant
        // plane detection reliable.
        let p1 = IPlane::from_points(ivec3(0, 0, 16), ivec3(1, 0, 16), ivec3(0, 1, 16)).unwrap();
        let p2 = IPlane::from_points(
            ivec3(-4096, 512, 16),
            ivec3(777, -13, 16),
            ivec3(64, 4096, 16),
        )
        .unwrap();
        assert_eq!(p1, p2);
        assert!(p1.same_as(&p2));
    }

    #[test]
    fn opposite_facing_planes_are_distinguished_but_recognized() {
        let up = IPlane::from_points(ivec3(0, 0, 0), ivec3(1, 0, 0), ivec3(0, 1, 0)).unwrap();
        let down = up.flipped();
        assert_ne!(up, down);
        assert!(up.same_plane_ignoring_facing(&down));
        assert!(!up.same_as(&down));
    }

    #[test]
    fn collinear_points_yield_no_plane() {
        assert!(IPlane::from_points(ivec3(0, 0, 0), ivec3(8, 0, 0), ivec3(16, 0, 0)).is_none());
        assert!(IPlane::from_points(ivec3(5, 5, 5), ivec3(5, 5, 5), ivec3(5, 5, 5)).is_none());
    }

    #[test]
    fn side_of_is_exact() {
        let floor = IPlane::from_points(ivec3(0, 0, 0), ivec3(0, 1, 0), ivec3(1, 0, 0)).unwrap();
        // Whichever way this plane faces, the two sides must be opposite and the
        // on-plane point must be exactly Zero.
        assert_eq!(floor.side_of(ivec3(0, 0, 0)), Sign::Zero);
        assert_eq!(floor.side_of(ivec3(500, -300, 0)), Sign::Zero);
        assert_eq!(
            floor.side_of(ivec3(0, 0, 1)).negated(),
            floor.side_of(ivec3(0, 0, -1))
        );
        assert!(floor.side_of(ivec3(0, 0, 1)).is_known());
    }

    #[test]
    fn three_planes_intersect_exactly_on_grid() {
        let px = IPlane { nx: 1, ny: 0, nz: 0, d: 16 };
        let py = IPlane { nx: 0, ny: 1, nz: 0, d: 32 };
        let pz = IPlane { nx: 0, ny: 0, nz: 1, d: 48 };
        let p = intersect3_exact(&px, &py, &pz).unwrap();
        assert!(p.is_integral());
        assert_eq!(p.to_ivec3().unwrap(), ivec3(16, 32, 48));
        assert_eq!(p.to_vec3(), vec3(16.0, 32.0, 48.0));
    }

    #[test]
    fn off_grid_intersection_is_detected_not_rounded() {
        // A 45-degree plane through an odd offset puts the corner on a half unit.
        // Rounding first would hide exactly the defect we need to report.
        let px = IPlane { nx: 1, ny: 0, nz: 0, d: 0 };
        let pz = IPlane { nx: 0, ny: 0, nz: 1, d: 0 };
        let diag = IPlane { nx: 2, ny: 2, nz: 0, d: 1 };
        let p = intersect3_exact(&px, &pz, &diag).unwrap();
        assert!(!p.is_integral());
        assert_eq!(p.to_vec3(), vec3(0.0, 0.5, 0.0));
        assert!(p.to_ivec3().is_none());
    }

    #[test]
    fn grid_membership_respects_spacing() {
        let p = RatVec3 { x: 16, y: 32, z: 48, den: 1 };
        assert!(p.is_on_grid(16));
        assert!(p.is_on_grid(8));
        assert!(!p.is_on_grid(64), "48 is not a multiple of 64");
    }

    #[test]
    fn parallel_planes_have_no_intersection_point() {
        let a = IPlane { nx: 0, ny: 0, nz: 1, d: 0 };
        let b = IPlane { nx: 0, ny: 0, nz: 1, d: 64 };
        let c = IPlane { nx: 1, ny: 0, nz: 0, d: 0 };
        assert!(intersect3_exact(&a, &b, &c).is_none());
        // Coincident planes likewise.
        assert!(intersect3_exact(&a, &a, &c).is_none());
    }

    #[test]
    fn reduction_produces_a_canonical_form() {
        let a = IPlane { nx: 4, ny: 0, nz: 0, d: 64 }.reduced();
        assert_eq!(a, IPlane { nx: 1, ny: 0, nz: 0, d: 16 });
        // A normal that shares no factor with d must not be scaled.
        let b = IPlane { nx: 2, ny: 0, nz: 0, d: 3 }.reduced();
        assert_eq!(b, IPlane { nx: 2, ny: 0, nz: 0, d: 3 });
    }

    #[test]
    fn exact_and_float_planes_agree_on_direction() {
        let a = ivec3(0, 0, 0);
        let b = ivec3(64, 0, 0);
        let c = ivec3(0, 64, 0);
        let ip = IPlane::from_points(a, b, c).unwrap();
        let fp =
            crate::math::Plane::from_points(a.to_vec3(), b.to_vec3(), c.to_vec3()).unwrap();
        let ipf = ip.to_plane();
        assert!(
            ipf.approx_eq(&fp),
            "exact plane {ipf:?} disagrees with float plane {fp:?}"
        );
    }
}
