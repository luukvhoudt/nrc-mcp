//! The Solid IR (§4.1): a tree the agent authors, which compiles to valid brushes.
//!
//! The central property is that **there is no way to write an invalid program**. Every node
//! evaluates to a set of convex polytopes, every operator preserves convexity, and every
//! failure is "this shape is wrong" with a reason — never "this file is corrupt". Errors carry
//! the path to the node that failed, because a nested tree with a bad parameter is otherwise
//! very hard to debug from the outside.
//!
//! Evaluation is bounded on three axes: tree depth, node count and resulting part count. A
//! recursive IR arriving from outside the process needs all three, and hitting one says which.

use crate::csg;
use crate::poly::Solid;
use crate::prim::{self, Axis};
use nrc_core::exact::IVec3;

/// Deepest tree accepted. Real sculpting nests a handful of levels; hundreds means a mistake.
pub const MAX_DEPTH: usize = 64;
/// Most nodes accepted in one program.
pub const MAX_NODES: usize = 4096;

#[derive(Clone, Debug)]
pub enum Node {
    // --- primitives ------------------------------------------------------
    Box {
        min: IVec3,
        max: IVec3,
    },
    Wedge {
        min: IVec3,
        max: IVec3,
        along: Axis,
        up: Axis,
    },
    Prism {
        min: IVec3,
        max: IVec3,
        axis: Axis,
        sides: usize,
        start_deg: f64,
    },
    Cone {
        min: IVec3,
        max: IVec3,
        axis: Axis,
        sides: usize,
        start_deg: f64,
    },
    Pyramid {
        min: IVec3,
        max: IVec3,
        axis: Axis,
    },
    Stair {
        origin: IVec3,
        width: i64,
        steps: usize,
        rise: i64,
        run: i64,
        along: Axis,
        up: Axis,
    },
    Pipe {
        min: IVec3,
        max: IVec3,
        axis: Axis,
        wall: i64,
        sides: usize,
        start_deg: f64,
    },
    /// An explicit set of half-spaces.
    ///
    /// The escape hatch, and still safe by construction: an intersection of half-spaces is
    /// convex whatever the planes are. It exists because a collision hull fitted to a mesh is
    /// naturally a set of planes with chosen normals (a k-DOP), and forcing that through the
    /// parametric primitives would lose the point.
    Planes(Vec<nrc_core::exact::IPlane>),
    Arch {
        centre: IVec3,
        outer_radius: i64,
        thickness: i64,
        depth: i64,
        segments: usize,
        axis: Axis,
    },

    // --- operators -------------------------------------------------------
    Union(Vec<Node>),
    Intersect(Vec<Node>),
    Subtract {
        from: std::boxed::Box<Node>,
        cut: Vec<Node>,
    },
    Hollow {
        solid: std::boxed::Box<Node>,
        thickness: i64,
        open_faces: Vec<usize>,
    },
    /// Sugar for subtracting a box, which is what a doorway or window actually is. Named
    /// separately because it is the operation a caller reaches for, and naming it means the
    /// intent survives in the saved IR.
    CarveOpening {
        wall: std::boxed::Box<Node>,
        min: IVec3,
        max: IVec3,
    },
    Translate {
        node: std::boxed::Box<Node>,
        by: IVec3,
    },
    Mirror {
        node: std::boxed::Box<Node>,
        axis: Axis,
        at: i64,
    },
    Array {
        node: std::boxed::Box<Node>,
        count: usize,
        offset: IVec3,
    },
}

impl Node {
    pub fn kind(&self) -> &'static str {
        match self {
            Node::Box { .. } => "box",
            Node::Wedge { .. } => "wedge",
            Node::Prism { .. } => "prism",
            Node::Cone { .. } => "cone",
            Node::Pyramid { .. } => "pyramid",
            Node::Stair { .. } => "stair",
            Node::Pipe { .. } => "pipe",
            Node::Planes(_) => "planes",
            Node::Arch { .. } => "arch",
            Node::Union(_) => "union",
            Node::Intersect(_) => "intersect",
            Node::Subtract { .. } => "subtract",
            Node::Hollow { .. } => "hollow",
            Node::CarveOpening { .. } => "carve_opening",
            Node::Translate { .. } => "translate",
            Node::Mirror { .. } => "mirror",
            Node::Array { .. } => "array",
        }
    }

    pub fn children(&self) -> Vec<&Node> {
        match self {
            Node::Union(v) | Node::Intersect(v) => v.iter().collect(),
            Node::Subtract { from, cut } => {
                let mut out = vec![from.as_ref()];
                out.extend(cut.iter());
                out
            }
            Node::Hollow { solid, .. } => vec![solid.as_ref()],
            Node::CarveOpening { wall, .. } => vec![wall.as_ref()],
            Node::Translate { node, .. } | Node::Mirror { node, .. } | Node::Array { node, .. } => {
                vec![node.as_ref()]
            }
            _ => Vec::new(),
        }
    }

    pub fn node_count(&self) -> usize {
        1 + self
            .children()
            .iter()
            .map(|c| c.node_count())
            .sum::<usize>()
    }

    pub fn depth(&self) -> usize {
        1 + self.children().iter().map(|c| c.depth()).max().unwrap_or(0)
    }
}

/// What evaluation produced, including anything the caller should know but did not ask.
#[derive(Clone, Debug, Default)]
pub struct Evaluation {
    pub solid: Solid,
    /// Non-fatal facts: a rounded inset, a merge that could not be made.
    pub warnings: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EvalError {
    /// Path from the root, e.g. `subtract/cut[1]/prism`, so a nested failure is locatable.
    pub path: String,
    pub message: String,
}

impl std::fmt::Display for EvalError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.path, self.message)
    }
}

impl std::error::Error for EvalError {}

/// Evaluate an IR tree into a solid.
pub fn evaluate(root: &Node) -> Result<Evaluation, EvalError> {
    let nodes = root.node_count();
    if nodes > MAX_NODES {
        return Err(EvalError {
            path: root.kind().to_string(),
            message: format!("{nodes} nodes exceeds the limit of {MAX_NODES}"),
        });
    }
    let depth = root.depth();
    if depth > MAX_DEPTH {
        return Err(EvalError {
            path: root.kind().to_string(),
            message: format!("nesting {depth} deep exceeds the limit of {MAX_DEPTH}"),
        });
    }
    let mut out = Evaluation::default();
    out.solid = eval(root, root.kind().to_string(), &mut out.warnings)?;
    Ok(out)
}

fn at(path: &str, message: impl Into<String>) -> EvalError {
    EvalError {
        path: path.to_string(),
        message: message.into(),
    }
}

fn eval(node: &Node, path: String, warnings: &mut Vec<String>) -> Result<Solid, EvalError> {
    let built = match node {
        Node::Box { min, max } => prim::cuboid(*min, *max),
        Node::Wedge {
            min,
            max,
            along,
            up,
        } => prim::wedge(*min, *max, *along, *up),
        Node::Prism {
            min,
            max,
            axis,
            sides,
            start_deg,
        } => prim::prism(*min, *max, *axis, *sides, *start_deg),
        Node::Cone {
            min,
            max,
            axis,
            sides,
            start_deg,
        } => prim::cone(*min, *max, *axis, *sides, *start_deg),
        Node::Pyramid { min, max, axis } => prim::pyramid(*min, *max, *axis),
        Node::Planes(planes) => {
            if planes.len() < 4 {
                Err(format!(
                    "a shape needs at least 4 half-spaces to enclose a volume, got {}",
                    planes.len()
                ))
            } else {
                Ok(crate::poly::Solid::single(
                    crate::poly::Polytope::from_planes(planes.iter().copied()),
                ))
            }
        }
        Node::Stair {
            origin,
            width,
            steps,
            rise,
            run,
            along,
            up,
        } => prim::stair(*origin, *width, *steps, *rise, *run, *along, *up),
        Node::Pipe {
            min,
            max,
            axis,
            wall,
            sides,
            start_deg,
        } => prim::pipe(*min, *max, *axis, *wall, *sides, *start_deg),
        Node::Arch {
            centre,
            outer_radius,
            thickness,
            depth,
            segments,
            axis,
        } => prim::arch(*centre, *outer_radius, *thickness, *depth, *segments, *axis),

        Node::Union(parts) => {
            if parts.is_empty() {
                return Err(at(&path, "a union needs at least one shape"));
            }
            let mut acc = Solid::default();
            for (i, child) in parts.iter().enumerate() {
                let s = eval(child, format!("{path}/[{i}]{}", child.kind()), warnings)?;
                acc = csg::union(&acc, &s);
            }
            Ok(acc)
        }

        Node::Intersect(parts) => {
            if parts.len() < 2 {
                return Err(at(&path, "an intersection needs at least two shapes"));
            }
            let mut acc = eval(
                &parts[0],
                format!("{path}/[0]{}", parts[0].kind()),
                warnings,
            )?;
            for (i, child) in parts.iter().enumerate().skip(1) {
                let s = eval(child, format!("{path}/[{i}]{}", child.kind()), warnings)?;
                acc = csg::intersect(&acc, &s);
                if acc.is_empty() {
                    warnings.push(format!(
                        "{path}: the intersection became empty at shape {i}, so nothing \
                         downstream can produce geometry"
                    ));
                    break;
                }
            }
            Ok(acc)
        }

        Node::Subtract { from, cut } => {
            let mut acc = eval(from, format!("{path}/from:{}", from.kind()), warnings)?;
            for (i, child) in cut.iter().enumerate() {
                let c = eval(child, format!("{path}/cut[{i}]:{}", child.kind()), warnings)?;
                acc = csg::subtract(&acc, &c);
                if acc.is_empty() {
                    warnings.push(format!("{path}: cut {i} removed everything that was left"));
                    break;
                }
            }
            Ok(acc)
        }

        Node::CarveOpening { wall, min, max } => {
            let base = eval(wall, format!("{path}/wall:{}", wall.kind()), warnings)?;
            let opening = prim::cuboid(*min, *max).map_err(|e| at(&path, e))?;
            let result = csg::subtract(&base, &opening);
            if result.is_empty() {
                return Err(at(
                    &path,
                    "the opening removed the whole wall; check that it is smaller than the wall",
                ));
            }
            Ok(result)
        }

        Node::Hollow {
            solid,
            thickness,
            open_faces,
        } => {
            let inner = eval(solid, format!("{path}/solid:{}", solid.kind()), warnings)?;
            if inner.len() != 1 {
                return Err(at(
                    &path,
                    format!(
                        "hollow needs a single convex shape, but its input is {} parts. Hollow \
                         each part separately, or hollow the shape before combining it.",
                        inner.len()
                    ),
                ));
            }
            let (shell, warns) =
                csg::hollow(&inner.parts[0], *thickness, open_faces).map_err(|e| at(&path, e))?;
            for w in warns {
                warnings.push(format!("{path}: {w}"));
            }
            Ok(shell)
        }

        Node::Translate { node: inner, by } => {
            let s = eval(inner, format!("{path}/{}", inner.kind()), warnings)?;
            Ok(Solid::new(
                s.parts.iter().map(|p| p.translated(*by)).collect(),
            ))
        }

        Node::Mirror {
            node: inner,
            axis,
            at: k,
        } => {
            let s = eval(inner, format!("{path}/{}", inner.kind()), warnings)?;
            Ok(Solid::new(
                s.parts.iter().map(|p| p.mirrored(*axis, *k)).collect(),
            ))
        }

        Node::Array {
            node: inner,
            count,
            offset,
        } => {
            if *count == 0 {
                return Err(at(&path, "an array needs a count of at least 1"));
            }
            if offset.x == 0 && offset.y == 0 && offset.z == 0 && *count > 1 {
                return Err(at(
                    &path,
                    "an array with a zero offset would stack every copy in the same place",
                ));
            }
            let s = eval(inner, format!("{path}/{}", inner.kind()), warnings)?;
            let mut parts = Vec::new();
            for i in 0..*count {
                let by = nrc_core::exact::ivec3(
                    offset.x * i as i64,
                    offset.y * i as i64,
                    offset.z * i as i64,
                );
                parts.extend(s.parts.iter().map(|p| p.translated(by)));
                if parts.len() > csg::MAX_PARTS {
                    return Err(at(
                        &path,
                        format!(
                            "an array of {count} would produce more than {} brushes",
                            csg::MAX_PARTS
                        ),
                    ));
                }
            }
            Ok(Solid::new(parts))
        }
    };

    let solid = built.map_err(|e| at(&path, e))?.solid_parts_only();
    if solid.is_empty() {
        return Err(at(&path, "this shape encloses no volume"));
    }
    if solid.len() > csg::MAX_PARTS {
        return Err(at(
            &path,
            format!(
                "{} parts exceeds the limit of {}",
                solid.len(),
                csg::MAX_PARTS
            ),
        ));
    }
    Ok(solid)
}

#[cfg(test)]
mod tests {
    use super::*;
    use nrc_core::exact::ivec3;

    fn bx(x0: i64, y0: i64, z0: i64, x1: i64, y1: i64, z1: i64) -> Node {
        Node::Box {
            min: ivec3(x0, y0, z0),
            max: ivec3(x1, y1, z1),
        }
    }

    #[test]
    fn a_box_evaluates_to_one_brush() {
        let e = evaluate(&bx(0, 0, 0, 64, 64, 64)).unwrap();
        assert_eq!(e.solid.len(), 1);
        assert!(e.warnings.is_empty());
    }

    #[test]
    fn a_doorway_expressed_as_carve_opening_gives_three_brushes() {
        let ir = Node::CarveOpening {
            wall: std::boxed::Box::new(bx(0, 0, 0, 256, 16, 128)),
            min: ivec3(96, -8, 0),
            max: ivec3(160, 24, 96),
        };
        let e = evaluate(&ir).unwrap();
        assert_eq!(e.solid.len(), 3);
        assert!(
            !e.solid.contains(ivec3(128, 8, 48)),
            "the opening should be open"
        );
    }

    #[test]
    fn a_room_is_a_hollowed_box() {
        let ir = Node::Hollow {
            solid: std::boxed::Box::new(bx(0, 0, 0, 512, 512, 256)),
            thickness: 16,
            open_faces: vec![],
        };
        let e = evaluate(&ir).unwrap();
        assert_eq!(e.solid.len(), 6);
        assert!(!e.solid.contains(ivec3(256, 256, 128)));
    }

    #[test]
    fn a_room_with_a_door_composes_hollow_and_carve() {
        // The workflow §4.1 describes: hollow a box, then cut a doorway through one wall.
        let room = Node::Hollow {
            solid: std::boxed::Box::new(bx(0, 0, 0, 512, 512, 256)),
            thickness: 16,
            open_faces: vec![],
        };
        let ir = Node::Subtract {
            from: std::boxed::Box::new(room),
            cut: vec![bx(224, -8, 0, 288, 24, 112)],
        };
        let e = evaluate(&ir).unwrap();
        assert!(
            !e.solid.contains(ivec3(256, 8, 48)),
            "the doorway should be cut through"
        );
        assert!(
            e.solid.contains(ivec3(256, 8, 200)),
            "the wall above it should remain"
        );
        assert!(
            e.solid.contains(ivec3(256, 256, 8)),
            "the floor should remain"
        );
    }

    #[test]
    fn hollow_refuses_a_multi_part_input_and_says_what_to_do() {
        let ir = Node::Hollow {
            solid: std::boxed::Box::new(Node::Union(vec![
                bx(0, 0, 0, 64, 64, 64),
                bx(128, 0, 0, 192, 64, 64),
            ])),
            thickness: 8,
            open_faces: vec![],
        };
        let e = evaluate(&ir).unwrap_err();
        assert!(e.message.contains("single convex shape"), "{e}");
        assert!(
            e.message.contains("separately"),
            "should suggest a fix: {e}"
        );
    }

    #[test]
    fn errors_name_the_failing_node_by_path() {
        let ir = Node::Subtract {
            from: std::boxed::Box::new(bx(0, 0, 0, 64, 64, 64)),
            cut: vec![bx(0, 0, 0, 0, 0, 0)],
        };
        let e = evaluate(&ir).unwrap_err();
        assert!(e.path.contains("cut[0]"), "path was {:?}", e.path);
        assert!(e.path.contains("box"), "path was {:?}", e.path);
    }

    #[test]
    fn translate_and_mirror_are_exact() {
        let moved = Node::Translate {
            node: std::boxed::Box::new(bx(0, 0, 0, 64, 64, 64)),
            by: ivec3(128, 0, 0),
        };
        let e = evaluate(&moved).unwrap();
        assert!(e.solid.contains(ivec3(160, 32, 32)));
        assert!(!e.solid.contains(ivec3(32, 32, 32)));

        let mirrored = Node::Mirror {
            node: std::boxed::Box::new(bx(0, 0, 0, 64, 64, 64)),
            axis: Axis::X,
            at: 0,
        };
        let e = evaluate(&mirrored).unwrap();
        assert!(e.solid.contains(ivec3(-32, 32, 32)));
        assert!(!e.solid.contains(ivec3(32, 32, 32)));
    }

    #[test]
    fn mirroring_twice_returns_the_original() {
        let once = Node::Mirror {
            node: std::boxed::Box::new(bx(16, 0, 0, 64, 64, 64)),
            axis: Axis::X,
            at: 100,
        };
        let twice = Node::Mirror {
            node: std::boxed::Box::new(once),
            axis: Axis::X,
            at: 100,
        };
        let e = evaluate(&twice).unwrap();
        let orig = evaluate(&bx(16, 0, 0, 64, 64, 64)).unwrap();
        assert_eq!(e.solid.parts[0], orig.solid.parts[0]);
    }

    #[test]
    fn an_array_repeats_along_an_offset() {
        let ir = Node::Array {
            node: std::boxed::Box::new(bx(0, 0, 0, 32, 32, 32)),
            count: 4,
            offset: ivec3(64, 0, 0),
        };
        let e = evaluate(&ir).unwrap();
        assert_eq!(e.solid.len(), 4);
        assert!(e.solid.contains(ivec3(200, 16, 16)));
        assert!(!e.solid.contains(ivec3(48, 16, 16)), "gaps between copies");
    }

    #[test]
    fn an_array_with_no_offset_is_refused() {
        let ir = Node::Array {
            node: std::boxed::Box::new(bx(0, 0, 0, 32, 32, 32)),
            count: 4,
            offset: ivec3(0, 0, 0),
        };
        assert!(evaluate(&ir).unwrap_err().message.contains("same place"));
    }

    #[test]
    fn an_empty_union_or_short_intersection_is_refused() {
        assert!(evaluate(&Node::Union(vec![]))
            .unwrap_err()
            .message
            .contains("at least one"));
        assert!(evaluate(&Node::Intersect(vec![bx(0, 0, 0, 8, 8, 8)]))
            .unwrap_err()
            .message
            .contains("at least two"));
    }

    #[test]
    fn a_subtraction_that_removes_everything_reports_it_rather_than_returning_nothing() {
        let ir = Node::Subtract {
            from: std::boxed::Box::new(bx(0, 0, 0, 64, 64, 64)),
            cut: vec![bx(-8, -8, -8, 72, 72, 72)],
        };
        let e = evaluate(&ir).unwrap_err();
        assert!(e.message.contains("encloses no volume"), "{e}");
    }

    #[test]
    fn nesting_and_node_count_are_bounded() {
        let mut deep = bx(0, 0, 0, 64, 64, 64);
        for _ in 0..MAX_DEPTH + 2 {
            deep = Node::Translate {
                node: std::boxed::Box::new(deep),
                by: ivec3(1, 0, 0),
            };
        }
        let e = evaluate(&deep).unwrap_err();
        assert!(e.message.contains("nesting"), "{e}");

        let wide = Node::Union(
            (0..MAX_NODES + 2)
                .map(|i| bx(0, 0, 0, 8, 8, 8 + i as i64))
                .collect(),
        );
        assert!(evaluate(&wide)
            .unwrap_err()
            .message
            .contains("nodes exceeds"));
    }

    #[test]
    fn node_metadata_describes_the_tree() {
        let ir = Node::Subtract {
            from: std::boxed::Box::new(bx(0, 0, 0, 64, 64, 64)),
            cut: vec![bx(0, 0, 0, 8, 8, 8), bx(8, 8, 8, 16, 16, 16)],
        };
        assert_eq!(ir.kind(), "subtract");
        assert_eq!(ir.node_count(), 4);
        assert_eq!(ir.depth(), 2);
    }

    #[test]
    fn an_angled_hollow_reports_its_rounded_inset_through_the_tree() {
        let wedge = Node::Wedge {
            min: ivec3(0, 0, 0),
            max: ivec3(512, 512, 256),
            along: Axis::X,
            up: Axis::Z,
        };
        let ir = Node::Hollow {
            solid: std::boxed::Box::new(wedge),
            thickness: 16,
            open_faces: vec![],
        };
        let e = evaluate(&ir).unwrap();
        assert!(!e.warnings.is_empty(), "an angled inset should warn");
        assert!(
            e.warnings[0].contains("hollow"),
            "warning should name the node: {:?}",
            e.warnings
        );
    }
}
