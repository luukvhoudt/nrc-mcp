//! Floating-point vector and plane primitives.
//!
//! These are the *convenience* layer, used for rendering, bounds, distances and
//! reporting. Anything that decides whether geometry is valid — coplanarity, convexity,
//! plane identity, winding orientation — must go through [`crate::exact`] instead.
//! See that module for why.

use std::fmt;
use std::ops::{Add, Div, Mul, Neg, Sub};

/// q3map2's plane-normal comparison epsilon (`tools/quake3/q3map2/q3map2.h`).
/// Reproduced here so our "will the compiler consider these the same plane?" answer
/// matches the compiler's, rather than being merely defensible.
pub const NORMAL_EPSILON: f64 = 0.00001;
/// q3map2's plane-distance comparison epsilon.
pub const DIST_EPSILON: f64 = 0.01;
/// Largest coordinate q3map2 will accept before declaring a brush out of bounds.
pub const MAX_WORLD_COORD: f64 = 65536.0;

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Vec3 {
    pub x: f64,
    pub y: f64,
    pub z: f64,
}

pub const fn vec3(x: f64, y: f64, z: f64) -> Vec3 {
    Vec3 { x, y, z }
}

impl Vec3 {
    pub const ZERO: Vec3 = vec3(0.0, 0.0, 0.0);

    pub fn dot(self, o: Vec3) -> f64 {
        self.x * o.x + self.y * o.y + self.z * o.z
    }

    pub fn cross(self, o: Vec3) -> Vec3 {
        vec3(
            self.y * o.z - self.z * o.y,
            self.z * o.x - self.x * o.z,
            self.x * o.y - self.y * o.x,
        )
    }

    pub fn length(self) -> f64 {
        self.dot(self).sqrt()
    }

    pub fn length_squared(self) -> f64 {
        self.dot(self)
    }

    pub fn normalized(self) -> Option<Vec3> {
        let l = self.length();
        if l > 0.0 && l.is_finite() {
            Some(self / l)
        } else {
            None
        }
    }

    pub fn component(self, axis: Axis) -> f64 {
        match axis {
            Axis::X => self.x,
            Axis::Y => self.y,
            Axis::Z => self.z,
        }
    }

    /// Axis whose component has the largest magnitude — the standard way to pick a
    /// projection plane for a face without hitting a degenerate axis.
    pub fn major_axis(self) -> Axis {
        let (ax, ay, az) = (self.x.abs(), self.y.abs(), self.z.abs());
        if ax >= ay && ax >= az {
            Axis::X
        } else if ay >= az {
            Axis::Y
        } else {
            Axis::Z
        }
    }

    pub fn is_finite(self) -> bool {
        self.x.is_finite() && self.y.is_finite() && self.z.is_finite()
    }

    /// Snap each component to the nearest multiple of `grid`.
    pub fn snapped(self, grid: f64) -> Vec3 {
        if grid <= 0.0 {
            return self;
        }
        vec3(
            (self.x / grid).round() * grid,
            (self.y / grid).round() * grid,
            (self.z / grid).round() * grid,
        )
    }

    pub fn to_array(self) -> [f64; 3] {
        [self.x, self.y, self.z]
    }
}

impl Add for Vec3 {
    type Output = Vec3;
    fn add(self, o: Vec3) -> Vec3 {
        vec3(self.x + o.x, self.y + o.y, self.z + o.z)
    }
}
impl Sub for Vec3 {
    type Output = Vec3;
    fn sub(self, o: Vec3) -> Vec3 {
        vec3(self.x - o.x, self.y - o.y, self.z - o.z)
    }
}
impl Mul<f64> for Vec3 {
    type Output = Vec3;
    fn mul(self, s: f64) -> Vec3 {
        vec3(self.x * s, self.y * s, self.z * s)
    }
}
impl Div<f64> for Vec3 {
    type Output = Vec3;
    fn div(self, s: f64) -> Vec3 {
        vec3(self.x / s, self.y / s, self.z / s)
    }
}
impl Neg for Vec3 {
    type Output = Vec3;
    fn neg(self) -> Vec3 {
        vec3(-self.x, -self.y, -self.z)
    }
}

impl From<[f64; 3]> for Vec3 {
    fn from(a: [f64; 3]) -> Vec3 {
        vec3(a[0], a[1], a[2])
    }
}

impl fmt::Display for Vec3 {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "({} {} {})", self.x, self.y, self.z)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Axis {
    X,
    Y,
    Z,
}

impl Axis {
    pub fn as_str(self) -> &'static str {
        match self {
            Axis::X => "x",
            Axis::Y => "y",
            Axis::Z => "z",
        }
    }
}

/// A plane in the form `normal · p = dist`, with the solid half-space being
/// `normal · p <= dist`. This is q3's convention: a brush is the intersection of the
/// *behind* half-spaces of its faces.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Plane {
    pub normal: Vec3,
    pub dist: f64,
}

impl Plane {
    pub fn new(normal: Vec3, dist: f64) -> Self {
        Self { normal, dist }
    }

    /// Plane through three points, using the Radiant/q3 winding convention: the points
    /// are given clockwise when viewed from the *front* (outside) of the face, so the
    /// normal points out of the brush.
    ///
    /// The expression deliberately mirrors q3map2's `PlaneFromPoints` — `cross(c - a,
    /// b - a)` — so that a disagreement between us and the compiler about which way a
    /// face points is impossible by construction rather than by argument.
    ///
    /// Returns `None` when the points are collinear or coincident, i.e. define no plane.
    pub fn from_points(a: Vec3, b: Vec3, c: Vec3) -> Option<Plane> {
        let normal = (c - a).cross(b - a).normalized()?;
        Some(Plane { normal, dist: normal.dot(a) })
    }

    /// Signed distance from the plane; positive is in front (outside the brush).
    pub fn distance_to(&self, p: Vec3) -> f64 {
        self.normal.dot(p) - self.dist
    }

    pub fn flipped(&self) -> Plane {
        Plane { normal: -self.normal, dist: -self.dist }
    }

    /// True if two planes are the same plane to q3map2's tolerances — which is the
    /// tolerance that actually decides whether the compiler merges or splits them.
    pub fn approx_eq(&self, o: &Plane) -> bool {
        (self.normal.x - o.normal.x).abs() < NORMAL_EPSILON
            && (self.normal.y - o.normal.y).abs() < NORMAL_EPSILON
            && (self.normal.z - o.normal.z).abs() < NORMAL_EPSILON
            && (self.dist - o.dist).abs() < DIST_EPSILON
    }

    /// Intersection point of three planes, or `None` if they do not meet in a point.
    ///
    /// Floating-point: use [`crate::exact::intersect3_exact`] when the answer feeds a
    /// validity decision. This exists for rendering and reporting.
    pub fn intersect3(a: &Plane, b: &Plane, c: &Plane) -> Option<Vec3> {
        let bc = b.normal.cross(c.normal);
        let den = a.normal.dot(bc);
        // Near-parallel planes produce a huge, meaningless point. q3map2 uses a
        // comparable guard; without it a redundant plane yields a vertex at 1e17 and
        // the brush silently becomes garbage.
        if den.abs() < 1e-9 {
            return None;
        }
        let ca = c.normal.cross(a.normal);
        let ab = a.normal.cross(b.normal);
        Some((bc * a.dist + ca * b.dist + ab * c.dist) / den)
    }
}

/// Axis-aligned bounding box. An empty box is `min > max` on every axis, which makes
/// `extend` work without a separate "is initialized" flag.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Aabb {
    pub min: Vec3,
    pub max: Vec3,
}

impl Default for Aabb {
    fn default() -> Self {
        Self::EMPTY
    }
}

impl Aabb {
    pub const EMPTY: Aabb = Aabb {
        min: vec3(f64::INFINITY, f64::INFINITY, f64::INFINITY),
        max: vec3(f64::NEG_INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY),
    };

    pub fn is_empty(&self) -> bool {
        self.min.x > self.max.x || self.min.y > self.max.y || self.min.z > self.max.z
    }

    pub fn extend(&mut self, p: Vec3) {
        self.min = vec3(self.min.x.min(p.x), self.min.y.min(p.y), self.min.z.min(p.z));
        self.max = vec3(self.max.x.max(p.x), self.max.y.max(p.y), self.max.z.max(p.z));
    }

    pub fn union(mut self, o: Aabb) -> Aabb {
        if o.is_empty() {
            return self;
        }
        self.extend(o.min);
        self.extend(o.max);
        self
    }

    pub fn size(&self) -> Vec3 {
        if self.is_empty() {
            Vec3::ZERO
        } else {
            self.max - self.min
        }
    }

    pub fn center(&self) -> Vec3 {
        if self.is_empty() {
            Vec3::ZERO
        } else {
            (self.min + self.max) * 0.5
        }
    }

    pub fn contains(&self, p: Vec3) -> bool {
        !self.is_empty()
            && p.x >= self.min.x
            && p.x <= self.max.x
            && p.y >= self.min.y
            && p.y <= self.max.y
            && p.z >= self.min.z
            && p.z <= self.max.z
    }

    pub fn intersects(&self, o: &Aabb) -> bool {
        !self.is_empty()
            && !o.is_empty()
            && self.min.x <= o.max.x
            && self.max.x >= o.min.x
            && self.min.y <= o.max.y
            && self.max.y >= o.min.y
            && self.min.z <= o.max.z
            && self.max.z >= o.min.z
    }

    pub fn expanded(&self, by: f64) -> Aabb {
        if self.is_empty() {
            return *self;
        }
        Aabb {
            min: self.min - vec3(by, by, by),
            max: self.max + vec3(by, by, by),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plane_from_points_normal_points_outward() {
        // A floor at z=0. Radiant lists the points so the normal faces +Z (up, out of
        // the solid below it).
        let p = Plane::from_points(vec3(0.0, 0.0, 0.0), vec3(64.0, 0.0, 0.0), vec3(0.0, 64.0, 0.0))
            .unwrap();
        assert_eq!(p.normal, vec3(0.0, 0.0, -1.0));
        assert_eq!(p.dist, 0.0);
    }

    #[test]
    fn collinear_points_define_no_plane() {
        assert!(
            Plane::from_points(vec3(0.0, 0.0, 0.0), vec3(1.0, 0.0, 0.0), vec3(2.0, 0.0, 0.0))
                .is_none()
        );
        assert!(Plane::from_points(Vec3::ZERO, Vec3::ZERO, Vec3::ZERO).is_none());
    }

    #[test]
    fn three_axis_planes_meet_at_a_point() {
        let px = Plane::new(vec3(1.0, 0.0, 0.0), 16.0);
        let py = Plane::new(vec3(0.0, 1.0, 0.0), 32.0);
        let pz = Plane::new(vec3(0.0, 0.0, 1.0), 48.0);
        assert_eq!(Plane::intersect3(&px, &py, &pz).unwrap(), vec3(16.0, 32.0, 48.0));
    }

    #[test]
    fn parallel_planes_do_not_meet() {
        let a = Plane::new(vec3(0.0, 0.0, 1.0), 0.0);
        let b = Plane::new(vec3(0.0, 0.0, 1.0), 64.0);
        let c = Plane::new(vec3(1.0, 0.0, 0.0), 0.0);
        assert!(Plane::intersect3(&a, &b, &c).is_none());
    }

    #[test]
    fn empty_aabb_absorbs_first_point() {
        let mut b = Aabb::EMPTY;
        assert!(b.is_empty());
        b.extend(vec3(5.0, 5.0, 5.0));
        assert!(!b.is_empty());
        assert_eq!(b.min, b.max);
        assert_eq!(b.size(), Vec3::ZERO);
    }

    #[test]
    fn snapping_rounds_to_grid() {
        assert_eq!(vec3(7.0, 9.0, -7.0).snapped(8.0), vec3(8.0, 8.0, -8.0));
        assert_eq!(vec3(0.4, 0.6, -0.6).snapped(1.0), vec3(0.0, 1.0, -1.0));
        // A zero or negative grid must be a no-op, not a division by zero.
        assert_eq!(vec3(1.5, 0.0, 0.0).snapped(0.0), vec3(1.5, 0.0, 0.0));
    }

    #[test]
    fn major_axis_breaks_ties_deterministically() {
        assert_eq!(vec3(1.0, 1.0, 1.0).major_axis(), Axis::X);
        assert_eq!(vec3(0.0, 1.0, 1.0).major_axis(), Axis::Y);
        assert_eq!(vec3(0.0, 0.0, -1.0).major_axis(), Axis::Z);
    }
}
