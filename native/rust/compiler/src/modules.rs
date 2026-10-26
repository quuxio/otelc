//! Observe resolved original module routes before any diagnostic caller injection.
use crate::request;
use crate::request::{Request, digest};
use anyhow::{Context, Result, bail};
use rustc_hir::{ItemKind, def::DefKind, def_id::CRATE_DEF_ID};
use rustc_middle::ty::TyCtxt;
use rustc_span::{Span, def_id::LocalDefId};
use serde::Serialize;
use std::{collections::BTreeSet, fs, io::Write, path::PathBuf};

#[derive(Clone, Serialize)]
struct Location {
    file: PathBuf,
    start: u32,
    end: u32,
    expanded: bool,
}

#[derive(Serialize)]
struct Module {
    compiler_name: String,
    parent: Option<String>,
    kind: &'static str,
    declaration: Location,
    body: Location,
    eligible: bool,
    reason: Option<&'static str>,
}

fn location(tcx: TyCtxt<'_>, span: Span, inputs: &Request) -> Result<Location> {
    let map = tcx.sess.source_map();
    let source = map.lookup_source_file(span.lo());
    let file = PathBuf::from(source.name.prefer_local_unconditionally().to_string())
        .canonicalize()
        .context("module span has no physical source")?;
    let expected = inputs
        .sources
        .get(&file)
        .context("module uses an unbound original source")?;
    let original = fs::read_to_string(&file)?;
    if digest(original.as_bytes()) != *expected || !source.src_hash.matches(&original) {
        bail!("module source differs from the compiler's loaded bytes");
    }
    if map.lookup_source_file(span.hi()).start_pos != source.start_pos {
        bail!("module span crosses original source files");
    }
    let start = source.original_relative_byte_pos(span.lo()).0;
    let end = source.original_relative_byte_pos(span.hi()).0;
    if start > end || end as usize > original.len() {
        bail!("module span exceeds original input bytes");
    }
    Ok(Location {
        file,
        start,
        end,
        expanded: span.from_expansion(),
    })
}

fn unavailable(tcx: TyCtxt<'_>, mut id: LocalDefId, expanded: bool) -> Option<&'static str> {
    if expanded {
        return Some("expanded");
    }
    while id != CRATE_DEF_ID {
        let Some(parent) = tcx.opt_local_parent(id) else {
            return Some("block_module");
        };
        if tcx.def_kind(parent) != DefKind::Mod {
            return Some("block_module");
        }
        let expanded_body = parent != CRATE_DEF_ID
            && matches!(tcx.hir_expect_item(parent).kind, ItemKind::Mod(_, module)
                if module.spans.inner_span.from_expansion());
        if tcx.def_span(parent).from_expansion() || expanded_body {
            return Some("expanded");
        }
        id = parent;
    }
    None
}

pub(crate) fn publish(tcx: TyCtxt<'_>) -> Result<()> {
    let inputs = request();
    let body = location(tcx, tcx.hir_root_module().spans.inner_span, inputs)?;
    if body.file != inputs.entry {
        bail!("module inventory root differs from original crate entry");
    }
    let mut rows = vec![Module {
        compiler_name: tcx.def_path_str(CRATE_DEF_ID),
        parent: None,
        kind: "crate",
        declaration: body.clone(),
        body,
        eligible: true,
        reason: None,
    }];
    for item_id in tcx.hir_free_items() {
        let item = tcx.hir_expect_item(item_id.owner_id.def_id);
        let ItemKind::Mod(_, module) = item.kind else {
            continue;
        };
        let id = item.owner_id.def_id;
        let declaration = location(tcx, item.span, inputs)?;
        let body = location(tcx, module.spans.inner_span, inputs)?;
        let reason = unavailable(tcx, id, declaration.expanded || body.expanded);
        rows.push(Module {
            compiler_name: tcx.def_path_str(id),
            parent: tcx
                .opt_local_parent(id)
                .map(|parent| tcx.def_path_str(parent)),
            kind: if declaration.file == body.file {
                "inline"
            } else {
                "external"
            },
            declaration,
            body,
            eligible: reason.is_none(),
            reason,
        });
    }
    rows.sort_by(|left, right| left.compiler_name.cmp(&right.compiler_name));
    let names: BTreeSet<_> = rows.iter().map(|row| &row.compiler_name).collect();
    if rows.len() > 65536 || names.len() != rows.len() {
        bail!("duplicate or excessive compiler module identities");
    }
    let value = serde_json::json!({"schema_version":1, "kind":"otelc.rust.module-inventory",
        "mode":"module-inspect", "byte_coordinates":"original-utf8", "sources":inputs.sources,
        "modules":rows, "source_complete":false});
    let bytes = serde_json::to_vec(&value)?;
    if bytes.len() > 8 * 1024 * 1024 {
        bail!("Rust module inventory exceeds 8 MiB");
    }
    fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&inputs.output)?
        .write_all(&bytes)?;
    Ok(())
}
