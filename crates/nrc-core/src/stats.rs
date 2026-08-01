//! Map statistics — the read-only analysis behind `map_stats` and `structural_audit`.

use crate::model::{Map, TexDefKind};
use crate::winding::brush_geometry;
use std::collections::BTreeMap;

/// Bit 27 of a face's *contents* marks the brush as detail.
///
/// From upstream: `BRUSH_DETAIL_FLAG = 27`, `BRUSH_DETAIL_MASK = 1 << 27`
/// (`radiant/brush.h`). q3map2 reads the same bit as `C_DETAIL`. Worth having exactly
/// right, because the structural-vs-detail split is the single biggest lever on vis
/// performance (§6.1) and a wrong mask would silently invert the entire audit.
pub const BRUSH_DETAIL_FLAG: u32 = 27;
pub const BRUSH_DETAIL_MASK: i64 = 1 << BRUSH_DETAIL_FLAG;

#[derive(Clone, Debug, Default)]
pub struct MapStats {
    pub entities: usize,
    pub brushes: usize,
    pub patches: usize,
    pub raw_primitives: usize,
    pub faces: usize,
    /// Brushes whose faces carry the detail contents bit.
    pub detail_brushes: usize,
    /// Brushes that seal the map and block vis.
    pub structural_brushes: usize,
    pub entity_counts: BTreeMap<String, usize>,
    pub shader_counts: BTreeMap<String, usize>,
    pub texdef_kinds: Vec<TexDefKind>,
    pub patch_kinds: BTreeMap<String, usize>,
    pub bounds_min: [f64; 3],
    pub bounds_max: [f64; 3],
    pub bounds_empty: bool,
    /// Vertices on the given grid, and total vertices evaluated.
    pub on_grid: usize,
    pub total_vertices: usize,
    pub grid: i64,
    /// Brushes the exact kernel could not evaluate, with the reason counted by kind.
    pub unevaluated_brushes: usize,
    pub is_valve220: bool,
}

impl MapStats {
    /// Fraction of vertices on the grid, or `None` if nothing could be evaluated.
    pub fn grid_fraction(&self) -> Option<f64> {
        if self.total_vertices == 0 {
            None
        } else {
            Some(self.on_grid as f64 / self.total_vertices as f64)
        }
    }

    /// Shaders sorted by face count, most used first — the ones worth looking at.
    pub fn top_shaders(&self, n: usize) -> Vec<(&str, usize)> {
        let mut v: Vec<(&str, usize)> = self
            .shader_counts
            .iter()
            .map(|(k, &c)| (k.as_str(), c))
            .collect();
        v.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(b.0)));
        v.truncate(n);
        v
    }
}

/// Gather statistics. `grid` is the authoring grid to measure alignment against.
pub fn map_stats(map: &Map, grid: i64) -> MapStats {
    let mut s = MapStats {
        grid,
        is_valve220: map.is_valve220(),
        texdef_kinds: map.texdef_kinds(),
        ..Default::default()
    };

    s.entities = map.entities.len();
    for e in &map.entities {
        let name = if e.classname().is_empty() {
            "<no classname>"
        } else {
            e.classname()
        };
        *s.entity_counts.entry(name.to_string()).or_default() += 1;

        for p in &e.prims {
            match p {
                crate::model::Primitive::Brush(_) => s.brushes += 1,
                crate::model::Primitive::Patch(pt) => {
                    s.patches += 1;
                    *s.patch_kinds.entry(pt.kind.clone()).or_default() += 1;
                }
                crate::model::Primitive::Raw(_) => s.raw_primitives += 1,
            }
        }
    }

    for b in map.all_brushes() {
        s.faces += b.faces.len();
        // A brush is detail if any face says so; that is how the compiler reads it, since
        // the flag lives on sides but applies to the brush.
        let detail = b.faces.iter().any(|f| {
            f.surface
                .as_ref()
                .is_some_and(|sf| (sf.contents.value() as i64) & BRUSH_DETAIL_MASK != 0)
        });
        if detail {
            s.detail_brushes += 1;
        } else {
            s.structural_brushes += 1;
        }

        for f in &b.faces {
            *s.shader_counts.entry(f.shader.clone()).or_default() += 1;
        }

        match brush_geometry(&b.faces) {
            Ok(g) => {
                s.total_vertices += g.vertices.len();
                s.on_grid += g.vertices.len() - g.off_grid_vertices(grid).len();
            }
            Err(_) => s.unevaluated_brushes += 1,
        }
    }

    let b = map.bounds();
    s.bounds_empty = b.is_empty();
    if !b.is_empty() {
        s.bounds_min = b.min.to_array();
        s.bounds_max = b.max.to_array();
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::parse::parse_map;

    /// A 64-unit cube at the origin. Point order gives outward normals under q3's
    /// convention `n = cross(c - a, b - a)` with the solid at `n · p <= d`.
    const BOX64: &str = "{\n\
        ( 0 0 64 ) ( 0 1 64 ) ( 1 0 64 ) t/top 0 0 0 0.5 0.5 0 0 0\n\
        ( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) t/bot 0 0 0 0.5 0.5 0 0 0\n\
        ( 0 0 0 ) ( 0 0 1 ) ( 1 0 0 ) t/side 0 0 0 0.5 0.5 0 0 0\n\
        ( 0 64 0 ) ( 1 64 0 ) ( 0 64 1 ) t/side 0 0 0 0.5 0.5 0 0 0\n\
        ( 0 0 0 ) ( 0 1 0 ) ( 0 0 1 ) t/side 0 0 0 0.5 0.5 0 0 0\n\
        ( 64 0 0 ) ( 64 0 1 ) ( 64 1 0 ) t/side 0 0 0 0.5 0.5 0 0 0\n\
        }\n";

    #[test]
    fn counts_a_simple_map() {
        let m = parse_map(&format!("{{\n\"classname\" \"worldspawn\"\n{BOX64}}}\n")).unwrap();
        let s = map_stats(&m, 8);
        assert_eq!(s.entities, 1);
        assert_eq!(s.brushes, 1);
        assert_eq!(s.faces, 6);
        assert_eq!(s.patches, 0);
        assert_eq!(s.entity_counts["worldspawn"], 1);
        assert_eq!(s.total_vertices, 8);
        assert_eq!(s.on_grid, 8);
        assert_eq!(s.grid_fraction(), Some(1.0));
        assert_eq!(s.bounds_max, [64.0, 64.0, 64.0]);
        assert!(!s.bounds_empty);
    }

    #[test]
    fn shader_histogram_ranks_by_use() {
        let m = parse_map(&format!("{{\n\"classname\" \"worldspawn\"\n{BOX64}}}\n")).unwrap();
        let s = map_stats(&m, 8);
        assert_eq!(s.top_shaders(1), vec![("t/side", 4)]);
        assert_eq!(s.shader_counts["t/top"], 1);
    }

    #[test]
    fn detail_bit_27_classifies_brushes() {
        // 134217728 == 1 << 27. A brush with that contents bit is detail; without it,
        // structural. Getting this mask wrong would invert every structural audit.
        assert_eq!(BRUSH_DETAIL_MASK, 134_217_728);
        let detail = BOX64.replace("0.5 0.5 0 0 0", "0.5 0.5 134217728 0 0");
        let m = parse_map(&format!(
            "{{\n\"classname\" \"worldspawn\"\n{BOX64}{detail}}}\n"
        ))
        .unwrap();
        let s = map_stats(&m, 8);
        assert_eq!(s.brushes, 2);
        assert_eq!(s.detail_brushes, 1);
        assert_eq!(s.structural_brushes, 1);
    }

    #[test]
    fn unevaluated_brushes_are_counted_not_hidden() {
        // A three-face brush cannot be evaluated; the count must say so rather than
        // silently reporting zero vertices as if the map were clean.
        let bad = "{\n\
            ( 0 0 0 ) ( 1 0 0 ) ( 0 1 0 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 1 0 ) ( 0 0 1 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            ( 0 0 0 ) ( 0 0 1 ) ( 1 0 0 ) a/b 0 0 0 0.5 0.5 0 0 0\n\
            }\n";
        let m = parse_map(&format!("{{\n\"classname\" \"worldspawn\"\n{bad}}}\n")).unwrap();
        let s = map_stats(&m, 1);
        assert_eq!(s.brushes, 1);
        assert_eq!(s.unevaluated_brushes, 1);
        assert_eq!(s.total_vertices, 0);
        assert_eq!(s.grid_fraction(), None);
    }

    #[test]
    fn grid_fraction_reports_partial_alignment() {
        let m = parse_map(&format!("{{\n\"classname\" \"worldspawn\"\n{BOX64}}}\n")).unwrap();
        let s = map_stats(&m, 128);
        // Only the origin corner lands on a 128 grid.
        assert_eq!(s.on_grid, 1);
        assert_eq!(s.grid_fraction(), Some(0.125));
    }

    #[test]
    fn patch_kinds_are_tallied() {
        let patch = "{\npatchDef2\n{\nx/y\n( 2 2 0 0 0 )\n(\n\
                     ( ( 0 0 0 0 0 ) ( 0 0 0 0 0 ) )\n( ( 0 0 0 0 0 ) ( 0 0 0 0 0 ) )\n)\n}\n}\n";
        let m = parse_map(&format!("{{\n\"classname\" \"worldspawn\"\n{patch}}}\n")).unwrap();
        let s = map_stats(&m, 8);
        assert_eq!(s.patches, 1);
        assert_eq!(s.patch_kinds["patchDef2"], 1);
    }

    #[test]
    fn an_empty_map_reports_empty_bounds_rather_than_zeros_that_look_real() {
        let s = map_stats(&parse_map("").unwrap(), 8);
        assert!(s.bounds_empty);
        assert_eq!(s.entities, 0);
        assert_eq!(s.grid_fraction(), None);
    }
}
