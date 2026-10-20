//! Add typed calls after coroutine layout and application MIR optimisation.
use crate::hooks::{self, Hooks};
use anyhow::{Context, Result, bail};
use rustc_abi::FieldIdx;
use rustc_middle::{
    mir::*,
    ty::{self, TyCtxt},
};
use rustc_span::{Spanned, def_id::DefId};

fn call<'tcx>(
    tcx: TyCtxt<'tcx>,
    def: DefId,
    args: Vec<Operand<'tcx>>,
    destination: Local,
    target: BasicBlock,
    info: SourceInfo,
    cleanup: bool,
) -> Terminator<'tcx> {
    Terminator {
        source_info: info,
        attributes: Default::default(),
        kind: TerminatorKind::Call {
            func: Operand::function_handle(tcx, def, [], info.span),
            args: args
                .into_iter()
                .map(|node| Spanned {
                    node,
                    span: info.span,
                })
                .collect(),
            destination: destination.into(),
            target: Some(target),
            unwind: if cleanup {
                UnwindAction::Terminate(UnwindTerminateReason::InCleanup)
            } else {
                UnwindAction::Continue
            },
            call_source: CallSource::Misc,
            fn_span: info.span,
        },
    }
}
fn info(body: &Body<'_>) -> SourceInfo {
    SourceInfo {
        span: body.span,
        scope: OUTERMOST_SOURCE_SCOPE,
    }
}
fn move_start<'tcx>(
    blocks: &mut rustc_index::IndexVec<BasicBlock, BasicBlockData<'tcx>>,
) -> BasicBlock {
    let original = std::mem::replace(&mut blocks[START_BLOCK], BasicBlockData::new(None, false));
    let target = blocks.push(original);
    // Optimisation can turn the original entry into a loop target. Its back
    // edges must never rerun admission or overwrite an outstanding guard.
    for (block, data) in blocks.iter_enumerated_mut() {
        if block != START_BLOCK {
            data.terminator_mut().successors_mut(|successor| {
                if *successor == START_BLOCK {
                    *successor = target;
                }
            });
        }
    }
    target
}

pub(crate) fn synchronous<'tcx>(
    tcx: TyCtxt<'tcx>,
    body: &mut Body<'tcx>,
    name: Option<&str>,
    startup: bool,
    allocation: bool,
) -> Result<()> {
    let hooks = Hooks::load(tcx)?;
    let source = info(body);
    let unit = body
        .local_decls
        .push(LocalDecl::with_source_info(tcx.types.unit, source));
    let enter = if allocation {
        Some(hooks.get(tcx, "allocation_enter")?)
    } else {
        name.map(|_| hooks.get(tcx, "sync_enter")).transpose()?
    };
    let guard = enter.map(|enter| {
        let ty = tcx
            .fn_sig(enter)
            .instantiate(tcx, ty::GenericArgs::empty())
            .skip_norm_wip()
            .skip_binder()
            .output();
        body.local_decls
            .push(LocalDecl::with_source_info(ty, source))
    });
    let exit = if allocation {
        Some(hooks.get(tcx, "allocation_exit")?)
    } else {
        name.map(|_| hooks.get(tcx, "sync_exit")).transpose()?
    };
    let unwind_exit = if allocation {
        exit
    } else {
        name.map(|_| hooks.get(tcx, "sync_unwind")).transpose()?
    };
    let start = startup.then(|| hooks.get(tcx, "start")).transpose()?;
    let stop = startup.then(|| hooks.get(tcx, "stop")).transpose()?;
    let blocks = body.basic_blocks.as_mut();
    let count = blocks.len();
    let suffix = |blocks: &mut rustc_index::IndexVec<BasicBlock, BasicBlockData<'tcx>>,
                  mut target: BasicBlock,
                  cleanup: bool| {
        if let Some(stop) = stop {
            target = blocks.push(BasicBlockData::new(
                Some(call(tcx, stop, vec![], unit, target, source, cleanup)),
                cleanup,
            ));
        }
        let completion = if cleanup { unwind_exit } else { exit };
        if let (Some(exit), Some(guard)) = (completion, guard) {
            target = blocks.push(BasicBlockData::new(
                Some(call(
                    tcx,
                    exit,
                    vec![Operand::Move(guard.into())],
                    unit,
                    target,
                    source,
                    cleanup,
                )),
                cleanup,
            ));
        }
        target
    };
    let resume = blocks.push(BasicBlockData::new(
        Some(Terminator {
            source_info: source,
            attributes: Default::default(),
            kind: TerminatorKind::UnwindResume,
        }),
        true,
    ));
    let cleanup = suffix(blocks, resume, true);
    for index in 0..count {
        let block = BasicBlock::from_usize(index);
        let terminal = blocks[block].terminator().clone();
        if matches!(
            terminal.kind,
            TerminatorKind::Return | TerminatorKind::UnwindResume
        ) {
            let is_cleanup = blocks[block].is_cleanup;
            let target = blocks.push(BasicBlockData::new(Some(terminal.clone()), is_cleanup));
            let target = suffix(blocks, target, is_cleanup);
            blocks[block].terminator = Some(Terminator {
                source_info: terminal.source_info,
                attributes: Default::default(),
                kind: TerminatorKind::Goto { target },
            });
        } else if matches!(terminal.kind, TerminatorKind::TailCall { .. }) {
            bail!("Rust MIR tail calls require separate cleanup qualification");
        } else if let Some(unwind) = blocks[block].terminator_mut().unwind_mut()
            && *unwind == UnwindAction::Continue
        {
            *unwind = UnwindAction::Cleanup(cleanup);
        }
    }
    let mut target = move_start(blocks);
    if let (Some(enter), Some(guard)) = (enter, guard) {
        let arguments = if allocation {
            vec![]
        } else {
            vec![hooks::string(
                tcx,
                name.context("Rust MIR function name is missing")?,
                source.span,
            )]
        };
        target = blocks.push(BasicBlockData::new(
            Some(call(tcx, enter, arguments, guard, target, source, false)),
            false,
        ));
    }
    blocks[START_BLOCK].terminator = Some(if let Some(start) = start {
        call(tcx, start, vec![], unit, target, source, false)
    } else {
        Terminator {
            source_info: source,
            attributes: Default::default(),
            kind: TerminatorKind::Goto { target },
        }
    });
    Ok(())
}

pub(crate) fn future<'tcx>(
    tcx: TyCtxt<'tcx>,
    body: &mut Body<'tcx>,
    name: &str,
    site: u128,
    dropping: bool,
) -> Result<()> {
    let hooks = Hooks::load(tcx)?;
    let source = info(body);
    let enter = hooks.get(tcx, if dropping { "drop_enter" } else { "poll_enter" })?;
    let exit = hooks.get(tcx, if dropping { "drop_exit" } else { "poll_exit" })?;
    let unwind_exit = hooks.get(tcx, "poll_unwind")?;
    let guard_ty = tcx
        .fn_sig(enter)
        .instantiate(tcx, ty::GenericArgs::empty())
        .skip_norm_wip()
        .skip_binder()
        .output();
    let guard = body
        .local_decls
        .push(LocalDecl::with_source_info(guard_ty, source));
    let unit = body
        .local_decls
        .push(LocalDecl::with_source_info(tcx.types.unit, source));
    let address = body
        .local_decls
        .push(LocalDecl::with_source_info(tcx.types.usize, source));
    let receiver = Local::from_usize(1);
    let receiver_place = if dropping {
        Place::from(receiver)
    } else {
        let ty::Adt(def, args) = body.local_decls[receiver].ty.kind() else {
            bail!("Rust MIR poll receiver is not Pin")
        };
        if Some(def.did()) != tcx.lang_items().pin_type()
            || args.len() != 1
            || def.non_enum_variant().fields.len() != 1
        {
            bail!("Rust MIR poll receiver has an unqualified Pin representation");
        }
        Place::from(receiver).project_deeper(
            &[ProjectionElem::Field(
                FieldIdx::from_usize(0),
                args.type_at(0),
            )],
            tcx,
        )
    };
    let ty::Ref(_, pointee, rustc_hir::Mutability::Mut) =
        *receiver_place.ty(&body.local_decls, tcx).ty.kind()
    else {
        bail!("Rust MIR coroutine receiver is not a mutable reference");
    };
    if !matches!(pointee.kind(), ty::Coroutine(..)) {
        bail!("Rust MIR receiver does not identify a coroutine")
    }
    let pointer_ty = ty::Ty::new_imm_ptr(tcx, pointee);
    let pointer = body
        .local_decls
        .push(LocalDecl::with_source_info(pointer_ty, source));
    let receiver_value = receiver_place.project_deeper(&[ProjectionElem::Deref], tcx);
    let ready = if dropping {
        None
    } else {
        let output = body.local_decls[RETURN_PLACE].ty;
        let ty::Adt(def, _) = output.kind() else {
            bail!("Rust MIR async result is not Poll")
        };
        if Some(def.did()) != tcx.lang_items().poll() || def.variants().len() != 2 {
            bail!("Rust MIR async result has an unqualified Poll representation")
        }
        let variant = def
            .variants()
            .iter_enumerated()
            .find(|(_, variant)| Some(variant.def_id) == tcx.lang_items().poll_ready_variant())
            .context("Rust MIR Poll::Ready is absent")?
            .0;
        let discriminant = def.discriminant_for_variant(tcx, variant);
        let tag = body
            .local_decls
            .push(LocalDecl::with_source_info(discriminant.ty, source));
        let result = body
            .local_decls
            .push(LocalDecl::with_source_info(tcx.types.bool, source));
        let value = Operand::Constant(Box::new(ConstOperand {
            span: source.span,
            user_ty: None,
            const_: Const::from_bits(
                tcx,
                discriminant.val,
                ty::TypingEnv::fully_monomorphized(),
                discriminant.ty,
            ),
        }));
        Some((tag, result, value))
    };
    let blocks = body.basic_blocks.as_mut();
    let count = blocks.len();
    let resume = blocks.push(BasicBlockData::new(
        Some(Terminator {
            source_info: source,
            attributes: Default::default(),
            kind: TerminatorKind::UnwindResume,
        }),
        true,
    ));
    let cleanup = blocks.push(BasicBlockData::new(
        Some(call(
            tcx,
            unwind_exit,
            vec![Operand::Move(guard.into())],
            unit,
            resume,
            source,
            true,
        )),
        true,
    ));
    for index in 0..count {
        let block = BasicBlock::from_usize(index);
        let terminal = blocks[block].terminator().clone();
        if matches!(
            terminal.kind,
            TerminatorKind::Return | TerminatorKind::UnwindResume
        ) {
            let unwinding = matches!(terminal.kind, TerminatorKind::UnwindResume);
            let is_cleanup = blocks[block].is_cleanup;
            let target = blocks.push(BasicBlockData::new(Some(terminal), is_cleanup));
            let mut args = vec![Operand::Move(guard.into())];
            if !unwinding && let Some((tag, result, value)) = &ready {
                blocks[block].statements.push(Statement::new(
                    source,
                    StatementKind::Assign(Box::new((
                        (*tag).into(),
                        Rvalue::Discriminant(RETURN_PLACE.into()),
                    ))),
                ));
                blocks[block].statements.push(Statement::new(
                    source,
                    StatementKind::Assign(Box::new((
                        (*result).into(),
                        Rvalue::BinaryOp(
                            BinOp::Eq,
                            Box::new((Operand::Copy((*tag).into()), value.clone())),
                        ),
                    ))),
                ));
                args.push(Operand::Copy((*result).into()));
            }
            blocks[block].terminator = Some(call(
                tcx,
                if unwinding { unwind_exit } else { exit },
                args,
                unit,
                target,
                source,
                is_cleanup,
            ));
        } else if matches!(terminal.kind, TerminatorKind::TailCall { .. }) {
            bail!("Rust MIR tail calls require separate cleanup qualification");
        } else if let Some(unwind) = blocks[block].terminator_mut().unwind_mut()
            && *unwind == UnwindAction::Continue
        {
            *unwind = UnwindAction::Cleanup(cleanup);
        }
    }
    let target = move_start(blocks);
    blocks[START_BLOCK].statements.push(Statement::new(
        source,
        StatementKind::Assign(Box::new((
            pointer.into(),
            Rvalue::RawPtr(RawPtrKind::Const, receiver_value),
        ))),
    ));
    blocks[START_BLOCK].statements.push(Statement::new(
        source,
        StatementKind::Assign(Box::new((
            address.into(),
            Rvalue::Cast(
                CastKind::PointerExposeProvenance,
                Operand::Copy(pointer.into()),
                tcx.types.usize,
            ),
        ))),
    ));
    let mut args = vec![
        hooks::site(tcx, site, source.span),
        Operand::Copy(address.into()),
    ];
    if !dropping {
        args.insert(0, hooks::string(tcx, name, source.span));
    }
    blocks[START_BLOCK].terminator = Some(call(tcx, enter, args, guard, target, source, false));
    Ok(())
}
