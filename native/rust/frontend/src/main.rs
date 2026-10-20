#![feature(rustc_private)]
//! Compiler-owned declaration and monomorphisation identities, before LLVM runs.
extern crate rustc_driver;
extern crate rustc_hir;
extern crate rustc_interface;
extern crate rustc_middle;
extern crate rustc_span;

use anyhow::{Context, Result, bail};
mod definitions;
use definitions::Definitions;
use quux_otelc_rust_adapter::compiler_frontend::{CompiledDefinition, Request};
use rustc_driver::{Callbacks, Compilation};
use rustc_hir::def::DefKind;
use rustc_interface::interface::Compiler;
use rustc_middle::{
    mono::MonoItem,
    ty::{InstanceKind, TyCtxt},
};
use rustc_span::def_id::LocalDefId;
use serde::Serialize;
use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    fs,
    path::PathBuf,
};

#[derive(Serialize)]
struct Inventory<'a> {
    schema_version: u32,
    functions: Vec<CompiledDefinition>,
    sources: &'a BTreeMap<PathBuf, String>,
    discovery_argv: &'a Vec<String>,
}

struct Probe {
    request: Request,
    output: PathBuf,
    error: Option<anyhow::Error>,
}

impl Probe {
    fn inventory(&self, tcx: TyCtxt<'_>) -> Result<()> {
        let definitions = Definitions {
            request: &self.request,
            runtime_futures: false,
        };
        let mut symbols: HashMap<LocalDefId, BTreeSet<String>> = HashMap::new();
        for unit in tcx.collect_and_partition_mono_items(()).codegen_units {
            for item in unit.items().keys() {
                if let MonoItem::Fn(instance) = item
                    && instance.def_id().is_local()
                    && matches!(instance.def, InstanceKind::Item(_))
                {
                    symbols
                        .entry(instance.def_id().expect_local())
                        .or_default()
                        .insert(tcx.symbol_name(*instance).name.to_string());
                }
            }
        }
        let mut functions = vec![];
        for id in tcx.hir_body_owners() {
            if !matches!(
                tcx.def_kind(id),
                DefKind::Fn | DefKind::AssocFn | DefKind::Closure
            ) {
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
            || !functions
                .iter()
                .any(|row| row.definition.site.startup && row.emitted)
        {
            bail!("Rust compiler inventory exceeds its bound or has no compiled main");
        }
        let bytes = serde_json::to_vec(&Inventory {
            schema_version: 3,
            functions,
            sources: &self.request.sources,
            discovery_argv: &self.request.discovery_argv,
        })?;
        if bytes.len() > 4 * 1024 * 1024 {
            bail!("Rust compiler inventory exceeds 4 MiB");
        }
        fs::write(&self.output, bytes)?;
        Ok(())
    }
}

impl Callbacks for Probe {
    fn after_analysis<'tcx>(&mut self, _: &Compiler, tcx: TyCtxt<'tcx>) -> Compilation {
        match self.inventory(tcx) {
            Ok(()) => Compilation::Continue,
            Err(error) => {
                self.error = Some(error);
                Compilation::Stop
            }
        }
    }
}

fn run() -> Result<()> {
    let request = std::env::var_os("OTELC_RUST_FRONTEND_REQUEST")
        .context("Rust frontend request is missing")?;
    let output = std::env::var_os("OTELC_RUST_LLVM_INVENTORY")
        .context("Rust frontend inventory path is missing")?;
    let mut probe = Probe {
        request: Request::load(std::path::Path::new(&request))?,
        output: output.into(),
        error: None,
    };
    rustc_driver::run_compiler(&std::env::args().collect::<Vec<_>>(), &mut probe);
    if let Some(error) = probe.error {
        return Err(error);
    }
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("otelc Rust frontend: {error:#}");
        std::process::exit(1);
    }
}
