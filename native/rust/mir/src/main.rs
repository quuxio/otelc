#![feature(rustc_private)]
//! Pinned compiler-owned probes; application source and coroutine layout stay original.
extern crate rustc_abi;
extern crate rustc_ast;
extern crate rustc_driver;
extern crate rustc_hir;
extern crate rustc_index;
extern crate rustc_interface;
extern crate rustc_middle;
extern crate rustc_parse;
extern crate rustc_span;

#[path = "../../frontend/src/definitions.rs"]
mod definitions;
mod hooks;
mod instrument;
mod inventory;

use anyhow::{Context, Result, bail};
use definitions::Definitions;
use quux_otelc_rust_adapter::compiler_frontend::Request;
use rustc_driver::{Callbacks, Compilation};
use rustc_hir::{CoroutineDesugaring, CoroutineKind, CoroutineSource, def::DefKind};
use rustc_interface::interface::{Compiler, Config};
use rustc_middle::{mir::Body, ty::TyCtxt};
use rustc_span::{FileName, def_id::LocalDefId};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    path::Path,
    sync::{Mutex, OnceLock},
};

type Provider = for<'tcx> fn(TyCtxt<'tcx>, LocalDefId) -> &'tcx Body<'tcx>;
static ORIGINAL: OnceLock<Provider> = OnceLock::new();
static REQUEST: OnceLock<Request> = OnceLock::new();
static SITES: OnceLock<Mutex<HashMap<u128, String>>> = OnceLock::new();

fn definition_source(tcx: TyCtxt<'_>, id: LocalDefId) -> Option<(LocalDefId, bool)> {
    match tcx.coroutine_kind(id) {
        Some(CoroutineKind::Desugared(CoroutineDesugaring::Async, CoroutineSource::Fn)) => {
            Some((tcx.local_parent(id), true))
        }
        Some(_) => None,
        None if matches!(
            tcx.def_kind(id),
            DefKind::Fn | DefKind::AssocFn | DefKind::Closure
        ) && !tcx.asyncness(id).is_async() =>
        {
            Some((id, false))
        }
        None => None,
    }
}
fn allocator_method(tcx: TyCtxt<'_>, id: LocalDefId) -> Result<bool> {
    let Some(trait_item) = tcx.trait_item_of(id.to_def_id()) else {
        return Ok(false);
    };
    let Some(trait_id) = tcx.trait_of_assoc(trait_item) else {
        return Ok(false);
    };
    let core = tcx
        .lang_items()
        .sized_trait()
        .context("qualified core Sized trait is missing")?
        .krate;
    if trait_id.krate != core {
        return Ok(false);
    }
    // Resolve the standard library's actual exported trait. An application
    // trait or function with the same spelling is not an allocator boundary.
    let child = |module, name: &str| {
        tcx.module_children(module)
            .iter()
            .find(|item| item.ident.name.as_str() == name)
            .and_then(|item| item.res.opt_def_id())
    };
    let alloc = child(trait_id.krate.as_def_id(), "alloc")
        .context("qualified core allocation module is missing")?;
    let global =
        child(alloc, "GlobalAlloc").context("qualified core GlobalAlloc trait is missing")?;
    Ok(trait_id == global)
}
fn site(tcx: TyCtxt<'_>, id: LocalDefId) -> Result<u128> {
    let identity = format!("{:?}", tcx.def_path_hash(id.to_def_id()));
    let hash = Sha256::digest(identity.as_bytes());
    let mut bytes = [0; 16];
    bytes.copy_from_slice(&hash[..16]);
    let value = u128::from_le_bytes(bytes);
    if value == 0 {
        bail!("Rust MIR future identity is invalid")
    }
    let mut sites = SITES
        .get_or_init(|| Mutex::new(HashMap::new()))
        .lock()
        .map_err(|_| anyhow::anyhow!("Rust MIR identities are poisoned"))?;
    if let Some(previous) = sites.get(&value) {
        if previous != &identity {
            bail!("Rust MIR compiler identity collision")
        }
    } else {
        if sites.len() >= 65536 {
            bail!("Rust MIR compiler identities exceed their bound")
        }
        sites.insert(value, identity);
    }
    Ok(value)
}
fn observe<'tcx>(
    tcx: TyCtxt<'tcx>,
    id: LocalDefId,
    original: &Body<'tcx>,
) -> Result<Option<Body<'tcx>>> {
    let Some((owner, asynchronous)) = definition_source(tcx, id) else {
        return Ok(None);
    };
    if !asynchronous && allocator_method(tcx, owner)? {
        // Safety also covers local allocator bodies in excluded files or an
        // external #[path] module: they can call selected application helpers.
        let mut body = original.clone();
        instrument::synchronous(tcx, &mut body, None, false, true)?;
        return Ok(Some(body));
    }
    let request = REQUEST.get().context("Rust MIR request is missing")?;
    let definitions = Definitions {
        request,
        runtime_futures: true,
    };
    let Some((definition, _)) = definitions.definition(tcx, owner)? else {
        return Ok(None);
    };
    if !definition.selected && !definition.site.startup {
        return Ok(None);
    }
    let mut body = original.clone();
    if asynchronous {
        let site = site(tcx, id)?;
        let coroutine = body
            .coroutine
            .as_mut()
            .context("Rust MIR async layout is absent")?;
        if coroutine.coroutine_layout.is_none() || coroutine.coroutine_drop_async.is_some() {
            bail!("Rust MIR async drop or unlowered layout requires separate qualification");
        }
        // The pinned compiler also creates an async proxy for ordinary sync
        // drop. That proxy calls this same drop glue; instrument it once here.
        let dropping = coroutine
            .coroutine_drop
            .as_mut()
            .context("Rust MIR coroutine drop glue is absent")?;
        instrument::future(tcx, dropping, &definition.name, site, true)?;
        instrument::future(tcx, &mut body, &definition.name, site, false)?;
    } else {
        instrument::synchronous(
            tcx,
            &mut body,
            definition.selected.then_some(definition.name.as_str()),
            definition.site.startup,
            false,
        )?;
    }
    Ok(Some(body))
}
fn instrument<'tcx>(tcx: TyCtxt<'tcx>, id: LocalDefId) -> &'tcx Body<'tcx> {
    // Installed exactly once by this process's compiler configuration callback.
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
        config.override_queries = Some(|_, providers| {
            // One rustc invocation lives in this helper process.
            ORIGINAL
                .set(providers.queries.optimized_mir)
                .expect("Rust MIR provider set once");
            providers.queries.optimized_mir = instrument;
        });
    }
    fn after_crate_root_parsing(
        &mut self,
        compiler: &Compiler,
        krate: &mut rustc_ast::Crate,
    ) -> Compilation {
        let parsed = (|| -> Result<_> {
            let request = REQUEST.get().context("Rust MIR request is missing")?;
            let file = compiler
                .sess
                .source_map()
                .lookup_source_file(krate.spans.inner_span.lo());
            let entry =
                std::path::PathBuf::from(file.name.prefer_local_unconditionally().to_string())
                    .canonicalize()
                    .context("Rust MIR original crate root is missing")?;
            if entry != request.entry {
                bail!("Rust MIR compiler input differs from its requested original crate root");
            }
            let mut parser = rustc_parse::new_parser_from_source_str(
                &compiler.sess.psess,
                FileName::Custom("<otelc-compiler-sdk>".into()),
                "extern crate quux_otelc_rust as _;".into(),
                rustc_parse::lexer::StripTokens::Nothing,
            )
            .map_err(|_| anyhow::anyhow!("Rust MIR SDK import could not be parsed"))?;
            parser
                .parse_item(
                    rustc_parse::parser::ForceCollect::No,
                    rustc_parse::parser::AllowConstBlockItems::No,
                )
                .map_err(|_| anyhow::anyhow!("Rust MIR SDK import is invalid"))?
                .context("Rust MIR SDK import is empty")
        })();
        match parsed {
            Ok(item) => {
                krate.items.insert(0, item);
                Compilation::Continue
            }
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
        let definitions = Definitions {
            request: REQUEST.get().expect("Rust MIR request loaded"),
            runtime_futures: true,
        };
        for id in tcx.hir_body_owners() {
            if tcx.coroutine_kind(id).is_some()
                || !matches!(
                    tcx.def_kind(id),
                    DefKind::Fn | DefKind::AssocFn | DefKind::Closure
                )
            {
                continue;
            }
            if let Err(error) = definitions.definition(tcx, id) {
                tcx.dcx().err(format!("otelc Rust compiler: {error:#}"));
            }
        }
        if let Some(output) = std::env::var_os("OTELC_RUST_MIR_INVENTORY")
            && let Err(error) = inventory::publish(tcx, definitions.request, Path::new(&output))
        {
            tcx.dcx().err(format!("otelc Rust compiler: {error:#}"));
            return Compilation::Stop;
        }
        Compilation::Continue
    }
}
fn run() -> Result<()> {
    if std::env::var_os("RUSTC_BOOTSTRAP").is_some() {
        bail!("Rust MIR application compilation requires RUSTC_BOOTSTRAP to be unset");
    }
    let path = std::env::var_os("OTELC_RUST_MIR_REQUEST")
        .context("Rust MIR compiler request is missing")?;
    let request = Request::load_for_backend(Path::new(&path), "compiler")?;
    REQUEST
        .set(request)
        .map_err(|_| anyhow::anyhow!("Rust MIR request already exists"))?;
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
