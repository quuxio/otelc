use anyhow::{bail, Result};
use quux_otelc_config::{
    common::{Annotations, SharedRuntime},
    Export, Functions, Metrics, Resource,
};
use serde::Deserialize;
use std::path::Path;

#[derive(Clone, Debug, Deserialize)]
pub struct Plan {
    pub language: String,
    pub execution_available: bool,
    pub sources: Functions,
    pub functions: Functions,
    pub annotations: Annotations,
    pub runtime: SharedRuntime,
    pub metrics: Metrics,
    pub export: Export,
    pub resource: Resource,
    pub metrics_endpoint: String,
}
impl Plan {
    pub fn load(path: &Path) -> Result<Self> {
        let plan: Self = serde_json::from_slice(&std::fs::read(path)?)?;
        if plan.language != "rust" || !plan.execution_available {
            bail!("Rust adapter requires an executable Rust policy");
        }
        if plan.runtime.max_functions == 0
            || plan.runtime.max_active_calls == 0
            || plan.runtime.shutdown_timeout_ms == 0
        {
            bail!("invalid resolved Rust runtime limits");
        }
        Ok(plan)
    }
}
