//! Observe original typed bodies using rustc's stable DefPath-based hashing.
//! These fingerprints are change detectors, not semantic equivalence proofs.
use crate::{request, request::Definition, source_definition};
use anyhow::{Context, Result, bail};
use rustc_data_structures::{
    stable_hash::{StableHash, StableHasher},
    steal::Steal,
};
use rustc_hashes::Hash128;
use rustc_hir::{def::DefKind, def_id::LOCAL_CRATE};
use rustc_middle::{mir::Body, ty::TyCtxt};
use rustc_span::def_id::LocalDefId;
use serde::Serialize;
use std::{
    collections::BTreeMap,
    fs,
    io::Write,
    sync::{Mutex, OnceLock},
};

type Provider = for<'tcx> fn(TyCtxt<'tcx>, LocalDefId) -> &'tcx Steal<Body<'tcx>>;
static ORIGINAL: OnceLock<Provider> = OnceLock::new();
static SNAPSHOTS: OnceLock<Mutex<State>> = OnceLock::new();

#[derive(Default)]
struct State {
    bodies: BTreeMap<String, Fingerprints>,
    errors: Vec<String>,
}

#[derive(Serialize)]
struct Fingerprints {
    phase: String,
    pre_optimization_mir: String,
    typeck: String,
}

#[derive(Serialize)]
struct Binding {
    function: Definition,
    // Numeric indices are diagnostics only and must not enter a comparison.
    diagnostic_def_index: u32,
    eligible: bool,
    reason: Option<&'static str>,
    fingerprints: Option<Fingerprints>,
}

fn unavailable(tcx: TyCtxt<'_>, id: LocalDefId, row: &Definition) -> Option<&'static str> {
    if row.expanded {
        Some("expanded")
    } else if row.asynchronous {
        Some("asynchronous")
    } else if !matches!(tcx.def_kind(id), DefKind::Fn | DefKind::AssocFn) {
        Some("body_kind")
    } else if tcx.is_const_fn(id.to_def_id()) {
        Some("const_function")
    } else {
        None
    }
}

fn fingerprint<T: StableHash + ?Sized>(tcx: TyCtxt<'_>, value: &T) -> String {
    tcx.with_stable_hashing_context(|mut context| {
        let mut hasher = StableHasher::new();
        value.stable_hash(&mut context, &mut hasher);
        let hash: Hash128 = hasher.finish();
        format!("{hash:032x}")
    })
}

pub(crate) fn install(providers: &mut rustc_middle::query::Providers) {
    ORIGINAL
        .set(providers.mir_drops_elaborated_and_const_checked)
        .expect("one original body-binding provider per compiler process");
    providers.mir_drops_elaborated_and_const_checked = snapshot;
}

fn snapshot<'tcx>(tcx: TyCtxt<'tcx>, id: LocalDefId) -> &'tcx Steal<Body<'tcx>> {
    let original = ORIGINAL
        .get()
        .expect("original body-binding provider installed")(tcx, id);
    let result = (|| -> Result<Option<(String, Fingerprints)>> {
        let Some(row) = source_definition(tcx, id)? else {
            return Ok(None);
        };
        if unavailable(tcx, id, &row).is_some() {
            return Ok(None);
        }
        if tcx.sess.opts.unstable_opts.incremental_ignore_spans {
            bail!("original body bindings require stable hashing with original spans");
        }
        let typeck = fingerprint(tcx, tcx.typeck(id));
        let body = original.borrow();
        Ok(Some((
            row.identity,
            Fingerprints {
                phase: format!("{:?}", body.phase),
                pre_optimization_mir: fingerprint(tcx, &*body),
                typeck,
            },
        )))
    })();
    // Stable hashing and compiler queries can recurse. Hold no state lock then.
    let mut state = SNAPSHOTS
        .get_or_init(Default::default)
        .lock()
        .expect("body-binding observer owns its snapshot lock");
    match result {
        Ok(Some((identity, row))) => {
            state.bodies.insert(identity, row);
        }
        Ok(None) => {}
        Err(error) => {
            let message = format!("Rust original body binding: {error:#}");
            tcx.dcx().err(message.clone());
            state.errors.push(message);
        }
    }
    original
}

pub(crate) fn publish(tcx: TyCtxt<'_>) -> Result<()> {
    if tcx.sess.opts.unstable_opts.incremental_ignore_spans {
        bail!("original body bindings require stable hashing with original spans");
    }
    let mut rows = Vec::new();
    for id in tcx.hir_body_owners() {
        let function = source_definition(tcx, id)?
            .context("an original body owner has no caller-bound physical source")?;
        let reason = unavailable(tcx, id, &function);
        if reason.is_none() {
            let _ = tcx.mir_drops_elaborated_and_const_checked(id);
        }
        rows.push(Binding {
            function,
            diagnostic_def_index: id.local_def_index.as_u32(),
            eligible: reason.is_none(),
            reason,
            fingerprints: None,
        });
    }
    rows.sort_by(|left, right| left.function.identity.cmp(&right.function.identity));
    if rows.len() > 65536
        || rows
            .windows(2)
            .any(|pair| pair[0].function.identity == pair[1].function.identity)
    {
        bail!("duplicate or excessive original body identities");
    }
    let mut state = SNAPSHOTS
        .get_or_init(Default::default)
        .lock()
        .expect("body-binding observer owns its snapshot lock");
    if !state.errors.is_empty() {
        bail!("original body snapshot failed: {}", state.errors.join("; "));
    }
    for row in &mut rows {
        if row.eligible {
            row.fingerprints = Some(
                state
                    .bodies
                    .remove(&row.function.identity)
                    .context("original typed body snapshot is missing")?,
            );
        }
    }
    if !state.bodies.is_empty() {
        bail!("body snapshot has no original inventory owner");
    }
    let value = serde_json::json!({"schema_version":1, "kind":"otelc.rust.body-bindings",
        "mode":"binding-inspect", "crate_name":tcx.crate_name(LOCAL_CRATE).to_string(),
        "algorithm":"rustc-1.98.1-stablehash-128-v1", "byte_coordinates":"original-utf8",
        "sources":request().sources, "original_body_count":rows.len(), "bodies":rows,
        "helper_exclusions":[], "source_complete":false, "semantic_complete":false});
    let bytes = serde_json::to_vec(&value)?;
    if bytes.len() > 8 * 1024 * 1024 {
        bail!("Rust body bindings exceed 8 MiB");
    }
    fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&request().output)?
        .write_all(&bytes)?;
    Ok(())
}
