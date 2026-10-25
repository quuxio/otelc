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
            .filter(|&&id| tcx.crate_name(id).as_str() == "otelc_rust_bridge")
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
        if tcx.def_kind(module) != DefKind::Mod {
            bail!("Rust MIR SDK compiler_mir must be a module");
        }
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
        let signature = tcx
            .fn_sig(def)
            .instantiate(tcx, ty::GenericArgs::empty())
            .skip_norm_wip()
            .skip_binder();
        let valid = signature.abi() == rustc_abi::ExternAbi::Rust
            && !signature.c_variadic()
            && match name {
                "sync_enter" => {
                    signature.inputs().len() == 1
                        && borrowed_str(signature.inputs()[0])
                        && signature.output() == tcx.types.u64
                }
                "sync_exit" | "sync_unwind" | "drop_exit" | "drop_unwind" => {
                    signature.inputs().len() == 1
                        && signature.inputs()[0] == tcx.types.u64
                        && signature.output() == tcx.types.unit
                }
                "drop_new" => {
                    signature.inputs().len() == 2
                        && signature.inputs().iter().all(|&input| borrowed_str(input))
                        && signature.output() == tcx.types.usize
                }
                "drop_enter" => {
                    signature.inputs().len() == 2
                        && signature.inputs()[0] == tcx.types.usize
                        && borrowed_str(signature.inputs()[1])
                        && signature.output() == tcx.types.u64
                }
                _ => false,
            };
        if !valid {
            bail!("Rust MIR SDK hook {name} has an incompatible signature");
        }
        Ok(def)
    }
}

fn borrowed_str(input: ty::Ty<'_>) -> bool {
    matches!(input.kind(), ty::Ref(_, inner, rustc_ast::Mutability::Not) if inner.is_str())
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
