//! Framebuffer, drawing primitives, and PNG output.

use miniz_oxide::deflate::compress_to_vec_zlib;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Rgb(pub u8, pub u8, pub u8);

impl Rgb {
    pub fn scaled(self, f: f64) -> Rgb {
        let c = |v: u8| (v as f64 * f).clamp(0.0, 255.0) as u8;
        Rgb(c(self.0), c(self.1), c(self.2))
    }

    pub fn mixed(self, other: Rgb, t: f64) -> Rgb {
        let t = t.clamp(0.0, 1.0);
        let m = |a: u8, b: u8| (a as f64 * (1.0 - t) + b as f64 * t).round() as u8;
        Rgb(m(self.0, other.0), m(self.1, other.1), m(self.2, other.2))
    }
}

/// An RGB framebuffer with an optional depth buffer.
pub struct Canvas {
    pub width: u32,
    pub height: u32,
    pixels: Vec<u8>,
    /// Smaller is nearer. `f32::INFINITY` means nothing drawn yet.
    depth: Vec<f32>,
}

impl Canvas {
    pub fn new(width: u32, height: u32, bg: Rgb) -> Canvas {
        let n = (width as usize) * (height as usize);
        let mut pixels = Vec::with_capacity(n * 3);
        for _ in 0..n {
            pixels.extend_from_slice(&[bg.0, bg.1, bg.2]);
        }
        Canvas {
            width,
            height,
            pixels,
            depth: vec![f32::INFINITY; n],
        }
    }

    fn index(&self, x: i64, y: i64) -> Option<usize> {
        if x < 0 || y < 0 || x >= self.width as i64 || y >= self.height as i64 {
            None
        } else {
            Some((y as usize) * (self.width as usize) + (x as usize))
        }
    }

    pub fn put(&mut self, x: i64, y: i64, c: Rgb) {
        if let Some(i) = self.index(x, y) {
            self.pixels[i * 3] = c.0;
            self.pixels[i * 3 + 1] = c.1;
            self.pixels[i * 3 + 2] = c.2;
        }
    }

    pub fn get(&self, x: i64, y: i64) -> Option<Rgb> {
        self.index(x, y).map(|i| {
            Rgb(
                self.pixels[i * 3],
                self.pixels[i * 3 + 1],
                self.pixels[i * 3 + 2],
            )
        })
    }

    /// Write a pixel only if it is nearer than what is already there.
    pub fn put_depth(&mut self, x: i64, y: i64, z: f32, c: Rgb) {
        if let Some(i) = self.index(x, y) {
            if z < self.depth[i] {
                self.depth[i] = z;
                self.pixels[i * 3] = c.0;
                self.pixels[i * 3 + 1] = c.1;
                self.pixels[i * 3 + 2] = c.2;
            }
        }
    }

    /// Write a pixel if it is at most `bias` behind what is there.
    ///
    /// Edges lie exactly on the faces they bound, so drawing them with a strict depth test
    /// makes them flicker in and out along the seam. A small tolerance draws the whole edge
    /// while still letting a nearer face hide it.
    pub fn put_depth_biased(&mut self, x: i64, y: i64, z: f32, bias: f32, c: Rgb) {
        if let Some(i) = self.index(x, y) {
            if z <= self.depth[i] + bias {
                self.depth[i] = self.depth[i].min(z);
                self.pixels[i * 3] = c.0;
                self.pixels[i * 3 + 1] = c.1;
                self.pixels[i * 3 + 2] = c.2;
            }
        }
    }

    pub fn depth_at(&self, x: i64, y: i64) -> f32 {
        self.index(x, y).map_or(f32::INFINITY, |i| self.depth[i])
    }

    /// Bresenham line, depth-tested with a tolerance.
    pub fn line(&mut self, a: (f64, f64, f32), b: (f64, f64, f32), c: Rgb, bias: f32) {
        let (x0, y0) = (a.0.round() as i64, a.1.round() as i64);
        let (x1, y1) = (b.0.round() as i64, b.1.round() as i64);
        let dx = (x1 - x0).abs();
        let dy = (y1 - y0).abs();
        let sx = if x0 < x1 { 1 } else { -1 };
        let sy = if y0 < y1 { 1 } else { -1 };
        let steps = dx.max(dy).max(1);

        let mut err = dx - dy;
        let (mut x, mut y) = (x0, y0);
        for i in 0..=steps {
            let t = i as f32 / steps as f32;
            let z = a.2 + (b.2 - a.2) * t;
            self.put_depth_biased(x, y, z, bias, c);
            if x == x1 && y == y1 {
                break;
            }
            let e2 = 2 * err;
            if e2 > -dy {
                err -= dy;
                x += sx;
            }
            if e2 < dx {
                err += dx;
                y += sy;
            }
        }
    }

    /// Filled triangle with per-vertex depth, using barycentric coverage.
    pub fn triangle(&mut self, p: [(f64, f64, f32); 3], c: Rgb) {
        let min_x = p.iter().map(|v| v.0).fold(f64::INFINITY, f64::min).floor() as i64;
        let max_x = p
            .iter()
            .map(|v| v.0)
            .fold(f64::NEG_INFINITY, f64::max)
            .ceil() as i64;
        let min_y = p.iter().map(|v| v.1).fold(f64::INFINITY, f64::min).floor() as i64;
        let max_y = p
            .iter()
            .map(|v| v.1)
            .fold(f64::NEG_INFINITY, f64::max)
            .ceil() as i64;

        // Twice the signed area. Zero means the triangle is edge-on and has no coverage.
        let area = (p[1].0 - p[0].0) * (p[2].1 - p[0].1) - (p[2].0 - p[0].0) * (p[1].1 - p[0].1);
        if area.abs() < 1e-12 {
            return;
        }

        let x_lo = min_x.max(0);
        let x_hi = max_x.min(self.width as i64 - 1);
        let y_lo = min_y.max(0);
        let y_hi = max_y.min(self.height as i64 - 1);

        for y in y_lo..=y_hi {
            for x in x_lo..=x_hi {
                let px = x as f64 + 0.5;
                let py = y as f64 + 0.5;
                let w0 = ((p[1].0 - px) * (p[2].1 - py) - (p[2].0 - px) * (p[1].1 - py)) / area;
                let w1 = ((p[2].0 - px) * (p[0].1 - py) - (p[0].0 - px) * (p[2].1 - py)) / area;
                let w2 = 1.0 - w0 - w1;
                // A small negative tolerance closes the hairline cracks that otherwise show
                // between the triangles of a fan-triangulated face.
                const EPS: f64 = -1e-9;
                if w0 < EPS || w1 < EPS || w2 < EPS {
                    continue;
                }
                let z = (w0 as f32) * p[0].2 + (w1 as f32) * p[1].2 + (w2 as f32) * p[2].2;
                self.put_depth(x, y, z, c);
            }
        }
    }

    /// Convex polygon as a triangle fan.
    pub fn polygon(&mut self, pts: &[(f64, f64, f32)], c: Rgb) {
        if pts.len() < 3 {
            return;
        }
        for i in 1..pts.len() - 1 {
            self.triangle([pts[0], pts[i], pts[i + 1]], c);
        }
    }

    pub fn rect_fill(&mut self, x0: i64, y0: i64, x1: i64, y1: i64, c: Rgb) {
        for y in y0.min(y1)..=y0.max(y1) {
            for x in x0.min(x1)..=x0.max(x1) {
                self.put(x, y, c);
            }
        }
    }

    pub fn rect_outline(&mut self, x0: i64, y0: i64, x1: i64, y1: i64, c: Rgb) {
        for x in x0.min(x1)..=x0.max(x1) {
            self.put(x, y0, c);
            self.put(x, y1, c);
        }
        for y in y0.min(y1)..=y0.max(y1) {
            self.put(x0, y, c);
            self.put(x1, y, c);
        }
    }

    /// A small filled cross, for marking a point that needs attention.
    pub fn marker(&mut self, cx: i64, cy: i64, radius: i64, c: Rgb) {
        for d in -radius..=radius {
            self.put(cx + d, cy, c);
            self.put(cx, cy + d, c);
        }
    }

    /// Copy another canvas in at an offset. Used to compose the contact sheet.
    pub fn blit(&mut self, other: &Canvas, ox: i64, oy: i64) {
        for y in 0..other.height as i64 {
            for x in 0..other.width as i64 {
                if let Some(c) = other.get(x, y) {
                    self.put(ox + x, oy + y, c);
                }
            }
        }
    }

    /// Encode as a PNG.
    pub fn to_png(&self) -> Vec<u8> {
        // Raw scanlines, each prefixed with filter type 0 (None). Filtering would compress
        // better, but flat-shaded renders are already highly compressible and correctness
        // here is worth more than a few percent.
        let mut raw = Vec::with_capacity(self.pixels.len() + self.height as usize);
        let stride = (self.width as usize) * 3;
        for y in 0..self.height as usize {
            raw.push(0);
            raw.extend_from_slice(&self.pixels[y * stride..(y + 1) * stride]);
        }
        let idat = compress_to_vec_zlib(&raw, 7);

        let mut png = Vec::with_capacity(idat.len() + 128);
        png.extend_from_slice(&[0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a]);

        let mut ihdr = Vec::with_capacity(13);
        ihdr.extend_from_slice(&self.width.to_be_bytes());
        ihdr.extend_from_slice(&self.height.to_be_bytes());
        ihdr.extend_from_slice(&[8, 2, 0, 0, 0]); // 8-bit, truecolour RGB, no interlace
        write_chunk(&mut png, b"IHDR", &ihdr);
        write_chunk(&mut png, b"IDAT", &idat);
        write_chunk(&mut png, b"IEND", &[]);
        png
    }
}

fn write_chunk(out: &mut Vec<u8>, kind: &[u8; 4], data: &[u8]) {
    out.extend_from_slice(&(data.len() as u32).to_be_bytes());
    out.extend_from_slice(kind);
    out.extend_from_slice(data);
    let mut crc = Crc32::new();
    crc.update(kind);
    crc.update(data);
    out.extend_from_slice(&crc.finish().to_be_bytes());
}

/// PNG's CRC-32 (IEEE 802.3, reflected). Bitwise rather than table-driven — a few million
/// iterations per image is nothing next to rasterization.
struct Crc32(u32);

impl Crc32 {
    fn new() -> Crc32 {
        Crc32(0xffff_ffff)
    }

    fn update(&mut self, data: &[u8]) {
        for &b in data {
            self.0 ^= b as u32;
            for _ in 0..8 {
                let mask = (self.0 & 1).wrapping_neg();
                self.0 = (self.0 >> 1) ^ (0xedb8_8320 & mask);
            }
        }
    }

    fn finish(self) -> u32 {
        !self.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canvas_starts_filled_with_the_background() {
        let c = Canvas::new(4, 3, Rgb(10, 20, 30));
        assert_eq!(c.get(0, 0), Some(Rgb(10, 20, 30)));
        assert_eq!(c.get(3, 2), Some(Rgb(10, 20, 30)));
        assert_eq!(c.get(4, 0), None, "out of bounds reads must be None");
        assert_eq!(c.get(-1, 0), None);
    }

    #[test]
    fn writes_outside_the_canvas_are_dropped_not_wrapped() {
        let mut c = Canvas::new(4, 4, Rgb(0, 0, 0));
        c.put(-1, 0, Rgb(255, 0, 0));
        c.put(4, 0, Rgb(255, 0, 0));
        c.put(0, 99, Rgb(255, 0, 0));
        // If any of those wrapped, some pixel would be red.
        for y in 0..4 {
            for x in 0..4 {
                assert_eq!(c.get(x, y), Some(Rgb(0, 0, 0)));
            }
        }
    }

    #[test]
    fn depth_test_keeps_the_nearest_fragment() {
        let mut c = Canvas::new(2, 2, Rgb(0, 0, 0));
        c.put_depth(0, 0, 5.0, Rgb(255, 0, 0));
        c.put_depth(0, 0, 9.0, Rgb(0, 255, 0)); // behind: ignored
        assert_eq!(c.get(0, 0), Some(Rgb(255, 0, 0)));
        c.put_depth(0, 0, 1.0, Rgb(0, 0, 255)); // in front: wins
        assert_eq!(c.get(0, 0), Some(Rgb(0, 0, 255)));
        assert_eq!(c.depth_at(0, 0), 1.0);
    }

    #[test]
    fn biased_depth_lets_an_edge_draw_over_its_own_face() {
        let mut c = Canvas::new(2, 2, Rgb(0, 0, 0));
        c.put_depth(0, 0, 10.0, Rgb(100, 100, 100));
        // Exactly coincident: a strict test would lose this, which is what makes edges
        // flicker along the face they bound.
        c.put_depth_biased(0, 0, 10.0, 0.5, Rgb(255, 255, 255));
        assert_eq!(c.get(0, 0), Some(Rgb(255, 255, 255)));
        // Well behind: still rejected.
        c.put_depth_biased(0, 0, 20.0, 0.5, Rgb(1, 2, 3));
        assert_eq!(c.get(0, 0), Some(Rgb(255, 255, 255)));
    }

    #[test]
    fn triangle_fills_its_interior() {
        let mut c = Canvas::new(16, 16, Rgb(0, 0, 0));
        c.triangle(
            [(1.0, 1.0, 1.0), (14.0, 1.0, 1.0), (1.0, 14.0, 1.0)],
            Rgb(255, 255, 255),
        );
        assert_eq!(
            c.get(2, 2),
            Some(Rgb(255, 255, 255)),
            "inside should be filled"
        );
        assert_eq!(
            c.get(13, 13),
            Some(Rgb(0, 0, 0)),
            "outside should be untouched"
        );
    }

    #[test]
    fn degenerate_triangle_draws_nothing_rather_than_dividing_by_zero() {
        let mut c = Canvas::new(8, 8, Rgb(0, 0, 0));
        c.triangle(
            [(1.0, 1.0, 1.0), (5.0, 1.0, 1.0), (3.0, 1.0, 1.0)],
            Rgb(255, 0, 0),
        );
        for y in 0..8 {
            for x in 0..8 {
                assert_eq!(c.get(x, y), Some(Rgb(0, 0, 0)));
            }
        }
    }

    #[test]
    fn polygon_of_a_square_covers_both_triangles() {
        let mut c = Canvas::new(16, 16, Rgb(0, 0, 0));
        c.polygon(
            &[
                (2.0, 2.0, 1.0),
                (12.0, 2.0, 1.0),
                (12.0, 12.0, 1.0),
                (2.0, 12.0, 1.0),
            ],
            Rgb(9, 9, 9),
        );
        for (x, y) in [(3, 3), (11, 3), (11, 11), (3, 11), (7, 7)] {
            assert_eq!(
                c.get(x, y),
                Some(Rgb(9, 9, 9)),
                "({x},{y}) should be covered"
            );
        }
    }

    #[test]
    fn line_reaches_both_endpoints() {
        let mut c = Canvas::new(16, 16, Rgb(0, 0, 0));
        c.line((2.0, 2.0, 1.0), (13.0, 9.0, 1.0), Rgb(255, 255, 255), 1.0);
        assert_eq!(c.get(2, 2), Some(Rgb(255, 255, 255)));
        assert_eq!(c.get(13, 9), Some(Rgb(255, 255, 255)));
    }

    #[test]
    fn a_zero_length_line_draws_one_pixel() {
        let mut c = Canvas::new(4, 4, Rgb(0, 0, 0));
        c.line((1.0, 1.0, 1.0), (1.0, 1.0, 1.0), Rgb(255, 0, 0), 1.0);
        assert_eq!(c.get(1, 1), Some(Rgb(255, 0, 0)));
    }

    #[test]
    fn blit_composites_at_an_offset() {
        let mut dst = Canvas::new(8, 8, Rgb(0, 0, 0));
        let mut src = Canvas::new(2, 2, Rgb(7, 7, 7));
        src.put(0, 0, Rgb(1, 2, 3));
        dst.blit(&src, 4, 4);
        assert_eq!(dst.get(4, 4), Some(Rgb(1, 2, 3)));
        assert_eq!(dst.get(5, 5), Some(Rgb(7, 7, 7)));
        assert_eq!(dst.get(3, 3), Some(Rgb(0, 0, 0)));
    }

    #[test]
    fn png_has_a_valid_signature_and_chunk_layout() {
        let png = Canvas::new(3, 2, Rgb(1, 2, 3)).to_png();
        assert_eq!(&png[..8], &[0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a]);
        // IHDR immediately after the signature, with a 13-byte payload.
        assert_eq!(&png[8..12], &13u32.to_be_bytes());
        assert_eq!(&png[12..16], b"IHDR");
        assert_eq!(&png[16..20], &3u32.to_be_bytes());
        assert_eq!(&png[20..24], &2u32.to_be_bytes());
        assert_eq!(&png[png.len() - 8..png.len() - 4], b"IEND");
    }

    #[test]
    fn png_pixel_data_decodes_back_to_what_was_drawn() {
        // Decode the IDAT ourselves so the encoder is checked against the format rather
        // than against itself.
        let mut c = Canvas::new(2, 1, Rgb(0, 0, 0));
        c.put(0, 0, Rgb(10, 20, 30));
        c.put(1, 0, Rgb(40, 50, 60));
        let png = c.to_png();

        let mut i = 8;
        let mut idat = Vec::new();
        while i + 8 <= png.len() {
            let len = u32::from_be_bytes(png[i..i + 4].try_into().unwrap()) as usize;
            let kind = &png[i + 4..i + 8];
            if kind == b"IDAT" {
                idat.extend_from_slice(&png[i + 8..i + 8 + len]);
            }
            i += 12 + len;
        }
        let raw = miniz_oxide::inflate::decompress_to_vec_zlib(&idat).expect("valid zlib");
        assert_eq!(
            raw,
            vec![0, 10, 20, 30, 40, 50, 60],
            "filter byte then RGB triples"
        );
    }

    #[test]
    fn crc32_matches_the_known_value_for_a_reference_input() {
        let mut c = Crc32::new();
        c.update(b"123456789");
        assert_eq!(c.finish(), 0xcbf4_3926);
    }

    #[test]
    fn colour_helpers_clamp_and_interpolate() {
        assert_eq!(Rgb(100, 100, 100).scaled(2.0), Rgb(200, 200, 200));
        assert_eq!(Rgb(200, 200, 200).scaled(4.0), Rgb(255, 255, 255));
        assert_eq!(Rgb(0, 0, 0).mixed(Rgb(100, 200, 40), 0.5), Rgb(50, 100, 20));
        assert_eq!(Rgb(0, 0, 0).mixed(Rgb(10, 10, 10), 5.0), Rgb(10, 10, 10));
    }
}
