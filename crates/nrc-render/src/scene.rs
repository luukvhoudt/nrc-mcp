//! Turning a `Map` into polygons a rasterizer can draw.
//!
//! Brush faces come from the exact hull ([`nrc_core::brush_geometry`]), so what gets drawn is
//! what the kernel believes the brush *is* — a brush the kernel cannot evaluate is reported
//! as skipped rather than guessed at, and that count reaches the caller. A render that
//! silently omits geometry is worse than no render.

use nrc_core::math::{vec3, Aabb, Vec3};
use nrc_core::model::{Map, Primitive};
use nrc_core::stats::BRUSH_DETAIL_MASK;
use nrc_core::winding::{brush_geometry, face_area};

/// What a polygon came from, which is what decides how it is coloured.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SurfaceKind {
    /// Brush in worldspawn without the detail bit: seals the map and blocks vis.
    Structural,
    /// Brush in worldspawn with the detail bit set.
    Detail,
    /// Brush belonging to a brush entity (a door, a platform, a trigger).
    BrushEntity,
    /// Tessellated patch.
    Patch,
}

impl SurfaceKind {
    pub fn as_str(self) -> &'static str {
        match self {
            SurfaceKind::Structural => "structural",
            SurfaceKind::Detail => "detail",
            SurfaceKind::BrushEntity => "brush_entity",
            SurfaceKind::Patch => "patch",
        }
    }
}

/// One convex polygon to draw.
#[derive(Clone, Debug)]
pub struct Facet {
    pub points: Vec<Vec3>,
    pub normal: Vec3,
    pub kind: SurfaceKind,
    /// True when the shader is one of the invisible utility shaders.
    pub is_caulk: bool,
    pub entity: usize,
    pub primitive: usize,
    pub face: usize,
    pub area: f64,
}

#[derive(Clone, Debug)]
pub struct Scene {
    pub facets: Vec<Facet>,
    /// Brush vertices that do not sit on the requested grid.
    pub off_grid_points: Vec<Vec3>,
    /// Entity origins, so point entities are visible at all.
    pub entity_points: Vec<(usize, Vec3)>,
    pub bounds: Aabb,
    /// Brushes the exact kernel declined to evaluate, with the reason.
    pub skipped: Vec<(usize, usize, String)>,
    pub counts: Counts,
}

#[derive(Clone, Copy, Debug, Default)]
pub struct Counts {
    pub structural: usize,
    pub detail: usize,
    pub brush_entity: usize,
    pub patches: usize,
    pub facets: usize,
    pub caulk_facets: usize,
}

#[derive(Clone, Debug)]
pub struct SceneOptions {
    /// Grid that off-grid vertices are measured against.
    pub grid: i64,
    /// Shader-name fragments that mean "not drawn in game".
    ///
    /// Data rather than a hardcoded list, and engine-level rather than game-level: every
    /// idTech game using this compiler shares `common/caulk`. Callers can override to match
    /// a mod's conventions without touching code.
    pub invisible_shader_fragments: Vec<String>,
    /// Subdivisions per patch span. Two is enough to read a curve's direction; four looks
    /// right at contact-sheet sizes.
    pub patch_subdivisions: usize,
    /// Only include geometry intersecting this box.
    pub region: Option<Aabb>,
}

impl Default for SceneOptions {
    fn default() -> Self {
        Self {
            grid: 1,
            invisible_shader_fragments: vec![
                "caulk".to_string(),
                "nodraw".to_string(),
                "hint".to_string(),
                "areaportal".to_string(),
                "clip".to_string(),
                "trigger".to_string(),
            ],
            patch_subdivisions: 4,
            region: None,
        }
    }
}

fn is_invisible(shader: &str, fragments: &[String]) -> bool {
    let lower = shader.to_ascii_lowercase();
    fragments.iter().any(|f| lower.contains(f.as_str()))
}

/// Build the drawable scene.
pub fn build(map: &Map, opts: &SceneOptions) -> Scene {
    let mut scene = Scene {
        facets: Vec::new(),
        off_grid_points: Vec::new(),
        entity_points: Vec::new(),
        bounds: Aabb::EMPTY,
        skipped: Vec::new(),
        counts: Counts::default(),
    };

    for (ei, ent) in map.entities.iter().enumerate() {
        let worldspawn = ent.is_worldspawn();
        if let Some(o) = ent.origin() {
            if !worldspawn {
                scene.entity_points.push((ei, o));
                scene.bounds.extend(o);
            }
        }

        for (pi, prim) in ent.prims.iter().enumerate() {
            match prim {
                Primitive::Brush(b) => {
                    let detail = b.faces.iter().any(|f| {
                        f.surface
                            .as_ref()
                            .is_some_and(|s| (s.contents.value() as i64) & BRUSH_DETAIL_MASK != 0)
                    });
                    let kind = if !worldspawn {
                        SurfaceKind::BrushEntity
                    } else if detail {
                        SurfaceKind::Detail
                    } else {
                        SurfaceKind::Structural
                    };

                    let geom = match brush_geometry(&b.faces) {
                        Ok(g) => g,
                        Err(e) => {
                            scene.skipped.push((ei, pi, e.to_string()));
                            continue;
                        }
                    };

                    if let Some(region) = opts.region {
                        if !geom.bounds().intersects(&region) {
                            continue;
                        }
                    }

                    match kind {
                        SurfaceKind::Structural => scene.counts.structural += 1,
                        SurfaceKind::Detail => scene.counts.detail += 1,
                        SurfaceKind::BrushEntity => scene.counts.brush_entity += 1,
                        SurfaceKind::Patch => {}
                    }

                    for &vi in &geom.off_grid_vertices(opts.grid) {
                        let p = geom.vertices[vi].to_vec3();
                        scene.off_grid_points.push(p);
                    }

                    for (fi, fg) in geom.faces.iter().enumerate() {
                        let Some(fg) = fg else { continue };
                        if !fg.contributes() {
                            continue;
                        }
                        let points: Vec<Vec3> = fg
                            .vertices
                            .iter()
                            .map(|&i| geom.vertices[i].to_vec3())
                            .collect();
                        for p in &points {
                            scene.bounds.extend(*p);
                        }
                        let shader = b.faces.get(fi).map(|f| f.shader.as_str()).unwrap_or("");
                        let caulk = is_invisible(shader, &opts.invisible_shader_fragments);
                        if caulk {
                            scene.counts.caulk_facets += 1;
                        }
                        scene.facets.push(Facet {
                            normal: fg.plane.to_plane().normal,
                            points,
                            kind,
                            is_caulk: caulk,
                            entity: ei,
                            primitive: pi,
                            face: fi,
                            area: face_area(&geom, fg),
                        });
                    }
                }

                Primitive::Patch(p) => {
                    scene.counts.patches += 1;
                    let caulk = is_invisible(&p.shader, &opts.invisible_shader_fragments);
                    let quads = tessellate_patch(p, opts.patch_subdivisions);
                    for q in quads {
                        if q.len() < 3 {
                            continue;
                        }
                        if let Some(region) = opts.region {
                            let mut bb = Aabb::EMPTY;
                            for v in &q {
                                bb.extend(*v);
                            }
                            if !bb.intersects(&region) {
                                continue;
                            }
                        }
                        for v in &q {
                            scene.bounds.extend(*v);
                        }
                        let normal = quad_normal(&q);
                        if caulk {
                            scene.counts.caulk_facets += 1;
                        }
                        scene.facets.push(Facet {
                            points: q,
                            normal,
                            kind: SurfaceKind::Patch,
                            is_caulk: caulk,
                            entity: ei,
                            primitive: pi,
                            face: 0,
                            area: 0.0,
                        });
                    }
                }

                Primitive::Raw(_) => {}
            }
        }
    }

    scene.counts.facets = scene.facets.len();
    scene
}

fn quad_normal(q: &[Vec3]) -> Vec3 {
    // Newell's method: correct for slightly non-planar quads, which tessellated Bézier
    // patches routinely are.
    let mut acc = Vec3::ZERO;
    for i in 0..q.len() {
        let a = q[i];
        let b = q[(i + 1) % q.len()];
        acc = acc
            + vec3(
                (a.y - b.y) * (a.z + b.z),
                (a.z - b.z) * (a.x + b.x),
                (a.x - b.x) * (a.y + b.y),
            );
    }
    acc.normalized().unwrap_or_else(|| vec3(0.0, 0.0, 1.0))
}

fn bez2(p0: Vec3, p1: Vec3, p2: Vec3, t: f64) -> Vec3 {
    let u = 1.0 - t;
    p0 * (u * u) + p1 * (2.0 * u * t) + p2 * (t * t)
}

/// Tessellate a patch into quads.
///
/// Quake 3 patches are **quadratic** Bézier surfaces built from overlapping 3x3 blocks of
/// control points, so a `w x h` patch contains `(w-1)/2` by `(h-1)/2` such blocks. Control
/// points are stored width-major, matching the file.
///
/// A patch whose dimensions are not valid odd numbers ≥ 3 falls back to drawing the control
/// mesh directly: less accurate, but a malformed patch still appears rather than vanishing
/// from the render with no explanation.
fn tessellate_patch(patch: &nrc_core::model::Patch, subdivisions: usize) -> Vec<Vec<Vec3>> {
    let (w, h) = (patch.width(), patch.height());
    if !patch.dimensions_consistent() || w < 2 || h < 2 {
        return Vec::new();
    }

    let ctrl = |i: usize, j: usize| -> Vec3 {
        let p = &patch.rows[i][j];
        if p.len() >= 3 {
            vec3(p[0].value(), p[1].value(), p[2].value())
        } else {
            Vec3::ZERO
        }
    };

    // Fallback: draw the control mesh.
    if w < 3 || h < 3 || w % 2 == 0 || h % 2 == 0 {
        let mut out = Vec::new();
        for i in 0..w - 1 {
            for j in 0..h - 1 {
                out.push(vec![
                    ctrl(i, j),
                    ctrl(i + 1, j),
                    ctrl(i + 1, j + 1),
                    ctrl(i, j + 1),
                ]);
            }
        }
        return out;
    }

    let n = subdivisions.clamp(1, 16);
    let mut out = Vec::new();
    for si in 0..(w - 1) / 2 {
        for sj in 0..(h - 1) / 2 {
            let c = |a: usize, b: usize| ctrl(2 * si + a, 2 * sj + b);
            let surface = |u: f64, v: f64| -> Vec3 {
                let a = bez2(c(0, 0), c(0, 1), c(0, 2), v);
                let b = bez2(c(1, 0), c(1, 1), c(1, 2), v);
                let d = bez2(c(2, 0), c(2, 1), c(2, 2), v);
                bez2(a, b, d, u)
            };
            for iu in 0..n {
                for iv in 0..n {
                    let (u0, u1) = (iu as f64 / n as f64, (iu + 1) as f64 / n as f64);
                    let (v0, v1) = (iv as f64 / n as f64, (iv + 1) as f64 / n as f64);
                    out.push(vec![
                        surface(u0, v0),
                        surface(u1, v0),
                        surface(u1, v1),
                        surface(u0, v1),
                    ]);
                }
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use nrc_core::parse_map;

    const BOX64: &str = "{\n\
        ( 0 0 64 ) ( 0 1 64 ) ( 1 0 64 ) t/top 0 0 0 0.5 0.5 0 0 0\n\
        ( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) t/bot 0 0 0 0.5 0.5 0 0 0\n\
        ( 0 0 0 ) ( 0 0 1 ) ( 1 0 0 ) common/caulk 0 0 0 0.5 0.5 0 0 0\n\
        ( 0 64 0 ) ( 1 64 0 ) ( 0 64 1 ) t/side 0 0 0 0.5 0.5 0 0 0\n\
        ( 0 0 0 ) ( 0 1 0 ) ( 0 0 1 ) t/side 0 0 0 0.5 0.5 0 0 0\n\
        ( 64 0 0 ) ( 64 0 1 ) ( 64 1 0 ) t/side 0 0 0 0.5 0.5 0 0 0\n\
        }\n";

    fn world(prims: &str) -> Map {
        parse_map(&format!("{{\n\"classname\" \"worldspawn\"\n{prims}}}\n")).unwrap()
    }

    /// An axis-aligned box brush. Point order gives outward normals under q3's convention
    /// (`n = cross(c - a, b - a)`, solid at `n · p <= d`).
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
    fn a_box_yields_six_facets_with_outward_normals() {
        let s = build(&world(BOX64), &SceneOptions::default());
        assert_eq!(s.facets.len(), 6);
        assert_eq!(s.counts.structural, 1);
        assert_eq!(s.counts.detail, 0);
        assert!(s.skipped.is_empty());

        // Every facet's normal must point away from the box centre.
        let centre = vec3(32.0, 32.0, 32.0);
        for f in &s.facets {
            let to_face = f.points[0] - centre;
            assert!(
                f.normal.dot(to_face) > 0.0,
                "normal {:?} points inward",
                f.normal
            );
            assert_eq!(f.points.len(), 4);
            assert_eq!(f.area, 4096.0);
        }
        assert_eq!(s.bounds.min, Vec3::ZERO);
        assert_eq!(s.bounds.max, vec3(64.0, 64.0, 64.0));
    }

    #[test]
    fn invisible_shaders_are_flagged_from_configuration() {
        let s = build(&world(BOX64), &SceneOptions::default());
        assert_eq!(s.counts.caulk_facets, 1);
        assert_eq!(s.facets.iter().filter(|f| f.is_caulk).count(), 1);

        // Override the list and nothing is invisible: the rule is data, not code.
        let opts = SceneOptions {
            invisible_shader_fragments: vec!["nothing_matches".into()],
            ..Default::default()
        };
        assert_eq!(build(&world(BOX64), &opts).counts.caulk_facets, 0);
    }

    #[test]
    fn the_detail_bit_changes_classification() {
        let detail = BOX64.replace("0.5 0.5 0 0 0", "0.5 0.5 134217728 0 0");
        let s = build(&world(&detail), &SceneOptions::default());
        assert_eq!(s.counts.detail, 1);
        assert_eq!(s.counts.structural, 0);
        assert!(s.facets.iter().all(|f| f.kind == SurfaceKind::Detail));
    }

    #[test]
    fn brush_entities_are_distinguished_from_worldspawn() {
        let src = format!(
            "{{\n\"classname\" \"worldspawn\"\n{BOX64}}}\n\
             {{\n\"classname\" \"group_entity_a\"\n{BOX64}}}\n"
        );
        let s = build(&parse_map(&src).unwrap(), &SceneOptions::default());
        assert_eq!(s.counts.structural, 1);
        assert_eq!(s.counts.brush_entity, 1);
    }

    #[test]
    fn point_entities_contribute_a_marker_and_extend_the_bounds() {
        let src = format!(
            "{{\n\"classname\" \"worldspawn\"\n{BOX64}}}\n\
             {{\n\"classname\" \"point_entity_a\"\n\"origin\" \"512 0 24\"\n}}\n"
        );
        let s = build(&parse_map(&src).unwrap(), &SceneOptions::default());
        assert_eq!(s.entity_points.len(), 1);
        assert_eq!(s.entity_points[0].1, vec3(512.0, 0.0, 24.0));
        assert_eq!(s.bounds.max.x, 512.0);
    }

    #[test]
    fn an_unevaluable_brush_is_reported_not_silently_dropped() {
        let three = "{\n\
            ( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 1 0 ) ( 0 0 1 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 0 1 ) ( 1 0 0 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            }\n";
        let s = build(&world(three), &SceneOptions::default());
        assert!(s.facets.is_empty());
        assert_eq!(s.skipped.len(), 1);
        assert!(s.skipped[0].2.contains("at least 4"));
    }

    #[test]
    fn off_grid_vertices_are_collected_for_marking() {
        let opts = SceneOptions {
            grid: 128,
            ..Default::default()
        };
        let s = build(&world(BOX64), &opts);
        // Only the origin corner is on a 128 grid; the other seven are not.
        assert_eq!(s.off_grid_points.len(), 7);
    }

    #[test]
    fn a_region_filter_excludes_distant_geometry() {
        let near = box_brush(0, 0, 0, 64, 64, 64);
        let far = box_brush(4096, 0, 0, 4160, 64, 64);
        let map = world(&format!("{near}{far}"));
        let all = build(&map, &SceneOptions::default());
        assert_eq!(all.facets.len(), 12, "both boxes present without a region");

        let mut region = Aabb::EMPTY;
        region.extend(vec3(-16.0, -16.0, -16.0));
        region.extend(vec3(80.0, 80.0, 80.0));
        let clipped = build(
            &map,
            &SceneOptions {
                region: Some(region),
                ..Default::default()
            },
        );
        assert_eq!(clipped.facets.len(), 6, "only the near box should survive");
        assert_eq!(clipped.bounds.max.x, 64.0);
    }

    #[test]
    fn a_quadratic_patch_tessellates_into_the_expected_number_of_quads() {
        // 3x3 control points is one Bezier span, so n*n quads.
        let patch = "{\npatchDef2\n{\nx/y\n( 3 3 0 0 0 )\n(\n\
            ( ( 0 0 0 0 0 ) ( 0 64 32 0 0 ) ( 0 128 0 0 0 ) )\n\
            ( ( 64 0 0 0 0 ) ( 64 64 32 0 0 ) ( 64 128 0 0 0 ) )\n\
            ( ( 128 0 0 0 0 ) ( 128 64 32 0 0 ) ( 128 128 0 0 0 ) )\n\
            )\n}\n}\n";
        let opts = SceneOptions {
            patch_subdivisions: 3,
            ..Default::default()
        };
        let s = build(&world(patch), &opts);
        assert_eq!(s.counts.patches, 1);
        assert_eq!(s.facets.len(), 9, "3 subdivisions squared");
        assert!(s.facets.iter().all(|f| f.kind == SurfaceKind::Patch));

        // The surface must interpolate its corners and bulge toward the control point
        // midway, not sit flat. Bezier evaluation accumulates float error, so the corner
        // check is approximate — exactness is the kernel's job, not the renderer's.
        assert!(
            s.bounds.max.z > 0.0 && s.bounds.max.z <= 32.0,
            "{:?}",
            s.bounds
        );
        assert!(s.bounds.min.x.abs() < 1e-9, "{:?}", s.bounds.min);
        assert!((s.bounds.max.x - 128.0).abs() < 1e-9, "{:?}", s.bounds.max);
    }

    #[test]
    fn a_five_by_three_patch_has_two_spans() {
        let rows: String = (0..5)
            .map(|i| {
                format!(
                    "( ( {x} 0 0 0 0 ) ( {x} 64 0 0 0 ) ( {x} 128 0 0 0 ) )\n",
                    x = i * 32
                )
            })
            .collect();
        let patch = format!("{{\npatchDef2\n{{\nx/y\n( 5 3 0 0 0 )\n(\n{rows})\n}}\n}}\n");
        let opts = SceneOptions {
            patch_subdivisions: 2,
            ..Default::default()
        };
        let s = build(&world(&patch), &opts);
        assert_eq!(s.facets.len(), 2 * 2 * 2, "two spans, 2x2 quads each");
    }

    #[test]
    fn an_even_dimensioned_patch_falls_back_to_the_control_mesh() {
        // Not a legal Bezier layout, but it must still be visible.
        let patch = "{\npatchDef2\n{\nx/y\n( 2 2 0 0 0 )\n(\n\
            ( ( 0 0 0 0 0 ) ( 0 64 0 0 0 ) )\n\
            ( ( 64 0 0 0 0 ) ( 64 64 0 0 0 ) )\n\
            )\n}\n}\n";
        let s = build(&world(patch), &SceneOptions::default());
        assert_eq!(s.facets.len(), 1, "one control-mesh quad");
    }

    #[test]
    fn an_inconsistent_patch_produces_no_geometry_rather_than_panicking() {
        let patch = "{\npatchDef2\n{\nx/y\n( 3 3 0 0 0 )\n(\n\
            ( ( 0 0 0 0 0 ) ( 0 64 0 0 0 ) )\n)\n}\n}\n";
        let s = build(&world(patch), &SceneOptions::default());
        assert_eq!(s.counts.patches, 1);
        assert!(s.facets.is_empty());
    }

    #[test]
    fn an_empty_map_builds_an_empty_scene() {
        let s = build(&parse_map("").unwrap(), &SceneOptions::default());
        assert!(s.facets.is_empty());
        assert!(s.bounds.is_empty());
        assert_eq!(s.counts.facets, 0);
    }
}
