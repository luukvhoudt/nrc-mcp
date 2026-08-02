//! `nrc` — command-line front end to the geometry kernel.
//!
//! This exists alongside the PyO3 bindings rather than instead of them, for two reasons
//! that both come from §1: a mise task can call it, so every kernel operation is a named,
//! replayable action rather than an in-process call a human cannot reproduce; and the
//! differential harness gets a stable text interface that does not depend on the Python
//! extension module building.
//!
//! Everything emits JSON on stdout and diagnostics on stderr, so output is pipeable.
//!
//! Exit codes: `0` clean, `1` the map has findings or does not round-trip, `2` the tool
//! could not do its job (missing file, parse error). Distinguishing 1 from 2 matters in
//! CI, where "the map is broken" and "the harness is broken" need different responses.

use nrc_core::stats::map_stats;
use nrc_core::validate::{validate_map, Severity, Thresholds};
use nrc_core::{round_trip_check, write_map};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

const USAGE: &str = "\
nrc — .map geometry kernel

USAGE:
    nrc roundtrip <file.map>...        verify byte-identical load/save (the §3.2 gate)
    nrc stats <file.map> [--grid N]    map statistics as JSON
    nrc validate <file.map> [--grid N] geometry and format findings as JSON
    nrc normalize <file.map> --write   re-serialize in place (refuses unless --write)
    nrc render <file.map> --out <png>  render a view (the §4.2 visual feedback loop)

OPTIONS:
    --grid N        authoring grid to measure alignment against (default 1)
    --quiet         JSON only, no human-readable summary on stderr
    --pretty        indent the JSON

RENDER OPTIONS:
    --out PATH      where to write the PNG (required)
    --view V        top | front | side | perspective | sheet     (default sheet)
    --overlay O     shaded | structural | caulk | offgrid        (default shaded)
    --size WxH      image size, e.g. 1200x900                    (default 1200x900)
    --grid-spacing N  world-unit grid to draw, 0 to disable      (default 64)
    --wireframe     force edges only; --solid forces filled faces
    --hide-invisible  skip caulk/nodraw/clip/trigger surfaces
                    (default: wireframe for ortho, solid for perspective)

EXIT CODES:
    0  clean    1  findings present / did not round-trip    2  tool error
";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() || args[0] == "-h" || args[0] == "--help" {
        print!("{USAGE}");
        return ExitCode::SUCCESS;
    }

    let cmd = args[0].clone();
    let mut files: Vec<PathBuf> = Vec::new();
    let mut grid: i64 = 1;
    let mut quiet = false;
    let mut pretty = false;
    let mut write = false;
    let mut out: Option<PathBuf> = None;
    let mut view = "sheet".to_string();
    let mut overlay = "shaded".to_string();
    let mut size = (1200u32, 900u32);
    let mut grid_spacing = 64.0f64;
    let mut wireframe: Option<bool> = None;
    let mut hide_invisible = false;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--grid" => {
                i += 1;
                match args.get(i).and_then(|s| s.parse::<i64>().ok()) {
                    Some(g) if g > 0 => grid = g,
                    _ => return fail("--grid needs a positive integer"),
                }
            }
            "--quiet" => quiet = true,
            "--pretty" => pretty = true,
            "--write" => write = true,
            "--wireframe" => wireframe = Some(true),
            "--solid" => wireframe = Some(false),
            "--hide-invisible" => hide_invisible = true,
            "--out" => {
                i += 1;
                match args.get(i) {
                    Some(p) => out = Some(PathBuf::from(p)),
                    None => return fail("--out needs a path"),
                }
            }
            "--view" => {
                i += 1;
                match args.get(i) {
                    Some(v) => view = v.clone(),
                    None => return fail("--view needs a value"),
                }
            }
            "--overlay" => {
                i += 1;
                match args.get(i) {
                    Some(v) => overlay = v.clone(),
                    None => return fail("--overlay needs a value"),
                }
            }
            "--size" => {
                i += 1;
                match args.get(i).and_then(|s| parse_size(s)) {
                    Some(s) => size = s,
                    None => return fail("--size needs WxH, e.g. 1200x900"),
                }
            }
            "--grid-spacing" => {
                i += 1;
                match args.get(i).and_then(|s| s.parse::<f64>().ok()) {
                    Some(g) if g >= 0.0 => grid_spacing = g,
                    _ => return fail("--grid-spacing needs a non-negative number"),
                }
            }
            other if other.starts_with('-') => {
                return fail(&format!("unknown option {other}"));
            }
            other => files.push(PathBuf::from(other)),
        }
        i += 1;
    }

    if files.is_empty() {
        return fail("no input files");
    }

    let result = match cmd.as_str() {
        "roundtrip" => cmd_roundtrip(&files, quiet),
        "stats" => cmd_stats(&files, grid),
        "validate" => cmd_validate(&files, grid, quiet),
        "normalize" => cmd_normalize(&files, write),
        "render" => cmd_render(
            &files,
            RenderArgs {
                out,
                view: &view,
                overlay: &overlay,
                size,
                grid,
                grid_spacing,
                wireframe,
                hide_invisible,
            },
        ),
        other => return fail(&format!("unknown command {other:?}\n\n{USAGE}")),
    };

    match result {
        Err(e) => fail(&e),
        Ok((value, code)) => {
            let text = if pretty {
                serde_json::to_string_pretty(&value)
            } else {
                serde_json::to_string(&value)
            };
            match text {
                Ok(t) => println!("{t}"),
                Err(e) => return fail(&format!("could not serialize output: {e}")),
            }
            code
        }
    }
}

fn fail(msg: &str) -> ExitCode {
    eprintln!("nrc: {msg}");
    ExitCode::from(2)
}

fn read(path: &Path) -> Result<String, String> {
    let bytes = std::fs::read(path).map_err(|e| format!("{}: {e}", path.display()))?;
    String::from_utf8(bytes).map_err(|e| {
        format!(
            "{} is not UTF-8 (byte {})",
            path.display(),
            e.utf8_error().valid_up_to()
        )
    })
}

fn cmd_roundtrip(files: &[PathBuf], quiet: bool) -> Result<(Value, ExitCode), String> {
    let mut out = Vec::new();
    let mut failed = 0usize;

    for f in files {
        let src = read(f)?;
        let entry = match round_trip_check(&src) {
            Err(e) => {
                failed += 1;
                json!({
                    "file": f.display().to_string(),
                    "ok": false,
                    "error": format!("parse failed at line {}: {}", e.line, e.message),
                })
            }
            Ok(r) => {
                if !r.identical {
                    failed += 1;
                }
                let mut e = json!({
                    "file": f.display().to_string(),
                    "ok": r.identical,
                    "input_bytes": r.input_len,
                    "output_bytes": r.output_len,
                    "entities": r.map.entities.len(),
                    "brushes": r.map.brush_count(),
                    "patches": r.map.patch_count(),
                });
                if let Some(d) = r.first_difference {
                    e["first_difference"] = json!({
                        "line": d.line,
                        "byte_offset": d.offset,
                        "expected": d.expected,
                        "actual": d.actual,
                    });
                }
                e
            }
        };
        out.push(entry);
    }

    if !quiet {
        eprintln!(
            "roundtrip: {}/{} identical",
            files.len() - failed,
            files.len()
        );
    }
    let code = if failed == 0 {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    };
    Ok((json!({"results": out, "failed": failed}), code))
}

fn cmd_stats(files: &[PathBuf], grid: i64) -> Result<(Value, ExitCode), String> {
    let mut out = Vec::new();
    for f in files {
        let src = read(f)?;
        let map = nrc_core::parse_map(&src)
            .map_err(|e| format!("{}: line {}: {}", f.display(), e.line, e.message))?;
        let s = map_stats(&map, grid);
        out.push(json!({
            "file": f.display().to_string(),
            "entities": s.entities,
            "brushes": s.brushes,
            "patches": s.patches,
            "raw_primitives": s.raw_primitives,
            "faces": s.faces,
            "detail_brushes": s.detail_brushes,
            "structural_brushes": s.structural_brushes,
            "texdef_kinds": s.texdef_kinds.iter().map(|k| k.as_str()).collect::<Vec<_>>(),
            "patch_kinds": s.patch_kinds,
            "is_valve220": s.is_valve220,
            "bounds": if s.bounds_empty { Value::Null } else {
                json!({"min": s.bounds_min, "max": s.bounds_max})
            },
            "grid": s.grid,
            "vertices_on_grid": s.on_grid,
            "vertices_total": s.total_vertices,
            "grid_fraction": s.grid_fraction(),
            "unevaluated_brushes": s.unevaluated_brushes,
            "top_shaders": s.top_shaders(15).iter()
                .map(|(n, c)| json!({"shader": n, "faces": c})).collect::<Vec<_>>(),
            "entity_counts": s.entity_counts,
        }));
    }
    Ok((json!({"results": out}), ExitCode::SUCCESS))
}

fn cmd_validate(files: &[PathBuf], grid: i64, quiet: bool) -> Result<(Value, ExitCode), String> {
    let t = Thresholds {
        grid,
        ..Default::default()
    };
    let mut out = Vec::new();
    let mut total_errors = 0usize;

    for f in files {
        let src = read(f)?;
        let map = nrc_core::parse_map(&src)
            .map_err(|e| format!("{}: line {}: {}", f.display(), e.line, e.message))?;
        let r = validate_map(&map, &t);
        total_errors += r.count(Severity::Error);

        out.push(json!({
            "file": f.display().to_string(),
            "summary": {
                "error": r.count(Severity::Error),
                "warning": r.count(Severity::Warning),
                "info": r.count(Severity::Info),
            },
            "findings": r.sorted().iter().map(|x| json!({
                "severity": x.severity.as_str(),
                "code": x.code,
                "message": x.message,
                "location": x.location.to_string(),
                "entity": x.location.entity,
                "primitive": x.location.primitive,
                "face": x.location.face,
                "rule_source": x.rule_source,
                "confidence": x.confidence.as_str(),
            })).collect::<Vec<_>>(),
        }));

        if !quiet {
            eprintln!(
                "{}: {} error(s), {} warning(s), {} info",
                f.display(),
                r.count(Severity::Error),
                r.count(Severity::Warning),
                r.count(Severity::Info)
            );
        }
    }

    let code = if total_errors == 0 {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    };
    Ok((json!({"results": out, "errors": total_errors}), code))
}

fn parse_size(s: &str) -> Option<(u32, u32)> {
    let (w, h) = s.split_once(['x', 'X', ','])?;
    Some((w.trim().parse().ok()?, h.trim().parse().ok()?))
}

struct RenderArgs<'a> {
    out: Option<PathBuf>,
    view: &'a str,
    overlay: &'a str,
    size: (u32, u32),
    grid: i64,
    grid_spacing: f64,
    wireframe: Option<bool>,
    hide_invisible: bool,
}

fn cmd_render(files: &[PathBuf], a: RenderArgs<'_>) -> Result<(Value, ExitCode), String> {
    use nrc_render::camera::OrthoAxis;
    use nrc_render::{
        contact_sheet, render, ContactSheetOptions, Overlay, RenderOptions, SceneOptions, View,
    };

    let out = a.out.ok_or("render needs --out <file.png>")?;
    if files.len() != 1 {
        return Err("render takes exactly one .map".into());
    }
    let src = read(&files[0])?;
    let map = nrc_core::parse_map(&src)
        .map_err(|e| format!("{}: line {}: {}", files[0].display(), e.line, e.message))?;

    let overlay = match a.overlay {
        "shaded" => Overlay::Shaded,
        "structural" | "structural_detail" => Overlay::StructuralDetail,
        "caulk" => Overlay::Caulk,
        "offgrid" | "off_grid" => Overlay::OffGrid,
        o => {
            return Err(format!(
                "unknown overlay {o:?}: use shaded, structural, caulk or offgrid"
            ))
        }
    };
    let scene = SceneOptions {
        grid: a.grid,
        ..Default::default()
    };
    let spacing = if a.grid_spacing > 0.0 {
        Some(a.grid_spacing)
    } else {
        None
    };

    let result = if a.view == "sheet" || a.view == "contact" {
        contact_sheet(
            &map,
            &ContactSheetOptions {
                width: a.size.0,
                height: a.size.1,
                overlay,
                grid_spacing: spacing,
                scene,
                hide_invisible: a.hide_invisible,
                player_eye: None,
            },
        )
    } else {
        let view = match a.view {
            "top" => View::Ortho(OrthoAxis::Top),
            "front" => View::Ortho(OrthoAxis::Front),
            "side" => View::Ortho(OrthoAxis::Side),
            "perspective" | "persp" => View::Perspective {
                eye: None,
                target: None,
                fov_deg: 55.0,
            },
            v => {
                return Err(format!(
                    "unknown view {v:?}: use top, front, side, perspective or sheet"
                ))
            }
        };
        render(
            &map,
            &RenderOptions {
                width: a.size.0,
                height: a.size.1,
                view,
                overlay,
                wireframe: a.wireframe,
                draw_edges: true,
                hide_invisible: a.hide_invisible,
                grid_spacing: spacing,
                annotate: true,
                scene,
            },
        )
    }
    .map_err(|e| e.to_string())?;

    std::fs::write(&out, &result.png).map_err(|e| format!("{}: {e}", out.display()))?;

    let ann = &result.annotations;
    Ok((
        json!({
            "file": files[0].display().to_string(),
            "png": out.display().to_string(),
            "png_bytes": result.png.len(),
            // The numbers §4.2 asks be surfaced come back as data, not burned into pixels.
            "view": ann.view,
            "overlay": ann.overlay,
            "size": [ann.width, ann.height],
            "bounds": match (ann.bounds_min, ann.bounds_max) {
                (Some(a), Some(b)) => json!({"min": a, "max": b, "size": ann.size}),
                _ => Value::Null,
            },
            "counts": {
                "structural_brushes": ann.counts.structural,
                "detail_brushes": ann.counts.detail,
                "brush_entities": ann.counts.brush_entity,
                "patches": ann.counts.patches,
                "facets_drawn": ann.counts.facets,
                "invisible_facets": ann.counts.caulk_facets,
            },
            "grid": ann.grid,
            "off_grid_vertices": ann.off_grid_vertices,
            "skipped_brushes": ann.skipped_brushes,
            "skipped_examples": ann.skipped_examples,
            "units_per_pixel": ann.units_per_pixel,
            "camera_eye": ann.camera_eye,
            "notes": ann.notes,
        }),
        ExitCode::SUCCESS,
    ))
}

fn cmd_normalize(files: &[PathBuf], write: bool) -> Result<(Value, ExitCode), String> {
    let mut out = Vec::new();
    for f in files {
        let src = read(f)?;
        let map = nrc_core::parse_map(&src)
            .map_err(|e| format!("{}: line {}: {}", f.display(), e.line, e.message))?;
        let text = write_map(&map);
        let changed = text != src;
        // Overwriting someone's level is not something to do because a flag was
        // forgotten, so the default is a dry run and the flag is required to write.
        if changed && write {
            std::fs::write(f, &text).map_err(|e| format!("{}: {e}", f.display()))?;
        }
        out.push(json!({
            "file": f.display().to_string(),
            "would_change": changed,
            "written": changed && write,
            "bytes_before": src.len(),
            "bytes_after": text.len(),
        }));
    }
    if !write {
        eprintln!("normalize: dry run (pass --write to modify files)");
    }
    Ok((json!({"results": out}), ExitCode::SUCCESS))
}
