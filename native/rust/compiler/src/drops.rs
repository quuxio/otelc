//! Insert lifetime hooks after compiler drop elaboration, before optimisation.
use crate::{
    definition,
    hooks::{self, Hooks},
    instrument, request, source_definition,
};
use anyhow::{Context, Result, bail};
use rustc_data_structures::steal::Steal;
use rustc_hir::def::DefKind;
use rustc_middle::{
    mir::{visit::Visitor, *},
    ty::{self, TyCtxt},
};
use rustc_span::def_id::{DefId, LocalDefId};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{Mutex, OnceLock},
};

type Provider = for<'tcx> fn(TyCtxt<'tcx>, LocalDefId) -> &'tcx Steal<Body<'tcx>>;
static ORIGINAL: OnceLock<Provider> = OnceLock::new();
static OBSERVED: OnceLock<Mutex<BTreeSet<String>>> = OnceLock::new();

struct Value {
    key: Local,
    usr: String,
    name: String,
}

struct DropHooks {
    enter: DefId,
    exit: DefId,
    unwind: DefId,
    unit: Local,
}

#[derive(Clone, Copy)]
struct Completion {
    guard: Local,
    unit: Local,
    source: SourceInfo,
}

pub(crate) fn install(providers: &mut rustc_middle::query::Providers) {
    ORIGINAL
        .set(providers.mir_drops_elaborated_and_const_checked)
        .expect("one Rust lifetime weaving provider per process");
    providers.mir_drops_elaborated_and_const_checked = weave;
}

fn weave<'tcx>(tcx: TyCtxt<'tcx>, id: LocalDefId) -> &'tcx Steal<Body<'tcx>> {
    let original = ORIGINAL
        .get()
        .expect("Rust lifetime weaving provider installed")(tcx, id);
    let result = (|| -> Result<Option<Body<'tcx>>> {
        let Some(row) = definition(tcx, id)? else {
            return Ok(None);
        };
        let Some(bindings) = request()
            .lifetimes
            .as_ref()
            .and_then(|rows| rows.get(&row.identity))
        else {
            return Ok(None);
        };
        if !matches!(tcx.def_kind(id), DefKind::Fn | DefKind::AssocFn)
            || row.asynchronous
            || row.expanded
            || tcx.is_const_fn(id.to_def_id())
        {
            bail!("Rust automatic lifetimes require an ordinary synchronous non-const body");
        }
        let mut body = original.borrow().clone();
        let expected = request()
            .lifetime_bodies
            .as_ref()
            .and_then(|rows| rows.get(&row.identity))
            .context("Rust lifetime MIR body fingerprint is missing")?;
        if crate::lifetimes::body_digest(tcx, id, &body)? != *expected {
            bail!(
                "Rust lifetime MIR body binding differs; rediscover with matching compiler flags"
            );
        }
        insert(tcx, id, &mut body, bindings)?;
        OBSERVED
            .get_or_init(Default::default)
            .lock()
            .map_err(|_| anyhow::anyhow!("Rust lifetime weaving lock poisoned"))?
            .insert(row.identity);
        Ok(Some(body))
    })();
    match result {
        Ok(Some(body)) => tcx.alloc_steal_mir(body),
        Ok(None) => original,
        Err(error) => {
            tcx.dcx()
                .err(format!("otelc Rust lifetime weaving: {error:#}"));
            original
        }
    }
}

pub(crate) fn force(tcx: TyCtxt<'_>) -> Result<()> {
    let expected: BTreeSet<_> = request()
        .lifetimes
        .as_ref()
        .context("Rust lifetime weaving requires local bindings")?
        .keys()
        .cloned()
        .collect();
    for id in tcx.hir_body_owners() {
        if let Some(row) = definition(tcx, id)?
            && expected.contains(&row.identity)
        {
            let _ = tcx.mir_drops_elaborated_and_const_checked(id);
        }
    }
    let observed = OBSERVED
        .get_or_init(Default::default)
        .lock()
        .map_err(|_| anyhow::anyhow!("Rust lifetime weaving lock poisoned"))?;
    if *observed != expected {
        bail!("Rust automatic lifetime body binding is missing or refused")
    }
    Ok(())
}

// Borrowing and ordinary field reads preserve ownership. Every other ownership
// operand must be a supported whole-local move; never silently lose an escape.
struct Ownership<'a> {
    tracked: &'a BTreeSet<Local>,
    refused: bool,
}
impl<'tcx> Visitor<'tcx> for Ownership<'_> {
    fn visit_operand(&mut self, operand: &Operand<'tcx>, _: Location) {
        self.refused |= match operand {
            Operand::Move(place) => self.tracked.contains(&place.local),
            Operand::Copy(place) => {
                place.projection.is_empty() && self.tracked.contains(&place.local)
            }
            _ => false,
        };
    }
    fn visit_assign(
        &mut self,
        destination: &Place<'tcx>,
        value: &Rvalue<'tcx>,
        location: Location,
    ) {
        if destination.projection.is_empty() && self.tracked.contains(&destination.local) {
            if let Rvalue::Use(Operand::Move(source), _) = value
                && source.projection.is_empty()
                && self.tracked.contains(&source.local)
                && source.local != destination.local
            {
                return;
            }
            if !matches!(
                value,
                Rvalue::Aggregate(..) | Rvalue::Use(Operand::Constant(_), _)
            ) {
                self.refused = true;
            }
        }
        self.super_assign(destination, value, location);
    }
    fn visit_terminator(&mut self, terminal: &Terminator<'tcx>, location: Location) {
        self.refused |= match &terminal.kind {
            TerminatorKind::Call { destination, .. } => {
                destination.projection.is_empty() && self.tracked.contains(&destination.local)
            }
            TerminatorKind::Drop { place, drop, .. } => {
                self.tracked.contains(&place.local)
                    && (!place.projection.is_empty() || drop.is_some())
            }
            TerminatorKind::TailCall { .. } => true,
            _ => false,
        };
        self.super_terminator(terminal, location);
    }
}

fn values<'tcx>(
    tcx: TyCtxt<'tcx>,
    id: LocalDefId,
    body: &mut Body<'tcx>,
    bindings: &BTreeMap<u32, String>,
) -> Result<BTreeMap<Local, Value>> {
    if body.phase != MirPhase::Runtime(RuntimePhase::PostCleanup)
        || body.local_decls.len() > 65536
        || body.basic_blocks.len() > 65536
    {
        bail!("unexpected or excessive Rust lifetime runtime MIR");
    }
    let environment = ty::TypingEnv::post_analysis(tcx, id);
    let mut rows = BTreeMap::new();
    for (&index, expected) in bindings {
        let local = Local::from_usize(index as usize);
        let declaration = body
            .local_decls
            .get(local)
            .context("Rust lifetime local is missing")?;
        if index as usize <= body.arg_count {
            bail!("Rust lifetime arguments/returns require the ownership call profile");
        }
        let ty::Adt(adt, _) = declaration.ty.kind() else {
            bail!(
                "Rust lifetime local {index} is not an ADT: {}",
                declaration.ty
            )
        };
        let source = source_definition(
            tcx,
            adt.did()
                .as_local()
                .context("Rust lifetime type must belong to the original application")?,
        )?
        .context("Rust lifetime type declaration is outside the bound sources")?;
        if source.compiler_name != *expected
            || !declaration.ty.needs_drop(tcx, environment)
            || tcx
                .layout_of(environment.as_query_input(declaration.ty))
                .is_err()
        {
            bail!("Rust lifetime type/layout/drop binding differs");
        }
        let name = declaration.ty.to_string();
        let usr = format!("rust:{}|{name}", source.compiler_name);
        if name.len() > 4096 || usr.len() > 4096 || name.contains('\0') || usr.contains('\0') {
            bail!("Rust lifetime type identity exceeds the native metadata bound");
        }
        let info = declaration.source_info;
        let key = body
            .local_decls
            .push(LocalDecl::with_source_info(tcx.types.usize, info));
        rows.insert(
            local,
            Value {
                key,
                usr: usr + "\0",
                name: name + "\0",
            },
        );
    }
    let tracked = rows.keys().copied().collect();
    let mut ownership = Ownership {
        tracked: &tracked,
        refused: false,
    };
    ownership.visit_body(body);
    if ownership.refused {
        bail!("Rust lifetime ownership escape/copy/projected drop requires a separate profile")
    }
    Ok(rows)
}

fn probe<'tcx>(
    tcx: TyCtxt<'tcx>,
    hook: DefId,
    args: Vec<Operand<'tcx>>,
    destination: Local,
    target: BasicBlock,
    info: SourceInfo,
    cleanup: bool,
) -> Terminator<'tcx> {
    let mut result = instrument::call(tcx, hook, args, destination, target, info, cleanup);
    // These private hooks only validate bounded bytes, use non-panicking atomic
    // operations and call noexcept SDK boundaries. They never catch app panics.
    if let TerminatorKind::Call { unwind, .. } = &mut result.kind {
        *unwind = UnwindAction::Unreachable;
    }
    result
}

fn finish_drop<'tcx>(
    tcx: TyCtxt<'tcx>,
    blocks: &mut rustc_index::IndexVec<BasicBlock, BasicBlockData<'tcx>>,
    hook: DefId,
    completion: Completion,
    target: BasicBlock,
    cleanup: bool,
) -> BasicBlock {
    blocks.push(BasicBlockData::new(
        Some(probe(
            tcx,
            hook,
            vec![Operand::Copy(completion.guard.into())],
            completion.unit,
            target,
            completion.source,
            cleanup,
        )),
        cleanup,
    ))
}

fn insert<'tcx>(
    tcx: TyCtxt<'tcx>,
    id: LocalDefId,
    body: &mut Body<'tcx>,
    bindings: &BTreeMap<u32, String>,
) -> Result<()> {
    let rows = values(tcx, id, body, bindings)?;
    let hooks = Hooks::load(tcx)?;
    let new = hooks.get(tcx, "drop_new")?;
    let enter = hooks.get(tcx, "drop_enter")?;
    let exit = hooks.get(tcx, "drop_exit")?;
    let unwind = hooks.get(tcx, "drop_unwind")?;
    let source = SourceInfo {
        span: body.span,
        scope: OUTERMOST_SOURCE_SCOPE,
    };
    let unit = body
        .local_decls
        .push(LocalDecl::with_source_info(tcx.types.unit, source));
    wrap_drops(
        tcx,
        body,
        &rows,
        DropHooks {
            enter,
            exit,
            unwind,
            unit,
        },
    );
    constructors(tcx, body, &rows, new);
    Ok(())
}

fn wrap_drops<'tcx>(
    tcx: TyCtxt<'tcx>,
    body: &mut Body<'tcx>,
    rows: &BTreeMap<Local, Value>,
    hooks: DropHooks,
) {
    let count = body.basic_blocks.len();
    // Drop elaboration retains compiler drop flags. A normal destructor invoked
    // while cleaning up an outer panic still completes with exception_exit=false.
    for index in 0..count {
        let block = BasicBlock::from_usize(index);
        let terminal = body.basic_blocks[block].terminator().clone();
        let TerminatorKind::Drop {
            place,
            target,
            unwind: action,
            ..
        } = terminal.kind
        else {
            continue;
        };
        let Some(value) = rows.get(&place.local) else {
            continue;
        };
        let info = terminal.source_info;
        let cleanup = body.basic_blocks[block].is_cleanup;
        let guard = body
            .local_decls
            .push(LocalDecl::with_source_info(tcx.types.u64, info));
        let blocks = body.basic_blocks.as_mut();
        let completion = Completion {
            guard,
            unit: hooks.unit,
            source: info,
        };
        let normal = finish_drop(tcx, blocks, hooks.exit, completion, target, cleanup);
        let action = match action {
            UnwindAction::Continue => {
                let resume = blocks.push(BasicBlockData::new(
                    Some(Terminator {
                        source_info: info,
                        attributes: Default::default(),
                        kind: TerminatorKind::UnwindResume,
                    }),
                    true,
                ));
                UnwindAction::Cleanup(finish_drop(
                    tcx,
                    blocks,
                    hooks.unwind,
                    completion,
                    resume,
                    true,
                ))
            }
            UnwindAction::Cleanup(target) => UnwindAction::Cleanup(finish_drop(
                tcx,
                blocks,
                hooks.unwind,
                completion,
                target,
                true,
            )),
            other => other,
        };
        let mut original = terminal;
        if let TerminatorKind::Drop { target, unwind, .. } = &mut original.kind {
            *target = normal;
            *unwind = action;
        }
        let target = blocks.push(BasicBlockData::new(Some(original), cleanup));
        blocks[block].terminator = Some(probe(
            tcx,
            hooks.enter,
            vec![
                Operand::Copy(value.key.into()),
                hooks::string(tcx, &value.usr, info.span),
            ],
            guard,
            target,
            info,
            cleanup,
        ));
    }
}

fn constructors<'tcx>(
    tcx: TyCtxt<'tcx>,
    body: &mut Body<'tcx>,
    rows: &BTreeMap<Local, Value>,
    new: DefId,
) {
    let blocks = body.basic_blocks.as_mut();
    let count = blocks.len();
    // Split only at original successful assignments; the value operation always
    // precedes its observation and every original successor remains reachable.
    for index in 0..count {
        let block = BasicBlock::from_usize(index);
        let statements = std::mem::take(&mut blocks[block].statements);
        let terminal = blocks[block].terminator.take();
        let cleanup = blocks[block].is_cleanup;
        let mut current = block;
        for statement in statements {
            let info = statement.source_info;
            let action = if let StatementKind::Assign(assignment) = &statement.kind {
                let (destination, rvalue) = &**assignment;
                if destination.projection.is_empty() {
                    rows.get(&destination.local).map(|value| {
                        (
                            value,
                            match rvalue {
                                Rvalue::Use(Operand::Move(place), _) => rows.get(&place.local),
                                _ => None,
                            },
                        )
                    })
                } else {
                    None
                }
            } else {
                None
            };
            blocks[current].statements.push(statement);
            if let Some((value, Some(previous))) = action {
                blocks[current].statements.push(Statement::new(
                    info,
                    StatementKind::Assign(Box::new((
                        value.key.into(),
                        Rvalue::Use(Operand::Copy(previous.key.into()), WithRetag::No),
                    ))),
                ));
            } else if let Some((value, None)) = action {
                let target = blocks.push(BasicBlockData::new(None, cleanup));
                blocks[current].terminator = Some(probe(
                    tcx,
                    new,
                    vec![
                        hooks::string(tcx, &value.usr, info.span),
                        hooks::string(tcx, &value.name, info.span),
                    ],
                    value.key,
                    target,
                    info,
                    cleanup,
                ));
                current = target;
            }
        }
        blocks[current].terminator = terminal;
    }
}
