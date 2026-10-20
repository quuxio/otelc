//! Compiler-owned declarations and emitted symbols from the original crate.
use crate::{definition_source, definitions::Definitions};
use anyhow::{Result, bail};
use quux_otelc_rust_adapter::compiler_frontend::{CompiledDefinition, Request};
use rustc_hir::def::DefKind;
use rustc_middle::{
    mono::MonoItem,
    ty::{InstanceKind, TyCtxt},
};
use rustc_span::def_id::LocalDefId;
use serde::Serialize;
use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    fs,
    path::{Path, PathBuf},
};

#[derive(Serialize)]
struct Inventory<'a> {
    schema_version: u32,
    functions: Vec<CompiledDefinition>,
    sources: &'a BTreeMap<PathBuf, String>,
    discovery_argv: &'a Vec<String>,
}

pub(crate) fn publish(tcx: TyCtxt<'_>, request: &Request, output: &Path) -> Result<()> {
    let definitions = Definitions {
        request,
        runtime_futures: true,
    };
    let mut symbols: HashMap<LocalDefId, BTreeSet<String>> = HashMap::new();
    for unit in tcx.collect_and_partition_mono_items(()).codegen_units {
        for item in unit.items().keys() {
            if let MonoItem::Fn(instance) = item
                && instance.def_id().is_local()
                && matches!(instance.def, InstanceKind::Item(_))
            {
                let id = instance.def_id().expect_local();
                let owner = definition_source(tcx, id).map_or(id, |(owner, _)| owner);
                symbols
                    .entry(owner)
                    .or_default()
                    .insert(tcx.symbol_name(*instance).name.to_string());
            }
        }
    }
    let mut functions = Vec::new();
    for id in tcx.hir_body_owners() {
        if tcx.coroutine_kind(id).is_some()
            || !matches!(
                tcx.def_kind(id),
                DefKind::Fn | DefKind::AssocFn | DefKind::Closure
            )
        {
            continue;
        }
        let Some((definition, expanded)) = definitions.definition(tcx, id)? else {
            continue;
        };
        let names = symbols.remove(&id).unwrap_or_default();
        for symbol in if names.is_empty() {
            BTreeSet::from([String::new()])
        } else {
            names
        } {
            functions.push(CompiledDefinition {
                definition: definition.clone(),
                compiler_name: tcx.def_path_str(id),
                compiler_expansion: expanded,
                emitted: !symbol.is_empty(),
                compiled_symbol: symbol,
            });
        }
    }
    if functions.len() > 65536
        || tcx.entry_fn(()).is_some()
            && !functions
                .iter()
                .any(|row| row.definition.site.startup && row.emitted)
    {
        bail!("Rust compiler inventory exceeds its bound or has no compiled main");
    }
    let bytes = serde_json::to_vec(&Inventory {
        schema_version: 3,
        functions,
        sources: &request.sources,
        discovery_argv: &request.discovery_argv,
    })?;
    if bytes.len() > 4 * 1024 * 1024 {
        bail!("Rust compiler inventory exceeds 4 MiB");
    }
    fs::write(output, bytes)?;
    Ok(())
}
