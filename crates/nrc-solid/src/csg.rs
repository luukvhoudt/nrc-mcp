//! Boolean operations on solids, exactly.
//!
//! §4.1 calls `subtract` "the core engineering risk" and asks for "an exact convex
//! decomposition of an arbitrary polyhedron into a *small* number of brushes", warning that "a
//! naive decomposition that emits 200 brushes for a doorway is worse than useless".
//!
//! # Subtraction
//!
//! For convex `A` and `B` with `B`'s half-spaces `h₁…hₙ`:
//!
//! ```text
//! A \ B  =  ⋃ᵢ ( A ∩ h₁ ∩ … ∩ hᵢ₋₁ ∩ ¬hᵢ )
//! ```
//!
//! Each term is an intersection of half-spaces, so each is convex *by construction* — nothing
//! has to be decomposed after the fact. The terms are pairwise disjoint, and terms that
//! enclose no volume drop out exactly.
//!
//! This is not merely correct, it is close to optimal on the shape mappers actually cut. A
//! doorway through a wall — subtract a box that spans the wall's thickness — yields exactly
//! three brushes: the column left of the opening, the column right of it, and the lintel
//! above. The four terms that would be slivers are exactly empty and vanish. That is what a
//! mapper would build by hand.
//!
//! # Merging
//!
//! Adjacent pieces are then merged where their union is genuinely convex. The test is exact,
//! not a volume comparison:
//!
//! > For convex `P` and `Q` sharing a plane `h` (with `P` on the inside of `h` and `Q` on the
//! > inside of `¬h`), `P ∪ Q` is convex **iff** every other plane of `P` contains all of `Q`
//! > and every other plane of `Q` contains all of `P`.
//!
//! When that holds, the merged polytope is the intersection of both plane sets minus the
//! shared pair, and it equals `P ∪ Q` exactly. Both directions follow from convexity, and
//! every step is an integer side test — which matters, because merging wrongly would fill the
//! hole back in, and a float test would eventually do exactly that.

use crate::poly::{Polytope, Solid};
use nrc_core::exact::{IPlane, Sign};

/// Above this, decomposition stops splitting and reports what it managed.
///
/// A guard, not a tuning knob: subtracting an `n`-plane solid multiplies piece count by up to
/// `n` each time, so a chain of subtractions can grow without bound. Reaching this means the
/// IR asked for something that will not produce usable brushwork anyway.
pub const MAX_PARTS: usize = 512;

/// Union: concatenate. Overlapping brushes are legal in Quake and splitting them would add
/// geometry for no benefit.
pub fn union(a: &Solid, b: &Solid) -> Solid {
    let mut parts = a.parts.clone();
    parts.extend(b.parts.iter().cloned());
    Solid { parts }
}

/// Intersection: every pair of parts, keeping the ones that enclose volume.
pub fn intersect(a: &Solid, b: &Solid) -> Solid {
    let mut parts = Vec::new();
    for pa in &a.parts {
        for pb in &b.parts {
            if parts.len() >= MAX_PARTS {
                return Solid { parts };
            }
            let merged = Polytope::from_planes(
                pa.planes()
                    .iter()
                    .copied()
                    .chain(pb.planes().iter().copied()),
            );
            if merged.is_solid() {
                parts.push(merged.simplified());
            }
        }
    }
    Solid { parts }
}

/// `A \ B` for convex `A` and `B`.
///
/// Returns the pieces of `A` outside `B`. An `A` that does not meet `B` comes back unchanged,
/// and an `A` entirely inside `B` comes back empty.
pub fn subtract_convex(a: &Polytope, b: &Polytope) -> Vec<Polytope> {
    if !b.is_solid() {
        // Nothing to remove. Returning `A` unchanged is the only safe reading — treating a
        // degenerate cutter as "removes everything" would silently delete geometry.
        return vec![a.clone()];
    }
    // Cheap rejection: disjoint bounds mean nothing is removed. Worth it because most
    // subtractions in a real map touch a small part of it.
    if !a.bounds().intersects(&b.bounds()) {
        return vec![a.clone()];
    }

    let cutters = b.simplified();
    let mut pieces = Vec::new();
    let mut kept: Vec<IPlane> = Vec::new();

    for plane in cutters.planes() {
        // A ∩ (already-inside planes) ∩ outside(this plane)
        let candidate = Polytope::from_planes(
            a.planes()
                .iter()
                .copied()
                .chain(kept.iter().copied())
                .chain(std::iter::once(plane.flipped())),
        );
        if candidate.is_solid() {
            pieces.push(candidate.simplified());
        }
        kept.push(*plane);
        if pieces.len() >= MAX_PARTS {
            break;
        }
    }

    // Every piece empty means A was wholly inside B.
    pieces
}

/// `A \ B` for solids, subtracting every part of `B` from every part of `A`.
pub fn subtract(a: &Solid, b: &Solid) -> Solid {
    let mut current = a.parts.clone();
    for cutter in &b.parts {
        let mut next = Vec::new();
        for part in &current {
            next.extend(subtract_convex(part, cutter));
            if next.len() >= MAX_PARTS {
                break;
            }
        }
        current = next;
        if current.is_empty() {
            break;
        }
    }
    merge_all(Solid { parts: current })
}

/// True if `P ∪ Q` is convex, and if so the plane pair they share.
///
/// Exact throughout. See the module note for why the criterion is sufficient as well as
/// necessary.
fn mergeable(p: &Polytope, q: &Polytope) -> Option<IPlane> {
    // Exactly one shared opposite pair. Two would mean they meet on two faces, which for
    // convex bodies means one is degenerate.
    let mut shared: Option<IPlane> = None;
    for a in p.planes() {
        if q.planes().contains(&a.flipped()) {
            if shared.is_some() {
                return None;
            }
            shared = Some(*a);
        }
    }
    let h = shared?;
    let anti = h.flipped();

    let pv = p.vertices();
    let qv = q.vertices();
    if pv.is_empty() || qv.is_empty() {
        return None;
    }

    let inside = |plane: &IPlane, verts: &[nrc_core::exact::RatVec3]| -> bool {
        verts
            .iter()
            .all(|v| matches!(plane.side_of_rat(v), Sign::Negative | Sign::Zero))
    };

    // Every other plane of P must contain all of Q, and vice versa.
    if !p
        .planes()
        .iter()
        .filter(|g| **g != h)
        .all(|g| inside(g, &qv))
    {
        return None;
    }
    if !q
        .planes()
        .iter()
        .filter(|g| **g != anti)
        .all(|g| inside(g, &pv))
    {
        return None;
    }
    Some(h)
}

/// Merge two polytopes if their union is convex.
pub fn try_merge(p: &Polytope, q: &Polytope) -> Option<Polytope> {
    let h = mergeable(p, q)?;
    let anti = h.flipped();
    let merged = Polytope::from_planes(
        p.planes()
            .iter()
            .copied()
            .filter(|g| *g != h)
            .chain(q.planes().iter().copied().filter(|g| *g != anti)),
    );
    if merged.is_solid() {
        Some(merged.simplified())
    } else {
        None
    }
}

/// Greedily merge every mergeable pair.
///
/// Restarts after each successful merge rather than doing one pass: a merge creates new
/// adjacency, and a single pass leaves obvious merges on the table. Brush counts are small
/// enough that the quadratic cost does not matter, and §4.1 is explicit that this should
/// "optimize for brush count, not speed" because "mappers will look at the output forever".
pub fn merge_all(solid: Solid) -> Solid {
    let mut parts = solid.parts;
    let mut changed = true;
    // Bound the loop so a pathological input cannot spin. Each merge strictly reduces the
    // part count, so this can never be hit by correct operation.
    let mut budget = parts.len() * parts.len() + 16;

    while changed && budget > 0 {
        changed = false;
        'outer: for i in 0..parts.len() {
            for j in (i + 1)..parts.len() {
                if let Some(m) = try_merge(&parts[i], &parts[j]) {
                    parts[i] = m;
                    parts.remove(j);
                    changed = true;
                    budget -= 1;
                    break 'outer;
                }
            }
        }
    }
    Solid { parts }
}

/// Hollow a solid into a shell of the given wall thickness (§4.1's "single most used op").
///
/// Implemented as `solid \ inset(solid)`, which is why it inherits the subtraction's
/// properties: the shell is made of convex pieces, and a box hollowed with all faces closed
/// comes back as six brushes — exactly the walls, floor and ceiling a mapper would draw.
///
/// `open_faces` names plane indices (into the *simplified* plane list) to leave without a
/// wall, so a room can have an open ceiling or a missing side. Such a face keeps its
/// **original** plane in the cavity rather than being omitted: the cavity then reaches the
/// outer surface there, so no wall is produced, and — importantly — the cavity stays bounded.
/// Omitting the plane instead would make the cavity infinite in that direction and there
/// would be nothing to subtract.
///
/// Insetting a plane means moving it inward by `thickness`, which for an integer plane means
/// adjusting `d`, and that is only exact when the normal is a unit axis. For an angled face
/// the distance is rounded, shifting that wall by up to half a unit; the caller is told rather
/// than left to discover it while measuring.
pub fn hollow(
    solid: &Polytope,
    thickness: i64,
    open_faces: &[usize],
) -> Result<(Solid, Vec<String>), String> {
    if thickness <= 0 {
        return Err(format!("wall thickness must be positive, got {thickness}"));
    }
    let outer = solid.simplified();
    if !outer.is_solid() {
        return Err("cannot hollow a shape that encloses no volume".to_string());
    }

    let mut warnings = Vec::new();
    let mut inner_planes = Vec::new();
    for (i, plane) in outer.planes().iter().enumerate() {
        if open_faces.contains(&i) {
            // Keep the outer plane: the cavity reaches the surface here, so no wall forms,
            // and the cavity remains bounded.
            inner_planes.push(*plane);
            continue;
        }
        // Moving a plane inward by `t` means reducing `d` by `t * |n|`. `|n|` is integral only
        // for axis-aligned normals; otherwise round and say so.
        let len_sq = plane.nx * plane.nx + plane.ny * plane.ny + plane.nz * plane.nz;
        let len = (len_sq as f64).sqrt();
        let shift = thickness as f64 * len;
        let rounded = shift.round() as i128;
        if (shift - rounded as f64).abs() > 1e-9 {
            warnings.push(format!(
                "face {i} is not axis-aligned, so a {thickness}-unit inset is not exactly \
                 representable; it was rounded, shifting that wall by up to half a unit"
            ));
        }
        inner_planes.push(IPlane {
            nx: plane.nx,
            ny: plane.ny,
            nz: plane.nz,
            d: plane.d - rounded,
        });
    }

    if inner_planes.len() < 4 {
        return Err("a cavity needs at least four bounding faces".into());
    }
    if open_faces.len() >= outer.len() {
        return Err(format!(
            "all {} faces were left open, so there are no walls to build",
            outer.len()
        ));
    }
    let inner = Polytope::from_planes(inner_planes);
    if !inner.is_solid() {
        return Err(format!(
            "a wall thickness of {thickness} leaves no cavity — the shape is at most \
             {:.0} units across at its narrowest",
            outer.min_thickness().unwrap_or(0.0)
        ));
    }

    let shell = subtract(&Solid::single(outer), &Solid::single(inner));
    if shell.is_empty() {
        return Err("hollowing produced no walls".into());
    }
    Ok((shell, warnings))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::poly::box_polytope;
    use nrc_core::exact::ivec3;

    fn bx(x0: i64, y0: i64, z0: i64, x1: i64, y1: i64, z1: i64) -> Polytope {
        box_polytope(ivec3(x0, y0, z0), ivec3(x1, y1, z1)).unwrap()
    }

    #[test]
    fn a_doorway_through_a_wall_yields_exactly_three_brushes() {
        // The case §4.1 names. A wall 256 long, 16 thick, 128 tall; a 64-wide, 96-tall opening
        // cut through it. A mapper would build three brushes: left, right, lintel.
        let wall = bx(0, 0, 0, 256, 16, 128);
        let hole = bx(96, -8, 0, 160, 24, 96);
        let pieces = subtract_convex(&wall, &hole);
        assert_eq!(pieces.len(), 3, "got {} pieces", pieces.len());

        // Volume must be conserved exactly: wall minus the part of the hole inside it.
        let removed = 64.0 * 16.0 * 96.0;
        let total: f64 = pieces.iter().map(Polytope::volume).sum();
        assert!(
            (total - (wall.volume() - removed)).abs() < 1e-6,
            "volume {total} != {}",
            wall.volume() - removed
        );
        // And nothing may remain inside the opening.
        assert!(!pieces.iter().any(|p| p.contains(ivec3(128, 8, 48))));
        assert!(
            pieces.iter().any(|p| p.contains(ivec3(32, 8, 48))),
            "left column missing"
        );
        assert!(
            pieces.iter().any(|p| p.contains(ivec3(200, 8, 48))),
            "right column missing"
        );
        assert!(
            pieces.iter().any(|p| p.contains(ivec3(128, 8, 120))),
            "lintel missing"
        );
    }

    #[test]
    fn a_window_fully_inside_a_wall_yields_four_brushes() {
        let wall = bx(0, 0, 0, 256, 16, 128);
        let window = bx(96, -8, 32, 160, 24, 96);
        let pieces = subtract_convex(&wall, &window);
        assert_eq!(pieces.len(), 4, "left, right, below, above");
        assert!(!pieces.iter().any(|p| p.contains(ivec3(128, 8, 64))));
        assert!(
            pieces.iter().any(|p| p.contains(ivec3(128, 8, 16))),
            "sill missing"
        );
        assert!(
            pieces.iter().any(|p| p.contains(ivec3(128, 8, 112))),
            "lintel missing"
        );
    }

    #[test]
    fn subtracting_a_corner_yields_two_brushes() {
        let a = bx(0, 0, 0, 64, 64, 64);
        let pieces = subtract_convex(&a, &bx(32, 32, -8, 96, 96, 72));
        assert_eq!(pieces.len(), 2);
        let total: f64 = pieces.iter().map(Polytope::volume).sum();
        assert!((total - (64.0 * 64.0 * 64.0 - 32.0 * 32.0 * 64.0)).abs() < 1e-6);
    }

    #[test]
    fn a_cutter_that_swallows_the_solid_leaves_nothing() {
        let pieces = subtract_convex(&bx(0, 0, 0, 64, 64, 64), &bx(-8, -8, -8, 72, 72, 72));
        assert!(pieces.is_empty());
    }

    #[test]
    fn a_cutter_that_misses_changes_nothing() {
        let a = bx(0, 0, 0, 64, 64, 64);
        let pieces = subtract_convex(&a, &bx(256, 256, 256, 320, 320, 320));
        assert_eq!(pieces.len(), 1);
        assert_eq!(pieces[0], a);
    }

    #[test]
    fn a_cutter_touching_only_a_face_removes_nothing() {
        // Coincident faces, no overlap. Removing volume here would be wrong.
        let a = bx(0, 0, 0, 64, 64, 64);
        let pieces = subtract_convex(&a, &bx(64, 0, 0, 128, 64, 64));
        let total: f64 = pieces.iter().map(Polytope::volume).sum();
        assert!((total - a.volume()).abs() < 1e-6, "volume changed: {total}");
    }

    #[test]
    fn a_degenerate_cutter_never_deletes_geometry() {
        let a = bx(0, 0, 0, 64, 64, 64);
        let flat = Polytope::from_planes([
            IPlane {
                nx: 0,
                ny: 0,
                nz: 1,
                d: 32,
            },
            IPlane {
                nx: 0,
                ny: 0,
                nz: -1,
                d: -32,
            },
        ]);
        assert_eq!(subtract_convex(&a, &flat), vec![a.clone()]);
    }

    #[test]
    fn two_adjacent_boxes_merge_back_into_one() {
        let left = bx(0, 0, 0, 32, 64, 64);
        let right = bx(32, 0, 0, 64, 64, 64);
        let m = try_merge(&left, &right).expect("should merge");
        assert_eq!(m.len(), 6);
        assert_eq!(m, bx(0, 0, 0, 64, 64, 64));
    }

    #[test]
    fn boxes_that_would_not_form_a_convex_union_do_not_merge() {
        // Adjacent on a face but different heights: the union is an L, not a box.
        let a = bx(0, 0, 0, 32, 64, 64);
        let b = bx(32, 0, 0, 64, 64, 32);
        assert!(try_merge(&a, &b).is_none(), "an L-shape must not merge");

        // Not touching at all.
        assert!(try_merge(&bx(0, 0, 0, 32, 64, 64), &bx(64, 0, 0, 96, 64, 64)).is_none());

        // Overlapping rather than adjacent.
        assert!(try_merge(&bx(0, 0, 0, 40, 64, 64), &bx(32, 0, 0, 64, 64, 64)).is_none());
    }

    #[test]
    fn merge_all_reassembles_a_split_box() {
        let parts = vec![
            bx(0, 0, 0, 16, 64, 64),
            bx(16, 0, 0, 32, 64, 64),
            bx(32, 0, 0, 48, 64, 64),
            bx(48, 0, 0, 64, 64, 64),
        ];
        let merged = merge_all(Solid::new(parts));
        assert_eq!(merged.len(), 1, "four slabs should become one box");
        assert_eq!(merged.parts[0], bx(0, 0, 0, 64, 64, 64));
    }

    #[test]
    fn merging_never_adds_solid_where_there_was_none() {
        // The property that makes an exact merge test necessary: merging the three doorway
        // pieces must not close the doorway.
        let wall = bx(0, 0, 0, 256, 16, 128);
        let hole = bx(96, -8, 0, 160, 24, 96);
        let merged = merge_all(Solid::new(subtract_convex(&wall, &hole)));
        assert!(
            !merged.contains(ivec3(128, 8, 48)),
            "merging filled the doorway back in"
        );
        let total: f64 = merged.parts.iter().map(Polytope::volume).sum();
        assert!((total - (wall.volume() - 64.0 * 16.0 * 96.0)).abs() < 1e-6);
    }

    #[test]
    fn intersect_produces_the_overlap_only() {
        let a = Solid::single(bx(0, 0, 0, 64, 64, 64));
        let b = Solid::single(bx(32, 32, 32, 96, 96, 96));
        let i = intersect(&a, &b);
        assert_eq!(i.len(), 1);
        assert_eq!(i.parts[0], bx(32, 32, 32, 64, 64, 64));
    }

    #[test]
    fn intersect_of_disjoint_solids_is_empty() {
        let a = Solid::single(bx(0, 0, 0, 64, 64, 64));
        let b = Solid::single(bx(128, 0, 0, 192, 64, 64));
        assert!(intersect(&a, &b).is_empty());
    }

    #[test]
    fn union_keeps_both_parts_even_when_they_overlap() {
        let a = Solid::single(bx(0, 0, 0, 64, 64, 64));
        let b = Solid::single(bx(32, 0, 0, 96, 64, 64));
        assert_eq!(union(&a, &b).len(), 2, "overlapping brushes are legal");
    }

    #[test]
    fn hollowing_a_box_yields_six_walls() {
        let (shell, warnings) = hollow(&bx(0, 0, 0, 512, 512, 256), 16, &[]).unwrap();
        assert_eq!(shell.len(), 6, "floor, ceiling and four walls");
        assert!(warnings.is_empty(), "axis-aligned insets are exact");
        // The interior must be hollow and the walls solid.
        assert!(
            !shell.contains(ivec3(256, 256, 128)),
            "interior should be empty"
        );
        assert!(shell.contains(ivec3(256, 256, 8)), "floor should be solid");
        assert!(shell.contains(ivec3(8, 256, 128)), "wall should be solid");
    }

    #[test]
    fn hollowing_with_an_open_face_leaves_a_gap() {
        let full = hollow(&bx(0, 0, 0, 512, 512, 256), 16, &[]).unwrap().0;
        // Plane 5 of a box is the +Z face; leaving it out removes the ceiling.
        let (open, _) = hollow(&bx(0, 0, 0, 512, 512, 256), 16, &[5]).unwrap();
        assert!(open.len() < full.len());
        assert!(
            !open.contains(ivec3(256, 256, 248)),
            "ceiling should be gone"
        );
        assert!(open.contains(ivec3(256, 256, 8)), "floor should remain");
    }

    #[test]
    fn hollowing_refuses_impossible_requests_with_a_reason() {
        let e = hollow(&bx(0, 0, 0, 64, 64, 64), 0, &[]).unwrap_err();
        assert!(e.contains("must be positive"), "{e}");

        // A wall thicker than half the shape leaves no cavity.
        let e = hollow(&bx(0, 0, 0, 64, 64, 64), 40, &[]).unwrap_err();
        assert!(e.contains("no cavity"), "{e}");
        assert!(
            e.contains("64"),
            "the message should say how big the shape is: {e}"
        );

        // Leaving every face open leaves nothing to build.
        let e = hollow(&bx(0, 0, 0, 64, 64, 64), 8, &[0, 1, 2, 3, 4, 5]).unwrap_err();
        assert!(e.contains("no walls to build"), "{e}");
    }

    #[test]
    fn hollowing_an_angled_shape_warns_that_the_inset_was_rounded() {
        // A 45-degree cut means the inset distance is irrational, so it must be rounded — and
        // a mapper measuring that wall would notice, so it is reported.
        let wedge = bx(0, 0, 0, 256, 256, 128).clipped_by(IPlane {
            nx: 1,
            ny: 1,
            nz: 0,
            d: 320,
        });
        let (_, warnings) = hollow(&wedge, 16, &[]).unwrap();
        assert!(!warnings.is_empty(), "an angled inset should warn");
        assert!(warnings[0].contains("half a unit"), "{:?}", warnings);
    }

    #[test]
    fn subtracting_from_a_multi_part_solid_touches_only_what_it_meets() {
        let s = Solid::new(vec![bx(0, 0, 0, 64, 64, 64), bx(256, 0, 0, 320, 64, 64)]);
        let cut = subtract(&s, &Solid::single(bx(-8, -8, -8, 32, 72, 72)));
        // The far box is untouched; the near one loses a corner.
        assert!(cut.contains(ivec3(300, 32, 32)));
        assert!(!cut.contains(ivec3(16, 32, 32)));
        assert!(cut.contains(ivec3(48, 32, 32)));
    }

    #[test]
    fn a_chain_of_subtractions_stays_bounded() {
        // Ten cuts through one box. The guard exists so this cannot grow without limit.
        let mut s = Solid::single(bx(0, 0, 0, 1024, 64, 64));
        for i in 0..10 {
            let x = 32 + i * 96;
            s = subtract(&s, &Solid::single(bx(x, -8, -8, x + 32, 72, 72)));
        }
        assert!(s.len() <= MAX_PARTS);
        assert_eq!(s.len(), 11, "ten cuts across a bar leave eleven segments");
    }
}
