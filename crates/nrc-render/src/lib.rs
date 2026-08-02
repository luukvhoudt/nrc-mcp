//! Headless rendering for `.map` visual feedback (§4.2).
//!
//! The spec calls this non-negotiable, and the reason is worth restating: "sculpting blind
//! fails", and deprioritizing the visual loop is "the most likely way this ends up producing
//! technically valid, aesthetically dead levels". So this renders from the `.map` itself with
//! no editor, no GPU and no display.
//!
//! # Wireframe for orthographic, solid for perspective
//!
//! §4.2 asks for "orthographic wireframe views", and that turns out to be exactly right for a
//! reason worth recording, because filled views were tried first and were useless: looking
//! straight down at a *sealed* map, the only thing a solid render can show is the underside of
//! its sky brush. One flat grey rectangle, every time.
//!
//! Backface-culled wireframe instead produces a genuine architectural floor plan — rooms,
//! corridors and stairs all legible through the ceiling. So [`RenderOptions::wireframe`] is
//! `None` (automatic) by default: wireframe for orthographic views, solid for perspective and
//! player-eye views. That is also what Radiant does with its own 2D and 3D panes.
//!
//! # Numbers come back as data, not pixels
//!
//! The spec asks that renders be annotated with
//! "what the agent can't see: dimensions, brush count, off-grid vertex markers, non-convex
//! highlights, caulk/texture state, structural-vs-detail colouring". The genuinely spatial
//! parts — markers, colouring, the grid, coordinate ticks — are drawn. The counts and
//! dimensions come back in [`RenderResult::annotations`], because an agent reading an exact
//! number beats an agent reading its own render, and a human gets legible text at any size.

pub mod camera;
pub mod canvas;
pub mod font;
pub mod scene;

use camera::{clip_near, Camera, OrthoAxis};
use canvas::{Canvas, Rgb};
use nrc_core::math::{vec3, Aabb, Vec3};
use nrc_core::model::Map;
pub use scene::{Counts, SceneOptions, SurfaceKind};

/// Hard limits, so a mistaken argument cannot ask for a 40-gigapixel image.
pub const MIN_DIMENSION: u32 = 32;
pub const MAX_DIMENSION: u32 = 4096;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RenderError {
    /// Requested size is outside [`MIN_DIMENSION`]..=[`MAX_DIMENSION`].
    BadSize(String),
}

impl std::fmt::Display for RenderError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RenderError::BadSize(m) => f.write_str(m),
        }
    }
}

impl std::error::Error for RenderError {}

/// What to look at, and from where.
#[derive(Clone, Debug)]
pub enum View {
    /// An axis-aligned orthographic view, auto-framed to the geometry.
    Ortho(OrthoAxis),
    /// A perspective view. `eye`/`target` default to a three-quarter framing of the map.
    Perspective {
        eye: Option<Vec3>,
        target: Option<Vec3>,
        fov_deg: f64,
    },
    /// A view from standing height at a floor position, looking along `yaw_deg`.
    ///
    /// `eye_height` has no default on purpose. It is a game-specific physics constant, and
    /// §7.4 names hardcoding one here as a seam violation — the caller reads it from the
    /// profile. (The design document's own 56-unit figure turned out to be wrong for the
    /// first target game; see `docs/spec-corrections.md`.)
    PlayerEye {
        position: Vec3,
        yaw_deg: f64,
        eye_height: f64,
        fov_deg: f64,
    },
}

impl View {
    /// Whether this view reads better as a wireframe when the caller has no preference.
    pub fn prefers_wireframe(&self) -> bool {
        matches!(self, View::Ortho(_))
    }

    pub fn label(&self) -> String {
        match self {
            View::Ortho(a) => a.as_str().to_string(),
            View::Perspective { .. } => "perspective".into(),
            View::PlayerEye { .. } => "player_eye".into(),
        }
    }
}

/// What the colours mean.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Overlay {
    /// Neutral surfaces, lit. Best for reading shape.
    Shaded,
    /// Structural, detail, brush-entity and patch surfaces in distinct hues — the §6.1
    /// distinction that matters most for vis performance.
    StructuralDetail,
    /// Highlights surfaces that are not drawn in game (caulk, nodraw, clip, triggers).
    Caulk,
    /// Marks vertices that miss the grid.
    OffGrid,
}

impl Overlay {
    pub fn as_str(self) -> &'static str {
        match self {
            Overlay::Shaded => "shaded",
            Overlay::StructuralDetail => "structural_detail",
            Overlay::Caulk => "caulk",
            Overlay::OffGrid => "off_grid",
        }
    }
}

#[derive(Clone, Debug)]
pub struct RenderOptions {
    pub width: u32,
    pub height: u32,
    pub view: View,
    pub overlay: Overlay,
    /// Draw edges only, with no filled faces. `None` chooses per view: wireframe for
    /// orthographic (a solid top-down of a sealed map shows only its sky brush), solid for
    /// perspective and player-eye.
    pub wireframe: Option<bool>,
    /// Outline each face. On by default: flat shading alone hides coplanar brush boundaries,
    /// which is exactly the detail a mapper is looking for.
    pub draw_edges: bool,
    /// Skip surfaces that are not drawn in game (caulk, nodraw, clip, triggers).
    ///
    /// A sealed map is wrapped in a caulk or sky shell, so a perspective view of a whole map
    /// otherwise shows nothing but that shell. Hiding it reveals the architecture inside.
    /// Counts in [`Annotations`] still include hidden surfaces, so nothing disappears
    /// silently.
    pub hide_invisible: bool,
    /// World-unit grid spacing to draw in orthographic views. `None` disables it.
    pub grid_spacing: Option<f64>,
    /// Draw coordinate ticks, a scale bar and axis letters.
    pub annotate: bool,
    pub scene: SceneOptions,
}

impl Default for RenderOptions {
    fn default() -> Self {
        Self {
            width: 900,
            height: 700,
            view: View::Ortho(OrthoAxis::Top),
            overlay: Overlay::Shaded,
            wireframe: None,
            draw_edges: true,
            hide_invisible: false,
            grid_spacing: Some(64.0),
            annotate: true,
            scene: SceneOptions::default(),
        }
    }
}

/// Everything about a render that is a number rather than a pixel.
#[derive(Clone, Debug, Default)]
pub struct Annotations {
    pub view: String,
    pub overlay: String,
    pub width: u32,
    pub height: u32,
    pub bounds_min: Option<[f64; 3]>,
    pub bounds_max: Option<[f64; 3]>,
    pub size: Option<[f64; 3]>,
    pub counts: Counts,
    pub grid: i64,
    pub off_grid_vertices: usize,
    pub skipped_brushes: usize,
    /// A few representative reasons, so a caller learns *why* without a wall of text.
    pub skipped_examples: Vec<String>,
    pub units_per_pixel: Option<f64>,
    pub camera_eye: Option<[f64; 3]>,
    pub camera_target: Option<[f64; 3]>,
    /// Things worth saying in words, e.g. that the map was empty.
    pub notes: Vec<String>,
}

pub struct RenderResult {
    pub png: Vec<u8>,
    pub annotations: Annotations,
}

impl std::fmt::Debug for RenderResult {
    /// Summarizes the image rather than dumping it. A derived `Debug` here would print a
    /// megabyte of PNG bytes into a test failure or a log line.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RenderResult")
            .field("png_bytes", &self.png.len())
            .field("annotations", &self.annotations)
            .finish()
    }
}

// ---------------------------------------------------------------------------
// Palette
// ---------------------------------------------------------------------------

const BG: Rgb = Rgb(24, 26, 30);
const GRID_MINOR: Rgb = Rgb(38, 41, 47);
const GRID_MAJOR: Rgb = Rgb(52, 57, 66);
const AXIS_LINE: Rgb = Rgb(84, 92, 106);
const EDGE: Rgb = Rgb(16, 17, 20);
const TEXT: Rgb = Rgb(176, 184, 196);
const MARKER_BAD: Rgb = Rgb(255, 72, 72);
const ENTITY_POINT: Rgb = Rgb(120, 220, 140);

const NEUTRAL: Rgb = Rgb(168, 170, 176);
const C_STRUCTURAL: Rgb = Rgb(212, 148, 92);
const C_DETAIL: Rgb = Rgb(112, 150, 196);
const C_BRUSH_ENTITY: Rgb = Rgb(120, 200, 132);
const C_PATCH: Rgb = Rgb(178, 132, 208);
const C_INVISIBLE: Rgb = Rgb(214, 92, 196);

fn base_colour(f: &scene::Facet, overlay: Overlay) -> Rgb {
    match overlay {
        Overlay::Shaded | Overlay::OffGrid => match f.kind {
            SurfaceKind::Patch => NEUTRAL.mixed(C_PATCH, 0.25),
            _ => NEUTRAL,
        },
        Overlay::StructuralDetail => match f.kind {
            SurfaceKind::Structural => C_STRUCTURAL,
            SurfaceKind::Detail => C_DETAIL,
            SurfaceKind::BrushEntity => C_BRUSH_ENTITY,
            SurfaceKind::Patch => C_PATCH,
        },
        Overlay::Caulk => {
            if f.is_caulk {
                C_INVISIBLE
            } else {
                NEUTRAL.mixed(BG, 0.35)
            }
        }
    }
}

/// Flat lambert shading from a fixed three-quarter light, plus ambient.
///
/// A fixed light rather than a headlight: with a headlight every face normal to the view is
/// equally bright and the shape disappears, which defeats the point of the render.
fn shade(normal: Vec3) -> f64 {
    let light = vec3(0.42, 0.5, 0.757).normalized().unwrap();
    const AMBIENT: f64 = 0.42;
    AMBIENT + (1.0 - AMBIENT) * normal.dot(light).max(0.0)
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

fn check_size(w: u32, h: u32) -> Result<(), RenderError> {
    for (name, v) in [("width", w), ("height", h)] {
        if !(MIN_DIMENSION..=MAX_DIMENSION).contains(&v) {
            return Err(RenderError::BadSize(format!(
                "{name} must be between {MIN_DIMENSION} and {MAX_DIMENSION}, got {v}"
            )));
        }
    }
    Ok(())
}

fn build_camera(view: &View, bounds: Aabb, width: u32, height: u32) -> Camera {
    match view {
        View::Ortho(axis) => Camera::fit_ortho(*axis, bounds, width, height, 26.0),
        View::Perspective {
            eye,
            target,
            fov_deg,
        } => match (eye, target) {
            (Some(e), t) => {
                let t = t.unwrap_or_else(|| {
                    if bounds.is_empty() {
                        Vec3::ZERO
                    } else {
                        bounds.center()
                    }
                });
                Camera::look_at(*e, t, *fov_deg, width, height, 1.0)
            }
            (None, _) => Camera::frame(bounds, *fov_deg, width, height),
        },
        View::PlayerEye {
            position,
            yaw_deg,
            eye_height,
            fov_deg,
        } => {
            let eye = *position + vec3(0.0, 0.0, *eye_height);
            let yaw = yaw_deg.to_radians();
            let target = eye + vec3(yaw.cos(), yaw.sin(), 0.0) * 256.0;
            Camera::look_at(eye, target, *fov_deg, width, height, 1.0)
        }
    }
}

/// Render one view.
pub fn render(map: &Map, opts: &RenderOptions) -> Result<RenderResult, RenderError> {
    check_size(opts.width, opts.height)?;

    let sc = scene::build(map, &opts.scene);
    let cam = build_camera(&opts.view, sc.bounds, opts.width, opts.height);
    let mut canvas = Canvas::new(opts.width, opts.height, BG);

    if let (Some(spacing), true) = (opts.grid_spacing, cam.ortho_extent().is_some()) {
        draw_grid(&mut canvas, &cam, spacing);
    }

    draw_facets(&mut canvas, &cam, &sc, opts);

    if opts.overlay == Overlay::OffGrid {
        for p in &sc.off_grid_points {
            if let Some((x, y, _)) = cam.project(*p) {
                canvas.marker(x.round() as i64, y.round() as i64, 3, MARKER_BAD);
            }
        }
    }

    for (_, p) in &sc.entity_points {
        if let Some((x, y, _)) = cam.project(*p) {
            let (x, y) = (x.round() as i64, y.round() as i64);
            canvas.rect_outline(x - 2, y - 2, x + 2, y + 2, ENTITY_POINT);
        }
    }

    if opts.annotate {
        annotate(&mut canvas, &cam, opts);
    }

    Ok(RenderResult {
        png: canvas.to_png(),
        annotations: annotations_for(&sc, &cam, opts),
    })
}

fn draw_facets(canvas: &mut Canvas, cam: &Camera, sc: &scene::Scene, opts: &RenderOptions) {
    let near = cam.near();
    let wireframe = opts
        .wireframe
        .unwrap_or_else(|| opts.view.prefers_wireframe());
    for f in &sc.facets {
        if opts.hide_invisible && f.is_caulk {
            continue;
        }
        // Cull faces turned away from the viewer. In view space the eye looks along +z, so a
        // visible surface has a normal with a negative depth component. This is what makes an
        // interior view work: inside a room the walls' normals point at the eye.
        let n_view = cam.to_view(f.points[0] + f.normal) - cam.to_view(f.points[0]);
        if n_view.z >= 0.0 {
            continue;
        }

        let view_pts: Vec<Vec3> = f.points.iter().map(|p| cam.to_view(*p)).collect();
        let clipped = clip_near(&view_pts, near);
        if clipped.len() < 3 {
            continue;
        }
        let screen: Vec<(f64, f64, f32)> = clipped.iter().map(|v| cam.project_view(*v)).collect();

        if !wireframe {
            let c = base_colour(f, opts.overlay).scaled(shade(f.normal));
            canvas.polygon(&screen, c);
        }

        if opts.draw_edges || wireframe {
            let edge = if wireframe {
                base_colour(f, opts.overlay)
            } else {
                EDGE
            };
            // Edges sit exactly on the face they bound, so a strict depth test makes them
            // dash in and out along the seam. Wireframe has no fills to hide behind at all.
            let bias = if wireframe { f32::INFINITY } else { 0.75 };
            for i in 0..screen.len() {
                canvas.line(screen[i], screen[(i + 1) % screen.len()], edge, bias);
            }
        }
    }
}

/// Grid lines, with the world axes emphasised.
fn draw_grid(canvas: &mut Canvas, cam: &Camera, spacing: f64) {
    let Some((h0, h1, v0, v1)) = cam.ortho_extent() else {
        return;
    };
    let ppu = cam.pixels_per_unit().unwrap_or(1.0);
    if spacing <= 0.0 {
        return;
    }
    // Coarsen until lines are at least a few pixels apart, so a whole-map view does not turn
    // into a solid field of grid.
    let mut step = spacing;
    while step * ppu < 6.0 && step < 1.0e6 {
        step *= 2.0;
    }
    let major = step * 8.0;

    let mut h = (h0 / step).floor() * step;
    while h <= h1 {
        if let Some(x) = cam.ortho_screen_h(h) {
            let c = pick_grid_colour(h, major);
            let x = x.round() as i64;
            for y in 0..canvas.height as i64 {
                canvas.put(x, y, c);
            }
        }
        h += step;
    }

    let mut v = (v0 / step).floor() * step;
    while v <= v1 {
        if let Some(y) = cam.ortho_screen_v(v) {
            let c = pick_grid_colour(v, major);
            let y = y.round() as i64;
            for x in 0..canvas.width as i64 {
                canvas.put(x, y, c);
            }
        }
        v += step;
    }
}

fn pick_grid_colour(coord: f64, major: f64) -> Rgb {
    if coord.abs() < 1e-9 {
        AXIS_LINE
    } else if (coord % major).abs() < 1e-6 {
        GRID_MAJOR
    } else {
        GRID_MINOR
    }
}

/// Coordinate ticks, a scale bar, and axis letters.
fn annotate(canvas: &mut Canvas, cam: &Camera, opts: &RenderOptions) {
    let scale = if opts.width >= 700 { 2 } else { 1 };

    if let (Some((h0, h1, v0, v1)), Some(ppu)) = (cam.ortho_extent(), cam.pixels_per_unit()) {
        // Label spacing chosen so labels cannot collide, whatever the zoom.
        let mut step = opts.grid_spacing.unwrap_or(64.0).max(1.0);
        while step * ppu < 90.0 && step < 1.0e6 {
            step *= 2.0;
        }

        let mut h = (h0 / step).ceil() * step;
        while h <= h1 {
            if let Some(x) = cam.ortho_screen_h(h) {
                let label = font::fmt_coord(h);
                let w = font::text_width(&label, scale);
                let x = x.round() as i64 - w / 2;
                font::draw_text(canvas, x, 4, &label, scale, TEXT);
            }
            h += step;
        }

        // Vertical labels run down the left edge, where they can collide with the horizontal
        // labels along the top and with the scale bar along the bottom. Both strips are
        // reserved rather than letting the text overprint into an unreadable smudge.
        let top_reserved = 4 + font::text_height(scale) + 2;
        let bottom_reserved = canvas.height as i64 - 18 - font::text_height(scale);

        let mut v = (v0 / step).ceil() * step;
        while v <= v1 {
            if let Some(y) = cam.ortho_screen_v(v) {
                let ty = y.round() as i64 - font::text_height(scale) / 2;
                if ty >= top_reserved && ty + font::text_height(scale) <= bottom_reserved {
                    font::draw_text(canvas, 4, ty, &font::fmt_coord(v), scale, TEXT);
                }
            }
            v += step;
        }

        if let View::Ortho(axis) = &opts.view {
            let (hl, vl) = axis.axis_labels();
            let hw = font::text_width(hl, scale);
            font::draw_text(
                canvas,
                canvas.width as i64 - hw - 5,
                canvas.height as i64 / 2,
                hl,
                scale,
                AXIS_LINE,
            );
            font::draw_text(
                canvas,
                canvas.width as i64 / 2,
                canvas.height as i64 - font::text_height(scale) - 5,
                vl,
                scale,
                AXIS_LINE,
            );
        }

        draw_scale_bar(canvas, ppu, scale);
    }
}

/// A bar of a round world length, labelled. The only reliable way to judge size in an
/// auto-framed view, where the zoom is whatever the geometry demanded.
fn draw_scale_bar(canvas: &mut Canvas, pixels_per_unit: f64, text_scale: i64) {
    if pixels_per_unit <= 0.0 || !pixels_per_unit.is_finite() {
        return;
    }
    // Prefer power-of-two lengths: map dimensions are powers of two, so 512 is a more useful
    // reference than 500.
    let mut length = 1.0f64;
    while length * pixels_per_unit < 70.0 && length < 1.0e6 {
        length *= 2.0;
    }
    while length * pixels_per_unit > 200.0 && length > 1.0 {
        length /= 2.0;
    }

    let px = (length * pixels_per_unit).round() as i64;
    let y = canvas.height as i64 - 12;
    let x0 = 8;
    let x1 = x0 + px;
    canvas.rect_fill(x0, y, x1, y + 2, TEXT);
    canvas.rect_fill(x0, y - 3, x0 + 1, y + 5, TEXT);
    canvas.rect_fill(x1 - 1, y - 3, x1, y + 5, TEXT);
    let label = font::fmt_coord(length);
    font::draw_text(
        canvas,
        x0,
        y - font::text_height(text_scale) - 5,
        &label,
        text_scale,
        TEXT,
    );
}

fn annotations_for(sc: &scene::Scene, cam: &Camera, opts: &RenderOptions) -> Annotations {
    let mut a = Annotations {
        view: opts.view.label(),
        overlay: opts.overlay.as_str().to_string(),
        width: opts.width,
        height: opts.height,
        counts: sc.counts,
        grid: opts.scene.grid,
        off_grid_vertices: sc.off_grid_points.len(),
        skipped_brushes: sc.skipped.len(),
        skipped_examples: sc
            .skipped
            .iter()
            .take(3)
            .map(|(e, p, why)| format!("entity {e}, brush {p}: {why}"))
            .collect(),
        units_per_pixel: cam.pixels_per_unit().map(|p| 1.0 / p),
        ..Default::default()
    };

    if !sc.bounds.is_empty() {
        a.bounds_min = Some(sc.bounds.min.to_array());
        a.bounds_max = Some(sc.bounds.max.to_array());
        a.size = Some(sc.bounds.size().to_array());
    } else {
        a.notes
            .push("nothing was drawn: the map has no evaluable geometry".into());
    }

    match &opts.view {
        View::Perspective { eye, target, .. } => {
            if let Some(e) = eye {
                a.camera_eye = Some(e.to_array());
            }
            if let Some(t) = target {
                a.camera_target = Some(t.to_array());
            }
        }
        View::PlayerEye {
            position,
            eye_height,
            ..
        } => {
            a.camera_eye = Some((*position + vec3(0.0, 0.0, *eye_height)).to_array());
            a.notes.push(format!(
                "eye height {eye_height} units above the given position"
            ));
        }
        View::Ortho(_) => {}
    }

    if sc.skipped_count() > 0 {
        a.notes.push(format!(
            "{} brush(es) were skipped because the kernel could not evaluate them exactly",
            sc.skipped.len()
        ));
    }
    if opts.overlay == Overlay::OffGrid && sc.off_grid_points.is_empty() {
        a.notes
            .push(format!("no vertices are off a grid of {}", opts.scene.grid));
    }
    a
}

impl scene::Scene {
    fn skipped_count(&self) -> usize {
        self.skipped.len()
    }
}

// ---------------------------------------------------------------------------
// Contact sheet
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
pub struct ContactSheetOptions {
    /// Size of the whole sheet. Each of the four panels gets a quarter.
    pub width: u32,
    pub height: u32,
    pub overlay: Overlay,
    pub grid_spacing: Option<f64>,
    pub scene: SceneOptions,
    /// See [`RenderOptions::hide_invisible`].
    pub hide_invisible: bool,
    /// Replaces the perspective panel with a view from standing height here.
    pub player_eye: Option<(Vec3, f64, f64)>,
}

impl Default for ContactSheetOptions {
    fn default() -> Self {
        Self {
            width: 1200,
            height: 900,
            overlay: Overlay::Shaded,
            grid_spacing: Some(64.0),
            scene: SceneOptions::default(),
            hide_invisible: false,
            player_eye: None,
        }
    }
}

/// Three orthographic views plus one perspective view, in one image (§4.2).
///
/// One call returning one image is the point: it is what a mutating tool can afford to
/// return by default, and three orthographic views plus a perspective one are what make a
/// shape unambiguous.
pub fn contact_sheet(map: &Map, opts: &ContactSheetOptions) -> Result<RenderResult, RenderError> {
    check_size(opts.width, opts.height)?;
    let (pw, ph) = (opts.width / 2, opts.height / 2);
    check_size(pw, ph).map_err(|_| {
        RenderError::BadSize(format!(
            "a contact sheet needs each panel at least {MIN_DIMENSION}px, so width and \
             height must be at least {}",
            MIN_DIMENSION * 2
        ))
    })?;

    let fourth = match opts.player_eye {
        Some((position, yaw_deg, eye_height)) => View::PlayerEye {
            position,
            yaw_deg,
            eye_height,
            fov_deg: 90.0,
        },
        None => View::Perspective {
            eye: None,
            target: None,
            fov_deg: 55.0,
        },
    };

    let views = [
        View::Ortho(OrthoAxis::Top),
        View::Ortho(OrthoAxis::Front),
        View::Ortho(OrthoAxis::Side),
        fourth,
    ];

    let mut sheet = Canvas::new(opts.width, opts.height, Rgb(12, 13, 15));
    let mut panels = Vec::new();

    for (i, view) in views.into_iter().enumerate() {
        let is_ortho = matches!(view, View::Ortho(_));
        let panel_opts = RenderOptions {
            width: pw,
            height: ph,
            view,
            overlay: opts.overlay,
            wireframe: None,
            draw_edges: true,
            hide_invisible: opts.hide_invisible,
            grid_spacing: if is_ortho { opts.grid_spacing } else { None },
            annotate: true,
            scene: opts.scene.clone(),
        };
        // Rendering each panel to its own canvas and compositing keeps the depth buffers
        // independent, which they must be — they are different projections.
        let sc = scene::build(map, &panel_opts.scene);
        let cam = build_camera(&panel_opts.view, sc.bounds, pw, ph);
        let mut c = Canvas::new(pw, ph, BG);
        if let (Some(sp), true) = (panel_opts.grid_spacing, cam.ortho_extent().is_some()) {
            draw_grid(&mut c, &cam, sp);
        }
        draw_facets(&mut c, &cam, &sc, &panel_opts);
        if panel_opts.overlay == Overlay::OffGrid {
            for p in &sc.off_grid_points {
                if let Some((x, y, _)) = cam.project(*p) {
                    c.marker(x.round() as i64, y.round() as i64, 3, MARKER_BAD);
                }
            }
        }
        for (_, p) in &sc.entity_points {
            if let Some((x, y, _)) = cam.project(*p) {
                let (x, y) = (x.round() as i64, y.round() as i64);
                c.rect_outline(x - 2, y - 2, x + 2, y + 2, ENTITY_POINT);
            }
        }
        annotate(&mut c, &cam, &panel_opts);

        let (ox, oy) = ((i as u32 % 2) * pw, (i as u32 / 2) * ph);
        sheet.blit(&c, ox as i64, oy as i64);
        sheet.rect_outline(
            ox as i64,
            oy as i64,
            (ox + pw) as i64 - 1,
            (oy + ph) as i64 - 1,
            Rgb(60, 64, 72),
        );
        panels.push(annotations_for(&sc, &cam, &panel_opts));
    }

    // The sheet's own annotations come from the top panel, which is the one framing the whole
    // map, plus a note naming the panel order so a caller knows which quadrant is which.
    let mut ann = panels.remove(0);
    ann.view = "contact_sheet".into();
    ann.width = opts.width;
    ann.height = opts.height;
    ann.units_per_pixel = None;
    ann.notes.insert(
        0,
        format!(
            "panels clockwise from top-left: top (XY), front (XZ), side (YZ), {}",
            if opts.player_eye.is_some() {
                "player eye"
            } else {
                "perspective"
            }
        ),
    );
    Ok(RenderResult {
        png: sheet.to_png(),
        annotations: ann,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use nrc_core::parse_map;

    const BOX_FACES: &str = "\
        ( 0 0 64 ) ( 0 1 64 ) ( 1 0 64 ) t/top 0 0 0 0.5 0.5 0 0 0\n\
        ( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) t/bot 0 0 0 0.5 0.5 0 0 0\n\
        ( 0 0 0 ) ( 0 0 1 ) ( 1 0 0 ) common/caulk 0 0 0 0.5 0.5 0 0 0\n\
        ( 0 64 0 ) ( 1 64 0 ) ( 0 64 1 ) t/side 0 0 0 0.5 0.5 0 0 0\n\
        ( 0 0 0 ) ( 0 1 0 ) ( 0 0 1 ) t/side 0 0 0 0.5 0.5 0 0 0\n\
        ( 64 0 0 ) ( 64 0 1 ) ( 64 1 0 ) t/side 0 0 0 0.5 0.5 0 0 0\n";

    fn box_map() -> Map {
        parse_map(&format!(
            "{{\n\"classname\" \"worldspawn\"\n{{\n{BOX_FACES}}}\n}}\n"
        ))
        .unwrap()
    }

    /// Count pixels that are not the background, i.e. how much got drawn.
    fn ink(png_canvas: &Canvas) -> usize {
        let mut n = 0;
        for y in 0..png_canvas.height as i64 {
            for x in 0..png_canvas.width as i64 {
                if png_canvas.get(x, y) != Some(BG) {
                    n += 1;
                }
            }
        }
        n
    }

    fn render_canvas(map: &Map, opts: &RenderOptions) -> Canvas {
        // Mirror `render` but keep the canvas, so tests can inspect pixels.
        let sc = scene::build(map, &opts.scene);
        let cam = build_camera(&opts.view, sc.bounds, opts.width, opts.height);
        let mut c = Canvas::new(opts.width, opts.height, BG);
        draw_facets(&mut c, &cam, &sc, opts);
        c
    }

    #[test]
    fn a_box_renders_a_valid_png_with_content() {
        let r = render(&box_map(), &RenderOptions::default()).unwrap();
        assert_eq!(&r.png[..4], &[0x89, b'P', b'N', b'G']);
        assert!(r.png.len() > 200, "suspiciously small PNG");
        assert_eq!(r.annotations.counts.structural, 1);
        assert_eq!(r.annotations.counts.facets, 6);
        assert_eq!(r.annotations.size, Some([64.0, 64.0, 64.0]));
        assert_eq!(r.annotations.view, "top");
    }

    #[test]
    fn something_is_actually_drawn() {
        let opts = RenderOptions {
            grid_spacing: None,
            annotate: false,
            ..Default::default()
        };
        let drawn = ink(&render_canvas(&box_map(), &opts));
        assert!(
            drawn > 1000,
            "only {drawn} pixels drawn; the box should fill the frame"
        );
    }

    #[test]
    fn backfaces_are_culled_so_only_three_faces_of_a_box_can_show() {
        // From outside, an axis-aligned box shows at most three faces. The top view shows
        // exactly one, so the visible surface must be a single flat colour.
        let opts = RenderOptions {
            view: View::Ortho(OrthoAxis::Top),
            grid_spacing: None,
            annotate: false,
            draw_edges: false,
            wireframe: Some(false),
            ..Default::default()
        };
        let c = render_canvas(&box_map(), &opts);
        let mut colours = std::collections::BTreeSet::new();
        for y in 0..c.height as i64 {
            for x in 0..c.width as i64 {
                if let Some(p) = c.get(x, y) {
                    if p != BG {
                        colours.insert((p.0, p.1, p.2));
                    }
                }
            }
        }
        assert_eq!(colours.len(), 1, "expected one lit face, got {colours:?}");
    }

    /// An axis-aligned box brush with outward normals.
    fn box_brush(x0: i64, y0: i64, z0: i64, x1: i64, y1: i64, z1: i64) -> String {
        let t = "a/b 0 0 0 0.5 0.5 0 0 0";
        format!(
            "{{\n\
             ( {x0} {y0} {z1} ) ( {x0} {yp} {z1} ) ( {xp} {y0} {z1} ) {t}\n\
             ( {x0} {y0} {z0} ) ( {xp} {y0} {z0} ) ( {x0} {yp} {z0} ) {t}\n\
             ( {x0} {y0} {z0} ) ( {x0} {y0} {zp} ) ( {xp} {y0} {z0} ) {t}\n\
             ( {x0} {y1} {z0} ) ( {xp} {y1} {z0} ) ( {x0} {y1} {zp} ) {t}\n\
             ( {x0} {y0} {z0} ) ( {x0} {yp} {z0} ) ( {x0} {y0} {zp} ) {t}\n\
             ( {x1} {y0} {z0} ) ( {x1} {y0} {zp} ) ( {x1} {yp} {z0} ) {t}\n\
             }}\n",
            xp = x0 + 1,
            yp = y0 + 1,
            zp = z0 + 1
        )
    }

    #[test]
    fn the_depth_buffer_shows_the_nearer_of_two_stacked_brushes() {
        // Two boxes on the same footprint at different heights. Looking down, the taller one
        // must win the depth test at every pixel they share.
        let low = box_brush(0, 0, 0, 64, 64, 64);
        let high = box_brush(0, 0, 128, 64, 64, 192);
        let map = parse_map(&format!(
            "{{\n\"classname\" \"worldspawn\"\n{low}{high}}}\n"
        ))
        .unwrap();
        assert_eq!(map.brush_count(), 2);

        let opts = RenderOptions {
            view: View::Ortho(OrthoAxis::Top),
            grid_spacing: None,
            annotate: false,
            draw_edges: false,
            wireframe: Some(false),
            ..Default::default()
        };
        let c = render_canvas(&map, &opts);
        let (mx, my) = (c.width as i64 / 2, c.height as i64 / 2);
        assert_ne!(
            c.get(mx, my),
            Some(BG),
            "something should be drawn at the centre"
        );
        // Top view depth is -z, so the z=192 surface has depth -192.
        let depth = c.depth_at(mx, my);
        assert!(
            (depth + 192.0).abs() < 1e-3,
            "expected the z=192 surface (depth -192), got {depth}"
        );
    }

    #[test]
    fn all_three_ortho_axes_and_perspective_render() {
        for view in [
            View::Ortho(OrthoAxis::Top),
            View::Ortho(OrthoAxis::Front),
            View::Ortho(OrthoAxis::Side),
            View::Perspective {
                eye: None,
                target: None,
                fov_deg: 60.0,
            },
        ] {
            let label = view.label();
            let opts = RenderOptions {
                view,
                ..Default::default()
            };
            let r = render(&box_map(), &opts).unwrap();
            assert!(r.png.len() > 200, "{label} produced no image");
        }
    }

    #[test]
    fn a_camera_inside_a_room_still_sees_its_walls() {
        // The near-plane clipping case. A hollow room with the eye in the middle: without
        // clipping, every wall has a vertex behind the eye and the frame comes back empty.
        let wall = |x0: i64, y0: i64, z0: i64, x1: i64, y1: i64, z1: i64| {
            format!(
                "{{\n\
                 ( {x0} {y0} {z1} ) ( {x0} {y02} {z1} ) ( {x02} {y0} {z1} ) t/w 0 0 0 0.5 0.5 0 0 0\n\
                 ( {x0} {y0} {z0} ) ( {x02} {y0} {z0} ) ( {x0} {y02} {z0} ) t/w 0 0 0 0.5 0.5 0 0 0\n\
                 ( {x0} {y0} {z0} ) ( {x0} {y0} {z02} ) ( {x02} {y0} {z0} ) t/w 0 0 0 0.5 0.5 0 0 0\n\
                 ( {x0} {y1} {z0} ) ( {x02} {y1} {z0} ) ( {x0} {y1} {z02} ) t/w 0 0 0 0.5 0.5 0 0 0\n\
                 ( {x0} {y0} {z0} ) ( {x0} {y02} {z0} ) ( {x0} {y0} {z02} ) t/w 0 0 0 0.5 0.5 0 0 0\n\
                 ( {x1} {y0} {z0} ) ( {x1} {y0} {z02} ) ( {x1} {y02} {z0} ) t/w 0 0 0 0.5 0.5 0 0 0\n\
                 }}\n",
                x02 = x0 + 1,
                y02 = y0 + 1,
                z02 = z0 + 1
            )
        };
        let room = format!(
            "{{\n\"classname\" \"worldspawn\"\n{}{}{}{}{}{}}}\n",
            wall(-272, -272, -16, 272, 272, 0),   // floor
            wall(-272, -272, 256, 272, 272, 272), // ceiling
            wall(-272, -272, 0, -256, 272, 256),  // -X
            wall(256, -272, 0, 272, 272, 256),    // +X
            wall(-256, -272, 0, 256, -256, 256),  // -Y
            wall(-256, 256, 0, 256, 272, 256),    // +Y
        );
        let map = parse_map(&room).unwrap();
        assert!(map.brush_count() == 6, "test fixture should have 6 brushes");

        let opts = RenderOptions {
            view: View::PlayerEye {
                position: Vec3::ZERO,
                yaw_deg: 0.0,
                eye_height: 69.375,
                fov_deg: 90.0,
            },
            grid_spacing: None,
            annotate: false,
            ..Default::default()
        };
        let drawn = ink(&render_canvas(&map, &opts));
        assert!(
            drawn > 10_000,
            "a camera inside a room drew only {drawn} pixels; near-plane clipping is broken"
        );
    }

    #[test]
    fn player_eye_height_is_reported_and_applied() {
        let r = render(
            &box_map(),
            &RenderOptions {
                view: View::PlayerEye {
                    position: vec3(10.0, 20.0, 30.0),
                    yaw_deg: 90.0,
                    eye_height: 69.375,
                    fov_deg: 90.0,
                },
                ..Default::default()
            },
        )
        .unwrap();
        assert_eq!(r.annotations.camera_eye, Some([10.0, 20.0, 99.375]));
        assert!(r.annotations.notes.iter().any(|n| n.contains("69.375")));
    }

    #[test]
    fn overlays_change_the_colours_they_are_meant_to() {
        let base = RenderOptions {
            grid_spacing: None,
            annotate: false,
            draw_edges: false,
            wireframe: Some(false),
            view: View::Ortho(OrthoAxis::Side),
            ..Default::default()
        };
        let shaded = render_canvas(&box_map(), &base);
        let caulk = render_canvas(
            &box_map(),
            &RenderOptions {
                overlay: Overlay::Caulk,
                ..base.clone()
            },
        );
        // The side view shows the +X face, which is not caulk, so the caulk overlay must
        // dim it rather than highlight it.
        let mid = (shaded.width as i64 / 2, shaded.height as i64 / 2);
        assert_ne!(
            shaded.get(mid.0, mid.1),
            caulk.get(mid.0, mid.1),
            "the caulk overlay should recolour visible surfaces"
        );

        let sd = render_canvas(
            &box_map(),
            &RenderOptions {
                overlay: Overlay::StructuralDetail,
                ..base
            },
        );
        assert_ne!(shaded.get(mid.0, mid.1), sd.get(mid.0, mid.1));
    }

    #[test]
    fn off_grid_overlay_reports_and_marks() {
        let opts = RenderOptions {
            overlay: Overlay::OffGrid,
            scene: SceneOptions {
                grid: 128,
                ..Default::default()
            },
            ..Default::default()
        };
        let r = render(&box_map(), &opts).unwrap();
        assert_eq!(r.annotations.off_grid_vertices, 7);
        assert_eq!(r.annotations.overlay, "off_grid");

        // On a grid the box does satisfy, the note says so explicitly.
        let clean = RenderOptions {
            overlay: Overlay::OffGrid,
            scene: SceneOptions {
                grid: 64,
                ..Default::default()
            },
            ..Default::default()
        };
        let r = render(&box_map(), &clean).unwrap();
        assert_eq!(r.annotations.off_grid_vertices, 0);
        assert!(r
            .annotations
            .notes
            .iter()
            .any(|n| n.contains("no vertices are off")));
    }

    #[test]
    fn wireframe_draws_less_than_solid() {
        let solid = RenderOptions {
            grid_spacing: None,
            annotate: false,
            ..Default::default()
        };
        let forced_solid = RenderOptions {
            wireframe: Some(false),
            ..solid.clone()
        };
        let wire = RenderOptions {
            wireframe: Some(true),
            ..solid.clone()
        };
        let a = ink(&render_canvas(&box_map(), &forced_solid));
        let b = ink(&render_canvas(&box_map(), &wire));
        assert!(b < a, "wireframe ({b}) should draw less than solid ({a})");
        assert!(b > 0, "wireframe drew nothing");

        // With no preference, an orthographic view picks wireframe and a perspective view
        // picks solid. A solid top-down of a sealed map shows only its sky brush, which is
        // why this default exists.
        assert!(View::Ortho(OrthoAxis::Top).prefers_wireframe());
        assert!(!View::Perspective {
            eye: None,
            target: None,
            fov_deg: 60.0
        }
        .prefers_wireframe());
        assert!(!View::PlayerEye {
            position: Vec3::ZERO,
            yaw_deg: 0.0,
            eye_height: 64.0,
            fov_deg: 90.0
        }
        .prefers_wireframe());
        let auto = ink(&render_canvas(&box_map(), &solid));
        assert_eq!(
            auto, b,
            "an ortho view with no preference should render as wireframe"
        );
    }

    #[test]
    fn an_unevaluable_brush_is_surfaced_in_the_annotations() {
        let three = "{\n\
            ( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 1 0 ) ( 0 0 1 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 0 1 ) ( 1 0 0 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            }\n";
        let map = parse_map(&format!("{{\n\"classname\" \"worldspawn\"\n{three}}}\n")).unwrap();
        let r = render(&map, &RenderOptions::default()).unwrap();
        assert_eq!(r.annotations.skipped_brushes, 1);
        assert_eq!(r.annotations.skipped_examples.len(), 1);
        assert!(r.annotations.notes.iter().any(|n| n.contains("skipped")));
    }

    #[test]
    fn an_empty_map_renders_a_blank_frame_and_says_so() {
        let r = render(&parse_map("").unwrap(), &RenderOptions::default()).unwrap();
        assert!(r.png.len() > 100);
        assert_eq!(r.annotations.bounds_min, None);
        assert!(r
            .annotations
            .notes
            .iter()
            .any(|n| n.contains("nothing was drawn")));
    }

    #[test]
    fn absurd_sizes_are_refused_with_a_useful_message() {
        for (w, h) in [(0, 100), (100, 0), (99_999, 100), (100, 99_999)] {
            let opts = RenderOptions {
                width: w,
                height: h,
                ..Default::default()
            };
            let e = render(&box_map(), &opts).unwrap_err();
            assert!(
                e.to_string().contains("must be between"),
                "unhelpful message: {e}"
            );
        }
    }

    #[test]
    fn contact_sheet_composes_four_panels() {
        let r = contact_sheet(&box_map(), &ContactSheetOptions::default()).unwrap();
        assert_eq!(&r.png[..4], &[0x89, b'P', b'N', b'G']);
        assert_eq!(r.annotations.view, "contact_sheet");
        assert_eq!(r.annotations.width, 1200);
        assert!(r.annotations.notes[0].contains("top (XY)"));
        assert!(r.annotations.notes[0].contains("perspective"));
        assert_eq!(r.annotations.counts.facets, 6);
    }

    #[test]
    fn contact_sheet_can_swap_in_a_player_eye_panel() {
        let opts = ContactSheetOptions {
            player_eye: Some((vec3(32.0, 32.0, 64.0), 45.0, 69.375)),
            ..Default::default()
        };
        let r = contact_sheet(&box_map(), &opts).unwrap();
        assert!(r.annotations.notes[0].contains("player eye"));
    }

    #[test]
    fn contact_sheet_refuses_a_size_too_small_to_split() {
        let opts = ContactSheetOptions {
            width: 40,
            height: 40,
            ..Default::default()
        };
        let e = contact_sheet(&box_map(), &opts).unwrap_err();
        assert!(e.to_string().contains("at least"), "got {e}");
    }

    #[test]
    fn a_patch_is_visible_in_the_render() {
        let patch = "{\npatchDef2\n{\nx/y\n( 3 3 0 0 0 )\n(\n\
            ( ( 0 0 0 0 0 ) ( 0 64 64 0 0 ) ( 0 128 0 0 0 ) )\n\
            ( ( 64 0 0 0 0 ) ( 64 64 64 0 0 ) ( 64 128 0 0 0 ) )\n\
            ( ( 128 0 0 0 0 ) ( 128 64 64 0 0 ) ( 128 128 0 0 0 ) )\n\
            )\n}\n}\n";
        let map = parse_map(&format!("{{\n\"classname\" \"worldspawn\"\n{patch}}}\n")).unwrap();
        let opts = RenderOptions {
            grid_spacing: None,
            annotate: false,
            ..Default::default()
        };
        let drawn = ink(&render_canvas(&map, &opts));
        assert!(
            drawn > 500,
            "a patch should be visible; drew {drawn} pixels"
        );
        let r = render(&map, &RenderOptions::default()).unwrap();
        assert_eq!(r.annotations.counts.patches, 1);
    }

    #[test]
    fn annotations_do_not_claim_a_scale_for_a_perspective_view() {
        let r = render(
            &box_map(),
            &RenderOptions {
                view: View::Perspective {
                    eye: None,
                    target: None,
                    fov_deg: 60.0,
                },
                ..Default::default()
            },
        )
        .unwrap();
        assert_eq!(
            r.annotations.units_per_pixel, None,
            "a perspective view has no single scale, so it must not report one"
        );
    }

    #[test]
    fn ortho_views_report_a_usable_scale() {
        let r = render(&box_map(), &RenderOptions::default()).unwrap();
        let upp = r.annotations.units_per_pixel.expect("ortho has a scale");
        assert!(upp > 0.0 && upp.is_finite(), "got {upp}");
    }
}
