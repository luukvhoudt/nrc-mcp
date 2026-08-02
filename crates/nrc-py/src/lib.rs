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
    m.add_function(wrap_pyfunction!(solid_compile, m)?)?;
    m.add_function(wrap_pyfunction!(solid_commit, m)?)?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Solid IR (§4)
// ---------------------------------------------------------------------------

use nrc_solid::ir::Node;
use nrc_solid::prim::Axis as SolidAxis;

fn ivec_from(obj: &Bound<'_, PyAny>, what: &str) -> PyResult<nrc_core::exact::IVec3> {
    let v: Vec<f64> = obj
        .extract()
        .map_err(|_| err(format!("{what} must be a list of three numbers")))?;
    if v.len() != 3 {
        return Err(err(format!("{what} must have exactly three components")));
    }
    for c in &v {
        if !c.is_finite() || c.fract() != 0.0 {
            return Err(err(format!(
                "{what} must be whole numbers — the Solid IR works on the integer grid, and \
                 {c} is not on it"
            )));
        }
    }
    Ok(nrc_core::exact::ivec3(
        v[0] as i64,
        v[1] as i64,
        v[2] as i64,
    ))
}

fn axis_from(d: &Bound<'_, PyDict>, key: &str, default: &str) -> PyResult<SolidAxis> {
    let raw: String = match d.get_item(key)? {
        Some(v) => v
            .extract()
            .map_err(|_| err(format!("{key} must be a string")))?,
        None => default.to_string(),
    };
    SolidAxis::parse(&raw).ok_or_else(|| err(format!("{key} must be x, y or z — got {raw:?}")))
}

fn get<'py>(d: &Bound<'py, PyDict>, key: &str, op: &str) -> PyResult<Bound<'py, PyAny>> {
    d.get_item(key)?
        .ok_or_else(|| err(format!("a {op:?} node needs a {key:?} field")))
}

fn i64_from(d: &Bound<'_, PyDict>, key: &str, op: &str) -> PyResult<i64> {
    get(d, key, op)?
        .extract()
        .map_err(|_| err(format!("{key} must be a whole number in a {op:?} node")))
}

fn usize_from(d: &Bound<'_, PyDict>, key: &str, op: &str) -> PyResult<usize> {
    let v = i64_from(d, key, op)?;
    usize::try_from(v).map_err(|_| err(format!("{key} must not be negative, got {v}")))
}

fn f64_or(d: &Bound<'_, PyDict>, key: &str, default: f64) -> PyResult<f64> {
    match d.get_item(key)? {
        Some(v) => v
            .extract()
            .map_err(|_| err(format!("{key} must be a number"))),
        None => Ok(default),
    }
}

fn child(d: &Bound<'_, PyDict>, key: &str, op: &str, depth: usize) -> PyResult<Box<Node>> {
    let raw = get(d, key, op)?;
    let dict = raw
        .cast::<PyDict>()
        .map_err(|_| err(format!("{key} in a {op:?} node must be another IR node")))?;
    Ok(Box::new(node_from_dict(dict, depth + 1)?))
}

fn children(d: &Bound<'_, PyDict>, key: &str, op: &str, depth: usize) -> PyResult<Vec<Node>> {
    let raw = get(d, key, op)?;
    let list: Vec<Bound<'_, PyAny>> = raw
        .extract()
        .map_err(|_| err(format!("{key} in a {op:?} node must be a list of IR nodes")))?;
    let mut out = Vec::with_capacity(list.len());
    for item in list {
        let dict = item
            .cast::<PyDict>()
            .map_err(|_| err(format!("every entry of {key} must be an IR node")))?;
        out.push(node_from_dict(dict, depth + 1)?);
    }
    Ok(out)
}

/// Turn a Python dict into an IR node.
///
/// Errors name the field and the operator, because an agent debugging a nested tree from the
/// outside has nothing else to go on. The depth guard is here as well as in the evaluator: a
/// deeply nested dict would otherwise blow the stack during *conversion*, before the
/// evaluator's own limit could apply.
fn node_from_dict(d: &Bound<'_, PyDict>, depth: usize) -> PyResult<Node> {
    if depth > nrc_solid::ir::MAX_DEPTH {
        return Err(err(format!(
            "IR nesting deeper than {} is not accepted",
            nrc_solid::ir::MAX_DEPTH
        )));
    }
    let op: String = d
        .get_item("op")?
        .ok_or_else(|| err("every IR node needs an \"op\" field"))?
        .extract()
        .map_err(|_| err("\"op\" must be a string"))?;

    Ok(match op.as_str() {
        "box" => Node::Box {
            min: ivec_from(&get(d, "min", &op)?, "min")?,
            max: ivec_from(&get(d, "max", &op)?, "max")?,
        },
        "wedge" => Node::Wedge {
            min: ivec_from(&get(d, "min", &op)?, "min")?,
            max: ivec_from(&get(d, "max", &op)?, "max")?,
            along: axis_from(d, "along", "x")?,
            up: axis_from(d, "up", "z")?,
        },
        "prism" | "cylinder" => Node::Prism {
            min: ivec_from(&get(d, "min", &op)?, "min")?,
            max: ivec_from(&get(d, "max", &op)?, "max")?,
            axis: axis_from(d, "axis", "z")?,
            sides: usize_from(d, "sides", &op)?,
            start_deg: f64_or(d, "start_deg", 0.0)?,
        },
        "cone" => Node::Cone {
            min: ivec_from(&get(d, "min", &op)?, "min")?,
            max: ivec_from(&get(d, "max", &op)?, "max")?,
            axis: axis_from(d, "axis", "z")?,
            sides: usize_from(d, "sides", &op)?,
            start_deg: f64_or(d, "start_deg", 0.0)?,
        },
        "pyramid" => Node::Pyramid {
            min: ivec_from(&get(d, "min", &op)?, "min")?,
            max: ivec_from(&get(d, "max", &op)?, "max")?,
            axis: axis_from(d, "axis", "z")?,
        },
        "stair" => Node::Stair {
            origin: ivec_from(&get(d, "origin", &op)?, "origin")?,
            width: i64_from(d, "width", &op)?,
            steps: usize_from(d, "steps", &op)?,
            rise: i64_from(d, "rise", &op)?,
            run: i64_from(d, "run", &op)?,
            along: axis_from(d, "along", "x")?,
            up: axis_from(d, "up", "z")?,
        },
        "pipe" => Node::Pipe {
            min: ivec_from(&get(d, "min", &op)?, "min")?,
            max: ivec_from(&get(d, "max", &op)?, "max")?,
            axis: axis_from(d, "axis", "z")?,
            wall: i64_from(d, "wall", &op)?,
            sides: usize_from(d, "sides", &op)?,
            start_deg: f64_or(d, "start_deg", 0.0)?,
        },
        "arch" => Node::Arch {
            centre: ivec_from(
                &get(d, "centre", &op).or_else(|_| get(d, "center", &op))?,
                "centre",
            )?,
            outer_radius: i64_from(d, "outer_radius", &op)?,
            thickness: i64_from(d, "thickness", &op)?,
            depth: i64_from(d, "depth", &op)?,
            segments: usize_from(d, "segments", &op)?,
            axis: axis_from(d, "axis", "z")?,
        },
        "planes" => {
            let raw: Vec<Vec<f64>> = get(d, "planes", &op)?
                .extract()
                .map_err(|_| err("planes must be a list of [nx, ny, nz, d] lists"))?;
            let mut out = Vec::with_capacity(raw.len());
            for (i, pl) in raw.iter().enumerate() {
                if pl.len() != 4 {
                    return Err(err(format!(
                        "plane {i} needs exactly four numbers [nx, ny, nz, d], got {}",
                        pl.len()
                    )));
                }
                for c in pl {
                    if !c.is_finite() || c.fract() != 0.0 {
                        return Err(err(format!(
                            "plane {i} has a non-integer coefficient ({c}); half-spaces must be \
                             integral so the geometry stays exact"
                        )));
                    }
                }
                if pl[0] == 0.0 && pl[1] == 0.0 && pl[2] == 0.0 {
                    return Err(err(format!("plane {i} has a zero normal")));
                }
                out.push(nrc_core::exact::IPlane {
                    nx: pl[0] as i128,
                    ny: pl[1] as i128,
                    nz: pl[2] as i128,
                    d: pl[3] as i128,
                });
            }
            Node::Planes(out)
        }
        "union" => Node::Union(children(d, "parts", &op, depth)?),
        "intersect" => Node::Intersect(children(d, "parts", &op, depth)?),
        "subtract" => Node::Subtract {
            from: child(d, "from", &op, depth)?,
            cut: children(d, "cut", &op, depth)?,
        },
        "hollow" => Node::Hollow {
            solid: child(d, "solid", &op, depth)?,
            thickness: i64_from(d, "thickness", &op)?,
            open_faces: match d.get_item("open_faces")? {
                Some(v) => v
                    .extract()
                    .map_err(|_| err("open_faces must be a list of face indices"))?,
                None => Vec::new(),
            },
        },
        "carve_opening" => Node::CarveOpening {
            wall: child(d, "wall", &op, depth)?,
            min: ivec_from(&get(d, "min", &op)?, "min")?,
            max: ivec_from(&get(d, "max", &op)?, "max")?,
        },
        "translate" => Node::Translate {
            node: child(d, "node", &op, depth)?,
            by: ivec_from(&get(d, "by", &op)?, "by")?,
        },
        "mirror" => Node::Mirror {
            node: child(d, "node", &op, depth)?,
            axis: axis_from(d, "axis", "x")?,
            at: match d.get_item("at")? {
                Some(v) => v.extract().map_err(|_| err("at must be a whole number"))?,
                None => 0,
            },
        },
        "array" => Node::Array {
            node: child(d, "node", &op, depth)?,
            count: usize_from(d, "count", &op)?,
            offset: ivec_from(&get(d, "offset", &op)?, "offset")?,
        },
        other => {
            return Err(err(format!(
                "unknown operator {other:?}. Available: box, wedge, prism, cone, pyramid, \
                 stair, pipe, arch, planes, union, intersect, subtract, hollow, \
                 carve_opening, translate, mirror, array"
            )))
        }
    })
}

fn texture_spec_from(d: Option<&Bound<'_, PyDict>>) -> PyResult<nrc_solid::emit::TextureSpec> {
    let mut spec = nrc_solid::emit::TextureSpec::default();
    let Some(d) = d else { return Ok(spec) };
    if let Some(v) = d.get_item("default")? {
        spec.default = v
            .extract()
            .map_err(|_| err("default texture must be a string"))?;
    }
    if let Some(v) = d.get_item("top")? {
        spec.top = Some(
            v.extract()
                .map_err(|_| err("top texture must be a string"))?,
        );
    }
    if let Some(v) = d.get_item("bottom")? {
        spec.bottom = Some(
            v.extract()
                .map_err(|_| err("bottom texture must be a string"))?,
        );
    }
    if let Some(v) = d.get_item("scale")? {
        spec.scale = v
            .extract()
            .map_err(|_| err("texture scale must be a number"))?;
    }
    if let Some(v) = d.get_item("detail")? {
        spec.detail = v
            .extract()
            .map_err(|_| err("detail must be true or false"))?;
    }
    Ok(spec)
}

/// Compile an IR tree and describe the result, without touching any map.
#[pyfunction]
#[pyo3(signature = (ir, textures=None, grid=1))]
fn solid_compile<'py>(
    py: Python<'py>,
    ir: &Bound<'py, PyDict>,
    textures: Option<&Bound<'py, PyDict>>,
    grid: i64,
) -> PyResult<Bound<'py, PyDict>> {
    let node = node_from_dict(ir, 0)?;
    let evaluated = nrc_solid::ir::evaluate(&node).map_err(|e| err(e.to_string()))?;
    let spec = texture_spec_from(textures)?;
    let (brushes, report) = nrc_solid::emit::emit(&evaluated.solid, &spec, grid);

    let d = PyDict::new(py);
    d.set_item("op", node.kind())?;
    d.set_item("nodes", node.node_count())?;
    d.set_item("depth", node.depth())?;
    d.set_item("parts", evaluated.solid.len())?;
    d.set_item("brushes", report.brushes)?;
    d.set_item("faces", report.faces)?;
    d.set_item("off_grid_vertices", report.off_grid_vertices)?;
    d.set_item("non_integer_plane_faces", report.non_integer_faces)?;
    d.set_item("volume", evaluated.solid.volume())?;
    let mut warnings = evaluated.warnings.clone();
    warnings.extend(report.warnings.clone());
    d.set_item("warnings", warnings)?;

    let b = evaluated.solid.bounds();
    if b.is_empty() {
        d.set_item("bounds", py.None())?;
    } else {
        let bb = PyDict::new(py);
        bb.set_item("min", b.min.to_array())?;
        bb.set_item("max", b.max.to_array())?;
        bb.set_item("size", b.size().to_array())?;
        d.set_item("bounds", bb)?;
    }
    // Minimum thickness across parts, which is what the thin-brush post-condition checks.
    let thin = evaluated
        .solid
        .parts
        .iter()
        .filter_map(|p| p.min_thickness())
        .fold(f64::INFINITY, f64::min);
    d.set_item(
        "min_thickness",
        if thin.is_finite() { Some(thin) } else { None },
    )?;
    d.set_item("_brush_count_check", brushes.len())?;
    Ok(d)
}

/// Compile an IR tree and insert the resulting brushes into a map.
///
/// A free function rather than a method so that `PyMap` keeps a single `#[pymethods]` block;
/// it takes the map by reference and mutates it in place either way.
///
/// `dry_run` compiles and reports without touching the map, which is what `solid_preview`
/// uses. Nothing is written to disk here regardless — that still needs an explicit `save`.
#[pyfunction]
#[pyo3(signature = (map, ir, textures=None, grid=1, target_classname="worldspawn", dry_run=false, label=None))]
#[allow(clippy::too_many_arguments)]
fn solid_commit<'py>(
    py: Python<'py>,
    map: &Bound<'py, PyMap>,
    ir: &Bound<'py, PyDict>,
    textures: Option<&Bound<'py, PyDict>>,
    grid: i64,
    target_classname: &str,
    dry_run: bool,
    label: Option<String>,
) -> PyResult<Bound<'py, PyDict>> {
    let node = node_from_dict(ir, 0)?;
    let evaluated = nrc_solid::ir::evaluate(&node).map_err(|e| err(e.to_string()))?;
    let spec = texture_spec_from(textures)?;
    let (brushes, report) = nrc_solid::emit::emit(&evaluated.solid, &spec, grid);

    if brushes.is_empty() {
        return Err(err(
            "compilation produced no brushes; check the warnings from solid_compile",
        ));
    }

    let d = PyDict::new(py);
    d.set_item("brushes_created", brushes.len())?;
    d.set_item("faces", report.faces)?;
    d.set_item("off_grid_vertices", report.off_grid_vertices)?;
    d.set_item("non_integer_plane_faces", report.non_integer_faces)?;
    let mut warnings = evaluated.warnings.clone();
    warnings.extend(report.warnings.clone());
    d.set_item("warnings", warnings)?;
    d.set_item("dry_run", dry_run)?;
    let undo_group = label
        .map(|l| format!("solid_commit:{l}"))
        .unwrap_or_else(|| format!("solid_commit:{}", node.kind()));
    d.set_item("undo_group", undo_group)?;

    if dry_run {
        d.set_item("committed", false)?;
        return Ok(d);
    }

    let mut m = map.borrow_mut();
    // Marker comments name the group, so a human reading the .map afterwards can see where the
    // brushes came from and delete them as a unit.
    let count = brushes.len();
    let mut prims: Vec<nrc_core::model::Primitive> = brushes
        .into_iter()
        .map(nrc_core::model::Primitive::Brush)
        .collect();
    if let Some(nrc_core::model::Primitive::Brush(b)) = prims.first_mut() {
        b.leading
            .push(format!("// nrc-mcp {} ({count} brushes)", node.kind()));
    }

    let target = m
        .inner
        .entities
        .iter_mut()
        .find(|e| e.classname() == target_classname);
    match target {
        Some(e) => e.prims.extend(prims),
        None => {
            let mut e = nrc_core::model::Entity::default();
            e.set("classname", target_classname);
            e.prims = prims;
            m.inner.entities.push(e);
            d.set_item("created_entity", target_classname)?;
        }
    }
    d.set_item("committed", true)?;
    d.set_item("map_brushes_now", m.inner.brush_count())?;
    Ok(d)
}
