//! Parser for the `.map` text format.
//!
//! Two decisions shape this file.
//!
//! **Texdef format is detected per face, from syntax alone.** A `(` where the shader
//! name should be means brush primitives; a `[` after the shader means Valve 220;
//! neither means classic axial projection. No file-level mode flag is consulted, and
//! `"mapversion" "220"` is recorded but not trusted, because the syntax is unambiguous
//! and the key is not always present.
//!
//! **The end of a face line is found by line number, not by counting tokens.** The
//! trailing `contents surfaceflags value` trio is optional and some dialects append
//! further tokens. Consuming "the rest of this line" handles every case we have seen
//! and degrades into [`Face::extra`] rather than a parse error for ones we have not.

use crate::lex::{tokenize, Tok, Token};
use crate::model::*;
use crate::num::Num;
use std::fmt;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ParseError {
    pub line: u32,
    pub message: String,
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "line {}: {}", self.line, self.message)
    }
}

impl std::error::Error for ParseError {}

/// Parse a `.map` source into a document.
pub fn parse_map(src: &str) -> Result<Map, ParseError> {
    let toks = tokenize(src).map_err(|e| ParseError {
        line: e.line,
        message: e.message,
    })?;
    let mut p = Parser { src, toks, pos: 0 };
    p.map()
}

struct Parser<'a> {
    src: &'a str,
    toks: Vec<Token>,
    pos: usize,
}

impl<'a> Parser<'a> {
    fn peek(&self) -> &Tok {
        &self.toks[self.pos].tok
    }

    fn peek_line(&self) -> u32 {
        self.toks[self.pos].line
    }

    fn line(&self) -> u32 {
        self.toks[self.pos.min(self.toks.len() - 1)].line
    }

    fn err<T>(&self, message: impl Into<String>) -> Result<T, ParseError> {
        Err(ParseError {
            line: self.line(),
            message: message.into(),
        })
    }

    fn advance(&mut self) -> &Tok {
        let i = self.pos;
        if self.pos < self.toks.len() - 1 {
            self.pos += 1;
        }
        &self.toks[i].tok
    }

    fn eat(&mut self, want: &Tok) -> bool {
        if self.peek() == want {
            self.advance();
            true
        } else {
            false
        }
    }

    fn expect(&mut self, want: &Tok) -> Result<(), ParseError> {
        if self.eat(want) {
            Ok(())
        } else {
            let got = self.peek().describe();
            self.err(format!("expected {}, found {}", want.describe(), got))
        }
    }

    fn expect_ident(&mut self, what: &str) -> Result<String, ParseError> {
        match self.peek().clone() {
            Tok::Ident(s) => {
                self.advance();
                Ok(s)
            }
            other => {
                let d = other.describe();
                self.err(format!("expected {what}, found {d}"))
            }
        }
    }

    /// A numeric literal, keeping its source text so it round-trips verbatim.
    fn expect_num(&mut self, what: &str) -> Result<Num, ParseError> {
        let line = self.peek_line();
        match self.peek().clone() {
            Tok::Ident(s) => match s.parse::<f64>() {
                Ok(v) => {
                    self.advance();
                    Ok(Num::parsed(&s, v))
                }
                Err(_) => Err(ParseError {
                    line,
                    message: format!("expected {what} to be a number, found {s:?}"),
                }),
            },
            other => {
                let d = other.describe();
                self.err(format!("expected {what}, found {d}"))
            }
        }
    }

    /// Consecutive comments that stand on their own line.
    fn own_line_comments(&mut self) -> Vec<String> {
        let mut out = Vec::new();
        while let Tok::Comment {
            text,
            own_line: true,
        } = self.peek()
        {
            out.push(text.clone());
            self.advance();
        }
        out
    }

    /// A comment on the same line as what we just parsed.
    fn trailing_comment(&mut self) -> Option<String> {
        if let Tok::Comment {
            text,
            own_line: false,
        } = self.peek()
        {
            let t = text.clone();
            self.advance();
            return Some(t);
        }
        None
    }

    fn map(&mut self) -> Result<Map, ParseError> {
        // Everything before the first token is whitespace by construction, and this fork
        // always writes a leading newline, so it has to be carried through verbatim.
        let prologue = self.src[..self.toks[0].start].to_string();
        debug_assert!(prologue.chars().all(char::is_whitespace));

        // Likewise everything after the last real token. `toks` always ends with `Eof`,
        // so the last real token is the one before it — if there is one at all.
        let epilogue = match self.toks.len().checked_sub(2).map(|i| self.toks[i].end) {
            Some(end) => self.src[end..].to_string(),
            None => String::new(), // no tokens: the prologue already holds the whole file
        };
        debug_assert!(epilogue.chars().all(char::is_whitespace));

        let mut m = Map {
            prologue,
            epilogue,
            line_ending: LineEnding::detect(self.src),
            ..Default::default()
        };

        loop {
            // Comments here belong to whatever comes next: an entity, or the end of file.
            let pending = self.own_line_comments();
            match self.peek() {
                Tok::Eof => {
                    m.footer = pending;
                    break;
                }
                Tok::LBrace => {
                    let mut e = self.entity()?;
                    e.leading = pending;
                    m.entities.push(e);
                }
                other => {
                    let d = other.describe();
                    return self.err(format!(
                        "expected an entity block or end of file, found {d}"
                    ));
                }
            }
        }
        Ok(m)
    }

    fn entity(&mut self) -> Result<Entity, ParseError> {
        self.expect(&Tok::LBrace)?;
        let mut e = Entity::default();

        loop {
            let pending = self.own_line_comments();
            match self.peek().clone() {
                Tok::RBrace => {
                    self.advance();
                    e.trailing.extend(pending);
                    return Ok(e);
                }
                Tok::Str(key) => {
                    // A comment above a key line is rare enough that it does not occur
                    // anywhere in our corpus. Rather than index trivia against a key
                    // list that tools reorder, we keep the text and emit it before the
                    // closing brace — nothing is lost, the position is approximate, and
                    // the round-trip test will say so if it ever happens.
                    e.trailing.extend(pending);
                    self.advance();
                    let value = match self.peek().clone() {
                        Tok::Str(v) => {
                            self.advance();
                            v
                        }
                        other => {
                            let d = other.describe();
                            return self.err(format!(
                                "key {key:?} has no value: expected a quoted string, found {d}"
                            ));
                        }
                    };
                    e.keys.push((key, value));
                    self.trailing_comment();
                }
                Tok::LBrace => {
                    let prim = self.primitive(pending)?;
                    e.prims.push(prim);
                }
                other => {
                    let d = other.describe();
                    return self.err(format!(
                        "expected a key, a primitive block, or '}}', found {d}"
                    ));
                }
            }
        }
    }

    fn primitive(&mut self, leading: Vec<String>) -> Result<Primitive, ParseError> {
        let open_tok = self.pos;
        self.expect(&Tok::LBrace)?;

        let keyword = match self.peek().clone() {
            Tok::Ident(k) => Some(k),
            _ => None,
        };

        match keyword.as_deref() {
            // Bare face list: axial projection or Valve 220.
            None => {
                let faces = self.face_list()?;
                self.expect(&Tok::RBrace)?;
                Ok(Primitive::Brush(Brush {
                    leading,
                    style: BrushStyle::Bare,
                    faces,
                }))
            }
            Some(kw) if kw.starts_with("patch") => {
                let kw = kw.to_string();
                self.advance();
                let patch = self.patch_body(leading, kw)?;
                self.expect(&Tok::RBrace)?;
                Ok(Primitive::Patch(patch))
            }
            Some(kw) if kw == "brushDef" => {
                let kw = kw.to_string();
                self.advance();
                self.expect(&Tok::LBrace)?;
                let faces = self.face_list()?;
                self.expect(&Tok::RBrace)?;
                self.expect(&Tok::RBrace)?;
                Ok(Primitive::Brush(Brush {
                    leading,
                    style: BrushStyle::Keyword(kw),
                    faces,
                }))
            }
            // Any other keyword — `brushDef3`, a fork-specific construct, something from
            // a future upstream. We cannot reason about it, but we refuse to lose it.
            Some(kw) => {
                let kw = kw.to_string();
                let text = self.skip_balanced_block(open_tok)?;
                Ok(Primitive::Raw(RawBlock {
                    leading,
                    keyword: kw,
                    text,
                }))
            }
        }
    }

    /// Consume a `{ ... }` block without interpreting it, returning its source text.
    ///
    /// `open_tok` must index the opening `{`, which the caller has already consumed.
    fn skip_balanced_block(&mut self, open_tok: usize) -> Result<String, ParseError> {
        let start_byte = self.toks[open_tok].start;
        let start_line = self.toks[open_tok].line;
        let mut depth = 1usize;
        loop {
            match self.peek() {
                Tok::LBrace => depth += 1,
                Tok::RBrace => {
                    depth -= 1;
                    if depth == 0 {
                        let end_byte = self.toks[self.pos].end;
                        self.advance();
                        return Ok(self.src[start_byte..end_byte].to_string());
                    }
                }
                Tok::Eof => {
                    return Err(ParseError {
                        line: start_line,
                        message: "unterminated block: reached end of file looking for '}'".into(),
                    })
                }
                _ => {}
            }
            self.advance();
        }
    }

    fn face_list(&mut self) -> Result<Vec<Face>, ParseError> {
        let mut faces: Vec<Face> = Vec::new();
        loop {
            let leading = self.own_line_comments();
            if matches!(self.peek(), Tok::RBrace) {
                if !leading.is_empty() {
                    // Comments just before the closing brace of a brush: attach to the
                    // last face so they survive. If there is no face, the brush is
                    // degenerate anyway and will be reported as such.
                    if let Some(last) = faces.last_mut() {
                        last.leading.extend(leading);
                    }
                }
                return Ok(faces);
            }
            if matches!(self.peek(), Tok::Eof) {
                return self.err("unterminated brush: reached end of file looking for '}'");
            }
            let mut f = self.face()?;
            f.leading = leading;
            faces.push(f);
        }
    }

    /// `( x y z )`
    fn paren_triple(&mut self, what: &str) -> Result<[Num; 3], ParseError> {
        self.expect(&Tok::LParen)?;
        let a = self.expect_num(what)?;
        let b = self.expect_num(what)?;
        let c = self.expect_num(what)?;
        self.expect(&Tok::RParen)?;
        Ok([a, b, c])
    }

    /// `[ a b c d ]`
    fn bracket_quad(&mut self, what: &str) -> Result<[Num; 4], ParseError> {
        self.expect(&Tok::LBracket)?;
        let a = self.expect_num(what)?;
        let b = self.expect_num(what)?;
        let c = self.expect_num(what)?;
        let d = self.expect_num(what)?;
        self.expect(&Tok::RBracket)?;
        Ok([a, b, c, d])
    }

    fn face(&mut self) -> Result<Face, ParseError> {
        let points = [
            self.paren_triple("plane point")?,
            self.paren_triple("plane point")?,
            self.paren_triple("plane point")?,
        ];

        // Brush primitives put the 2x3 texture matrix between the points and the shader,
        // so a '(' here disambiguates the format with no lookahead beyond one token.
        let bp_matrix = if matches!(self.peek(), Tok::LParen) {
            self.expect(&Tok::LParen)?;
            let r0 = self.paren_triple("texture matrix")?;
            let r1 = self.paren_triple("texture matrix")?;
            self.expect(&Tok::RParen)?;
            Some([r0, r1])
        } else {
            None
        };

        let shader = self.expect_ident("shader name")?;

        let tex = if matches!(self.peek(), Tok::LBracket) {
            if bp_matrix.is_some() {
                return self
                    .err("face has both a brush-primitives texture matrix and Valve 220 axes");
            }
            let u = self.bracket_quad("texture U axis")?;
            let v = self.bracket_quad("texture V axis")?;
            let rotate = self.expect_num("texture rotation")?;
            let sx = self.expect_num("texture X scale")?;
            let sy = self.expect_num("texture Y scale")?;
            TexDef::Valve220 {
                u,
                v,
                rotate,
                scale: [sx, sy],
            }
        } else if let Some(m) = bp_matrix {
            TexDef::BrushPrimitives { m }
        } else {
            let shift = [
                self.expect_num("texture X shift")?,
                self.expect_num("texture Y shift")?,
            ];
            let rotate = self.expect_num("texture rotation")?;
            let scale = [
                self.expect_num("texture X scale")?,
                self.expect_num("texture Y scale")?,
            ];
            TexDef::Axial {
                shift,
                rotate,
                scale,
            }
        };

        // Whatever remains on this line is the optional contents/flags/value trio, and
        // then anything a dialect we do not model has appended.
        let face_line = self.toks[self.pos.saturating_sub(1)].line;
        let mut rest: Vec<String> = Vec::new();
        while self.peek_line() == face_line {
            match self.peek().clone() {
                Tok::Ident(s) => {
                    self.advance();
                    rest.push(s);
                }
                _ => break,
            }
        }

        let (surface, extra) = if rest.len() >= 3 {
            let parse3 =
                |s: &str| -> Option<Num> { s.parse::<f64>().ok().map(|v| Num::parsed(s, v)) };
            match (parse3(&rest[0]), parse3(&rest[1]), parse3(&rest[2])) {
                (Some(contents), Some(flags), Some(value)) => (
                    Some(SurfaceFlags {
                        contents,
                        flags,
                        value,
                    }),
                    rest[3..].to_vec(),
                ),
                // Three non-numeric trailing tokens is not a flag trio; keep them raw
                // rather than corrupting them into zeroes.
                _ => (None, rest),
            }
        } else {
            (None, rest)
        };

        let trailing = self.trailing_comment();

        Ok(Face {
            leading: Vec::new(),
            trailing,
            points,
            shader,
            tex,
            surface,
            extra,
        })
    }

    fn patch_body(&mut self, leading: Vec<String>, kind: String) -> Result<Patch, ParseError> {
        self.expect(&Tok::LBrace)?;
        let shader = self.expect_ident("patch shader name")?;

        // Header arity varies by patch flavour (5 for patchDef2, 7 for patchDef3), so we
        // read whatever is inside the parentheses instead of asserting a count.
        self.expect(&Tok::LParen)?;
        let mut header = Vec::new();
        while !matches!(self.peek(), Tok::RParen) {
            if matches!(self.peek(), Tok::Eof) {
                return self.err("unterminated patch header");
            }
            header.push(self.expect_num("patch header value")?);
        }
        self.expect(&Tok::RParen)?;

        if header.len() < 2 {
            return self.err(format!(
                "{kind} header needs at least width and height, found {} value(s)",
                header.len()
            ));
        }

        // Control grid: ( ( (p) (p) ... ) ( ... ) )
        self.expect(&Tok::LParen)?;
        let mut rows = Vec::new();
        while !matches!(self.peek(), Tok::RParen) {
            if matches!(self.peek(), Tok::Eof) {
                return self.err("unterminated patch control grid");
            }
            self.expect(&Tok::LParen)?;
            let mut row = Vec::new();
            while !matches!(self.peek(), Tok::RParen) {
                if matches!(self.peek(), Tok::Eof) {
                    return self.err("unterminated patch control row");
                }
                self.expect(&Tok::LParen)?;
                let mut pt = Vec::new();
                while !matches!(self.peek(), Tok::RParen) {
                    if matches!(self.peek(), Tok::Eof) {
                        return self.err("unterminated patch control point");
                    }
                    pt.push(self.expect_num("patch control point component")?);
                }
                self.expect(&Tok::RParen)?;
                row.push(pt);
            }
            self.expect(&Tok::RParen)?;
            rows.push(row);
        }
        self.expect(&Tok::RParen)?;
        self.expect(&Tok::RBrace)?;

        Ok(Patch {
            leading,
            kind,
            shader,
            header,
            rows,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_an_axial_brush_from_a_real_map() {
        // Verbatim from corpus/real/ut4_woolis.map.
        let src = "\
// entity 0
{
\"classname\" \"worldspawn\"
// brush 0
{
( 768 1280 -8 ) ( 640 1280 -8 ) ( 640 0 -8 ) common/caulk 0 0 0 0.500000 0.500000 0 4 0
( 648 0 0 ) ( 648 1280 0 ) ( 776 1280 0 ) battlecow_floors/concrete_sidewalk_b 0 0 0 0.500000 0.500000 0 0 0
}
}
";
        let m = parse_map(src).unwrap();
        assert_eq!(m.entities.len(), 1);
        let e = &m.entities[0];
        assert_eq!(e.leading, vec!["// entity 0"]);
        assert_eq!(e.classname(), "worldspawn");
        assert_eq!(e.prims.len(), 1);

        let b = e.prims[0].as_brush().unwrap();
        assert_eq!(b.leading, vec!["// brush 0"]);
        assert_eq!(b.style, BrushStyle::Bare);
        assert_eq!(b.faces.len(), 2);

        let f = &b.faces[0];
        assert_eq!(f.shader, "common/caulk");
        assert_eq!(f.points[0][2].value(), -8.0);
        match &f.tex {
            TexDef::Axial {
                shift,
                rotate,
                scale,
            } => {
                assert_eq!(shift[0].value(), 0.0);
                assert_eq!(rotate.value(), 0.0);
                // The formatting must survive: 0.500000, not 0.5.
                assert_eq!(scale[0].to_string(), "0.500000");
            }
            other => panic!("expected axial texdef, got {other:?}"),
        }
        let s = f.surface.as_ref().unwrap();
        assert_eq!(
            (s.contents.value(), s.flags.value(), s.value.value()),
            (0.0, 4.0, 0.0)
        );
    }

    #[test]
    fn parses_brush_primitives_including_negative_zero() {
        // Verbatim from corpus/real/garden_couch.map — note the `-0` literals, which are
        // legal, meaningful to no one, and must round-trip anyway.
        let src = "\
{
\"classname\" \"worldspawn\"
{
brushDef
{
( 6 8 75 ) ( 6 0 75 ) ( -2 8 75 ) ( ( 0.0078125 0 -0 ) ( -0 0.0078125 0 ) ) abbey2/abbey2_hfx_wood2_light 0 0 0
}
}
}
";
        let m = parse_map(src).unwrap();
        let b = m.entities[0].prims[0].as_brush().unwrap();
        assert_eq!(b.style, BrushStyle::Keyword("brushDef".into()));
        let f = &b.faces[0];
        assert_eq!(f.shader, "abbey2/abbey2_hfx_wood2_light");
        match &f.tex {
            TexDef::BrushPrimitives { m } => {
                assert_eq!(m[0][0].value(), 0.0078125);
                assert_eq!(
                    m[0][2].to_string(),
                    "-0",
                    "negative zero must stay verbatim"
                );
                assert_eq!(m[1][0].to_string(), "-0");
            }
            other => panic!("expected brush primitives, got {other:?}"),
        }
        assert!(f.surface.is_some());
    }

    #[test]
    fn parses_valve220_axes() {
        let src = "\
{
\"mapversion\" \"220\"
\"classname\" \"worldspawn\"
{
( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) WALL01 [ 1 0 0 16 ] [ 0 -1 0 -8 ] 0 1 1
}
}
";
        let m = parse_map(src).unwrap();
        assert!(m.is_valve220());
        let f = &m.entities[0].prims[0].as_brush().unwrap().faces[0];
        assert_eq!(f.shader, "WALL01");
        match &f.tex {
            TexDef::Valve220 {
                u,
                v,
                rotate,
                scale,
            } => {
                assert_eq!(u[3].value(), 16.0);
                assert_eq!(v[1].value(), -1.0);
                assert_eq!(rotate.value(), 0.0);
                assert_eq!(scale, &[Num::new(1.0), Num::new(1.0)]);
            }
            other => panic!("expected valve220, got {other:?}"),
        }
        // Valve 220 faces normally carry no flag trio, and we must not invent one.
        assert!(
            f.surface.is_none(),
            "surface flags should be absent, got {:?}",
            f.surface
        );
        assert!(f.extra.is_empty());
    }

    #[test]
    fn detects_texdef_format_per_face_not_per_file() {
        // A hand-merged map with both conventions in one brush list must survive.
        let src = "\
{
\"classname\" \"worldspawn\"
{
( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) a/b 0 0 0 0.5 0.5 0 0 0
}
{
brushDef
{
( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) ( ( 1 0 0 ) ( 0 1 0 ) ) a/b 0 0 0
}
}
}
";
        let m = parse_map(src).unwrap();
        assert_eq!(
            m.texdef_kinds(),
            vec![TexDefKind::Axial, TexDefKind::BrushPrimitives]
        );
    }

    #[test]
    fn parses_patchdef2_with_width_major_rows() {
        // Verbatim shape from corpus/real/ut4_dofa.map: 9 wide, 3 high, so 9 rows of 3.
        let src = "\
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
";
        let m = parse_map(src).unwrap();
        let p = m.entities[0].prims[0].as_patch().unwrap();
        assert_eq!(p.kind, "patchDef2");
        assert_eq!(p.shader, "dofa/concrete_white");
        assert_eq!((p.width(), p.height()), (3, 2));
        assert_eq!(p.rows.len(), 3, "outer nesting must run over width");
        assert_eq!(p.rows[0].len(), 2);
        assert_eq!(p.rows[0][0].len(), 5);
        assert!(p.dimensions_consistent());
        // Long decimals must survive exactly.
        assert_eq!(p.rows[0][0][2].to_string(), "111.9999847412");
    }

    #[test]
    fn parses_patchdef3_seven_value_header() {
        let src = "\
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
";
        let m = parse_map(src).unwrap();
        let p = m.entities[0].prims[0].as_patch().unwrap();
        assert_eq!(p.header.len(), 7);
        assert_eq!((p.width(), p.height()), (3, 3));
        assert!(p.dimensions_consistent());
    }

    #[test]
    fn unknown_primitive_keyword_is_preserved_verbatim() {
        let src = "\
{
\"classname\" \"worldspawn\"
{
someFutureDef
{
( 0 0 0 ) whatever ( nested { } )
}
}
}
";
        let m = parse_map(src).unwrap();
        match &m.entities[0].prims[0] {
            Primitive::Raw(r) => {
                assert_eq!(r.keyword, "someFutureDef");
                assert!(r.text.starts_with('{'));
                assert!(r.text.ends_with('}'));
                assert!(r.text.contains("someFutureDef"));
                assert!(r.text.contains("nested"));
            }
            other => panic!("expected a raw block, got {other:?}"),
        }
    }

    #[test]
    fn duplicate_and_ordered_keys_are_preserved() {
        let src = "{\n\"classname\" \"x\"\n\"angle\" \"90\"\n\"angle\" \"180\"\n}\n";
        let m = parse_map(src).unwrap();
        assert_eq!(m.entities[0].keys.len(), 3);
        assert_eq!(m.entities[0].get("angle"), Some("90"));
    }

    #[test]
    fn empty_entity_and_empty_map_are_legal() {
        assert_eq!(parse_map("").unwrap().entities.len(), 0);
        let m = parse_map("{\n}\n").unwrap();
        assert_eq!(m.entities.len(), 1);
        assert!(m.entities[0].keys.is_empty());
        assert!(m.entities[0].prims.is_empty());
    }

    #[test]
    fn trailing_comments_after_the_last_entity_become_the_footer() {
        let m = parse_map("{\n}\n// end of map\n").unwrap();
        assert_eq!(m.footer, vec!["// end of map"]);
    }

    #[test]
    fn malformed_input_reports_a_useful_line() {
        let e = parse_map("{\n\"classname\"\n}\n").unwrap_err();
        assert!(e.message.contains("has no value"), "got {}", e.message);
        assert_eq!(e.line, 3);

        let e = parse_map("{\n{\n( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) sh 0 0 0 0.5\n}\n}\n").unwrap_err();
        assert!(
            e.message.contains("texture Y scale"),
            "a truncated axial texdef should name the missing field, got {}",
            e.message
        );

        assert!(
            parse_map("{\n\"a\" \"b\"\n").is_err(),
            "unterminated entity"
        );
        assert!(parse_map("garbage").is_err());
    }

    #[test]
    fn a_face_with_two_texdef_conventions_is_rejected() {
        let src = "{\n{\n( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) ( ( 1 0 0 ) ( 0 1 0 ) ) sh [ 1 0 0 0 ] [ 0 1 0 0 ] 0 1 1\n}\n}\n";
        let e = parse_map(src).unwrap_err();
        assert!(e.message.contains("both"), "got {}", e.message);
    }

    #[test]
    fn face_line_split_across_lines_still_finds_its_flag_trio() {
        // Whitespace-insensitive by spec; the trio is on the same line as the texdef end.
        let src = "{\n{\n( 0 0 0 )\n( 1 0 0 )\n( 0 1 0 ) sh 0 0 0 0.5 0.5 1 2 3\n}\n}\n";
        let m = parse_map(src).unwrap();
        let s = m.entities[0].prims[0].as_brush().unwrap().faces[0]
            .surface
            .as_ref()
            .unwrap();
        assert_eq!(
            (s.contents.value(), s.flags.value(), s.value.value()),
            (1.0, 2.0, 3.0)
        );
    }
}
