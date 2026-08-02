//! A tiny bitmap font, for the annotations that must be *in* the image.
//!
//! Deliberately limited to numerals, a few separators, and the axis letters X/Y/Z. Those are
//! the labels that only mean something in place — a coordinate has to sit next to the tick
//! it belongs to.
//!
//! Everything else that §4.2 asks be surfaced — brush counts, dimensions, warnings, which
//! overlay is active — comes back as structured data alongside the image
//! ([`crate::RenderResult::annotations`]) rather than being burned into pixels. That is
//! better for the caller, not merely cheaper: an agent can read a number exactly instead of
//! reading its own render, and a human gets legible text at any resolution.
//!
//! Glyphs are 3x5, stored column-major with bit 0 as the top row.

const GLYPH_W: i64 = 3;
const GLYPH_H: i64 = 5;

fn glyph(c: char) -> Option<[u8; 3]> {
    Some(match c.to_ascii_uppercase() {
        ' ' => [0x00, 0x00, 0x00],
        '0' => [0x1F, 0x11, 0x1F],
        '1' => [0x12, 0x1F, 0x10],
        '2' => [0x1D, 0x15, 0x17],
        '3' => [0x15, 0x15, 0x1F],
        '4' => [0x07, 0x04, 0x1F],
        '5' => [0x17, 0x15, 0x1D],
        '6' => [0x1F, 0x15, 0x1D],
        '7' => [0x01, 0x01, 0x1F],
        '8' => [0x1F, 0x15, 0x1F],
        '9' => [0x17, 0x15, 0x1F],
        '-' => [0x04, 0x04, 0x04],
        '.' => [0x00, 0x10, 0x00],
        ',' => [0x00, 0x18, 0x00],
        ':' => [0x00, 0x0A, 0x00],
        '/' => [0x18, 0x04, 0x03],
        'X' => [0x1B, 0x04, 0x1B],
        'Y' => [0x07, 0x1C, 0x07],
        'Z' => [0x19, 0x15, 0x13],
        _ => return None,
    })
}

/// Width in pixels of `text` at the given scale, including inter-glyph spacing.
pub fn text_width(text: &str, scale: i64) -> i64 {
    let n = text.chars().count() as i64;
    if n == 0 {
        0
    } else {
        n * (GLYPH_W + 1) * scale - scale
    }
}

pub fn text_height(scale: i64) -> i64 {
    GLYPH_H * scale
}

/// Draw `text` with its top-left at `(x, y)`.
///
/// Characters with no glyph are drawn as a small box, so a missing glyph is visible as a
/// gap in a label rather than silently shifting everything after it.
pub fn draw_text(
    canvas: &mut crate::canvas::Canvas,
    x: i64,
    y: i64,
    text: &str,
    scale: i64,
    colour: crate::canvas::Rgb,
) {
    let scale = scale.max(1);
    let mut cx = x;
    for ch in text.chars() {
        match glyph(ch) {
            Some(cols) => {
                for (gx, col) in cols.iter().enumerate() {
                    for gy in 0..GLYPH_H {
                        if col & (1 << gy) != 0 {
                            let px = cx + gx as i64 * scale;
                            let py = y + gy * scale;
                            canvas.rect_fill(px, py, px + scale - 1, py + scale - 1, colour);
                        }
                    }
                }
            }
            None => {
                canvas.rect_outline(
                    cx,
                    y,
                    cx + GLYPH_W * scale - 1,
                    y + GLYPH_H * scale - 1,
                    colour,
                );
            }
        }
        cx += (GLYPH_W + 1) * scale;
    }
}

/// Format a coordinate for a label: integral values lose their decimal point, and anything
/// else keeps one place. Axis ticks are almost always integral, and `1024.0` reads worse
/// than `1024` when space is this tight.
pub fn fmt_coord(v: f64) -> String {
    if !v.is_finite() {
        return "-".to_string();
    }
    if (v - v.round()).abs() < 1e-6 {
        format!("{}", v.round() as i64)
    } else {
        format!("{v:.1}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::canvas::{Canvas, Rgb};

    #[test]
    fn every_declared_glyph_is_reachable_and_lowercase_maps_up() {
        for c in "0123456789-.,:/XYZ ".chars() {
            assert!(glyph(c).is_some(), "{c:?} should have a glyph");
        }
        assert_eq!(glyph('x'), glyph('X'));
        assert_eq!(glyph('%'), None);
    }

    #[test]
    fn widths_account_for_spacing_without_a_trailing_gap() {
        assert_eq!(text_width("", 1), 0);
        assert_eq!(text_width("1", 1), 3);
        assert_eq!(text_width("12", 1), 7); // 3 + 1 + 3
        assert_eq!(text_width("12", 2), 14);
        assert_eq!(text_height(2), 10);
    }

    #[test]
    fn drawing_stays_inside_the_reported_bounds() {
        let mut c = Canvas::new(40, 12, Rgb(0, 0, 0));
        draw_text(&mut c, 2, 2, "10", 2, Rgb(255, 255, 255));
        let w = text_width("10", 2);
        let h = text_height(2);
        // Nothing may be drawn outside the advertised box, or labels will overlap.
        for y in 0..12i64 {
            for x in 0..40i64 {
                let inside = x >= 2 && x < 2 + w && y >= 2 && y < 2 + h;
                if !inside {
                    assert_eq!(c.get(x, y), Some(Rgb(0, 0, 0)), "ink escaped at ({x},{y})");
                }
            }
        }
    }

    #[test]
    fn text_actually_marks_pixels() {
        let mut c = Canvas::new(20, 10, Rgb(0, 0, 0));
        draw_text(&mut c, 0, 0, "8", 1, Rgb(255, 255, 255));
        let lit = (0..10i64)
            .flat_map(|y| (0..20i64).map(move |x| (x, y)))
            .filter(|&(x, y)| c.get(x, y) == Some(Rgb(255, 255, 255)))
            .count();
        // '8' is the densest glyph: 13 of the 15 cells.
        assert_eq!(lit, 13);
    }

    #[test]
    fn unknown_characters_draw_a_box_rather_than_vanishing() {
        let mut c = Canvas::new(20, 10, Rgb(0, 0, 0));
        draw_text(&mut c, 0, 0, "%", 1, Rgb(255, 255, 255));
        assert_eq!(
            c.get(0, 0),
            Some(Rgb(255, 255, 255)),
            "box outline expected"
        );
    }

    #[test]
    fn negative_positions_are_clipped_not_wrapped() {
        let mut c = Canvas::new(8, 8, Rgb(0, 0, 0));
        draw_text(&mut c, -20, -20, "123", 1, Rgb(255, 0, 0));
        for y in 0..8 {
            for x in 0..8 {
                assert_eq!(c.get(x, y), Some(Rgb(0, 0, 0)));
            }
        }
    }

    #[test]
    fn coordinates_drop_a_pointless_decimal() {
        assert_eq!(fmt_coord(1024.0), "1024");
        assert_eq!(fmt_coord(-64.0), "-64");
        assert_eq!(fmt_coord(0.0), "0");
        assert_eq!(fmt_coord(55.4666), "55.5");
        assert_eq!(fmt_coord(f64::NAN), "-");
    }
}
