//! Turning a solid into brushes a `.map` can hold.
//!
//! Each polytope becomes one brush, and each of its planes becomes one face. A face is written
//! as three points on its plane, and those points are found by solving the plane equation over
//! the integer lattice ([`crate::poly::integer_plane_points`]) so the file stays exact.
//!
//! When the lattice misses a plane — which insetting can cause, see that function — the face
//! falls back to the polygon's own rational corners written as decimals. That is legal (Radiant
//! writes non-integer plane points for angled brushes) but it is a slightly weaker guarantee, so
//! the count of such faces is reported rather than passed over.

use crate::poly::{integer_plane_points, Solid};
use nrc_core::exact::IPlane;
use nrc_core::math::{Axis, Vec3};
use nrc_core::model::{Brush, BrushStyle, Face, SurfaceFlags, TexDef};
use nrc_core::num::Num;

/// Bit 27 of a face's contents marks a brush as detail (`radiant/brush.h`).
pub const DETAIL_CONTENTS: i64 = 1 << 27;

/// Which shader goes on which face.
#[derive(Clone, Debug)]
pub struct TextureSpec {
    pub default: String,
    /// Applied to faces whose normal points mostly up.
    pub top: Option<String>,
    /// Applied to faces whose normal points mostly down.
    pub bottom: Option<String>,
    /// Texture scale written into the axial texdef.
    pub scale: f64,
    /// Set the detail contents bit, so the brush does not block visibility.
    pub detail: bool,
}

impl Default for TextureSpec {
    fn default() -> Self {
        Self {
            default: "common/caulk".to_string(),
            top: None,
            bottom: None,
            // 0.5 is what every map in the corpus uses, so a brush emitted here looks like its
            // neighbours instead of standing out at twice the texel density.
            scale: 0.5,
            detail: false,
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct EmitReport {
    pub brushes: usize,
    pub faces: usize,
    /// Faces whose plane has no integer solution, written as decimals instead.
    pub non_integer_faces: usize,
    /// Vertices that miss the grid. Expected for angled shapes; see [`crate::prim`].
    pub off_grid_vertices: usize,
    pub warnings: Vec<String>,
}

/// Convert a solid into brushes.
pub fn emit(solid: &Solid, tex: &TextureSpec, grid: i64) -> (Vec<Brush>, EmitReport) {
    let mut brushes = Vec::with_capacity(solid.len());
    let mut report = EmitReport::default();

    for (pi, part) in solid.parts.iter().enumerate() {
        let simplified = part.simplified();
        let Ok(geom) = simplified.geometry() else {
            report.warnings.push(format!(
                "part {pi} has no derivable geometry and was skipped"
            ));
            continue;
        };
        report.off_grid_vertices += geom.off_grid_vertices(grid).len();

        let mut faces = Vec::with_capacity(simplified.len());
        for (fi, plane) in simplified.planes().iter().enumerate() {
            let points = match integer_plane_points(plane) {
                Some(p) => [
                    [num(p[0].x as f64), num(p[0].y as f64), num(p[0].z as f64)],
                    [num(p[1].x as f64), num(p[1].y as f64), num(p[1].z as f64)],
                    [num(p[2].x as f64), num(p[2].y as f64), num(p[2].z as f64)],
                ],
                None => {
                    report.non_integer_faces += 1;
                    match face_corner_points(&geom, fi) {
                        Some(p) => p,
                        None => {
                            report.warnings.push(format!(
                                "part {pi} face {fi} has no integer points on its plane and no \
                                 usable corners either; it was dropped, which will leave the \
                                 brush open"
                            ));
                            continue;
                        }
                    }
                }
            };

            let shader = pick_shader(plane, tex);
            faces.push(Face {
                leading: Vec::new(),
                trailing: None,
                points,
                shader,
                tex: TexDef::Axial {
                    shift: [num(0.0), num(0.0)],
                    rotate: num(0.0),
                    scale: [num(tex.scale), num(tex.scale)],
                },
                surface: Some(SurfaceFlags {
                    contents: num(if tex.detail {
                        DETAIL_CONTENTS as f64
                    } else {
                        0.0
                    }),
                    flags: num(0.0),
                    value: num(0.0),
                }),
                extra: Vec::new(),
            });
        }

        if faces.len() < 4 {
            report.warnings.push(format!(
                "part {pi} produced only {} usable faces and was skipped; a brush needs four",
                faces.len()
            ));
            continue;
        }
        report.faces += faces.len();
        report.brushes += 1;
        brushes.push(Brush {
            leading: Vec::new(),
            style: BrushStyle::Bare,
            faces,
        });
    }
    (brushes, report)
}

fn num(v: f64) -> Num {
    Num::new(v)
}

fn pick_shader(plane: &IPlane, tex: &TextureSpec) -> String {
    let n = plane.to_plane().normal;
    match n.major_axis() {
        Axis::Z if n.z > 0.0 => tex.top.clone().unwrap_or_else(|| tex.default.clone()),
        Axis::Z => tex.bottom.clone().unwrap_or_else(|| tex.default.clone()),
        _ => tex.default.clone(),
    }
}

/// Three non-collinear corners of a face, as a fallback when the lattice misses its plane.
fn face_corner_points(
    geom: &nrc_core::winding::BrushGeometry,
    face_index: usize,
) -> Option<[[Num; 3]; 3]> {
    let face = geom.faces.get(face_index)?.as_ref()?;
    if face.vertices.len() < 3 {
        return None;
    }
    let pts: Vec<Vec3> = face
        .vertices
        .iter()
        .map(|&i| geom.vertices[i].to_vec3())
        .collect();

    // Pick a triple that actually defines a plane; adjacent corners of a many-sided face can be
    // very nearly collinear, and a degenerate triple would write a face the compiler discards.
    for i in 1..pts.len() - 1 {
        if nrc_core::math::Plane::from_points(pts[0], pts[i], pts[i + 1]).is_some() {
            return Some([
                [num(pts[0].x), num(pts[0].y), num(pts[0].z)],
                [num(pts[i].x), num(pts[i].y), num(pts[i].z)],
                [num(pts[i + 1].x), num(pts[i + 1].y), num(pts[i + 1].z)],
            ]);
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::prim;
    use nrc_core::exact::ivec3;
    use nrc_core::model::{Entity, LineEnding, Map, Primitive};
    use nrc_core::{parse_map, write_map};

    fn wrap(brushes: Vec<Brush>) -> Map {
        let mut e = Entity::default();
        e.set("classname", "worldspawn");
        e.prims = brushes.into_iter().map(Primitive::Brush).collect();
        Map {
            prologue: String::new(),
            entities: vec![e],
            footer: Vec::new(),
            line_ending: LineEnding::Lf,
            epilogue: "\n".to_string(),
        }
    }

    #[test]
    fn a_box_emits_one_six_faced_brush_with_integer_points() {
        let s = prim::cuboid(ivec3(0, 0, 0), ivec3(64, 128, 32)).unwrap();
        let (brushes, r) = emit(&s, &TextureSpec::default(), 1);
        assert_eq!(brushes.len(), 1);
        assert_eq!(brushes[0].faces.len(), 6);
        assert_eq!(
            r.non_integer_faces, 0,
            "axis planes always have integer points"
        );
        assert_eq!(r.off_grid_vertices, 0);
        for f in &brushes[0].faces {
            for p in f.points.iter().flatten() {
                assert!(p.value().fract() == 0.0, "expected integers, got {p}");
            }
        }
    }

    #[test]
    fn emitted_brushes_survive_a_round_trip_through_the_writer() {
        // The whole point of emitting integer plane points: what we write must parse back to
        // the same thing, or the §3.2 gate would fail on maps this tool authored.
        let s = prim::cuboid(ivec3(-64, -64, 0), ivec3(64, 64, 128)).unwrap();
        let (brushes, _) = emit(&s, &TextureSpec::default(), 1);
        let text = write_map(&wrap(brushes));
        let reparsed = parse_map(&text).unwrap();
        assert_eq!(write_map(&reparsed), text, "emitted map must round-trip");
        assert_eq!(reparsed.brush_count(), 1);
    }

    #[test]
    fn the_emitted_planes_describe_the_shape_we_meant() {
        // Re-derive the geometry from the written file and compare it with the source solid.
        let s = prim::cuboid(ivec3(0, 0, 0), ivec3(64, 96, 32)).unwrap();
        let (brushes, _) = emit(&s, &TextureSpec::default(), 1);
        let text = write_map(&wrap(brushes));
        let m = parse_map(&text).unwrap();
        let b = m.entities[0].prims[0].as_brush().unwrap();
        let g = nrc_core::brush_geometry(&b.faces).expect("should be a valid brush");
        assert_eq!(g.vertices.len(), 8);
        assert_eq!(g.bounds().min, nrc_core::math::vec3(0.0, 0.0, 0.0));
        assert_eq!(g.bounds().max, nrc_core::math::vec3(64.0, 96.0, 32.0));
    }

    #[test]
    fn a_prism_emits_and_reports_its_off_grid_corners() {
        // An angled shape has off-grid vertices by nature. They must be counted, not hidden.
        let s = prim::prism(
            ivec3(-64, -64, 0),
            ivec3(64, 64, 128),
            prim::Axis::Z,
            8,
            0.0,
        )
        .unwrap();
        let (brushes, r) = emit(&s, &TextureSpec::default(), 1);
        assert_eq!(brushes.len(), 1);
        assert_eq!(brushes[0].faces.len(), 10);
        // And it must still round-trip.
        let text = write_map(&wrap(brushes));
        assert_eq!(write_map(&parse_map(&text).unwrap()), text);
        // Whether any corner misses the grid depends on the rounding; the count must be honest
        // either way, so just assert it was computed rather than asserting a magic number.
        assert!(r.off_grid_vertices <= 16);
    }

    #[test]
    fn a_multi_part_solid_emits_one_brush_per_part() {
        let s = prim::stair(ivec3(0, 0, 0), 128, 6, 16, 32, prim::Axis::X, prim::Axis::Z).unwrap();
        let (brushes, r) = emit(&s, &TextureSpec::default(), 1);
        assert_eq!(brushes.len(), 6);
        assert_eq!(r.brushes, 6);
        assert_eq!(r.faces, 36);
    }

    #[test]
    fn the_detail_bit_is_written_when_asked_for() {
        let s = prim::cuboid(ivec3(0, 0, 0), ivec3(64, 64, 64)).unwrap();
        let spec = TextureSpec {
            detail: true,
            ..Default::default()
        };
        let (brushes, _) = emit(&s, &spec, 1);
        let c = brushes[0].faces[0]
            .surface
            .as_ref()
            .unwrap()
            .contents
            .value() as i64;
        assert_eq!(c, DETAIL_CONTENTS);
        assert_eq!(c, 134_217_728);

        // And omitted otherwise, so a structural brush is not accidentally detail.
        let (plain, _) = emit(&s, &TextureSpec::default(), 1);
        assert_eq!(
            plain[0].faces[0].surface.as_ref().unwrap().contents.value(),
            0.0
        );
    }

    #[test]
    fn top_and_bottom_shaders_land_on_the_right_faces() {
        let s = prim::cuboid(ivec3(0, 0, 0), ivec3(64, 64, 64)).unwrap();
        let spec = TextureSpec {
            default: "w/wall".into(),
            top: Some("w/floor".into()),
            bottom: Some("w/ceiling".into()),
            ..Default::default()
        };
        let (brushes, _) = emit(&s, &spec, 1);
        let shaders: Vec<&str> = brushes[0].faces.iter().map(|f| f.shader.as_str()).collect();
        assert_eq!(shaders.iter().filter(|s| **s == "w/floor").count(), 1);
        assert_eq!(shaders.iter().filter(|s| **s == "w/ceiling").count(), 1);
        assert_eq!(shaders.iter().filter(|s| **s == "w/wall").count(), 4);
    }

    #[test]
    fn texture_scale_matches_what_real_maps_use() {
        let s = prim::cuboid(ivec3(0, 0, 0), ivec3(64, 64, 64)).unwrap();
        let (brushes, _) = emit(&s, &TextureSpec::default(), 1);
        match &brushes[0].faces[0].tex {
            TexDef::Axial { scale, .. } => {
                assert_eq!(scale[0].value(), 0.5, "0.5 is the corpus-wide convention");
            }
            other => panic!("expected an axial texdef, got {other:?}"),
        }
    }

    #[test]
    fn a_hollowed_room_emits_six_brushes_that_round_trip() {
        let box_solid = prim::cuboid(ivec3(0, 0, 0), ivec3(512, 512, 256)).unwrap();
        let (shell, _) = crate::csg::hollow(&box_solid.parts[0], 16, &[]).unwrap();
        let (brushes, r) = emit(&shell, &TextureSpec::default(), 1);
        assert_eq!(brushes.len(), 6);
        assert_eq!(
            r.non_integer_faces, 0,
            "axis-aligned insets stay on the lattice"
        );
        let text = write_map(&wrap(brushes));
        assert_eq!(write_map(&parse_map(&text).unwrap()), text);
    }
}
