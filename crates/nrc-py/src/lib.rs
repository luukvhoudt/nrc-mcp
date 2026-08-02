//! Python bindings for the geometry kernel.
//!
//! The MCP server holds an open map across many tool calls, so the boundary is a
//! [`PyMap`] object rather than a set of free functions over file paths — re-parsing a
//! 850 KB map for every query would be wasteful and, worse, would let the map on disk
//! drift from the map being reasoned about.
//!
//! Values cross the boundary as plain Python dicts and lists. That keeps the Python side
//! free of wrapper types it would have to learn, and means an MCP tool can hand a result
//! straight to `json.dumps` without a serialization layer in between.
//!
//! Anything that can fail raises `ValueError` carrying the kernel's own message, including
//! the source line, because a parse failure a mapper cannot locate is not much better than
//! a crash.

use nrc_core::model::Primitive;
use nrc_core::stats::map_stats;
use nrc_core::validate::{validate_map, Severity, Thresholds};
use nrc_core::{parse_map, write_map, Map};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::path::PathBuf;

fn err(msg: impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(msg.to_string())
}

fn parse_overlay(name: &str) -> PyResult<nrc_render::Overlay> {
    use nrc_render::Overlay;
    Ok(match name {
        "shaded" => Overlay::Shaded,
        "structural" | "structural_detail" => Overlay::StructuralDetail,
        "caulk" => Overlay::Caulk,
        "offgrid" | "off_grid" => Overlay::OffGrid,
        other => {
            return Err(err(format!(
                "unknown overlay {other:?}: use shaded, structural, caulk or off_grid"
            )))
        }
    })
}

/// An open `.map` document.
#[pyclass(name = "Map", module = "nrc_py")]
pub struct PyMap {
    inner: Map,
    /// The bytes we loaded, kept so `round_trip()` can answer honestly rather than
    /// re-deriving what it hopes the file said.
    original: String,
    path: Option<PathBuf>,
}

#[pymethods]
impl PyMap {
    /// Parse from a string.
    #[staticmethod]
    fn parse(source: &str) -> PyResult<Self> {
        let inner = parse_map(source).map_err(err)?;
        Ok(Self {
            inner,
            original: source.to_string(),
            path: None,
        })
    }

    /// Load from disk.
    #[staticmethod]
    fn load(path: PathBuf) -> PyResult<Self> {
        let bytes = std::fs::read(&path).map_err(|e| err(format!("{}: {e}", path.display())))?;
        let source = String::from_utf8(bytes).map_err(|e| {
            err(format!(
                "{} is not valid UTF-8 (byte {}) — is it really a .map?",
                path.display(),
                e.utf8_error().valid_up_to()
            ))
        })?;
        let inner = parse_map(&source).map_err(|e| err(format!("{}: {e}", path.display())))?;
        Ok(Self {
            inner,
            original: source,
            path: Some(path),
        })
    }

    /// Serialize back to `.map` source.
    fn source(&self) -> String {
        write_map(&self.inner)
    }

    /// Write to disk. Defaults to the path this map was loaded from.
    ///
    /// Writes the whole file in one call, so a failure part-way cannot leave a truncated
    /// `.map` where somebody's level used to be.
    #[pyo3(signature = (path=None))]
    fn save(&self, path: Option<PathBuf>) -> PyResult<String> {
        let target = path
            .or_else(|| self.path.clone())
            .ok_or_else(|| err("no path given and this map was parsed from a string"))?;
        std::fs::write(&target, self.source())
            .map_err(|e| err(format!("{}: {e}", target.display())))?;
        Ok(target.display().to_string())
    }

    /// Whether re-serializing reproduces the bytes we loaded, and where it first differs.
    ///
    /// The §3.2 gate, available at runtime: the MCP layer calls this before offering to
    /// write to a user's map, so "we can reproduce your file exactly" is a checked claim
    /// rather than a promise made once in a test suite.
    fn round_trip<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let out = self.source();
        let d = PyDict::new(py);
        d.set_item("identical", out == self.original)?;
        d.set_item("input_bytes", self.original.len())?;
        d.set_item("output_bytes", out.len())?;
        if out != self.original {
            let a = self.original.as_bytes();
            let b = out.as_bytes();
            let offset = (0..a.len().min(b.len()))
                .find(|&i| a[i] != b[i])
                .unwrap_or(a.len().min(b.len()));
            let line = 1 + a[..offset].iter().filter(|&&c| c == b'\n').count();
            let diff = PyDict::new(py);
            diff.set_item("line", line)?;
            diff.set_item("byte_offset", offset)?;
            diff.set_item(
                "expected",
                self.original.lines().nth(line - 1).unwrap_or(""),
            )?;
            diff.set_item("actual", out.lines().nth(line - 1).unwrap_or(""))?;
            d.set_item("first_difference", diff)?;
        }
        Ok(d)
    }

    /// Map statistics. `grid` is the authoring grid alignment is measured against.
    #[pyo3(signature = (grid=1))]
    fn stats<'py>(&self, py: Python<'py>, grid: i64) -> PyResult<Bound<'py, PyDict>> {
        let s = map_stats(&self.inner, grid);
        let d = PyDict::new(py);
        d.set_item("entities", s.entities)?;
        d.set_item("brushes", s.brushes)?;
        d.set_item("patches", s.patches)?;
        d.set_item("raw_primitives", s.raw_primitives)?;
        d.set_item("faces", s.faces)?;
        d.set_item("detail_brushes", s.detail_brushes)?;
        d.set_item("structural_brushes", s.structural_brushes)?;
        d.set_item("is_valve220", s.is_valve220)?;
        d.set_item("grid", s.grid)?;
        d.set_item("vertices_on_grid", s.on_grid)?;
        d.set_item("vertices_total", s.total_vertices)?;
        d.set_item("grid_fraction", s.grid_fraction())?;
        d.set_item("unevaluated_brushes", s.unevaluated_brushes)?;
        d.set_item(
            "texdef_kinds",
            s.texdef_kinds
                .iter()
                .map(|k| k.as_str())
                .collect::<Vec<_>>(),
        )?;

        let patch_kinds = PyDict::new(py);
        for (k, v) in &s.patch_kinds {
            patch_kinds.set_item(k, v)?;
        }
        d.set_item("patch_kinds", patch_kinds)?;

        let ents = PyDict::new(py);
        for (k, v) in &s.entity_counts {
            ents.set_item(k, v)?;
        }
        d.set_item("entity_counts", ents)?;

        let top = PyList::empty(py);
        for (shader, faces) in s.top_shaders(20) {
            let e = PyDict::new(py);
            e.set_item("shader", shader)?;
            e.set_item("faces", faces)?;
            top.append(e)?;
        }
        d.set_item("top_shaders", top)?;

        if s.bounds_empty {
            d.set_item("bounds", py.None())?;
        } else {
            let b = PyDict::new(py);
            b.set_item("min", s.bounds_min)?;
            b.set_item("max", s.bounds_max)?;
            b.set_item(
                "size",
                [
                    s.bounds_max[0] - s.bounds_min[0],
                    s.bounds_max[1] - s.bounds_min[1],
                    s.bounds_max[2] - s.bounds_min[2],
                ],
            )?;
            d.set_item("bounds", b)?;
        }
        Ok(d)
    }

    /// Geometry and format findings. Game-agnostic: game rules live in the profile layer.
    #[pyo3(signature = (grid=1, severity_min="info"))]
    fn validate<'py>(
        &self,
        py: Python<'py>,
        grid: i64,
        severity_min: &str,
    ) -> PyResult<Bound<'py, PyDict>> {
        let floor = match severity_min {
            "error" => Severity::Error,
            "warning" => Severity::Warning,
            "info" => Severity::Info,
            other => {
                return Err(err(format!(
                    "severity_min must be one of error, warning, info — got {other:?}"
                )))
            }
        };
        let t = Thresholds {
            grid,
            ..Default::default()
        };
        let report = validate_map(&self.inner, &t);

        let findings = PyList::empty(py);
        for f in report.sorted() {
            if f.severity < floor {
                continue;
            }
            let e = PyDict::new(py);
            e.set_item("severity", f.severity.as_str())?;
            e.set_item("code", f.code)?;
            e.set_item("message", &f.message)?;
            e.set_item("location", f.location.to_string())?;
            e.set_item("entity", f.location.entity)?;
            e.set_item("primitive", f.location.primitive)?;
            e.set_item("face", f.location.face)?;
            e.set_item("rule_source", f.rule_source)?;
            e.set_item("confidence", f.confidence.as_str())?;
            findings.append(e)?;
        }

        let summary = PyDict::new(py);
        summary.set_item("error", report.count(Severity::Error))?;
        summary.set_item("warning", report.count(Severity::Warning))?;
        summary.set_item("info", report.count(Severity::Info))?;

        let d = PyDict::new(py);
        d.set_item("findings", findings)?;
        d.set_item("summary", summary)?;
        Ok(d)
    }

    /// Entities, optionally filtered by classname, with their keys in file order.
    #[pyo3(signature = (classname=None, with_keys=true))]
    fn entities<'py>(
        &self,
        py: Python<'py>,
        classname: Option<&str>,
        with_keys: bool,
    ) -> PyResult<Bound<'py, PyList>> {
        let out = PyList::empty(py);
        for (i, e) in self.inner.entities.iter().enumerate() {
            if let Some(want) = classname {
                if e.classname() != want {
                    continue;
                }
            }
            let d = PyDict::new(py);
            d.set_item("index", i)?;
            d.set_item("classname", e.classname())?;
            d.set_item("brushes", e.brushes().count())?;
            d.set_item("patches", e.patches().count())?;
            match e.origin() {
                Some(o) => d.set_item("origin", [o.x, o.y, o.z])?,
                None => d.set_item("origin", py.None())?,
            }
            if with_keys {
                // A list of pairs, not a dict: key order is meaningful and duplicate keys
                // occur in real maps, both of which a dict would silently destroy.
                let keys = PyList::empty(py);
                for (k, v) in &e.keys {
                    keys.append((k, v))?;
                }
                d.set_item("keys", keys)?;
            }
            out.append(d)?;
        }
        Ok(out)
    }

    /// Axis-aligned bounds as `(min, max)`, or `None` for an empty map.
    fn bounds(&self) -> Option<([f64; 3], [f64; 3])> {
        let b = self.inner.bounds();
        if b.is_empty() {
            None
        } else {
            Some((b.min.to_array(), b.max.to_array()))
        }
    }

    /// Exact vertices of one brush, plus what the kernel could determine about it.
    fn brush_geometry<'py>(
        &self,
        py: Python<'py>,
        entity: usize,
        primitive: usize,
    ) -> PyResult<Bound<'py, PyDict>> {
        let e = self
            .inner
            .entities
            .get(entity)
            .ok_or_else(|| err(format!("no entity {entity}")))?;
        let p = e
            .prims
            .get(primitive)
            .ok_or_else(|| err(format!("entity {entity} has no primitive {primitive}")))?;
        let b = match p {
            Primitive::Brush(b) => b,
            Primitive::Patch(_) => return Err(err("that primitive is a patch, not a brush")),
            Primitive::Raw(r) => {
                return Err(err(format!(
                    "that primitive is an unrecognized `{}` block and cannot be analysed",
                    r.keyword
                )))
            }
        };

        let d = PyDict::new(py);
        d.set_item("faces", b.faces.len())?;
        d.set_item(
            "shaders",
            b.faces
                .iter()
                .map(|f| f.shader.as_str())
                .collect::<Vec<_>>(),
        )?;
        match nrc_core::brush_geometry(&b.faces) {
            Err(deg) => {
                d.set_item("usable", false)?;
                d.set_item("reason", deg.to_string())?;
            }
            Ok(g) => {
                d.set_item("usable", true)?;
                let verts = PyList::empty(py);
                for v in &g.vertices {
                    let p = v.to_vec3();
                    verts.append([p.x, p.y, p.z])?;
                }
                d.set_item("vertices", verts)?;
                d.set_item("redundant_faces", g.redundant_faces())?;
                d.set_item("min_thickness", g.min_thickness())?;
                d.set_item("off_grid_vertices", g.off_grid_vertices(1))?;
                let bb = g.bounds();
                if !bb.is_empty() {
                    d.set_item("bounds", (bb.min.to_array(), bb.max.to_array()))?;
                }
            }
        }
        Ok(d)
    }

    /// Render a view, returning `(png_bytes, annotations)`.
    ///
    /// `eye_height` is required for `view="player_eye"` and must come from the game profile
    /// — hardcoding a standing height here would be the §7.4 seam violation the design
    /// document explicitly warns about, and the figure it assumed was wrong for the first
    /// target game anyway.
    #[pyo3(signature = (
        view = "top",
        overlay = "shaded",
        width = 900,
        height = 700,
        grid = 1,
        grid_spacing = 64.0,
        wireframe = None,
        hide_invisible = false,
        annotate = true,
        eye = None,
        target = None,
        yaw_deg = 0.0,
        eye_height = None,
        fov_deg = 55.0,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn render<'py>(
        &self,
        py: Python<'py>,
        view: &str,
        overlay: &str,
        width: u32,
        height: u32,
        grid: i64,
        grid_spacing: f64,
        wireframe: Option<bool>,
        hide_invisible: bool,
        annotate: bool,
        eye: Option<[f64; 3]>,
        target: Option<[f64; 3]>,
        yaw_deg: f64,
        eye_height: Option<f64>,
        fov_deg: f64,
    ) -> PyResult<(Bound<'py, pyo3::types::PyBytes>, Bound<'py, PyDict>)> {
        use nrc_render::camera::OrthoAxis;
        use nrc_render::{RenderOptions, SceneOptions, View};

        let overlay = parse_overlay(overlay)?;
        let scene = SceneOptions {
            grid,
            ..Default::default()
        };
        let spacing = if grid_spacing > 0.0 {
            Some(grid_spacing)
        } else {
            None
        };
        let to_vec = |a: [f64; 3]| nrc_core::math::vec3(a[0], a[1], a[2]);

        let result = if view == "sheet" || view == "contact_sheet" {
            nrc_render::contact_sheet(
                &self.inner,
                &nrc_render::ContactSheetOptions {
                    width,
                    height,
                    overlay,
                    grid_spacing: spacing,
                    scene,
                    hide_invisible,
                    player_eye: match (eye, eye_height) {
                        (Some(p), Some(h)) => Some((to_vec(p), yaw_deg, h)),
                        _ => None,
                    },
                },
            )
        } else {
            let v = match view {
                "top" => View::Ortho(OrthoAxis::Top),
                "front" => View::Ortho(OrthoAxis::Front),
                "side" => View::Ortho(OrthoAxis::Side),
                "perspective" => View::Perspective {
                    eye: eye.map(to_vec),
                    target: target.map(to_vec),
                    fov_deg,
                },
                "player_eye" => {
                    let position = eye.map(to_vec).ok_or_else(|| {
                        err("view='player_eye' needs `eye` as the floor position")
                    })?;
                    let eye_height = eye_height.ok_or_else(|| {
                        err(
                            "view='player_eye' needs `eye_height`; read it from the game \
                             profile rather than assuming a value",
                        )
                    })?;
                    View::PlayerEye {
                        position,
                        yaw_deg,
                        eye_height,
                        fov_deg,
                    }
                }
                other => {
                    return Err(err(format!(
                        "unknown view {other:?}: use top, front, side, perspective, \
                         player_eye or sheet"
                    )))
                }
            };
            nrc_render::render(
                &self.inner,
                &RenderOptions {
                    width,
                    height,
                    view: v,
                    overlay,
                    wireframe,
                    draw_edges: true,
                    hide_invisible,
                    grid_spacing: spacing,
                    annotate,
                    scene,
                },
            )
        }
        .map_err(err)?;

        let a = &result.annotations;
        let d = PyDict::new(py);
        d.set_item("view", &a.view)?;
        d.set_item("overlay", &a.overlay)?;
        d.set_item("width", a.width)?;
        d.set_item("height", a.height)?;
        d.set_item("grid", a.grid)?;
        d.set_item("off_grid_vertices", a.off_grid_vertices)?;
        d.set_item("skipped_brushes", a.skipped_brushes)?;
        d.set_item("skipped_examples", a.skipped_examples.clone())?;
        d.set_item("units_per_pixel", a.units_per_pixel)?;
        d.set_item("camera_eye", a.camera_eye)?;
        d.set_item("camera_target", a.camera_target)?;
        d.set_item("notes", a.notes.clone())?;
        d.set_item("png_bytes", result.png.len())?;

        let counts = PyDict::new(py);
        counts.set_item("structural_brushes", a.counts.structural)?;
        counts.set_item("detail_brushes", a.counts.detail)?;
        counts.set_item("brush_entities", a.counts.brush_entity)?;
        counts.set_item("patches", a.counts.patches)?;
        counts.set_item("facets", a.counts.facets)?;
        counts.set_item("invisible_facets", a.counts.caulk_facets)?;
        d.set_item("counts", counts)?;

        match (a.bounds_min, a.bounds_max) {
            (Some(lo), Some(hi)) => {
                let b = PyDict::new(py);
                b.set_item("min", lo)?;
                b.set_item("max", hi)?;
                b.set_item("size", a.size)?;
                d.set_item("bounds", b)?;
            }
            _ => d.set_item("bounds", py.None())?,
        }

        Ok((pyo3::types::PyBytes::new(py, &result.png), d))
    }

    #[getter]
    fn path(&self) -> Option<String> {
        self.path.as_ref().map(|p| p.display().to_string())
    }

    #[getter]
    fn entity_count(&self) -> usize {
        self.inner.entities.len()
    }

    fn __repr__(&self) -> String {
        format!(
            "<nrc_py.Map {} entities, {} brushes, {} patches{}>",
            self.inner.entities.len(),
            self.inner.brush_count(),
            self.inner.patch_count(),
            match &self.path {
                Some(p) => format!(" from {}", p.display()),
                None => String::new(),
            }
        )
    }
}

/// Parse and immediately re-serialize, reporting whether the bytes match.
#[pyfunction]
fn round_trip_check<'py>(py: Python<'py>, source: &str) -> PyResult<Bound<'py, PyDict>> {
    PyMap::parse(source)?.round_trip(py)
}

#[pymodule]
fn nrc_py(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyMap>()?;
    m.add_function(wrap_pyfunction!(round_trip_check, m)?)?;
    Ok(())
}
