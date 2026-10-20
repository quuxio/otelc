//! Resolve typed SDK hooks and construct constants without parsing source text.
use anyhow::{Context, Result, bail};
use rustc_hir::def::DefKind;
use rustc_middle::{
    mir::{Const, ConstOperand, ConstValue, Operand},
    ty::{self, TyCtxt},
};
use rustc_span::{Span, def_id::DefId};

pub(crate) struct Hooks {
    module: DefId,
}
impl Hooks {
    pub(crate) fn load(tcx: TyCtxt<'_>) -> Result<Self> {
        let crates: Vec<_> = tcx
            .crates(())
            .iter()
            .filter(|&&id| tcx.crate_name(id).as_str() == "quux_otelc_rust")
            .collect();
        if crates.len() != 1 {
            bail!("Rust MIR requires exactly one matching SDK instance");
        }
        let krate = crates[0];
        let module = tcx
            .module_children(krate.as_def_id())
            .iter()
            .find(|item| item.ident.name.as_str() == "compiler_mir")
            .context("Rust MIR SDK hooks are missing; rebuild the matching SDK")?
            .res
            .def_id();
        Ok(Self { module })
    }
    pub(crate) fn get(&self, tcx: TyCtxt<'_>, name: &str) -> Result<DefId> {
        let def = tcx
            .module_children(self.module)
            .iter()
            .find(|item| item.ident.name.as_str() == name)
            .with_context(|| format!("Rust MIR SDK hook {name} is missing"))?
            .res
            .def_id();
        if tcx.def_kind(def) != DefKind::Fn || !tcx.generics_of(def).own_params.is_empty() {
            bail!("Rust MIR SDK hook {name} has an incompatible definition");
        }
        Ok(def)
    }
}

pub(crate) fn string<'tcx>(tcx: TyCtxt<'tcx>, value: &str, span: Span) -> Operand<'tcx> {
    let ty = ty::Ty::new_imm_ref(tcx, tcx.lifetimes.re_static, tcx.types.str_);
    let alloc_id = tcx.allocate_bytes_dedup(value.as_bytes(), 0);
    Operand::Constant(Box::new(ConstOperand {
        span,
        user_ty: None,
        const_: Const::Val(
            ConstValue::Slice {
                alloc_id,
                meta: value.len() as u64,
            },
            ty,
        ),
    }))
}
pub(crate) fn site<'tcx>(tcx: TyCtxt<'tcx>, value: u128, span: Span) -> Operand<'tcx> {
    Operand::Constant(Box::new(ConstOperand {
        span,
        user_ty: None,
        const_: Const::from_bits(
            tcx,
            value,
            ty::TypingEnv::fully_monomorphized(),
            tcx.types.u128,
        ),
    }))
}
