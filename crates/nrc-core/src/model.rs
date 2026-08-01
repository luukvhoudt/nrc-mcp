//! The in-memory document model for a `.map` file (levels L0–L2 of §3.1).
//!
//! The organizing principle is that this type is a **faithful model of the file**, not
//! an idealized model of the geometry. Key order is preserved, comments are preserved,
//! number formatting is preserved, and a primitive we do not recognize is preserved
//! verbatim rather than dropped. Geometry lives one layer up, derived on demand
//! ([`crate::winding`]).
//!
//! That priority is not fussiness. §3.2 makes byte-identical round-trip the gate for
//! the whole project, and every "harmless" normalization is a diff a mapper has to
//! review and a reason not to trust the tool with their map.

use crate::num::Num;

/// Which texture-coordinate convention a face uses.
///
/// All three are supported, autodetected on load and preserved on save (§2). They are a
/// per-face property rather than a per-file one, because that is what the parser can
/// actually guarantee — and a file that mixes them (hand-edited, or merged from two
/// sources) then round-trips correctly instead of being silently rewritten.
#[derive(Clone, Debug, PartialEq)]
pub enum TexDef {
    /// Classic Quake 3 "axial projection": `shader xoff yoff rot xscale yscale`.
    /// The texture is projected along whichever world axis the face most faces.
    Axial {
        shift: [Num; 2],
        rotate: Num,
        scale: [Num; 2],
    },
    /// Brush primitives: an explicit 2x3 texture matrix, written as
    /// `( ( a b c ) ( d e f ) )` between the plane points and the shader name.
    /// Rotation and scale are implicit in the matrix, which is why it survives
    /// arbitrary brush transforms without drift.
    BrushPrimitives { m: [[Num; 3]; 2] },
    /// Valve 220: explicit U and V axes with offsets, `[ ux uy uz uoff ]`.
    Valve220 {
        u: [Num; 4],
        v: [Num; 4],
        rotate: Num,
        scale: [Num; 2],
    },
}

impl TexDef {
    pub fn kind(&self) -> TexDefKind {
        match self {
            TexDef::Axial { .. } => TexDefKind::Axial,
            TexDef::BrushPrimitives { .. } => TexDefKind::BrushPrimitives,
            TexDef::Valve220 { .. } => TexDefKind::Valve220,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum TexDefKind {
    Axial,
    BrushPrimitives,
    Valve220,
}

impl TexDefKind {
    pub fn as_str(self) -> &'static str {
        match self {
            TexDefKind::Axial => "axial",
            TexDefKind::BrushPrimitives => "brush_primitives",
            TexDefKind::Valve220 => "valve220",
        }
    }
}

/// The `contents surfaceflags value` trio that trails a face line.
///
/// Optional because Valve 220 faces normally omit it, and because a hand-written map
/// may too. Storing the absence lets us not invent `0 0 0` on save.
#[derive(Clone, Debug, PartialEq, Default)]
pub struct SurfaceFlags {
    pub contents: Num,
    pub flags: Num,
    pub value: Num,
}

/// One face of a brush: a plane given by three points, a shader, and texture mapping.
///
/// The plane is stored as the **three points**, not as a normal and distance, because
/// that is what the file stores. Deriving a plane loses information (many point triples
/// give the same plane) and re-deriving points from a plane is what makes other tools
/// produce enormous diffs on save.
#[derive(Clone, Debug, PartialEq)]
pub struct Face {
    /// Own-line comments immediately above this face.
    pub leading: Vec<String>,
    /// A comment on the same line, after the face definition.
    pub trailing: Option<String>,
    /// The three plane points, in file order.
    pub points: [[Num; 3]; 3],
    pub shader: String,
    pub tex: TexDef,
    pub surface: Option<SurfaceFlags>,
    /// Tokens after everything we model, kept so an unfamiliar dialect still saves
    /// correctly. Non-empty here is a signal worth reporting, not silently tolerating.
    pub extra: Vec<String>,
}

impl Face {
    /// The three defining points as floats, in file order.
    pub fn point_vecs(&self) -> [crate::math::Vec3; 3] {
        std::array::from_fn(|i| {
            crate::math::vec3(
                self.points[i][0].value(),
                self.points[i][1].value(),
                self.points[i][2].value(),
            )
        })
    }

    /// The plane this face lies in, or `None` if its points are collinear.
    pub fn plane(&self) -> Option<crate::math::Plane> {
        let p = self.point_vecs();
        crate::math::Plane::from_points(p[0], p[1], p[2])
    }

    /// The exact integer plane, or `None` if the points are off-grid or collinear.
    pub fn iplane(&self) -> Option<crate::exact::IPlane> {
        let p = self.point_vecs();
        let a = crate::exact::IVec3::try_from_vec3(p[0])?;
        let b = crate::exact::IVec3::try_from_vec3(p[1])?;
        let c = crate::exact::IVec3::try_from_vec3(p[2])?;
        crate::exact::IPlane::from_points(a, b, c)
    }

    /// True if all nine plane-point coordinates sit on the given grid.
    pub fn is_on_grid(&self, grid: f64) -> bool {
        if grid <= 0.0 {
            return true;
        }
        self.points
            .iter()
            .flatten()
            .all(|n| (n.value() / grid).fract() == 0.0)
    }
}

/// How a brush block is spelled in the file.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BrushStyle {
    /// Face lines directly inside `{ }`, no keyword. Used by both axial projection and
    /// Valve 220.
    Bare,
    /// A keyword (`brushDef`, `brushDef3`, …) followed by a nested `{ }`.
    Keyword(String),
}

/// A convex polytope defined by its face half-spaces (L1 of §3.1).
#[derive(Clone, Debug, PartialEq)]
pub struct Brush {
    pub leading: Vec<String>,
    pub style: BrushStyle,
    pub faces: Vec<Face>,
}

impl Brush {
    /// The dominant texdef convention among this brush's faces.
    pub fn texdef_kind(&self) -> Option<TexDefKind> {
        self.faces.first().map(|f| f.tex.kind())
    }
}

/// A Bézier control grid (L2 of §3.1).
///
/// Kept deliberately loose: `kind` is the literal keyword and `header` the literal
/// number list, because this fork has more patch spellings than the format is usually
/// documented with, and a patch variant we have not enumerated must still round-trip.
/// Typed access goes through [`Patch::width`] and friends.
#[derive(Clone, Debug, PartialEq)]
pub struct Patch {
    pub leading: Vec<String>,
    /// `patchDef2`, `patchDef3`, or a fork-specific spelling.
    pub kind: String,
    pub shader: String,
    /// The parenthesized header numbers. `patchDef2` has 5
    /// (`width height contents flags value`); `patchDef3` has 7, inserting
    /// `subdivX subdivY` after the dimensions.
    pub header: Vec<Num>,
    /// Control points as `rows[width][height][components]`. The outer index runs over
    /// *width*, matching the on-disk nesting.
    pub rows: Vec<Vec<Vec<Num>>>,
}

impl Patch {
    pub fn width(&self) -> usize {
        self.header.first().map_or(0, |n| n.value().max(0.0) as usize)
    }

    pub fn height(&self) -> usize {
        self.header.get(1).map_or(0, |n| n.value().max(0.0) as usize)
    }

    /// True if the declared dimensions match the control points actually present.
    /// A mismatch means the file is malformed, and it is much better to say so than to
    /// index off the end later.
    pub fn dimensions_consistent(&self) -> bool {
        let (w, h) = (self.width(), self.height());
        self.rows.len() == w && self.rows.iter().all(|r| r.len() == h)
    }

    /// Control point positions, ignoring texture coordinates.
    pub fn control_points(&self) -> Vec<crate::math::Vec3> {
        self.rows
            .iter()
            .flatten()
            .filter(|p| p.len() >= 3)
            .map(|p| crate::math::vec3(p[0].value(), p[1].value(), p[2].value()))
            .collect()
    }
}

/// A primitive block inside an entity.
#[derive(Clone, Debug, PartialEq)]
pub enum Primitive {
    Brush(Brush),
    Patch(Patch),
    /// A block whose keyword we do not recognize, preserved as raw source text.
    ///
    /// This is the difference between "we support the formats we know about" and "we
    /// never lose your data". A `.map` that reaches us with a construct from a newer
    /// upstream still saves byte-identically; it merely cannot be reasoned about.
    Raw(RawBlock),
}

#[derive(Clone, Debug, PartialEq)]
pub struct RawBlock {
    pub leading: Vec<String>,
    /// The keyword that opened the block, for reporting.
    pub keyword: String,
    /// Verbatim source text of the entire block, `{` through matching `}`.
    pub text: String,
}

impl Primitive {
    pub fn as_brush(&self) -> Option<&Brush> {
        match self {
            Primitive::Brush(b) => Some(b),
            _ => None,
        }
    }

    pub fn as_patch(&self) -> Option<&Patch> {
        match self {
            Primitive::Patch(p) => Some(p),
            _ => None,
        }
    }

    pub fn leading(&self) -> &[String] {
        match self {
            Primitive::Brush(b) => &b.leading,
            Primitive::Patch(p) => &p.leading,
            Primitive::Raw(r) => &r.leading,
        }
    }
}

/// An entity: an ordered key/value list plus zero or more primitives.
///
/// Keys are a `Vec`, not a map. Radiant writes `classname` first and mappers rely on
/// the order they see; duplicate keys also occur in the wild and the last one wins at
/// load time in the engine. A `HashMap` would silently reorder and deduplicate, turning
/// every save into a large diff.
#[derive(Clone, Debug, PartialEq, Default)]
pub struct Entity {
    pub leading: Vec<String>,
    pub keys: Vec<(String, String)>,
    pub prims: Vec<Primitive>,
    /// Own-line comments just before the entity's closing brace.
    pub trailing: Vec<String>,
}

impl Entity {
    /// First value for `key`, which is what Radiant's own lookup does.
    pub fn get(&self, key: &str) -> Option<&str> {
        self.keys
            .iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v.as_str())
    }

    pub fn classname(&self) -> &str {
        self.get("classname").unwrap_or("")
    }

    pub fn is_worldspawn(&self) -> bool {
        self.classname() == "worldspawn"
    }

    /// Set `key`, replacing the first existing occurrence in place so key order is
    /// stable, or appending if absent.
    pub fn set(&mut self, key: &str, value: impl Into<String>) {
        let value = value.into();
        if let Some(slot) = self.keys.iter_mut().find(|(k, _)| k == key) {
            slot.1 = value;
        } else {
            self.keys.push((key.to_string(), value));
        }
    }

    pub fn remove(&mut self, key: &str) -> bool {
        let before = self.keys.len();
        self.keys.retain(|(k, _)| k != key);
        self.keys.len() != before
    }

    /// Parse a whitespace-separated vector value such as `origin`.
    pub fn get_vec3(&self, key: &str) -> Option<crate::math::Vec3> {
        let v = self.get(key)?;
        let mut it = v.split_whitespace().map(|t| t.parse::<f64>());
        match (it.next(), it.next(), it.next()) {
            (Some(Ok(x)), Some(Ok(y)), Some(Ok(z))) => Some(crate::math::vec3(x, y, z)),
            _ => None,
        }
    }

    pub fn origin(&self) -> Option<crate::math::Vec3> {
        self.get_vec3("origin")
    }

    pub fn brushes(&self) -> impl Iterator<Item = &Brush> {
        self.prims.iter().filter_map(Primitive::as_brush)
    }

    pub fn patches(&self) -> impl Iterator<Item = &Patch> {
        self.prims.iter().filter_map(Primitive::as_patch)
    }
}

/// Line ending style, recorded so we write the file back the way we found it.
///
/// Not cosmetic: rewriting a CRLF map as LF changes every line, which makes the
/// round-trip test meaningless and a mapper's version-control diff unreadable.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub enum LineEnding {
    #[default]
    Lf,
    Crlf,
}

impl LineEnding {
    pub fn as_str(self) -> &'static str {
        match self {
            LineEnding::Lf => "\n",
            LineEnding::Crlf => "\r\n",
        }
    }

    /// Detect from the first line ending in the source; LF if there is none.
    pub fn detect(src: &str) -> LineEnding {
        match src.find('\n') {
            Some(i) if i > 0 && src.as_bytes()[i - 1] == b'\r' => LineEnding::Crlf,
            _ => LineEnding::Lf,
        }
    }
}

/// A whole `.map` document.
#[derive(Clone, Debug, Default)]
pub struct Map {
    /// Raw whitespace before the first token.
    ///
    /// Not pedantry: NetRadiant-custom's token writer starts with a pending `'\n'`
    /// separator, so **every map this fork saves begins with a blank line**
    /// (`libs/script/scripttokenwriter.h`). Dropping it would make a byte-identical
    /// round-trip impossible for exactly the files we care most about.
    pub prologue: String,
    pub entities: Vec<Entity>,
    /// Own-line comments after the last entity.
    pub footer: Vec<String>,
    pub line_ending: LineEnding,
    /// Raw whitespace after the last token, including the final line ending.
    ///
    /// A bool saying "did it end with a newline?" is not enough. Real maps end with
    /// `}\r\n\r\n\r\n` (two spare blank lines), with no newline at all, and with mixed
    /// endings — `corpus/real/ut4_dofa_ac.map` is the first of those and was the only map
    /// in the corpus this kernel initially failed to reproduce. Keeping the bytes handles
    /// every case without enumerating them.
    pub epilogue: String,
}

impl Map {
    pub fn worldspawn(&self) -> Option<&Entity> {
        self.entities.iter().find(|e| e.is_worldspawn())
    }

    pub fn worldspawn_mut(&mut self) -> Option<&mut Entity> {
        self.entities.iter_mut().find(|e| e.is_worldspawn())
    }

    pub fn all_brushes(&self) -> impl Iterator<Item = &Brush> {
        self.entities.iter().flat_map(Entity::brushes)
    }

    pub fn all_patches(&self) -> impl Iterator<Item = &Patch> {
        self.entities.iter().flat_map(Entity::patches)
    }

    pub fn brush_count(&self) -> usize {
        self.all_brushes().count()
    }

    pub fn patch_count(&self) -> usize {
        self.all_patches().count()
    }

    /// Bounding box over brush plane points and patch control points.
    ///
    /// Approximate by construction: a Bézier patch bulges outside its control hull's
    /// corners, and brush plane points may sit off the brush. Good enough for framing a
    /// camera, not for a containment test.
    pub fn bounds(&self) -> crate::math::Aabb {
        let mut b = crate::math::Aabb::EMPTY;
        for br in self.all_brushes() {
            for f in &br.faces {
                for p in f.point_vecs() {
                    b.extend(p);
                }
            }
        }
        for p in self.all_patches() {
            for c in p.control_points() {
                b.extend(c);
            }
        }
        b
    }

    /// Which texdef conventions appear anywhere in the file.
    pub fn texdef_kinds(&self) -> Vec<TexDefKind> {
        let mut v: Vec<_> = self
            .all_brushes()
            .flat_map(|b| b.faces.iter().map(|f| f.tex.kind()))
            .collect();
        v.sort_unstable();
        v.dedup();
        v
    }

    /// True if the map declares the Valve 220 map version.
    pub fn is_valve220(&self) -> bool {
        self.worldspawn().and_then(|w| w.get("mapversion")) == Some("220")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::math::vec3;

    fn n(v: f64) -> Num {
        Num::new(v)
    }

    fn face(points: [[f64; 3]; 3]) -> Face {
        Face {
            leading: vec![],
            trailing: None,
            points: std::array::from_fn(|i| std::array::from_fn(|j| n(points[i][j]))),
            shader: "common/caulk".into(),
            tex: TexDef::Axial {
                shift: [n(0.0), n(0.0)],
                rotate: n(0.0),
                scale: [n(0.5), n(0.5)],
            },
            surface: Some(SurfaceFlags::default()),
            extra: vec![],
        }
    }

    #[test]
    fn entity_get_returns_first_of_duplicate_keys() {
        // Duplicates occur in real maps; Radiant's lookup takes the first, so ours must.
        let mut e = Entity::default();
        e.keys.push(("angle".into(), "90".into()));
        e.keys.push(("angle".into(), "180".into()));
        assert_eq!(e.get("angle"), Some("90"));
    }

    #[test]
    fn setting_a_key_preserves_its_position() {
        let mut e = Entity::default();
        e.set("classname", "point_entity_a");
        e.set("origin", "0 0 24");
        e.set("classname", "worldspawn");
        assert_eq!(
            e.keys,
            vec![
                ("classname".to_string(), "worldspawn".to_string()),
                ("origin".to_string(), "0 0 24".to_string()),
            ],
            "an in-place edit must not reorder keys"
        );
    }

    #[test]
    fn origin_parses_and_rejects_malformed_values() {
        let mut e = Entity::default();
        e.set("origin", "0 -64 16");
        assert_eq!(e.origin(), Some(vec3(0.0, -64.0, 16.0)));
        e.set("origin", "0 -64");
        assert_eq!(e.origin(), None, "a two-component origin is not a vector");
        e.set("origin", "0 nope 16");
        assert_eq!(e.origin(), None);
    }

    #[test]
    fn face_plane_matches_its_points() {
        let f = face([[0.0, 0.0, 0.0], [64.0, 0.0, 0.0], [0.0, 64.0, 0.0]]);
        let p = f.plane().unwrap();
        assert_eq!(p.normal, vec3(0.0, 0.0, -1.0));
        // The exact plane must agree with the float one about direction.
        assert!(f.iplane().unwrap().to_plane().approx_eq(&p));
    }

    #[test]
    fn off_grid_face_has_no_exact_plane() {
        let f = face([[0.0, 0.0, 0.5], [64.0, 0.0, 0.5], [0.0, 64.0, 0.5]]);
        assert!(f.plane().is_some(), "float plane is still available");
        assert!(f.iplane().is_none(), "exact plane must refuse off-grid input");
        assert!(!f.is_on_grid(1.0));
        assert!(f.is_on_grid(0.5));
    }

    #[test]
    fn patch_dimension_consistency_is_checked() {
        let mut p = Patch {
            leading: vec![],
            kind: "patchDef2".into(),
            shader: "dofa/concrete_white".into(),
            header: vec![n(2.0), n(3.0), n(0.0), n(0.0), n(0.0)],
            rows: vec![
                vec![vec![n(0.0); 5], vec![n(0.0); 5], vec![n(0.0); 5]],
                vec![vec![n(0.0); 5], vec![n(0.0); 5], vec![n(0.0); 5]],
            ],
        };
        assert_eq!((p.width(), p.height()), (2, 3));
        assert!(p.dimensions_consistent());
        p.rows.pop();
        assert!(!p.dimensions_consistent());
    }

    #[test]
    fn line_ending_detection() {
        assert_eq!(LineEnding::detect("a\r\nb"), LineEnding::Crlf);
        assert_eq!(LineEnding::detect("a\nb"), LineEnding::Lf);
        assert_eq!(LineEnding::detect("no newline"), LineEnding::Lf);
        // A lone \n at index 0 has no preceding \r to inspect.
        assert_eq!(LineEnding::detect("\nb"), LineEnding::Lf);
    }

    #[test]
    fn map_bounds_covers_brushes_and_patches() {
        let mut m = Map::default();
        let mut e = Entity::default();
        e.set("classname", "worldspawn");
        e.prims.push(Primitive::Brush(Brush {
            leading: vec![],
            style: BrushStyle::Bare,
            faces: vec![face([[0.0, 0.0, 0.0], [64.0, 0.0, 0.0], [0.0, 64.0, 0.0]])],
        }));
        e.prims.push(Primitive::Patch(Patch {
            leading: vec![],
            kind: "patchDef2".into(),
            shader: "x".into(),
            header: vec![n(1.0), n(1.0), n(0.0), n(0.0), n(0.0)],
            rows: vec![vec![vec![n(-8.0), n(0.0), n(128.0), n(0.0), n(0.0)]]],
        }));
        m.entities.push(e);
        let b = m.bounds();
        assert_eq!(b.min, vec3(-8.0, 0.0, 0.0));
        assert_eq!(b.max, vec3(64.0, 64.0, 128.0));
        assert_eq!(m.brush_count(), 1);
        assert_eq!(m.patch_count(), 1);
    }

    #[test]
    fn empty_map_has_empty_bounds_not_a_panic() {
        let m = Map::default();
        assert!(m.bounds().is_empty());
        assert_eq!(m.brush_count(), 0);
        assert!(m.worldspawn().is_none());
    }
}
