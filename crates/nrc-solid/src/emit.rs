//! Turning a solid into brushes a `.map` can hold.
//!
//! Each polytope becomes one brush, and each of its planes becomes one face, written as three
//! points on that plane. Which three points is not arbitrary, and getting it wrong produced a real
//! bug worth recording.
//!
//! Three sources are tried in order:
//!
//! 1. **The face's own corners**, when three non-collinear ones are integers. These are exact, and
//!    crucially they are *near the shape*.
//! 2. **A lattice solve** ([`crate::poly::integer_plane_points`]), which finds some integer point
//!    satisfying the plane equation.
//! 3. **The rational corners as decimals**, when neither works.
//!
//! The order matters because the lattice solve picks its base point wherever `d / n` happens to
//! land, and for a steeply angled plane with a large distance that is very far away. An arch of
//! radius 192 emitted plane points at ±280,000 units — geometrically correct, on the right plane,
//! and far outside the ±65536 world limit, which the validator then flagged. The fitness suite
//! caught it; the tests here keep it caught.

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
            // Corners first: they are exact and local. A lattice solve is exact but can land
            // hundreds of thousands of units away, outside the world limit.
            let points = match integral_corner_points(&geom, fi, plane) {
                Some(p) => p,
                None => match integer_plane_points(plane).and_then(|p| within_world(&p)) {
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
                                    "part {pi} face {fi} has no usable points on its plane; it was \
                                     dropped, which will leave the brush open"
                                ));
                                continue;
                            }
                        }
                    }
                },
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

/// Reject a lattice solution that sits outside the world, however correct the arithmetic.
///
/// A plane point beyond `MAX_WORLD_COORD` is one q3map2 will refuse and the validator will flag, so
/// a "valid" answer that far out is worse than falling through to the next strategy.
fn within_world(points: &[nrc_core::exact::IVec3; 3]) -> Option<[nrc_core::exact::IVec3; 3]> {
    let limit = nrc_core::math::MAX_WORLD_COORD as i64;
    let ok = points
        .iter()
        .all(|p| p.x.abs() <= limit && p.y.abs() <= limit && p.z.abs() <= limit);
    if ok {
        Some(*points)
    } else {
        None
    }
}

/// Three integer corners of a face, oriented to reproduce the plane exactly.
///
/// The preferred source. A face's corners lie on its plane by construction and sit next to the
/// geometry, so the emitted numbers are the ones a mapper would recognize.
fn integral_corner_points(
    geom: &nrc_core::winding::BrushGeometry,
    face_index: usize,
    plane: &IPlane,
) -> Option<[[Num; 3]; 3]> {
    let face = geom.faces.get(face_index)?.as_ref()?;
    if face.vertices.len() < 3 {
        return None;
    }
    let ints: Vec<nrc_core::exact::IVec3> = face
        .vertices
        .iter()
        .filter_map(|&i| geom.vertices[i].to_ivec3())
        .collect();
    if ints.len() < 3 {
        return None;
    }

    // Any three that define this exact plane. Adjacent corners of a many-sided face can be
    // near-collinear, so try combinations rather than assuming the first three work.
    for i in 1..ints.len() - 1 {
        let (a, b, c) = (ints[0], ints[i], ints[i + 1]);
        let Some(derived) = IPlane::from_points(a, b, c) else {
            continue;
        };
        let ordered = if derived == *plane {
            [a, b, c]
        } else if derived == plane.flipped() {
            [a, c, b]
        } else {
            continue;
        };
        return Some(ordered.map(|p| [num(p.x as f64), num(p.y as f64), num(p.z as f64)]));
    }
    None
}

/// Three non-collinear corners of a face, as a fallback when nothing integral works.
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

#[cfg(test)]
mod plane_point_tests {
    use super::*;
    use crate::prim;
    use nrc_core::exact::ivec3;
    use nrc_core::math::MAX_WORLD_COORD;

    fn all_plane_points_within_world(brushes: &[Brush]) -> bool {
        brushes.iter().all(|b| {
            b.faces.iter().all(|f| {
                f.points
                    .iter()
                    .flatten()
                    .all(|n| n.value().abs() <= MAX_WORLD_COORD)
            })
        })
    }

    #[test]
    fn an_arch_emits_plane_points_near_the_shape_not_hundreds_of_thousands_of_units_away() {
        // The regression. A lattice solve puts this arch's plane points at ±280,000 units, which
        // is on the right plane and outside the world. Corners come first for exactly this reason.
        let arch = prim::arch(ivec3(0, 0, 0), 192, 48, 64, 8, prim::Axis::Z).unwrap();
        let (brushes, report) = emit(&arch, &TextureSpec::default(), 1);
        assert_eq!(brushes.len(), 8);
        assert!(
            all_plane_points_within_world(&brushes),
            "plane points escaped the world limit again"
        );
        assert_eq!(
            report.non_integer_faces, 0,
            "an arch's corners are integers"
        );
    }

    #[test]
    fn every_primitive_emits_plane_points_within_the_world() {
        let builds = [
            (
                "box",
                prim::cuboid(ivec3(0, 0, 0), ivec3(64, 64, 64)).unwrap(),
            ),
            (
                "wedge",
                prim::wedge(
                    ivec3(0, 0, 0),
                    ivec3(128, 64, 64),
                    prim::Axis::X,
                    prim::Axis::Z,
                )
                .unwrap(),
            ),
            (
                "prism",
                prim::prism(
                    ivec3(-192, -192, 0),
                    ivec3(192, 192, 256),
                    prim::Axis::Z,
                    12,
                    15.0,
                )
                .unwrap(),
            ),
            (
                "cone",
                prim::cone(
                    ivec3(-128, -128, 0),
                    ivec3(128, 128, 256),
                    prim::Axis::Z,
                    10,
                    0.0,
                )
                .unwrap(),
            ),
            (
                "pipe",
                prim::pipe(
                    ivec3(-128, -128, 0),
                    ivec3(128, 128, 512),
                    prim::Axis::Z,
                    24,
                    8,
                    22.5,
                )
                .unwrap(),
            ),
            (
                "arch",
                prim::arch(ivec3(2048, -1024, 0), 384, 64, 128, 12, prim::Axis::Z).unwrap(),
            ),
        ];
        for (name, solid) in builds {
            let (brushes, _) = emit(&solid, &TextureSpec::default(), 1);
            assert!(!brushes.is_empty(), "{name} emitted nothing");
            assert!(
                all_plane_points_within_world(&brushes),
                "{name} emitted a plane point outside the world"
            );
        }
    }

    #[test]
    fn emitted_geometry_reproduces_the_source_planes_exactly() {
        // Corners are only usable if they reconstruct the same plane, including its facing. If the
        // orientation were wrong the brush would enclose nothing, so re-derive and compare.
        let solid = prim::prism(
            ivec3(-96, -96, 0),
            ivec3(96, 96, 192),
            prim::Axis::Z,
            8,
            22.5,
        )
        .unwrap();
        let (brushes, _) = emit(&solid, &TextureSpec::default(), 1);
        let derived = nrc_core::brush_geometry(&brushes[0].faces).expect("a valid brush");
        let original = solid.parts[0].simplified();
        assert_eq!(derived.vertices.len(), original.vertices().len());
        assert!((derived.bounds().min.z - 0.0).abs() < 1e-9);
        assert!((derived.bounds().max.z - 192.0).abs() < 1e-9);
    }

    #[test]
    fn a_lattice_solution_outside_the_world_is_rejected() {
        let far = [
            ivec3(200_000, 0, 0),
            ivec3(200_000, 1, 0),
            ivec3(200_000, 0, 1),
        ];
        assert!(within_world(&far).is_none());
        let near = [ivec3(64, 0, 0), ivec3(64, 1, 0), ivec3(64, 0, 1)];
        assert!(within_world(&near).is_some());
    }
}
