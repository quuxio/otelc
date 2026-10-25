#![feature(rustc_private)]
//! Compiler-owned component: inspect original definitions, then weave typed MIR.
extern crate rustc_abi;
extern crate rustc_ast;
extern crate rustc_data_structures;
extern crate rustc_driver;
extern crate rustc_hir;
extern crate rustc_index;
extern crate rustc_interface;
extern crate rustc_middle;
extern crate rustc_parse;
extern crate rustc_span;

mod drops;
mod hooks;
mod instrument;
mod lifetimes;
mod request;

use anyhow::{Context, Result, bail};
use request::{Definition, Request, digest};
use rustc_driver::{Callbacks, Compilation};
use rustc_hir::def::DefKind;
use rustc_interface::interface::{Compiler, Config};
use rustc_middle::{mir::Body, ty::TyCtxt};
use rustc_span::{FileName, def_id::LocalDefId};
use std::{
    collections::BTreeSet,
    fs,
    io::Write,
    path::{Path, PathBuf},
    sync::OnceLock,
};

type Provider = for<'tcx> fn(TyCtxt<'tcx>, LocalDefId) -> &'tcx Body<'tcx>;
static ORIGINAL: OnceLock<Provider> = OnceLock::new();
static REQUEST: OnceLock<Request> = OnceLock::new();

fn request() -> &'static Request {
    // One checked request is installed before this process invokes rustc.
    REQUEST
        .get()
        .expect("Rust compiler request installed before callbacks")
}

fn definition(tcx: TyCtxt<'_>, id: LocalDefId) -> Result<Option<Definition>> {
    if !matches!(
        tcx.def_kind(id),
        DefKind::Fn | DefKind::AssocFn | DefKind::Closure
    ) {
        return Ok(None);
    }
    source_definition(tcx, id)
}

fn source_definition(tcx: TyCtxt<'_>, id: LocalDefId) -> Result<Option<Definition>> {
    let span = tcx.def_span(id);
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
    if !file.starts_with(&request().root) {
        return Ok(None);
    }
    let expected = request()
        .sources
        .get(&file)
        .context("compiler read an unbound Rust source")?;
    let text = fs::read_to_string(&file)?;
    if digest(text.as_bytes()) != *expected || !location.file.src_hash.matches(&text) {
        bail!("original Rust source differs from the compiler's loaded bytes");
    }
    if map.lookup_char_pos(span.hi()).file.start_pos != location.file.start_pos {
        bail!("Rust definition spans different source inputs");
    }
    let relative = file
        .strip_prefix(&request().root)?
        .to_str()
        .context("Rust source identity requires UTF-8")?;
    let compiler_name = tcx.def_path_str(id);
    Ok(Some(Definition {
        identity: format!("{relative}::{compiler_name}"),
        file,
        compiler_name,
        start: location.file.original_relative_byte_pos(span.lo()).0,
        end: location.file.original_relative_byte_pos(span.hi()).0,
        asynchronous: matches!(
            tcx.def_kind(id),
            DefKind::Fn | DefKind::AssocFn | DefKind::Closure
        ) && (tcx.asyncness(id).is_async() || tcx.coroutine_kind(id).is_some()),
        expanded: span.from_expansion(),
    }))
}

fn observe<'tcx>(
    tcx: TyCtxt<'tcx>,
    id: LocalDefId,
    original: &Body<'tcx>,
) -> Result<Option<Body<'tcx>>> {
    let Some(row) = definition(tcx, id)? else {
        return Ok(None);
    };
    if !request().selected.contains(&row.identity) {
        return Ok(None);
    }
    if row.asynchronous || row.expanded {
        bail!(
            "Rust async/macro completion requires its separate compiler profile: {}",
            row.identity
        );
    }
    let mut body = original.clone();
    instrument::synchronous(tcx, &mut body, &row.identity)?;
    Ok(Some(body))
}

fn instrument<'tcx>(tcx: TyCtxt<'tcx>, id: LocalDefId) -> &'tcx Body<'tcx> {
    // Installed once by this invocation's query configuration callback.
    let original = ORIGINAL.get().expect("Rust MIR provider installed")(tcx, id);
    match observe(tcx, id, original) {
        Ok(Some(body)) => tcx.arena.alloc(body),
        Ok(None) => original,
        Err(error) => {
            tcx.dcx().err(format!("otelc Rust compiler: {error:#}"));
            original
        }
    }
}

struct Probe;
impl Callbacks for Probe {
    fn config(&mut self, config: &mut Config) {
        if request().mode == "lifetime-inspect" {
            config.override_queries = Some(|_, providers| {
                lifetimes::install(&mut providers.queries);
            });
        }
        if request().weaving() {
            config.override_queries = Some(|_, providers| {
                if request().mode == "lifetime-weave" {
                    drops::install(&mut providers.queries);
                }
                ORIGINAL
                    .set(providers.queries.optimized_mir)
                    .expect("one Rust MIR provider per process");
                providers.queries.optimized_mir = instrument;
            });
        }
    }
    fn after_crate_root_parsing(
        &mut self,
        compiler: &Compiler,
        krate: &mut rustc_ast::Crate,
    ) -> Compilation {
        let parsed = (|| -> Result<_> {
            let file = compiler
                .sess
                .source_map()
                .lookup_source_file(krate.spans.inner_span.lo());
            let entry = PathBuf::from(file.name.prefer_local_unconditionally().to_string())
                .canonicalize()?;
            if entry != request().entry {
                bail!("compiler crate root differs from the original entry");
            }
            if !request().weaving() {
                return Ok(None);
            }
            let mut parser = rustc_parse::new_parser_from_source_str(
                &compiler.sess.psess,
                FileName::Custom("<otelc-compiler-bridge>".into()),
                "extern crate otelc_rust_bridge as _;".into(),
                rustc_parse::lexer::StripTokens::Nothing,
            )
            .map_err(|_| anyhow::anyhow!("cannot parse private Rust bridge import"))?;
            parser
                .parse_item(
                    rustc_parse::parser::ForceCollect::No,
                    rustc_parse::parser::AllowConstBlockItems::No,
                )
                .map_err(|_| anyhow::anyhow!("invalid private Rust bridge import"))?
                .context("private Rust bridge import is empty")
                .map(Some)
        })();
        match parsed {
            Ok(Some(item)) => {
                krate.items.insert(0, item);
                Compilation::Continue
            }
            Ok(None) => Compilation::Continue,
            Err(error) => {
                compiler
                    .sess
                    .dcx()
                    .err(format!("otelc Rust compiler: {error:#}"));
                Compilation::Stop
            }
        }
    }
    fn after_analysis<'tcx>(&mut self, _: &Compiler, tcx: TyCtxt<'tcx>) -> Compilation {
        let result = (|| -> Result<()> {
            if request().mode == "lifetime-inspect" {
                return lifetimes::publish(tcx);
            }
            let mut rows = Vec::new();
            for id in tcx.hir_body_owners() {
                if let Some(row) = definition(tcx, id)? {
                    rows.push(row);
                }
            }
            rows.sort_by(|left, right| left.identity.cmp(&right.identity));
            let identities: BTreeSet<_> = rows.iter().map(|row| row.identity.clone()).collect();
            if rows.len() > 65536
                || rows.len() != identities.len()
                || !request().selected.is_subset(&identities)
            {
                bail!("missing, duplicate or excessive Rust compiler identities");
            }
            if rows.iter().any(|row| {
                request().selected.contains(&row.identity) && (row.asynchronous || row.expanded)
            }) {
                bail!("selected Rust async/macro body requires a separate completion profile");
            }
            if rows
                .iter()
                .any(|row| request().selected.contains(&row.identity) && row.identity.len() > 4096)
            {
                bail!("selected Rust identity exceeds the native name bound");
            }
            if request().mode == "lifetime-weave" {
                drops::force(tcx)?;
            }
            let mut inventory = serde_json::json!({"schema_version":1,
                "kind":"otelc.rust.compiler-inventory","mode":request().mode,"definitions":rows,
                "sources":request().sources,"source_complete":false});
            if request().mode == "lifetime-weave" {
                inventory["schema_version"] = serde_json::json!(2);
                inventory["kind"] = serde_json::json!("otelc.rust.lifetime-weave");
                inventory["lifetimes"] = serde_json::to_value(&request().lifetimes)?;
                inventory["lifetime_bodies"] = serde_json::to_value(&request().lifetime_bodies)?;
                inventory["instrumentation_complete"] = serde_json::json!(false);
            }
            let bytes = serde_json::to_vec(&inventory)?;
            if bytes.len() > 8 * 1024 * 1024 {
                bail!("Rust inventory exceeds 8 MiB");
            }
            fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&request().output)?
                .write_all(&bytes)?;
            Ok(())
        })();
        match result {
            Err(error) => {
                tcx.dcx().err(format!("otelc Rust compiler: {error:#}"));
                Compilation::Stop
            }
            Ok(()) if !request().weaving() => Compilation::Stop,
            Ok(()) => Compilation::Continue,
        }
    }
}

fn run() -> Result<()> {
    let filename =
        std::env::var_os("OTELC_RUST_REQUEST").context("Rust compiler request is missing")?;
    REQUEST
        .set(Request::load(Path::new(&filename))?)
        .map_err(|_| anyhow::anyhow!("Rust request already installed"))?;
    rustc_driver::run_compiler(&std::env::args().collect::<Vec<_>>(), &mut Probe);
    Ok(())
}
fn main() -> std::process::ExitCode {
    rustc_driver::catch_with_exit_code(|| {
        if let Err(error) = run() {
            eprintln!("otelc Rust compiler: {error:#}");
            std::process::exit(1);
        }
    })
}
