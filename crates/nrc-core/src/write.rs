//! Serializer for the `.map` text format.
//!
//! The layout mirrors what NetRadiant-custom itself writes, verified against the real
//! maps in `corpus/real/`: **no indentation anywhere**, one face per line, tokens
//! separated by single spaces, parenthesized groups padded inside (`( 0 0 0 )`).
//!
//! Every number goes through [`crate::num::Num`], so an unmodified map reproduces its
//! own bytes and a modified one differs only where it was modified. That property is the
//! §3.2 gate, and it is verified for real by `tools/difftest.py`, not merely asserted
//! here.

use crate::model::*;
use crate::num::Num;
use std::fmt::Write as _;

/// Serialize a map back to `.map` source.
pub fn write_map(m: &Map) -> String {
    let nl = m.line_ending.as_str();

    // Build the body, then let the recorded epilogue supply *all* trailing whitespace.
    // Writing lines that each end in a newline and then trimming means we do not have to
    // special-case the last construct, and files that end with spare blank lines or with
    // no newline at all both come out exactly as they went in.
    let mut body = String::new();
    for e in &m.entities {
        write_entity(&mut body, e, nl);
    }
    for c in &m.footer {
        let _ = write!(body, "{c}{nl}");
    }
    while body.ends_with(['\n', '\r', ' ', '\t']) {
        body.pop();
    }

    let mut s = String::with_capacity(m.prologue.len() + body.len() + m.epilogue.len());
    s.push_str(&m.prologue);
    s.push_str(&body);
    s.push_str(&m.epilogue);
    s
}

fn write_entity(s: &mut String, e: &Entity, nl: &str) {
    for c in &e.leading {
        let _ = write!(s, "{c}{nl}");
    }
    let _ = write!(s, "{{{nl}");
    for (k, v) in &e.keys {
        let _ = write!(s, "\"{k}\" \"{v}\"{nl}");
    }
    for p in &e.prims {
        write_primitive(s, p, nl);
    }
    for c in &e.trailing {
        let _ = write!(s, "{c}{nl}");
    }
    let _ = write!(s, "}}{nl}");
}

fn write_primitive(s: &mut String, p: &Primitive, nl: &str) {
    for c in p.leading() {
        let _ = write!(s, "{c}{nl}");
    }
    match p {
        Primitive::Brush(b) => {
            let _ = write!(s, "{{{nl}");
            match &b.style {
                BrushStyle::Bare => {}
                BrushStyle::Keyword(kw) => {
                    let _ = write!(s, "{kw}{nl}{{{nl}");
                }
            }
            for f in &b.faces {
                write_face(s, f, nl);
            }
            if matches!(b.style, BrushStyle::Keyword(_)) {
                let _ = write!(s, "}}{nl}");
            }
            let _ = write!(s, "}}{nl}");
        }
        Primitive::Patch(p) => write_patch(s, p, nl),
        Primitive::Raw(r) => {
            // Already includes its own braces, and by definition we must not reformat it.
            let _ = write!(s, "{}{nl}", r.text);
        }
    }
}

fn write_face(s: &mut String, f: &Face, nl: &str) {
    for c in &f.leading {
        let _ = write!(s, "{c}{nl}");
    }
    for p in &f.points {
        write_triple(s, p);
        s.push(' ');
    }

    match &f.tex {
        TexDef::BrushPrimitives { m } => {
            // The matrix precedes the shader name in this format.
            s.push_str("( ");
            write_triple(s, &m[0]);
            s.push(' ');
            write_triple(s, &m[1]);
            s.push_str(" ) ");
            let _ = write!(s, "{}", f.shader);
        }
        TexDef::Axial { shift, rotate, scale } => {
            let _ = write!(
                s,
                "{} {} {} {} {} {}",
                f.shader, shift[0], shift[1], rotate, scale[0], scale[1]
            );
        }
        TexDef::Valve220 { u, v, rotate, scale } => {
            let _ = write!(s, "{} ", f.shader);
            write_quad(s, u);
            s.push(' ');
            write_quad(s, v);
            let _ = write!(s, " {} {} {}", rotate, scale[0], scale[1]);
        }
    }

    if let Some(sf) = &f.surface {
        let _ = write!(s, " {} {} {}", sf.contents, sf.flags, sf.value);
    }
    for x in &f.extra {
        let _ = write!(s, " {x}");
    }
    if let Some(c) = &f.trailing {
        let _ = write!(s, " {c}");
    }
    s.push_str(nl);
}

fn write_patch(s: &mut String, p: &Patch, nl: &str) {
    let _ = write!(s, "{{{nl}{}{nl}{{{nl}{}{nl}", p.kind, p.shader);

    s.push('(');
    for h in &p.header {
        let _ = write!(s, " {h}");
    }
    let _ = write!(s, " ){nl}({nl}");

    for row in &p.rows {
        s.push('(');
        for pt in row {
            s.push_str(" (");
            for c in pt {
                let _ = write!(s, " {c}");
            }
            s.push_str(" )");
        }
        let _ = write!(s, " ){nl}");
    }

    let _ = write!(s, "){nl}}}{nl}}}{nl}");
}

fn write_triple(s: &mut String, t: &[Num; 3]) {
    let _ = write!(s, "( {} {} {} )", t[0], t[1], t[2]);
}

fn write_quad(s: &mut String, q: &[Num; 4]) {
    let _ = write!(s, "[ {} {} {} {} ]", q[0], q[1], q[2], q[3]);
}

/// Rewrite the `// entity N` / `// brush N` marker comments the way Radiant does.
///
/// Needed for maps we author ourselves: a brand-new map has no such comments, and a map
/// that gained or lost brushes has stale ones. Deliberately *not* applied on save —
/// renumbering someone else's file would rewrite every marker line and drown the real
/// change in noise.
///
/// Only markers are touched; any other comment is left alone.
pub fn renumber_radiant_comments(m: &mut Map) {
    fn is_marker(c: &str) -> bool {
        let t = c.trim_start_matches('/').trim();
        let (word, rest) = match t.split_once(' ') {
            Some(p) => p,
            None => return false,
        };
        (word == "entity" || word == "brush") && rest.trim().parse::<u64>().is_ok()
    }

    for (ei, e) in m.entities.iter_mut().enumerate() {
        e.leading.retain(|c| !is_marker(c));
        e.leading.insert(0, format!("// entity {ei}"));

        let mut bi = 0usize;
        for p in e.prims.iter_mut() {
            let leading = match p {
                Primitive::Brush(b) => &mut b.leading,
                Primitive::Patch(pt) => &mut pt.leading,
                Primitive::Raw(r) => &mut r.leading,
            };
            leading.retain(|c| !is_marker(c));
            leading.insert(0, format!("// brush {bi}"));
            bi += 1;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::parse::parse_map;

    /// The property that matters: parse then write must reproduce the input exactly.
    fn assert_round_trips(src: &str) {
        let m = parse_map(src).expect("should parse");
        let out = write_map(&m);
        assert_eq!(out, src, "round-trip differed");
    }

    #[test]
    fn axial_brush_round_trips_byte_for_byte() {
        assert_round_trips(
            "\
// entity 0
{
\"classname\" \"worldspawn\"
// brush 0
{
( 768 1280 -8 ) ( 640 1280 -8 ) ( 640 0 -8 ) common/caulk 0 0 0 0.500000 0.500000 0 4 0
( 648 0 0 ) ( 648 1280 0 ) ( 776 1280 0 ) battlecow_floors/concrete_sidewalk_b 0 0 0 0.500000 0.500000 0 0 0
}
}
",
        );
    }

    #[test]
    fn brush_primitives_round_trip_including_negative_zero() {
        assert_round_trips(
            "\
// entity 0
{
\"classname\" \"worldspawn\"
// brush 0
{
brushDef
{
( 6 8 75 ) ( 6 0 75 ) ( -2 8 75 ) ( ( 0.0078125 0 -0 ) ( -0 0.0078125 0 ) ) abbey2/abbey2_hfx_wood2_light 0 0 0
}
}
}
",
        );
    }

    #[test]
    fn valve220_round_trips() {
        assert_round_trips(
            "\
{
\"mapversion\" \"220\"
\"classname\" \"worldspawn\"
{
( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) WALL01 [ 1 0 0 16 ] [ 0 -1 0 -8 ] 0 1 1
}
}
",
        );
    }

    #[test]
    fn patchdef2_round_trips_with_long_decimals() {
        assert_round_trips(
            "\
{
\"classname\" \"worldspawn\"
{
patchDef2
{
dofa/concrete_white
( 3 2 0 0 0 )
(
( ( 104 -932 111.9999847412 0 0 ) ( 104 -932 200 0 -0.265625 ) )
( ( 104 -952 111.9999847412 0.078125 0 ) ( 104 -952 200 0.078125 -0.265625 ) )
( ( 124 -952 111.9999847412 0.15625 0 ) ( 124 -952 200 0.15625 -0.265625 ) )
)
}
}
}
",
        );
    }

    #[test]
    fn patchdef3_round_trips() {
        assert_round_trips(
            "\
{
\"classname\" \"worldspawn\"
{
patchDef3
{
x/y
( 3 3 4 4 0 0 0 )
(
( ( 0 0 0 0 0 ) ( 0 0 0 0 0 ) ( 0 0 0 0 0 ) )
( ( 0 0 0 0 0 ) ( 0 0 0 0 0 ) ( 0 0 0 0 0 ) )
( ( 0 0 0 0 0 ) ( 0 0 0 0 0 ) ( 0 0 0 0 0 ) )
)
}
}
}
",
        );
    }

    #[test]
    fn unknown_block_round_trips_verbatim() {
        assert_round_trips(
            "\
{
\"classname\" \"worldspawn\"
{
someFutureDef
{
( 0 0 0 ) whatever
}
}
}
",
        );
    }

    #[test]
    fn crlf_files_stay_crlf() {
        let src = "{\r\n\"classname\" \"worldspawn\"\r\n}\r\n";
        assert_round_trips(src);
    }

    #[test]
    fn a_file_without_a_final_newline_does_not_gain_one() {
        assert_round_trips("{\n\"classname\" \"worldspawn\"\n}");
    }

    #[test]
    fn spare_blank_lines_at_end_of_file_are_preserved() {
        // Exactly how corpus/real/ut4_dofa_ac.map ends: CRLF, then two empty CRLF lines.
        assert_round_trips("{\r\n\"classname\" \"point_entity_a\"\r\n}\r\n\r\n\r\n");
        // And the LF equivalent, plus trailing spaces, which a hand edit can leave behind.
        assert_round_trips("{\n}\n\n\n");
        assert_round_trips("{\n}\n   \n");
    }

    #[test]
    fn a_file_of_only_whitespace_is_reproduced_not_emptied() {
        assert_round_trips("   \n\n");
        assert_round_trips("\n");
    }

    #[test]
    fn mutating_one_number_leaves_every_other_byte_alone() {
        let src = "\
{
\"classname\" \"worldspawn\"
{
( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) a/b 0 0 0 0.500000 0.500000 0 0 0
( 0 0 8 ) ( 1 0 8 ) ( 0 1 8 ) a/b 0 0 0 0.500000 0.500000 0 0 0
}
}
";
        let mut m = parse_map(src).unwrap();
        // Move the first face's first point up by 16.
        match &mut m.entities[0].prims[0] {
            Primitive::Brush(b) => b.faces[0].points[0][2].set(16.0),
            _ => unreachable!(),
        }
        let out = write_map(&m);
        assert!(out.contains("( 0 0 16 ) ( 1 0 0 )"), "the edit should apply:\n{out}");
        assert!(
            out.contains("( 0 0 8 ) ( 1 0 8 ) ( 0 1 8 ) a/b 0 0 0 0.500000 0.500000 0 0 0"),
            "the untouched face must keep its 0.500000 formatting:\n{out}"
        );
    }

    #[test]
    fn renumbering_replaces_stale_markers_without_touching_other_comments() {
        let src = "\
// entity 7
{
\"classname\" \"worldspawn\"
// brush 99
// a note from the mapper
{
( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) a/b 0 0 0 0.5 0.5 0 0 0
}
}
";
        let mut m = parse_map(src).unwrap();
        renumber_radiant_comments(&mut m);
        let out = write_map(&m);
        assert!(out.starts_with("// entity 0\n"), "{out}");
        assert!(out.contains("// brush 0\n// a note from the mapper\n"), "{out}");
        assert!(!out.contains("entity 7"));
        assert!(!out.contains("brush 99"));
    }

    #[test]
    fn renumbering_a_fresh_map_adds_markers() {
        let mut m = parse_map("{\n\"classname\" \"worldspawn\"\n}\n").unwrap();
        renumber_radiant_comments(&mut m);
        assert!(write_map(&m).starts_with("// entity 0\n"));
    }

    #[test]
    fn empty_map_writes_nothing() {
        let m = parse_map("").unwrap();
        assert_eq!(write_map(&m), "");
    }

    #[test]
    fn a_file_saved_by_this_fork_round_trips_including_its_leading_blank_line() {
        // Shape that NetRadiant-custom itself produces: a leading newline from the token
        // writer's pending separator, then the fork-specific `//@$&` layer records, then
        // the entities. The layer lines are comments as far as we are concerned, which is
        // exactly why they survive without us needing to model layers at all.
        assert_round_trips(
            "\n//@$& layerdef \"0\" -1 0 0 0\n// entity 0\n{\n\"classname\" \"worldspawn\"\n\
             //@$& layer 0\n// brush 0\n{\n( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) a/b 0 0 0 0.5 0.5 0 0 0\n}\n}\n",
        );
    }

    #[test]
    fn shader_names_are_stored_and_written_without_the_textures_prefix() {
        // On disk the `textures/` prefix is stripped and re-added by the reader, and an
        // empty shader is spelled `NULL`. We keep the on-disk spelling verbatim so that
        // resolving a shader is an explicit step, never an accidental rewrite.
        let src = "{\n{\n( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) NULL 0 0 0 0.5 0.5 0 0 0\n}\n}\n";
        let m = parse_map(src).unwrap();
        assert_eq!(m.entities[0].prims[0].as_brush().unwrap().faces[0].shader, "NULL");
        assert_eq!(write_map(&m), src);
    }
}
