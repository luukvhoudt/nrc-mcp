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

OPTIONS:
    --grid N        authoring grid to measure alignment against (default 1)
    --quiet         JSON only, no human-readable summary on stderr
    --pretty        indent the JSON

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
