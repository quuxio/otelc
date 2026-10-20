#![feature(rustc_private)]
//! Compiler-owned declaration and monomorphisation identities, before LLVM runs.
extern crate rustc_driver;
extern crate rustc_hir;
extern crate rustc_interface;
extern crate rustc_middle;
extern crate rustc_span;

use anyhow::{Context, Result, bail};
use quux_otelc_config::Selection;
use quux_otelc_rust_adapter::{
    compiler_frontend::{CompiledDefinition, Request, position},
    compiler_inventory::Definition,
    transform::CompilerSite,
};
use rustc_driver::{Callbacks, Compilation};
use rustc_hir::def::DefKind;
use rustc_interface::interface::Compiler;
use rustc_middle::{
    mono::MonoItem,
    ty::{InstanceKind, TyCtxt},
};
use rustc_span::{Span, def_id::LocalDefId};
use serde::Serialize;
use sha2::{Digest, Sha256};
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
    fn name(tcx: TyCtxt<'_>, id: LocalDefId) -> String {
        tcx.def_path(id.to_def_id())
            .data
            .iter()
            .map(|component| {
                component
                    .data
                    .get_opt_name()
                    .map(|name| name.to_string())
                    .unwrap_or_else(|| {
                        format!(
                            "<{}#{}>",
                            format!("{:?}", component.data).to_ascii_lowercase(),
                            component.disambiguator
                        )
                    })
            })
            .collect::<Vec<_>>()
            .join(".")
    }
    fn original(&self, tcx: TyCtxt<'_>, id: LocalDefId) -> Result<Option<Definition>> {
        let map = tcx.sess.source_map();
        let span = tcx.def_span(id);
        let location = map.lookup_char_pos(span.lo());
        let file = PathBuf::from(
            location
                .file
                .name
                .prefer_local_unconditionally()
                .to_string(),
        );
        let Ok(file) = file.canonicalize() else {
            return Ok(None);
        };
        if !self.request.sources.contains_key(&file)
            || !file.starts_with(&self.request.root)
            || self
                .request
                .protected_source_roots
                .iter()
                .any(|path| file.starts_with(path))
        {
            return Ok(None);
        }
        let body = tcx.hir_body_owned_by(id).value.span;
        if [span.hi(), body.lo(), body.hi()].iter().any(|position| {
            map.lookup_char_pos(*position).file.start_pos != location.file.start_pos
        }) {
            return Ok(None);
        }
        let source = fs::read_to_string(&file)?;
        if format!("{:x}", Sha256::digest(source.as_bytes())) != self.request.sources[&file]
            || !location.file.src_hash.matches(&source)
        {
            bail!("original Rust source differs from the compiler's loaded input");
        }
        let at = |position_| {
            position(
                &source,
                location.file.original_relative_byte_pos(position_).0 as usize,
            )
        };
        self.request.original(
            file.to_str().context("Rust frontend path must be UTF-8")?,
            (at(body.lo())?, at(body.hi())?),
            at(span.lo())?,
            tcx.def_kind(id) == DefKind::Closure,
        )
    }

    fn qualified(
        tcx: TyCtxt<'_>,
        id: LocalDefId,
        mut definition: Definition,
        expanded: bool,
    ) -> Result<Option<(Definition, bool)>> {
        if definition.unsupported == "const functions cannot contain runtime probes" {
            // CTFE has already finished. Only emitted runtime bodies receive IR
            // probes; no SDK call is inserted into Rust's const evaluator.
            definition.unsupported.clear();
        }
        definition.site.startup = tcx
            .entry_fn(())
            .is_some_and(|entry| entry.0 == id.to_def_id());
        if tcx.asyncness(id.to_def_id()).is_async() || tcx.coroutine_kind(id.to_def_id()).is_some()
        {
            definition.unsupported =
                "async compiler bodies require future-completion qualification".into();
        }
        if (definition.selected || definition.site.startup)
            && (!definition.unsupported.is_empty() || definition.name.len() > 1024)
        {
            bail!(
                "Rust LLVM: unsupported compiler definition {}: {}",
                definition.name,
                definition.unsupported
            );
        }
        Ok(Some((definition, expanded)))
    }

    fn definition(&self, tcx: TyCtxt<'_>, id: LocalDefId) -> Result<Option<(Definition, bool)>> {
        let request = &self.request;
        let original_span = tcx.def_span(id);
        let expanded = original_span.from_expansion();
        // include! and retained attribute expansions can still map exactly to
        // an original declaration. Preserve its configured name and annotations.
        if let Some(original) = self.original(tcx, id)? {
            return Self::qualified(tcx, id, original, expanded);
        }
        let span = if expanded {
            original_span.source_callsite()
        } else {
            original_span
        };
        let map = tcx.sess.source_map();
        let location = map.lookup_char_pos(span.lo());
        let file = PathBuf::from(
            location
                .file
                .name
                .prefer_local_unconditionally()
                .to_string(),
        );
        let Ok(file) = file.canonicalize() else {
            return Ok(None);
        };
        if !request.sources.contains_key(&file)
            || !file.starts_with(&request.root)
            || request
                .protected_source_roots
                .iter()
                .any(|path| file.starts_with(path))
            || file.extension().is_none_or(|extension| extension != "rs")
        {
            return Ok(None);
        }
        let source = fs::read_to_string(&file)?;
        if format!("{:x}", Sha256::digest(source.as_bytes())) != request.sources[&file]
            || !location.file.src_hash.matches(&source)
        {
            bail!("original Rust source differs from the compiler's loaded input");
        }
        let coordinates = |span: Span| -> Result<((usize, usize), (usize, usize))> {
            let end = map.lookup_char_pos(span.hi());
            if end.file.start_pos != location.file.start_pos {
                bail!("Rust compiler declaration spans multiple physical inputs");
            }
            Ok((
                position(
                    &source,
                    location.file.original_relative_byte_pos(span.lo()).0 as usize,
                )?,
                position(
                    &source,
                    location.file.original_relative_byte_pos(span.hi()).0 as usize,
                )?,
            ))
        };
        let declaration = coordinates(span)?;
        let body_span = tcx.hir_body_owned_by(id).value.span;
        let body = coordinates(if expanded {
            body_span.source_callsite()
        } else {
            body_span
        })?;
        let closure = tcx.def_kind(id) == DefKind::Closure;
        let original = if expanded {
            None
        } else {
            request.original(
                file.to_str()
                    .context("Rust frontend source path must be UTF-8")?,
                body,
                declaration.0,
                closure,
            )?
        };
        let startup = tcx
            .entry_fn(())
            .is_some_and(|entry| entry.0 == id.to_def_id());
        let definition = if let Some(original) = original {
            original
        } else {
            let relative = file.strip_prefix(&request.root)?;
            let source_selected = Selection::new(
                &request.policy.sources.include,
                &request.policy.sources.exclude,
                true,
            )?
            .accepts(
                relative
                    .to_str()
                    .context("Rust frontend source path must be UTF-8")?,
            );
            let prefix = request
                .entry
                .strip_prefix(&request.root)?
                .with_extension("");
            let name = format!(
                "{}.{}",
                prefix.to_string_lossy().replace(['/', '\\'], "."),
                Self::name(tcx, id)
            );
            let selected = source_selected
                && Selection::new(
                    &request.policy.functions.include,
                    &request.policy.functions.exclude,
                    false,
                )?
                .accepts(&name);
            Definition {
                file: file
                    .to_str()
                    .context("Rust frontend path must be UTF-8")?
                    .into(),
                name,
                suffix: tcx.def_path_str(id),
                unsupported: String::new(),
                selected,
                site: CompilerSite {
                    leaf: if closure {
                        "<closure>".into()
                    } else {
                        tcx.item_name(id.to_def_id()).to_string()
                    },
                    header_start: declaration.0.0,
                    header_end: declaration.1.0,
                    body_start_line: body.0.0,
                    body_start_column: body.0.1,
                    body_end_line: body.1.0,
                    body_end_column: body.1.1,
                    declaration_start_line: declaration.0.0,
                    declaration_start_column: declaration.0.1,
                    declaration_end_line: declaration.1.0,
                    declaration_end_column: declaration.1.1,
                    startup,
                    asynchronous: false,
                },
            }
        };
        Self::qualified(tcx, id, definition, expanded)
    }

    fn inventory(&self, tcx: TyCtxt<'_>) -> Result<()> {
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
            let Some((definition, expanded)) = self.definition(tcx, id)? else {
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
