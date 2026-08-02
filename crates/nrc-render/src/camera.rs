//! Projections: orthographic axis views and a perspective camera.
//!
//! Both expose the same two-step pipeline — world to *view* space, then view to screen —
//! because the renderer has to clip against the near plane in between. Projecting first and
//! clipping afterwards is what makes a camera standing inside a room render nothing, and
//! standing inside a room is precisely the player-eye view §4.2 asks for.

use nrc_core::math::{vec3, Aabb, Vec3};

/// Which pair of world axes an orthographic view shows.
///
/// Named for what the viewer sees, matching Radiant: the top view is the XY plane, the front
/// view is XZ, the side view is YZ.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OrthoAxis {
    /// Looking down: +X right, +Y up the screen.
    Top,
    /// Looking north: +X right, +Z up the screen.
    Front,
    /// Looking east: +Y right, +Z up the screen.
    Side,
}

impl OrthoAxis {
    pub fn as_str(self) -> &'static str {
        match self {
            OrthoAxis::Top => "top",
            OrthoAxis::Front => "front",
            OrthoAxis::Side => "side",
        }
    }

    /// Labels for the horizontal and vertical screen axes.
    pub fn axis_labels(self) -> (&'static str, &'static str) {
        match self {
            OrthoAxis::Top => ("X", "Y"),
            OrthoAxis::Front => ("X", "Z"),
            OrthoAxis::Side => ("Y", "Z"),
        }
    }

    /// World coordinates as (horizontal, vertical, depth). Depth increases away from the
    /// viewer, so the sign choices here are what make the depth test pick the near face.
    fn split(self, p: Vec3) -> Vec3 {
        match self {
            OrthoAxis::Top => vec3(p.x, p.y, -p.z),
            OrthoAxis::Front => vec3(p.x, p.z, p.y),
            OrthoAxis::Side => vec3(p.y, p.z, -p.x),
        }
    }
}

/// A camera that can turn world points into screen points.
#[derive(Clone, Debug)]
pub enum Camera {
    Ortho {
        axis: OrthoAxis,
        /// Pixels per world unit.
        scale: f64,
        /// World-space (horizontal, vertical) at the viewport centre.
        centre: (f64, f64),
        width: f64,
        height: f64,
    },
    Perspective {
        eye: Vec3,
        right: Vec3,
        up: Vec3,
        forward: Vec3,
        /// Focal length in pixels.
        focal: f64,
        width: f64,
        height: f64,
        near: f64,
    },
}

impl Camera {
    /// An orthographic camera framing `bounds`, inset by `padding` pixels.
    ///
    /// Degenerate bounds (a flat or empty map) still produce a usable camera rather than a
    /// division by zero — a single brush on a plane is a legitimate thing to look at.
    pub fn fit_ortho(
        axis: OrthoAxis,
        bounds: Aabb,
        width: u32,
        height: u32,
        padding: f64,
    ) -> Camera {
        let (w, h) = (width as f64, height as f64);
        let usable_w = (w - 2.0 * padding).max(1.0);
        let usable_h = (h - 2.0 * padding).max(1.0);

        let (lo, hi) = if bounds.is_empty() {
            (vec3(-64.0, -64.0, -64.0), vec3(64.0, 64.0, 64.0))
        } else {
            (bounds.min, bounds.max)
        };
        let a = axis.split(lo);
        let b = axis.split(hi);
        let (h0, h1) = (a.x.min(b.x), a.x.max(b.x));
        let (v0, v1) = (a.y.min(b.y), a.y.max(b.y));

        let span_h = (h1 - h0).max(1.0);
        let span_v = (v1 - v0).max(1.0);
        let scale = (usable_w / span_h).min(usable_h / span_v);

        Camera::Ortho {
            axis,
            scale,
            centre: ((h0 + h1) * 0.5, (v0 + v1) * 0.5),
            width: w,
            height: h,
        }
    }

    /// A perspective camera looking from `eye` at `target`.
    pub fn look_at(
        eye: Vec3,
        target: Vec3,
        fov_deg: f64,
        width: u32,
        height: u32,
        near: f64,
    ) -> Camera {
        let forward = (target - eye)
            .normalized()
            .unwrap_or_else(|| vec3(1.0, 0.0, 0.0));
        // World up is +Z. When the camera looks straight up or down that degenerates, so
        // fall back to a different reference rather than producing NaNs.
        let world_up = vec3(0.0, 0.0, 1.0);
        let right = forward
            .cross(world_up)
            .normalized()
            .or_else(|| forward.cross(vec3(0.0, 1.0, 0.0)).normalized())
            .unwrap_or_else(|| vec3(0.0, 1.0, 0.0));
        let up = right.cross(forward);

        let fov = fov_deg.clamp(1.0, 179.0).to_radians();
        let focal = (height as f64 * 0.5) / (fov * 0.5).tan();

        Camera::Perspective {
            eye,
            right,
            up,
            forward,
            focal,
            width: width as f64,
            height: height as f64,
            near: near.max(0.01),
        }
    }

    /// A perspective camera placed outside `bounds`, looking at its centre.
    ///
    /// The distance is derived from the bounding sphere and the field of view, so the whole
    /// map lands in frame whatever its proportions.
    pub fn frame(bounds: Aabb, fov_deg: f64, width: u32, height: u32) -> Camera {
        let (centre, radius) = if bounds.is_empty() {
            (Vec3::ZERO, 128.0)
        } else {
            (bounds.center(), bounds.size().length() * 0.5)
        };
        let fov = fov_deg.clamp(1.0, 179.0).to_radians();
        let dist = (radius / (fov * 0.5).sin()).max(radius + 16.0);
        // Down one of the diagonals and above: the standard three-quarter view, which shows
        // three faces of an axis-aligned box instead of one.
        let dir = vec3(-0.55, -0.55, 0.42)
            .normalized()
            .unwrap_or_else(|| vec3(-1.0, 0.0, 0.0));
        Camera::look_at(centre - dir * dist, centre, fov_deg, width, height, 1.0)
    }

    /// World point to view space. The `z` component is depth away from the viewer.
    pub fn to_view(&self, p: Vec3) -> Vec3 {
        match self {
            Camera::Ortho { axis, .. } => axis.split(p),
            Camera::Perspective {
                eye,
                right,
                up,
                forward,
                ..
            } => {
                let d = p - *eye;
                vec3(d.dot(*right), d.dot(*up), d.dot(*forward))
            }
        }
    }

    /// Minimum view-space depth that can be projected.
    pub fn near(&self) -> f64 {
        match self {
            // Orthographic projection has no vanishing point, so nothing needs clipping.
            Camera::Ortho { .. } => f64::NEG_INFINITY,
            Camera::Perspective { near, .. } => *near,
        }
    }

    /// View space to screen. Callers must clip to `near()` first.
    pub fn project_view(&self, v: Vec3) -> (f64, f64, f32) {
        match self {
            Camera::Ortho {
                scale,
                centre,
                width,
                height,
                ..
            } => (
                width * 0.5 + (v.x - centre.0) * scale,
                // Screen y grows downward; world vertical grows upward.
                height * 0.5 - (v.y - centre.1) * scale,
                v.z as f32,
            ),
            Camera::Perspective {
                focal,
                width,
                height,
                ..
            } => {
                let inv = focal / v.z;
                (
                    width * 0.5 + v.x * inv,
                    height * 0.5 - v.y * inv,
                    v.z as f32,
                )
            }
        }
    }

    /// World point straight to screen, or `None` if it is nearer than the near plane.
    pub fn project(&self, p: Vec3) -> Option<(f64, f64, f32)> {
        let v = self.to_view(p);
        if v.z < self.near() {
            None
        } else {
            Some(self.project_view(v))
        }
    }

    /// Pixels per world unit, for orthographic views only.
    pub fn pixels_per_unit(&self) -> Option<f64> {
        match self {
            Camera::Ortho { scale, .. } => Some(*scale),
            Camera::Perspective { .. } => None,
        }
    }

    /// The world-space rectangle an orthographic view covers, as
    /// `(h_min, h_max, v_min, v_max)`. Used to lay out grid lines and axis ticks.
    pub fn ortho_extent(&self) -> Option<(f64, f64, f64, f64)> {
        match self {
            Camera::Ortho {
                scale,
                centre,
                width,
                height,
                ..
            } => {
                let hw = width * 0.5 / scale;
                let hh = height * 0.5 / scale;
                Some((centre.0 - hw, centre.0 + hw, centre.1 - hh, centre.1 + hh))
            }
            Camera::Perspective { .. } => None,
        }
    }

    /// Screen position of a world-space horizontal coordinate, for an orthographic view.
    pub fn ortho_screen_h(&self, h: f64) -> Option<f64> {
        match self {
            Camera::Ortho {
                scale,
                centre,
                width,
                ..
            } => Some(width * 0.5 + (h - centre.0) * scale),
            _ => None,
        }
    }

    /// Screen position of a world-space vertical coordinate, for an orthographic view.
    pub fn ortho_screen_v(&self, v: f64) -> Option<f64> {
        match self {
            Camera::Ortho {
                scale,
                centre,
                height,
                ..
            } => Some(height * 0.5 - (v - centre.1) * scale),
            _ => None,
        }
    }
}

/// Clip a convex polygon in view space to `z >= near` (Sutherland–Hodgman).
///
/// Without this a camera inside a room renders nothing at all, because every face has at
/// least one vertex behind the eye and a whole-polygon reject throws the room away. With it,
/// the polygon is cut against the near plane and the visible part survives.
pub fn clip_near(poly: &[Vec3], near: f64) -> Vec<Vec3> {
    if !near.is_finite() {
        return poly.to_vec();
    }
    if poly.len() < 3 {
        return Vec::new();
    }
    let mut out: Vec<Vec3> = Vec::with_capacity(poly.len() + 2);
    for i in 0..poly.len() {
        let a = poly[i];
        let b = poly[(i + 1) % poly.len()];
        let a_in = a.z >= near;
        let b_in = b.z >= near;
        if a_in {
            out.push(a);
        }
        if a_in != b_in {
            let t = (near - a.z) / (b.z - a.z);
            out.push(a + (b - a) * t);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn box_bounds(min: Vec3, max: Vec3) -> Aabb {
        let mut b = Aabb::EMPTY;
        b.extend(min);
        b.extend(max);
        b
    }

    #[test]
    fn ortho_top_puts_plus_y_up_the_screen() {
        let b = box_bounds(vec3(-100.0, -100.0, 0.0), vec3(100.0, 100.0, 50.0));
        let cam = Camera::fit_ortho(OrthoAxis::Top, b, 200, 200, 0.0);
        let north = cam.project(vec3(0.0, 100.0, 0.0)).unwrap();
        let south = cam.project(vec3(0.0, -100.0, 0.0)).unwrap();
        assert!(north.1 < south.1, "+Y must render above -Y");
        let east = cam.project(vec3(100.0, 0.0, 0.0)).unwrap();
        let west = cam.project(vec3(-100.0, 0.0, 0.0)).unwrap();
        assert!(east.0 > west.0, "+X must render right of -X");
    }

    #[test]
    fn ortho_top_depth_prefers_higher_z() {
        let b = box_bounds(vec3(0.0, 0.0, 0.0), vec3(64.0, 64.0, 64.0));
        let cam = Camera::fit_ortho(OrthoAxis::Top, b, 100, 100, 0.0);
        let high = cam.project(vec3(0.0, 0.0, 64.0)).unwrap().2;
        let low = cam.project(vec3(0.0, 0.0, 0.0)).unwrap().2;
        assert!(high < low, "looking down, a higher surface must be nearer");
    }

    #[test]
    fn front_and_side_views_put_z_up() {
        let b = box_bounds(vec3(0.0, 0.0, 0.0), vec3(64.0, 64.0, 64.0));
        for axis in [OrthoAxis::Front, OrthoAxis::Side] {
            let cam = Camera::fit_ortho(axis, b, 100, 100, 0.0);
            let top = cam.project(vec3(32.0, 32.0, 64.0)).unwrap();
            let bottom = cam.project(vec3(32.0, 32.0, 0.0)).unwrap();
            assert!(top.1 < bottom.1, "{}: +Z must be up", axis.as_str());
        }
    }

    #[test]
    fn fit_centres_the_bounds_and_respects_padding() {
        let b = box_bounds(vec3(-512.0, -512.0, 0.0), vec3(512.0, 512.0, 128.0));
        let cam = Camera::fit_ortho(OrthoAxis::Top, b, 400, 400, 20.0);
        let c = cam.project(vec3(0.0, 0.0, 0.0)).unwrap();
        assert!((c.0 - 200.0).abs() < 1e-6, "centre should map to centre");
        assert!((c.1 - 200.0).abs() < 1e-6);
        // The extreme corner must land inside the padded area.
        let corner = cam.project(vec3(512.0, 512.0, 0.0)).unwrap();
        assert!(corner.0 <= 380.0 + 1e-6, "got {}", corner.0);
        assert!(corner.1 >= 20.0 - 1e-6, "got {}", corner.1);
    }

    #[test]
    fn degenerate_bounds_still_yield_a_usable_camera() {
        // A flat map, and an empty one. Both must project finite numbers.
        let flat = box_bounds(vec3(0.0, 0.0, 8.0), vec3(64.0, 64.0, 8.0));
        let cam = Camera::fit_ortho(OrthoAxis::Front, flat, 100, 100, 4.0);
        let p = cam.project(vec3(32.0, 32.0, 8.0)).unwrap();
        assert!(p.0.is_finite() && p.1.is_finite());

        let cam = Camera::fit_ortho(OrthoAxis::Top, Aabb::EMPTY, 100, 100, 4.0);
        let p = cam.project(Vec3::ZERO).unwrap();
        assert!(p.0.is_finite() && p.1.is_finite());
        assert!(cam.pixels_per_unit().unwrap() > 0.0);
    }

    #[test]
    fn perspective_makes_distant_things_smaller() {
        let cam = Camera::look_at(Vec3::ZERO, vec3(1.0, 0.0, 0.0), 90.0, 200, 200, 1.0);
        let near = cam.project(vec3(100.0, 20.0, 0.0)).unwrap();
        let far = cam.project(vec3(400.0, 20.0, 0.0)).unwrap();
        let near_off = (near.0 - 100.0).abs();
        let far_off = (far.0 - 100.0).abs();
        assert!(
            far_off < near_off,
            "the far point must be closer to the centre"
        );
    }

    #[test]
    fn points_behind_the_camera_do_not_project() {
        let cam = Camera::look_at(Vec3::ZERO, vec3(1.0, 0.0, 0.0), 90.0, 100, 100, 1.0);
        assert!(cam.project(vec3(-50.0, 0.0, 0.0)).is_none());
        assert!(cam.project(vec3(50.0, 0.0, 0.0)).is_some());
    }

    #[test]
    fn a_camera_looking_straight_down_does_not_produce_nans() {
        // forward parallel to world up degenerates the usual right-vector construction.
        let cam = Camera::look_at(vec3(0.0, 0.0, 256.0), Vec3::ZERO, 90.0, 100, 100, 1.0);
        let p = cam.project(vec3(10.0, 10.0, 0.0)).unwrap();
        assert!(p.0.is_finite() && p.1.is_finite(), "got {p:?}");
    }

    #[test]
    fn frame_places_the_whole_box_in_view() {
        let b = box_bounds(vec3(-256.0, -256.0, 0.0), vec3(256.0, 256.0, 256.0));
        let cam = Camera::frame(b, 60.0, 400, 300);
        for corner in [
            vec3(-256.0, -256.0, 0.0),
            vec3(256.0, 256.0, 256.0),
            vec3(-256.0, 256.0, 0.0),
            vec3(256.0, -256.0, 256.0),
        ] {
            let p = cam.project(corner).expect("corner should be in front");
            assert!(p.0 >= -1.0 && p.0 <= 401.0, "x out of frame: {}", p.0);
            assert!(p.1 >= -1.0 && p.1 <= 301.0, "y out of frame: {}", p.1);
        }
    }

    #[test]
    fn near_clipping_keeps_the_visible_part_of_a_straddling_polygon() {
        // A quad half behind the eye. A whole-polygon reject would drop it entirely, which
        // is why a camera inside a room used to render nothing.
        let poly = vec![
            vec3(-10.0, -10.0, -5.0),
            vec3(10.0, -10.0, -5.0),
            vec3(10.0, 10.0, 20.0),
            vec3(-10.0, 10.0, 20.0),
        ];
        let clipped = clip_near(&poly, 1.0);
        assert!(clipped.len() >= 3, "expected a polygon, got {clipped:?}");
        for v in &clipped {
            assert!(
                v.z >= 1.0 - 1e-9,
                "vertex survived behind the near plane: {v:?}"
            );
        }
    }

    #[test]
    fn near_clipping_passes_and_rejects_the_easy_cases() {
        let front = vec![
            vec3(0.0, 0.0, 10.0),
            vec3(1.0, 0.0, 10.0),
            vec3(0.0, 1.0, 10.0),
        ];
        assert_eq!(clip_near(&front, 1.0).len(), 3);

        let behind = vec![
            vec3(0.0, 0.0, -10.0),
            vec3(1.0, 0.0, -10.0),
            vec3(0.0, 1.0, -10.0),
        ];
        assert!(clip_near(&behind, 1.0).is_empty());

        // An orthographic camera has an infinite near plane and must not clip.
        assert_eq!(clip_near(&behind, f64::NEG_INFINITY).len(), 3);
    }

    #[test]
    fn ortho_extent_and_axis_mapping_agree() {
        let b = box_bounds(vec3(0.0, 0.0, 0.0), vec3(128.0, 128.0, 128.0));
        let cam = Camera::fit_ortho(OrthoAxis::Top, b, 256, 256, 0.0);
        let (h0, h1, v0, v1) = cam.ortho_extent().unwrap();
        assert!(h0 <= 0.0 && h1 >= 128.0);
        assert!(v0 <= 0.0 && v1 >= 128.0);
        // The screen-position helpers must agree with full projection.
        let p = cam.project(vec3(64.0, 96.0, 0.0)).unwrap();
        assert!((cam.ortho_screen_h(64.0).unwrap() - p.0).abs() < 1e-9);
        assert!((cam.ortho_screen_v(96.0).unwrap() - p.1).abs() < 1e-9);
    }
}
