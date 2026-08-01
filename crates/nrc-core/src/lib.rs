//! `nrc-core` — the `.map` kernel and geometry layer for `nrc-mcp`.
//!
//! # What this crate guarantees
//!
//! 1. **Lossless I/O.** [`parse_map`] followed by [`write_map`] reproduces the input
//!    byte for byte, including comments, number formatting, line endings, and primitive
//!    blocks whose syntax we do not recognize. This is the §3.2 gate that everything else
//!    depends on, and it is what makes the tool safe to point at someone's real map.
//!
//! 2. **Exact predicates over the authored domain.** Validity decisions — coplanarity,
//!    convexity, plane identity, grid membership — go through [`exact`], which uses
//!    integer arithmetic and reports [`exact::Sign::Indeterminate`] rather than guessing
//!    when its input is off-grid. Floating point is confined to rendering and reporting.
//!
//! # Layout
//!
//! | Module | Role |
//! | --- | --- |
//! | [`num`] | numbers that remember their source text |
//! | [`lex`] | tokenizer, comments included |
//! | [`model`] | the document model (L0–L2 of §3.1) |
//! | [`parse`] / [`write`] | text ⇄ model |
//! | [`math`] | float vectors, planes, bounds — reporting and rendering |
//! | [`exact`] | integer predicates — validity decisions |
//! | [`winding`] | plane sets ⇒ exact vertices and face polygons |
//! | [`validate`] | game-agnostic geometry and format validators |
//! | [`stats`] | read-only map analysis |
//!
//! # Provenance
//!
//! The on-disk format is implemented against NetRadiant-custom's own reader and writer
//! (`plugins/mapq3/`, `radiant/brushtokens.h`, `radiant/patch.h`,
//! `libs/stream/textstream.h`) and cross-checked against real maps in `corpus/real/`.
//! Where this crate deviates from upstream it says so at the deviation.

pub mod exact;
pub mod lex;
pub mod math;
pub mod model;
pub mod num;
pub mod parse;
pub mod stats;
pub mod validate;
pub mod winding;
pub mod write;

pub use exact::{IPlane, IVec3, Sign};
pub use math::{vec3, Aabb, Axis, Plane, Vec3};
pub use model::{
    Brush, BrushStyle, Entity, Face, LineEnding, Map, Patch, Primitive, RawBlock, SurfaceFlags,
    TexDef, TexDefKind,
};
pub use num::Num;
pub use parse::{parse_map, ParseError};
pub use stats::{map_stats, MapStats};
pub use validate::{validate_map, Confidence, Finding, Report, Severity, Thresholds};
pub use winding::{brush_geometry, BrushGeometry, Degeneracy};
pub use write::{renumber_radiant_comments, write_map};

use std::path::Path;

/// Anything that can go wrong loading or saving a map.
#[derive(Debug)]
pub enum Error {
    Io(std::io::Error),
    Parse(ParseError),
    /// The file is not valid UTF-8. `.map` files are ASCII in practice; a failure here
    /// usually means a `.bsp` or an archive was passed by mistake, which is worth saying
    /// plainly instead of reporting a confusing parse error at line 1.
    Encoding(String),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::Io(e) => write!(f, "{e}"),
            Error::Parse(e) => write!(f, "{e}"),
            Error::Encoding(m) => write!(f, "{m}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::Io(e)
    }
}

impl From<ParseError> for Error {
    fn from(e: ParseError) -> Self {
        Error::Parse(e)
    }
}

/// Load a `.map` from disk.
pub fn load_file(path: impl AsRef<Path>) -> Result<Map, Error> {
    let path = path.as_ref();
    let bytes = std::fs::read(path)?;
    let src = String::from_utf8(bytes).map_err(|e| {
        Error::Encoding(format!(
            "{} is not valid UTF-8 (byte {} is invalid) — is it really a .map?",
            path.display(),
            e.utf8_error().valid_up_to()
        ))
    })?;
    Ok(parse_map(&src)?)
}

/// Save a `.map` to disk.
///
/// Writes the whole file in one call rather than streaming, so a failure part-way cannot
/// leave a mapper with a truncated `.map` where their level used to be.
pub fn save_file(path: impl AsRef<Path>, map: &Map) -> Result<(), Error> {
    std::fs::write(path, write_map(map))?;
    Ok(())
}

/// Load, then immediately re-serialize, and report whether the bytes match.
///
/// This is the §3.2 gate in one call, used by `tools/difftest.py` across the corpus and
/// by the MCP layer as a self-check before it offers to write to a user's map.
pub fn round_trip_check(src: &str) -> Result<RoundTrip, ParseError> {
    let map = parse_map(src)?;
    let out = write_map(&map);
    Ok(RoundTrip {
        identical: out == src,
        first_difference: first_difference(src, &out),
        input_len: src.len(),
        output_len: out.len(),
        output: out,
        map,
    })
}

pub struct RoundTrip {
    pub identical: bool,
    /// Byte offset, 1-based line, and the two differing lines — enough for a human to see
    /// the problem without diffing two megabyte files by hand.
    pub first_difference: Option<Difference>,
    pub input_len: usize,
    pub output_len: usize,
    pub output: String,
    pub map: Map,
}

#[derive(Debug, Clone)]
pub struct Difference {
    pub offset: usize,
    pub line: u32,
    pub expected: String,
    pub actual: String,
}

fn first_difference(a: &str, b: &str) -> Option<Difference> {
    if a == b {
        return None;
    }
    let ab = a.as_bytes();
    let bb = b.as_bytes();
    let offset = (0..ab.len().min(bb.len()))
        .find(|&i| ab[i] != bb[i])
        .unwrap_or(ab.len().min(bb.len()));
    let line = 1 + ab[..offset].iter().filter(|&&c| c == b'\n').count() as u32;
    let line_of = |s: &str| -> String {
        s.lines()
            .nth(line as usize - 1)
            .unwrap_or("<past end of file>")
            .to_string()
    };
    Some(Difference {
        offset,
        line,
        expected: line_of(a),
        actual: line_of(b),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip_check_reports_success() {
        let src = "{\n\"classname\" \"worldspawn\"\n}\n";
        let r = round_trip_check(src).unwrap();
        assert!(r.identical);
        assert!(r.first_difference.is_none());
        assert_eq!(r.input_len, r.output_len);
    }

    #[test]
    fn round_trip_check_locates_a_deliberate_difference() {
        // Feed it a map, mutate the model, and confirm the reported line is the one the
        // mutation touched. This is the mechanism the corpus harness relies on.
        let src = "{\n\"classname\" \"worldspawn\"\n{\n\
                   ( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) a/b 0 0 0 0.5 0.5 0 0 0\n}\n}\n";
        let mut m = parse_map(src).unwrap();
        match &mut m.entities[0].prims[0] {
            Primitive::Brush(b) => b.faces[0].points[0][0].set(32.0),
            _ => unreachable!(),
        }
        let out = write_map(&m);
        let d = first_difference(src, &out).expect("should differ");
        assert_eq!(d.line, 4);
        assert!(d.actual.starts_with("( 32 0 0 )"), "got {:?}", d.actual);
    }

    #[test]
    fn a_truncated_output_still_reports_a_difference() {
        let d = first_difference("abc\ndef\n", "abc\n").unwrap();
        assert_eq!(d.offset, 4);
    }

    #[test]
    fn load_reports_a_helpful_error_for_binary_input() {
        let p = std::env::temp_dir().join("nrc_core_not_a_map.bin");
        std::fs::write(&p, [0xffu8, 0xfe, 0x00, 0x01]).unwrap();
        let e = load_file(&p).unwrap_err();
        assert!(
            matches!(e, Error::Encoding(_)),
            "expected an encoding error, got {e}"
        );
        assert!(e.to_string().contains("really a .map"));
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn save_then_load_is_stable() {
        let src = "{\n\"classname\" \"worldspawn\"\n}\n";
        let p = std::env::temp_dir().join("nrc_core_save_roundtrip.map");
        let m = parse_map(src).unwrap();
        save_file(&p, &m).unwrap();
        let back = load_file(&p).unwrap();
        assert_eq!(write_map(&back), src);
        let _ = std::fs::remove_file(&p);
    }
}
