//! Add typed calls after coroutine layout and application MIR optimisation.
use crate::hooks::{self, Hooks};
use anyhow::{Result, bail};
use rustc_middle::{
    mir::*,
    ty::{self, TyCtxt},
};
use rustc_span::{Spanned, def_id::DefId};

pub(crate) fn call<'tcx>(
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
    name: &str,
) -> Result<()> {
    let hooks = Hooks::load(tcx)?;
    let source = info(body);
    let unit = body
        .local_decls
        .push(LocalDecl::with_source_info(tcx.types.unit, source));
    let enter = hooks.get(tcx, "sync_enter")?;
    let exit = hooks.get(tcx, "sync_exit")?;
    let unwind_exit = hooks.get(tcx, "sync_unwind")?;
    let ty = tcx
        .fn_sig(enter)
        .instantiate(tcx, ty::GenericArgs::empty())
        .skip_norm_wip()
        .skip_binder()
        .output();
    let guard = body
        .local_decls
        .push(LocalDecl::with_source_info(ty, source));
    let blocks = body.basic_blocks.as_mut();
    let count = blocks.len();
    let suffix = |blocks: &mut rustc_index::IndexVec<BasicBlock, BasicBlockData<'tcx>>,
                  target: BasicBlock,
                  cleanup: bool| {
        blocks.push(BasicBlockData::new(
            Some(call(
                tcx,
                if cleanup { unwind_exit } else { exit },
                vec![Operand::Move(guard.into())],
                unit,
                target,
                source,
                cleanup,
            )),
            cleanup,
        ))
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
    let target = move_start(blocks);
    blocks[START_BLOCK].terminator = Some(call(
        tcx,
        enter,
        vec![hooks::string(tcx, name, source.span)],
        guard,
        target,
        source,
        false,
    ));
    Ok(())
}
