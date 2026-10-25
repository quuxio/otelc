//! Snapshot compiler-owned runtime MIR before ordinary optimisation can erase it.
//! This observer returns the original body unchanged and emits no runtime hooks.
use crate::{definition, request, source_definition};
use anyhow::{Context, Result, bail};
use rustc_data_structures::steal::Steal;
use rustc_hir::def::DefKind;
use rustc_middle::{
    mir::{self, Body, Operand, Place, Rvalue, StatementKind, TerminatorKind},
    ty::{self, TyCtxt},
};
use rustc_span::def_id::LocalDefId;
use serde::Serialize;
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::Write,
    sync::{Mutex, OnceLock},
};

type Provider = for<'tcx> fn(TyCtxt<'tcx>, LocalDefId) -> &'tcx Steal<Body<'tcx>>;
static ORIGINAL: OnceLock<Provider> = OnceLock::new();
static SNAPSHOTS: OnceLock<Mutex<State>> = OnceLock::new();

#[derive(Default)]
struct State {
    bodies: BTreeMap<String, Snapshot>,
    errors: Vec<String>,
}
#[derive(Clone, Serialize)]
struct Value {
    local: usize,
    role: &'static str,
    source: crate::request::Definition,
    compiler_type: String,
    rendered_type: String,
    kind: &'static str,
    needs_drop: bool,
    size: Option<u64>,
    alignment: Option<u64>,
    layout_error: Option<String>,
}
#[derive(Serialize)]
struct Site {
    kind: &'static str,
    block: usize,
    statement: Option<usize>,
    local: usize,
    other: Option<usize>,
    projected: bool,
    cleanup: bool,
    unwind: Option<String>,
}
#[derive(Serialize)]
struct Snapshot {
    function: crate::request::Definition,
    phase: String,
    local_count: usize,
    block_count: usize,
    argument_count: usize,
    locals: Vec<Value>,
    sites: Vec<Site>,
}

pub(crate) fn install(providers: &mut rustc_middle::query::Providers) {
    ORIGINAL
        .set(providers.mir_drops_elaborated_and_const_checked)
        .expect("one Rust lifetime snapshot provider per process");
    providers.mir_drops_elaborated_and_const_checked = snapshot;
}

fn eligible(tcx: TyCtxt<'_>, id: LocalDefId) -> Result<Option<crate::request::Definition>> {
    let Some(row) = definition(tcx, id)? else {
        return Ok(None);
    };
    if !matches!(tcx.def_kind(id), DefKind::Fn | DefKind::AssocFn)
        || row.asynchronous
        || row.expanded
        || tcx.is_const_fn(id.to_def_id())
    {
        return Ok(None);
    }
    Ok(Some(row))
}

fn snapshot<'tcx>(tcx: TyCtxt<'tcx>, id: LocalDefId) -> &'tcx Steal<Body<'tcx>> {
    let original = ORIGINAL.get().expect("Rust lifetime provider installed")(tcx, id);
    let observed = (|| -> Result<Option<Snapshot>> {
        let Some(row) = eligible(tcx, id)? else {
            return Ok(None);
        };
        capture(tcx, id, &original.borrow(), row).map(Some)
    })();
    // Type/layout queries can request other MIR. Never hold this lock while
    // invoking compiler queries or borrowing the next body.
    let mut state = SNAPSHOTS
        .get_or_init(Default::default)
        .lock()
        .expect("Rust lifetime observer owns its snapshot lock");
    match observed {
        Ok(Some(row)) => {
            state.bodies.insert(row.function.identity.clone(), row);
        }
        Ok(None) => {}
        Err(error) => {
            let message = format!("Rust lifetime snapshot: {error:#}");
            tcx.dcx().err(message.clone());
            state.errors.push(message);
        }
    }
    original
}

fn values<'tcx>(tcx: TyCtxt<'tcx>, id: LocalDefId, body: &Body<'tcx>) -> Result<Vec<Value>> {
    let mut rows = Vec::new();
    let environment = ty::TypingEnv::post_analysis(tcx, id);
    for (local, declaration) in body.local_decls.iter_enumerated() {
        let ty::Adt(adt, _) = declaration.ty.kind() else {
            continue;
        };
        let Some(local_type) = adt.did().as_local() else {
            continue;
        };
        let Some(source) = source_definition(tcx, local_type)? else {
            continue;
        };
        let (size, alignment, layout_error) =
            match tcx.layout_of(environment.as_query_input(declaration.ty)) {
                Ok(layout) => (
                    Some(layout.size.bytes()),
                    Some(layout.align.abi.bytes()),
                    None,
                ),
                Err(error) => (None, None, Some(format!("{error:?}"))),
            };
        let index = local.as_usize();
        rows.push(Value {
            local: index,
            role: if index == 0 {
                "return"
            } else if index <= body.arg_count {
                "argument"
            } else {
                "local"
            },
            compiler_type: source.compiler_name.clone(),
            rendered_type: declaration.ty.to_string(),
            source,
            kind: if adt.is_struct() {
                "struct"
            } else if adt.is_enum() {
                "enum"
            } else {
                "union"
            },
            needs_drop: declaration.ty.needs_drop(tcx, environment),
            size,
            alignment,
            layout_error,
        });
    }
    Ok(rows)
}

fn moved<'tcx>(operand: &Operand<'tcx>) -> Option<(&'static str, Place<'tcx>)> {
    match operand {
        Operand::Move(place) => Some(("move", *place)),
        Operand::Copy(place) => Some(("copy", *place)),
        _ => None,
    }
}

fn capture<'tcx>(
    tcx: TyCtxt<'tcx>,
    id: LocalDefId,
    body: &Body<'tcx>,
    function: crate::request::Definition,
) -> Result<Snapshot> {
    if body.local_decls.len() > 65536
        || body.basic_blocks.len() > 65536
        || body.phase != mir::MirPhase::Runtime(mir::RuntimePhase::PostCleanup)
    {
        bail!("unexpected or excessive pre-optimisation runtime MIR");
    }
    let locals = values(tcx, id, body)?;
    let tracked: BTreeSet<_> = locals.iter().map(|row| row.local).collect();
    let mut sites = Vec::new();
    for (block, data) in body.basic_blocks.iter_enumerated() {
        let mut site = |kind, statement, place: Place<'_>, other: Option<Place<'_>>, unwind| {
            if tracked.contains(&place.local.as_usize()) {
                sites.push(Site {
                    kind,
                    block: block.as_usize(),
                    statement,
                    local: place.local.as_usize(),
                    other: other.map(|p| p.local.as_usize()),
                    projected: !place.projection.is_empty()
                        || other.is_some_and(|p| !p.projection.is_empty()),
                    cleanup: data.is_cleanup,
                    unwind,
                });
            }
        };
        for (index, statement) in data.statements.iter().enumerate() {
            if let StatementKind::Assign(assignment) = &statement.kind {
                let (destination, value) = &**assignment;
                if let Rvalue::Use(operand, _) = value
                    && let Some((kind, source)) = moved(operand)
                {
                    site(kind, Some(index), source, Some(*destination), None);
                } else {
                    site(
                        if matches!(value, Rvalue::Aggregate(..)) {
                            "construction"
                        } else {
                            "assignment"
                        },
                        Some(index),
                        *destination,
                        None,
                        None,
                    );
                }
                if let Rvalue::Aggregate(_, operands) = value {
                    for operand in operands {
                        if let Some((_, source)) = moved(operand) {
                            site(
                                "aggregate_input",
                                Some(index),
                                source,
                                Some(*destination),
                                None,
                            );
                        }
                    }
                }
            }
        }
        match &data.terminator().kind {
            TerminatorKind::Drop { place, unwind, .. } => {
                site("drop", None, *place, None, Some(format!("{unwind:?}")))
            }
            TerminatorKind::Call {
                args, destination, ..
            } => {
                site("call_return", None, *destination, None, None);
                for argument in args {
                    if let Some((_, source)) = moved(&argument.node) {
                        site("call_argument", None, source, None, None);
                    }
                }
            }
            _ => {}
        }
    }
    if sites.len() > 262144 {
        bail!("Rust lifetime site inventory exceeds its bound");
    }
    Ok(Snapshot {
        function,
        phase: format!("{:?}", body.phase),
        local_count: body.local_decls.len(),
        block_count: body.basic_blocks.len(),
        argument_count: body.arg_count,
        locals,
        sites,
    })
}

pub(crate) fn publish(tcx: TyCtxt<'_>) -> Result<()> {
    let mut skipped = Vec::new();
    for id in tcx.hir_body_owners() {
        if let Some(row) = definition(tcx, id)? {
            if eligible(tcx, id)?.is_some() {
                let _ = tcx.mir_drops_elaborated_and_const_checked(id);
            } else {
                let reason = if row.asynchronous {
                    "async"
                } else if row.expanded {
                    "expanded"
                } else if tcx.is_const_fn(id.to_def_id()) {
                    "const"
                } else {
                    "closure"
                };
                skipped.push(serde_json::json!({"function":row,"reason":reason}));
            }
        }
    }
    let state = SNAPSHOTS
        .get_or_init(Default::default)
        .lock()
        .map_err(|_| anyhow::anyhow!("Rust lifetime snapshot lock poisoned"))?;
    if !state.errors.is_empty() {
        bail!("Rust lifetime observations failed: {:?}", state.errors);
    }
    let bytes = serde_json::to_vec(&serde_json::json!({"schema_version":1,
        "kind":"otelc.rust.lifetime-inventory","sources":request().sources,
        "target":tcx.sess.target.llvm_target.to_string(),"pointer_width":tcx.sess.target.pointer_width,
        "bodies":state.bodies.values().collect::<Vec<_>>(),"skipped":skipped,
        "source_complete":false,"instrumentation_complete":false}))?;
    if bytes.len() > 8 * 1024 * 1024 {
        bail!("Rust lifetime inventory exceeds 8 MiB");
    }
    fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&request().output)
        .context("create Rust lifetime inventory")?
        .write_all(&bytes)?;
    Ok(())
}
