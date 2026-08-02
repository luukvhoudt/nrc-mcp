//! Parametric primitives (§4.1).
//!
//! Every builder produces planes defined by **integer points**, and that turns out to buy more
//! than it promises. A brush's vertices are wherever three of its planes meet, and for these
//! primitives those meetings land exactly on the defining points: a prism's corners *are* the
//! rounded ring points, a cone's apex is the point it was built from. So every primitive here
//! has integer vertices, including the round-looking ones.
//!
//! Two honest caveats remain, and §4.1's "on-grid" wording covers neither:
//!
//! - **Integer is not the same as on a coarse grid.** An octagon of radius 64 has corners at
//!   ±59 and ±64, which are integers but not multiples of 8. Compilation reports the count of
//!   vertices missing the *requested authoring grid*, which is the number a mapper cares about.
//! - **CSG between two off-axis shapes can produce genuinely rational vertices.** Cutting a
//!   doorway through a wedge meets three non-axis planes at a point with a denominator. Those
//!   are reported too, and `validate` flags them.
//!
//! A caller who needs geometry strictly on a coarse grid sticks to boxes, wedges and stairs,
//! whose vertices are the grid-aligned corners they were given.

use crate::poly::{box_polytope, plane_facing_away_from, Polytope, Solid};
use nrc_core::exact::{ivec3, IPlane, IVec3};

/// Which axis a shape runs along.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
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

    pub fn parse(s: &str) -> Option<Axis> {
        match s.to_ascii_lowercase().as_str() {
            "x" => Some(Axis::X),
            "y" => Some(Axis::Y),
            "z" => Some(Axis::Z),
            _ => None,
        }
    }

    /// The two axes perpendicular to this one, in right-handed order.
    fn others(self) -> (Axis, Axis) {
        match self {
            Axis::X => (Axis::Y, Axis::Z),
            Axis::Y => (Axis::Z, Axis::X),
            Axis::Z => (Axis::X, Axis::Y),
        }
    }

    fn of(self, v: IVec3) -> i64 {
        match self {
            Axis::X => v.x,
            Axis::Y => v.y,
            Axis::Z => v.z,
        }
    }
}

fn compose(a: Axis, av: i64, b: Axis, bv: i64, c: Axis, cv: i64) -> IVec3 {
    let mut v = ivec3(0, 0, 0);
    for (axis, value) in [(a, av), (b, bv), (c, cv)] {
        match axis {
            Axis::X => v.x = value,
            Axis::Y => v.y = value,
            Axis::Z => v.z = value,
        }
    }
    v
}

pub type BuildResult = Result<Solid, String>;

fn ordered(min: IVec3, max: IVec3) -> (IVec3, IVec3) {
    (
        ivec3(min.x.min(max.x), min.y.min(max.y), min.z.min(max.z)),
        ivec3(min.x.max(max.x), min.y.max(max.y), min.z.max(max.z)),
    )
}

/// An axis-aligned box.
pub fn cuboid(min: IVec3, max: IVec3) -> BuildResult {
    let (lo, hi) = ordered(min, max);
    box_polytope(lo, hi).map(Solid::single).ok_or_else(|| {
        format!(
            "a box needs a positive extent on every axis; got {:?} to {:?}",
            (lo.x, lo.y, lo.z),
            (hi.x, hi.y, hi.z)
        )
    })
}

/// A box with one edge cut away diagonally — a ramp.
///
/// `along` is the axis the slope rises along; `up` is the axis it rises in. The low end sits at
/// the minimum of `along`.
pub fn wedge(min: IVec3, max: IVec3, along: Axis, up: Axis) -> BuildResult {
    if along == up {
        return Err(format!(
            "a wedge needs two different axes; both were {}",
            along.as_str()
        ));
    }
    let (lo, hi) = ordered(min, max);
    let base = box_polytope(lo, hi).ok_or("a wedge needs a positive extent on every axis")?;

    // The slope plane passes through the low edge at the bottom and the high edge at the top.
    let (a0, a1) = (along.of(lo), along.of(hi));
    let (u0, u1) = (up.of(lo), up.of(hi));
    let third = [Axis::X, Axis::Y, Axis::Z]
        .into_iter()
        .find(|x| *x != along && *x != up)
        .expect("three axes");
    let (t0, t1) = (third.of(lo), third.of(hi));

    let p1 = compose(along, a0, up, u0, third, t0);
    let p2 = compose(along, a0, up, u0, third, t1);
    let p3 = compose(along, a1, up, u1, third, t0);
    // An interior point strictly below the slope. It must be chosen carefully: the obvious
    // "a quarter along, a quarter up" lands exactly *on* the diagonal whenever the box's run
    // and rise are in the same ratio, which a 128x64 box is. Sitting halfway between the
    // tall end's base corner and the box centre is below the slope for any box with volume.
    let interior = compose(
        along,
        (a1 + (a0 + a1) / 2) / 2,
        up,
        (u0 + (u0 + u1) / 2) / 2,
        third,
        t0 + (t1 - t0) / 2,
    );
    let slope = plane_facing_away_from(p1, p2, p3, interior)
        // Fall back to the base corner of the tall end, which is a box vertex and therefore
        // strictly below the slope whenever the box has volume.
        .or_else(|| {
            let corner = compose(along, a1, up, u0, third, t0 + (t1 - t0) / 2);
            plane_facing_away_from(p1, p2, p3, corner)
        })
        .ok_or("the wedge slope is degenerate; check that the box has volume")?;

    let solid = base.clipped_by(slope).simplified();
    if !solid.is_solid() {
        return Err("the wedge collapsed; the box is too thin to cut".into());
    }
    Ok(Solid::single(solid))
}

/// Integer points on a circle of radius `r`, `sides` of them.
///
/// Rounded to integers so every plane is defined by integer points. `start_deg` rotates the
/// polygon, which matters for making a "cylinder" that has flat faces square to the world
/// rather than a vertex pointing at it.
fn ring(cx: i64, cy: i64, r: i64, sides: usize, start_deg: f64) -> Vec<(i64, i64)> {
    let mut pts = Vec::with_capacity(sides);
    let start = start_deg.to_radians();
    for i in 0..sides {
        let t = start + std::f64::consts::TAU * (i as f64) / (sides as f64);
        pts.push((
            cx + (r as f64 * t.cos()).round() as i64,
            cy + (r as f64 * t.sin()).round() as i64,
        ));
    }
    // Rounding collapses neighbours at small radii. Deduplicate globally, not just
    // consecutively, because the wrap-around pair can coincide too.
    let mut distinct: Vec<(i64, i64)> = Vec::with_capacity(pts.len());
    for p in pts {
        if !distinct.contains(&p) {
            distinct.push(p);
        }
    }
    distinct
}

/// Explain a collapsed ring rather than quietly returning a coarser shape.
///
/// Silently building an octagon when twelve sides were asked for is worse than refusing: the
/// caller would go on believing the geometry matches the parameters it recorded.
fn check_ring(pts: &[(i64, i64)], sides: usize, r: i64) -> Result<(), String> {
    if pts.len() == sides {
        return Ok(());
    }
    Err(format!(
        "a radius of {r} can only carry {} distinct corners, not {sides} — at this size the \
         rounded corners round onto each other. Use fewer sides or a larger cross-section.",
        pts.len()
    ))
}

/// An `n`-sided prism: a regular polygon extruded along `axis`.
///
/// This is what Quake calls a cylinder — the engine has no curved surfaces, so a cylinder *is*
/// a prism, and the side count is the only control over how round it looks.
pub fn prism(min: IVec3, max: IVec3, axis: Axis, sides: usize, start_deg: f64) -> BuildResult {
    if sides < 3 {
        return Err(format!("a prism needs at least 3 sides, got {sides}"));
    }
    if sides > 64 {
        return Err(format!(
            "{sides} sides is more than any brush-based renderer benefits from; 8 to 16 is \
             usual, and above 32 the extra faces cost more than they show"
        ));
    }
    let (lo, hi) = ordered(min, max);
    let (a, b) = axis.others();
    let (a0, a1) = (a.of(lo), a.of(hi));
    let (b0, b1) = (b.of(lo), b.of(hi));
    let (h0, h1) = (axis.of(lo), axis.of(hi));
    if a1 <= a0 || b1 <= b0 || h1 <= h0 {
        return Err("a prism needs a positive extent on every axis".into());
    }

    // Inscribe the polygon in the cross-section. Using the smaller half-extent keeps it inside
    // the requested bounds rather than bulging out of them.
    let cx = (a0 + a1) / 2;
    let cy = (b0 + b1) / 2;
    let r = (((a1 - a0) / 2).min((b1 - b0) / 2)).max(1);
    let pts = ring(cx, cy, r, sides, start_deg);
    check_ring(&pts, sides, r)?;

    let centre = compose(a, cx, b, cy, axis, (h0 + h1) / 2);
    let mut planes = vec![
        // Caps.
        axis_plane(axis, h1, true),
        axis_plane(axis, h0, false),
    ];
    for i in 0..pts.len() {
        let (x0, y0) = pts[i];
        let (x1, y1) = pts[(i + 1) % pts.len()];
        let p1 = compose(a, x0, b, y0, axis, h0);
        let p2 = compose(a, x1, b, y1, axis, h0);
        let p3 = compose(a, x0, b, y0, axis, h1);
        match plane_facing_away_from(p1, p2, p3, centre) {
            Some(pl) => planes.push(pl),
            // Two rounded points coincided; skipping the face keeps the solid convex and the
            // shape barely differs, but it is worth not pretending it was exact.
            None => continue,
        }
    }
    let solid = Polytope::from_planes(planes).simplified();
    if !solid.is_solid() {
        return Err("the prism collapsed; check the radius against the side count".into());
    }
    Ok(Solid::single(solid))
}

fn axis_plane(axis: Axis, d: i64, positive: bool) -> IPlane {
    let (nx, ny, nz) = match axis {
        Axis::X => (1i128, 0, 0),
        Axis::Y => (0, 1i128, 0),
        Axis::Z => (0, 0, 1i128),
    };
    if positive {
        IPlane {
            nx,
            ny,
            nz,
            d: d as i128,
        }
    } else {
        IPlane {
            nx: -nx,
            ny: -ny,
            nz: -nz,
            d: -(d as i128),
        }
    }
}

/// A cone or pyramid: a polygon base tapering to a point.
pub fn cone(min: IVec3, max: IVec3, axis: Axis, sides: usize, start_deg: f64) -> BuildResult {
    if sides < 3 {
        return Err(format!("a cone needs at least 3 base sides, got {sides}"));
    }
    let (lo, hi) = ordered(min, max);
    let (a, b) = axis.others();
    let (a0, a1) = (a.of(lo), a.of(hi));
    let (b0, b1) = (b.of(lo), b.of(hi));
    let (h0, h1) = (axis.of(lo), axis.of(hi));
    if a1 <= a0 || b1 <= b0 || h1 <= h0 {
        return Err("a cone needs a positive extent on every axis".into());
    }

    let cx = (a0 + a1) / 2;
    let cy = (b0 + b1) / 2;
    let r = (((a1 - a0) / 2).min((b1 - b0) / 2)).max(1);
    let pts = ring(cx, cy, r, sides, start_deg);
    check_ring(&pts, sides, r)?;

    let apex = compose(a, cx, b, cy, axis, h1);
    let interior = compose(a, cx, b, cy, axis, h0 + (h1 - h0) / 4);
    let mut planes = vec![axis_plane(axis, h0, false)];
    for i in 0..pts.len() {
        let (x0, y0) = pts[i];
        let (x1, y1) = pts[(i + 1) % pts.len()];
        let p1 = compose(a, x0, b, y0, axis, h0);
        let p2 = compose(a, x1, b, y1, axis, h0);
        if let Some(pl) = plane_facing_away_from(p1, p2, apex, interior) {
            planes.push(pl);
        }
    }
    let solid = Polytope::from_planes(planes).simplified();
    if !solid.is_solid() {
        return Err("the cone collapsed".into());
    }
    Ok(Solid::single(solid))
}

/// A pyramid — a four-sided cone, squared to the world.
pub fn pyramid(min: IVec3, max: IVec3, axis: Axis) -> BuildResult {
    cone(min, max, axis, 4, 45.0)
}

/// A flight of stairs as one box per step.
///
/// Separate boxes rather than a single stepped solid because a stepped shape is not convex, and
/// because that is how a mapper builds stairs: each step is its own brush, so each can be
/// retextured or turned into detail independently.
///
/// `rise` must not exceed the game's step height or the player cannot walk up it — the caller
/// checks that against the profile, since it is a game-specific constant.
pub fn stair(
    base_min: IVec3,
    width: i64,
    steps: usize,
    rise: i64,
    run: i64,
    along: Axis,
    up: Axis,
) -> BuildResult {
    if steps == 0 || steps > 256 {
        return Err(format!(
            "a stair needs between 1 and 256 steps, got {steps}"
        ));
    }
    if rise <= 0 || run <= 0 || width <= 0 {
        return Err(format!(
            "rise, run and width must all be positive; got rise {rise}, run {run}, width {width}"
        ));
    }
    if along == up {
        return Err("a stair needs different axes to run along and rise in".into());
    }
    let third = [Axis::X, Axis::Y, Axis::Z]
        .into_iter()
        .find(|x| *x != along && *x != up)
        .expect("three axes");

    let a0 = along.of(base_min);
    let u0 = up.of(base_min);
    let t0 = third.of(base_min);

    let mut parts = Vec::with_capacity(steps);
    for i in 0..steps {
        let i = i as i64;
        // Each step is a solid block from the base up, so the stair is walkable rather than
        // floating treads — matching how stairs are actually built.
        let lo = compose(along, a0 + i * run, up, u0, third, t0);
        let hi = compose(
            along,
            a0 + (i + 1) * run,
            up,
            u0 + (i + 1) * rise,
            third,
            t0 + width,
        );
        let (lo, hi) = ordered(lo, hi);
        parts.push(box_polytope(lo, hi).ok_or("a stair step collapsed")?);
    }
    Ok(Solid::new(parts))
}

/// A hollow tube: an outer prism with an inner one removed.
pub fn pipe(
    min: IVec3,
    max: IVec3,
    axis: Axis,
    wall: i64,
    sides: usize,
    start_deg: f64,
) -> BuildResult {
    if wall <= 0 {
        return Err(format!("pipe wall thickness must be positive, got {wall}"));
    }
    let (lo, hi) = ordered(min, max);
    let (a, b) = axis.others();
    // Check the wall against the cross-section before building anything. Without this, a wall
    // thicker than the radius shrinks the bore past the centre, `ordered` flips it back into a
    // valid small box, and the pipe comes out looking fine while being nothing of the kind.
    let across = (a.of(hi) - a.of(lo)).min(b.of(hi) - b.of(lo));
    if 2 * wall >= across {
        return Err(format!(
            "a wall of {wall} leaves no bore in a cross-section {across} units across; the wall \
             must be less than half of that"
        ));
    }
    let outer = prism(min, max, axis, sides, start_deg)?;
    let shrink = |v: IVec3, sign: i64| -> IVec3 {
        let mut out = v;
        for ax in [a, b] {
            match ax {
                Axis::X => out.x += sign * wall,
                Axis::Y => out.y += sign * wall,
                Axis::Z => out.z += sign * wall,
            }
        }
        out
    };
    // Extend the cutter past the caps so it cuts cleanly through rather than leaving a film.
    let mut inner_lo = shrink(lo, 1);
    let mut inner_hi = shrink(hi, -1);
    match axis {
        Axis::X => {
            inner_lo.x -= 1;
            inner_hi.x += 1;
        }
        Axis::Y => {
            inner_lo.y -= 1;
            inner_hi.y += 1;
        }
        Axis::Z => {
            inner_lo.z -= 1;
            inner_hi.z += 1;
        }
    }
    let inner = prism(inner_lo, inner_hi, axis, sides, start_deg)
        .map_err(|e| format!("a wall of {wall} leaves no bore: {e}"))?;
    let result = crate::csg::subtract(&outer, &inner);
    if result.is_empty() {
        return Err(format!("a wall of {wall} consumed the whole pipe"));
    }
    Ok(result)
}

/// An arch: a half-ring of trapezoid blocks spanning 180°.
///
/// Built as `segments` separate convex blocks rather than as one shape, because a ring is not
/// convex. Each block spans an angular slice between the inner and outer radius.
pub fn arch(
    centre: IVec3,
    outer_radius: i64,
    thickness: i64,
    depth: i64,
    segments: usize,
    axis: Axis,
) -> BuildResult {
    if outer_radius <= 0 || thickness <= 0 || depth <= 0 {
        return Err("arch radius, thickness and depth must all be positive".into());
    }
    if thickness >= outer_radius {
        return Err(format!(
            "a thickness of {thickness} is not less than the outer radius of {outer_radius}, so \
             there is no opening"
        ));
    }
    if !(2..=64).contains(&segments) {
        return Err(format!(
            "an arch needs between 2 and 64 segments, got {segments}"
        ));
    }

    let (a, b) = axis.others();
    let inner_radius = outer_radius - thickness;
    let d0 = axis.of(centre);
    let ca = a.of(centre);
    let cb = b.of(centre);

    let mut parts = Vec::with_capacity(segments);
    for i in 0..segments {
        let t0 = std::f64::consts::PI * (i as f64) / (segments as f64);
        let t1 = std::f64::consts::PI * ((i + 1) as f64) / (segments as f64);
        let pt = |r: i64, t: f64| -> (i64, i64) {
            (
                ca + (r as f64 * t.cos()).round() as i64,
                cb + (r as f64 * t.sin()).round() as i64,
            )
        };
        let (o0x, o0y) = pt(outer_radius, t0);
        let (o1x, o1y) = pt(outer_radius, t1);
        let (i0x, i0y) = pt(inner_radius, t0);
        let (i1x, i1y) = pt(inner_radius, t1);

        // A trapezoid in the plane, extruded along `axis`.
        let quad = [(o0x, o0y), (o1x, o1y), (i1x, i1y), (i0x, i0y)];
        let cx = quad.iter().map(|p| p.0).sum::<i64>() / 4;
        let cy = quad.iter().map(|p| p.1).sum::<i64>() / 4;
        let interior = compose(a, cx, b, cy, axis, d0 + depth / 2);

        let mut planes = vec![
            axis_plane(axis, d0 + depth, true),
            axis_plane(axis, d0, false),
        ];
        let mut ok = true;
        for k in 0..4 {
            let (x0, y0) = quad[k];
            let (x1, y1) = quad[(k + 1) % 4];
            let p1 = compose(a, x0, b, y0, axis, d0);
            let p2 = compose(a, x1, b, y1, axis, d0);
            let p3 = compose(a, x0, b, y0, axis, d0 + depth);
            match plane_facing_away_from(p1, p2, p3, interior) {
                Some(pl) => planes.push(pl),
                None => {
                    ok = false;
                    break;
                }
            }
        }
        if !ok {
            continue;
        }
        let block = Polytope::from_planes(planes).simplified();
        if block.is_solid() {
            parts.push(block);
        }
    }

    if parts.len() < 2 {
        return Err(format!(
            "only {} of {segments} arch segments survived — the radius is too small for that \
             many segments, so the rounded corners collapse onto each other",
            parts.len()
        ));
    }
    Ok(Solid::new(parts))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_cuboid_is_one_six_sided_brush() {
        let s = cuboid(ivec3(0, 0, 0), ivec3(64, 128, 32)).unwrap();
        assert_eq!(s.len(), 1);
        assert_eq!(s.parts[0].len(), 6);
        assert!((s.volume() - 64.0 * 128.0 * 32.0).abs() < 1e-6);
    }

    #[test]
    fn a_cuboid_accepts_its_corners_in_any_order() {
        let a = cuboid(ivec3(0, 0, 0), ivec3(64, 64, 64)).unwrap();
        let b = cuboid(ivec3(64, 64, 64), ivec3(0, 0, 0)).unwrap();
        assert_eq!(a.parts[0], b.parts[0]);
    }

    #[test]
    fn a_flat_cuboid_is_refused_with_a_reason() {
        let e = cuboid(ivec3(0, 0, 0), ivec3(64, 64, 0)).unwrap_err();
        assert!(e.contains("positive extent"), "{e}");
    }

    #[test]
    fn a_wedge_is_a_box_with_a_corner_removed() {
        let full = cuboid(ivec3(0, 0, 0), ivec3(128, 64, 64)).unwrap();
        let w = wedge(ivec3(0, 0, 0), ivec3(128, 64, 64), Axis::X, Axis::Z).unwrap();
        assert_eq!(w.len(), 1);
        assert_eq!(w.parts[0].len(), 5, "a ramp has five faces");
        // Half the volume, and the high corner is gone while the low one remains.
        assert!(
            (w.volume() - full.volume() / 2.0).abs() < 1.0,
            "{}",
            w.volume()
        );
        assert!(
            w.parts[0].contains(ivec3(120, 32, 32)),
            "the high end should be solid"
        );
        assert!(
            !w.parts[0].contains(ivec3(8, 32, 60)),
            "the low end should be cut away"
        );
    }

    #[test]
    fn a_wedge_needs_two_different_axes() {
        let e = wedge(ivec3(0, 0, 0), ivec3(64, 64, 64), Axis::X, Axis::X).unwrap_err();
        assert!(e.contains("two different axes"), "{e}");
    }

    #[test]
    fn an_eight_sided_prism_has_ten_faces() {
        let s = prism(ivec3(-64, -64, 0), ivec3(64, 64, 128), Axis::Z, 8, 22.5).unwrap();
        assert_eq!(s.len(), 1);
        assert_eq!(s.parts[0].len(), 10, "eight sides plus two caps");
        assert!(s.parts[0].is_solid());
        // Inscribed, so it must stay within the requested cross-section.
        let b = s.parts[0].bounds();
        assert!(b.min.x >= -64.0 && b.max.x <= 64.0, "{b:?}");
        assert!(b.min.z == 0.0 && b.max.z == 128.0);
    }

    #[test]
    fn prisms_can_run_along_any_axis() {
        for axis in [Axis::X, Axis::Y, Axis::Z] {
            let s = prism(ivec3(-32, -32, -32), ivec3(32, 32, 32), axis, 6, 0.0).unwrap();
            assert_eq!(s.parts[0].len(), 8, "{} failed", axis.as_str());
            assert!(s.parts[0].is_solid());
        }
    }

    #[test]
    fn prism_side_counts_are_bounded_with_an_explanation() {
        let e = prism(ivec3(0, 0, 0), ivec3(64, 64, 64), Axis::Z, 2, 0.0).unwrap_err();
        assert!(e.contains("at least 3"), "{e}");
        let e = prism(ivec3(0, 0, 0), ivec3(64, 64, 64), Axis::Z, 200, 0.0).unwrap_err();
        assert!(e.contains("cost more than they show"), "{e}");
    }

    #[test]
    fn a_prism_too_small_for_its_side_count_says_so() {
        // A radius of 2 cannot carry 32 distinct rounded corners.
        let e = prism(ivec3(0, 0, 0), ivec3(4, 4, 64), Axis::Z, 32, 0.0).unwrap_err();
        assert!(e.contains("round onto each other"), "{e}");
    }

    #[test]
    fn a_cone_tapers_to_a_point() {
        let s = cone(ivec3(-64, -64, 0), ivec3(64, 64, 128), Axis::Z, 8, 0.0).unwrap();
        assert!(s.parts[0].is_solid());
        assert!(s.parts[0].contains(ivec3(0, 0, 8)), "wide at the base");
        assert!(
            !s.parts[0].contains(ivec3(60, 60, 120)),
            "narrow at the tip"
        );
        // A cone is a third of its bounding prism, give or take the rounding.
        let prism_vol = prism(ivec3(-64, -64, 0), ivec3(64, 64, 128), Axis::Z, 8, 0.0)
            .unwrap()
            .volume();
        let ratio = s.volume() / prism_vol;
        assert!((0.28..0.40).contains(&ratio), "ratio was {ratio}");
    }

    #[test]
    fn a_pyramid_has_five_faces() {
        let s = pyramid(ivec3(-32, -32, 0), ivec3(32, 32, 64), Axis::Z).unwrap();
        assert_eq!(s.parts[0].len(), 5);
        assert!(s.parts[0].is_solid());
    }

    #[test]
    fn a_stair_is_one_box_per_step_rising_evenly() {
        let s = stair(ivec3(0, 0, 0), 128, 8, 16, 32, Axis::X, Axis::Z).unwrap();
        assert_eq!(s.len(), 8);
        let b = s.bounds();
        assert_eq!(b.max.x, 8.0 * 32.0);
        assert_eq!(b.max.z, 8.0 * 16.0);
        assert_eq!(b.max.y, 128.0);
        // Walkable: each step is solid from the base up, not a floating tread.
        assert!(s.contains(ivec3(16, 64, 4)));
        assert!(s.contains(ivec3(240, 64, 4)));
        // And the space above the last step is open.
        assert!(!s.contains(ivec3(240, 64, 200)));
    }

    #[test]
    fn stair_parameters_are_validated() {
        for (steps, rise, run, expect) in [
            (0usize, 16i64, 32i64, "between 1 and 256"),
            (8, 0, 32, "must all be positive"),
            (8, 16, -4, "must all be positive"),
        ] {
            let e = stair(ivec3(0, 0, 0), 64, steps, rise, run, Axis::X, Axis::Z).unwrap_err();
            assert!(e.contains(expect), "for {steps}/{rise}/{run}: {e}");
        }
    }

    #[test]
    fn a_pipe_is_hollow_along_its_axis() {
        let s = pipe(ivec3(-64, -64, 0), ivec3(64, 64, 256), Axis::Z, 16, 8, 22.5).unwrap();
        assert!(
            s.len() >= 4,
            "a tube wall needs several convex pieces, got {}",
            s.len()
        );
        assert!(!s.contains(ivec3(0, 0, 128)), "the bore should be open");
        assert!(s.contains(ivec3(58, 0, 128)), "the wall should be solid");
    }

    #[test]
    fn a_pipe_wall_thicker_than_its_radius_is_refused() {
        let e = pipe(ivec3(-32, -32, 0), ivec3(32, 32, 64), Axis::Z, 40, 8, 0.0).unwrap_err();
        assert!(e.contains("no bore") || e.contains("whole pipe"), "{e}");
    }

    #[test]
    fn an_arch_spans_a_half_circle_in_convex_blocks() {
        let s = arch(ivec3(0, 0, 0), 128, 32, 64, 6, Axis::Z).unwrap();
        assert_eq!(s.len(), 6);
        for p in &s.parts {
            assert!(p.is_solid());
            assert_eq!(p.len(), 6, "each block is a trapezoid prism");
        }
        // Solid in the ring, open under the arch and outside it.
        assert!(s.contains(ivec3(0, 112, 32)), "the crown should be solid");
        assert!(
            !s.contains(ivec3(0, 64, 32)),
            "under the arch should be open"
        );
        assert!(!s.contains(ivec3(0, 200, 32)), "outside should be open");
    }

    #[test]
    fn an_arch_with_no_opening_is_refused() {
        let e = arch(ivec3(0, 0, 0), 64, 64, 32, 6, Axis::Z).unwrap_err();
        assert!(e.contains("no opening"), "{e}");
    }

    #[test]
    fn an_arch_too_small_for_its_segment_count_explains_itself() {
        let e = arch(ivec3(0, 0, 0), 4, 2, 16, 48, Axis::Z).unwrap_err();
        assert!(e.contains("collapse onto each other"), "{e}");
    }

    #[test]
    fn every_primitive_defines_its_planes_from_integer_points() {
        // The guarantee this module actually makes. Derived vertices may be off-grid for
        // angled shapes — that is a property of the format — but planes always come from
        // integers, which is what keeps the .map exact.
        let builds = [
            cuboid(ivec3(0, 0, 0), ivec3(64, 64, 64)),
            wedge(ivec3(0, 0, 0), ivec3(64, 64, 64), Axis::X, Axis::Z),
            prism(ivec3(-64, -64, 0), ivec3(64, 64, 64), Axis::Z, 12, 0.0),
            cone(ivec3(-64, -64, 0), ivec3(64, 64, 64), Axis::Z, 8, 0.0),
            stair(ivec3(0, 0, 0), 64, 4, 16, 32, Axis::X, Axis::Z),
            arch(ivec3(0, 0, 0), 128, 32, 64, 8, Axis::Z),
        ];
        for b in builds {
            let s = b.expect("should build");
            for part in &s.parts {
                assert!(part.is_solid());
                // An integer plane is exactly what IPlane represents, so reaching here at all
                // proves the property; also confirm none degenerated.
                assert!(part.len() >= 4);
            }
        }
    }

    #[test]
    fn axis_parsing_accepts_what_a_caller_would_write() {
        assert_eq!(Axis::parse("z"), Some(Axis::Z));
        assert_eq!(Axis::parse("Z"), Some(Axis::Z));
        assert_eq!(Axis::parse("w"), None);
    }
}
