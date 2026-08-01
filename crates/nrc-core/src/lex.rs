//! Tokenizer for the `.map` text format.
//!
//! Two things distinguish this from a throwaway tokenizer. It **emits comments as
//! tokens** rather than skipping them, because Radiant writes `// entity 0` /
//! `// brush 3` markers that a lossless round-trip has to reproduce, and mappers write
//! notes to themselves that silently deleting would be unforgivable. And it records
//! whether each comment stood on **its own line**, which is what lets the parser decide
//! between "comment describing the thing below" and "comment trailing the line above".

use std::fmt;

#[derive(Clone, Debug, PartialEq)]
pub enum Tok {
    LBrace,
    RBrace,
    LParen,
    RParen,
    LBracket,
    RBracket,
    /// A double-quoted string, with the quotes stripped.
    Str(String),
    /// A bare word: keyword, shader name, or number. Kept as text so numbers can be
    /// reproduced verbatim (see [`crate::num::Num`]).
    Ident(String),
    /// A comment, including its `//` or `/* */` delimiters.
    Comment {
        text: String,
        own_line: bool,
    },
    Eof,
}

impl Tok {
    pub fn describe(&self) -> String {
        match self {
            Tok::LBrace => "'{'".into(),
            Tok::RBrace => "'}'".into(),
            Tok::LParen => "'('".into(),
            Tok::RParen => "')'".into(),
            Tok::LBracket => "'['".into(),
            Tok::RBracket => "']'".into(),
            Tok::Str(s) => format!("string {s:?}"),
            Tok::Ident(s) => format!("token {s:?}"),
            Tok::Comment { .. } => "comment".into(),
            Tok::Eof => "end of file".into(),
        }
    }
}

#[derive(Clone, Debug)]
pub struct Token {
    pub tok: Tok,
    /// 1-based source line, for error messages that a human can act on.
    pub line: u32,
    /// Byte range in the source. This is what lets the parser hand back an unrecognized
    /// primitive block *verbatim* instead of dropping it — a fork-specific construct we
    /// have never seen must survive a load/save cycle untouched.
    pub start: usize,
    pub end: usize,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LexError {
    pub line: u32,
    pub message: String,
}

impl fmt::Display for LexError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "line {}: {}", self.line, self.message)
    }
}

impl std::error::Error for LexError {}

/// Tokenize a whole `.map` source.
pub fn tokenize(src: &str) -> Result<Vec<Token>, LexError> {
    let b = src.as_bytes();
    let mut i = 0usize;
    let mut line = 1u32;
    // Whether a non-comment token has already appeared on the current line. A comment
    // that follows one trails it; a comment that does not introduces what comes next.
    let mut line_has_token = false;
    let mut out = Vec::new();

    while i < b.len() {
        let c = b[i];

        // Whitespace, tracking lines.
        if c == b'\n' {
            line += 1;
            line_has_token = false;
            i += 1;
            continue;
        }
        if c.is_ascii_whitespace() {
            i += 1;
            continue;
        }

        // Comments.
        if c == b'/' && i + 1 < b.len() && b[i + 1] == b'/' {
            let start = i;
            while i < b.len() && b[i] != b'\n' {
                i += 1;
            }
            // Trailing \r on CRLF files belongs to the line ending, not the comment.
            let mut end = i;
            if end > start && b[end - 1] == b'\r' {
                end -= 1;
            }
            out.push(Token {
                tok: Tok::Comment {
                    text: src[start..end].to_string(),
                    own_line: !line_has_token,
                },
                line,
                start,
                end: i,
            });
            continue;
        }
        if c == b'/' && i + 1 < b.len() && b[i + 1] == b'*' {
            let start = i;
            let start_line = line;
            i += 2;
            loop {
                if i + 1 >= b.len() {
                    return Err(LexError {
                        line: start_line,
                        message: "unterminated /* block comment".into(),
                    });
                }
                if b[i] == b'*' && b[i + 1] == b'/' {
                    i += 2;
                    break;
                }
                if b[i] == b'\n' {
                    line += 1;
                }
                i += 1;
            }
            out.push(Token {
                tok: Tok::Comment {
                    text: src[start..i].to_string(),
                    own_line: !line_has_token,
                },
                line: start_line,
                start,
                end: i,
            });
            line_has_token = false;
            continue;
        }

        // Quoted string. Entity values legitimately contain almost anything, so the
        // only terminator is the closing quote or a newline — matching Radiant, which
        // does not support escapes here. A `\"` in a key value is stored literally.
        if c == b'"' {
            let start_line = line;
            let tok_start = i;
            i += 1;
            let s = i;
            while i < b.len() && b[i] != b'"' {
                if b[i] == b'\n' {
                    return Err(LexError {
                        line: start_line,
                        message: "unterminated string (newline inside quotes)".into(),
                    });
                }
                i += 1;
            }
            if i >= b.len() {
                return Err(LexError {
                    line: start_line,
                    message: "unterminated string at end of file".into(),
                });
            }
            let text = src[s..i].to_string();
            i += 1; // consume the closing quote
            out.push(Token {
                tok: Tok::Str(text),
                line: start_line,
                start: tok_start,
                end: i,
            });
            line_has_token = true;
            continue;
        }

        // Punctuation.
        let punct = match c {
            b'{' => Some(Tok::LBrace),
            b'}' => Some(Tok::RBrace),
            b'(' => Some(Tok::LParen),
            b')' => Some(Tok::RParen),
            b'[' => Some(Tok::LBracket),
            b']' => Some(Tok::RBracket),
            _ => None,
        };
        if let Some(t) = punct {
            out.push(Token {
                tok: t,
                line,
                start: i,
                end: i + 1,
            });
            i += 1;
            line_has_token = true;
            continue;
        }

        // Bare word: shader name, keyword, or number. Runs to the next delimiter.
        // Note that a single `/` does *not* terminate it — shader names are paths like
        // `textures/urt/concrete_02` — but `//` does.
        let s = i;
        while i < b.len() {
            let d = b[i];
            if d.is_ascii_whitespace()
                || matches!(d, b'{' | b'}' | b'(' | b')' | b'[' | b']' | b'"')
                || (d == b'/' && i + 1 < b.len() && b[i + 1] == b'/')
            {
                break;
            }
            i += 1;
        }
        debug_assert!(
            i > s,
            "bare-word scan must always consume at least one byte"
        );
        out.push(Token {
            tok: Tok::Ident(src[s..i].to_string()),
            line,
            start: s,
            end: i,
        });
        line_has_token = true;
    }

    out.push(Token {
        tok: Tok::Eof,
        line,
        start: src.len(),
        end: src.len(),
    });
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn toks(src: &str) -> Vec<Tok> {
        tokenize(src).unwrap().into_iter().map(|t| t.tok).collect()
    }

    #[test]
    fn tokenizes_a_face_line() {
        let t = toks("( 0 0 0 ) common/caulk 0 0 0 0.5 0.5 0 0 0");
        assert_eq!(t[0], Tok::LParen);
        assert_eq!(t[1], Tok::Ident("0".into()));
        assert_eq!(t[4], Tok::RParen);
        assert_eq!(t[5], Tok::Ident("common/caulk".into()));
        assert_eq!(*t.last().unwrap(), Tok::Eof);
    }

    #[test]
    fn shader_paths_keep_single_slashes() {
        assert_eq!(
            toks("textures/urt/wall_01")[0],
            Tok::Ident("textures/urt/wall_01".into())
        );
    }

    #[test]
    fn double_slash_terminates_a_bare_word() {
        let t = toks("caulk//note");
        assert_eq!(t[0], Tok::Ident("caulk".into()));
        assert!(matches!(&t[1], Tok::Comment { text, .. } if text == "//note"));
    }

    #[test]
    fn own_line_comments_are_distinguished_from_trailing_ones() {
        let t = toks("// entity 0\n{\n\"a\" \"b\" // trailing\n}");
        assert!(matches!(&t[0], Tok::Comment { own_line: true, text } if text == "// entity 0"));
        assert!(matches!(&t[4], Tok::Comment { own_line: false, text } if text == "// trailing"));
    }

    #[test]
    fn crlf_files_do_not_absorb_the_carriage_return() {
        let t = toks("// entity 0\r\n{\r\n}\r\n");
        assert!(
            matches!(&t[0], Tok::Comment { text, .. } if text == "// entity 0"),
            "got {:?}",
            t[0]
        );
    }

    #[test]
    fn block_comments_span_lines_and_count_them() {
        let toks = tokenize("/* a\nb */\n{").unwrap();
        assert!(matches!(&toks[0].tok, Tok::Comment { text, .. } if text == "/* a\nb */"));
        assert_eq!(
            toks[1].line, 3,
            "line counting must survive a block comment"
        );
    }

    #[test]
    fn quoted_values_may_contain_punctuation() {
        let t = toks("\"origin\" \"0 -64 16\" \"model\" \"models/a.ase\"");
        assert_eq!(t[1], Tok::Str("0 -64 16".into()));
        assert_eq!(t[3], Tok::Str("models/a.ase".into()));
    }

    #[test]
    fn brackets_are_their_own_tokens_for_valve220() {
        let t = toks("[ 1 0 0 0 ]");
        assert_eq!(t[0], Tok::LBracket);
        assert_eq!(t[5], Tok::RBracket);
    }

    #[test]
    fn unterminated_constructs_are_errors_not_silent_truncation() {
        assert!(tokenize("\"unclosed").is_err());
        assert!(tokenize("\"unclosed\nnextline\"").is_err());
        assert!(tokenize("/* unclosed").is_err());
    }

    #[test]
    fn error_reports_the_line_the_problem_started_on() {
        let e = tokenize("{\n\n/* oops").unwrap_err();
        assert_eq!(e.line, 3);
    }

    #[test]
    fn empty_input_yields_only_eof() {
        assert_eq!(toks(""), vec![Tok::Eof]);
        assert_eq!(toks("   \n\t\n"), vec![Tok::Eof]);
    }
}
