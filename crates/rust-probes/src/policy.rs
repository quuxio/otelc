use anyhow::{bail, Result};
use quux_otelc_config::{
    common::{Annotations, SharedRuntime, TraceExport},
    Export, Functions, Metrics, Resource, Traces,
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
    #[serde(default)]
    pub traces: Traces,
    #[serde(default)]
    pub trace_export: Option<TraceExport>,
    pub export: Export,
    pub resource: Resource,
    pub metrics_endpoint: String,
}
impl Plan {
    pub fn load(path: &Path) -> Result<Self> {
        let plan: Self = serde_json::from_slice(&std::fs::read(path)?)?;
        plan.validate()?;
        Ok(plan)
    }
    pub fn validate(&self) -> Result<()> {
        let plan = self;
        if plan.language != "rust" || !plan.execution_available {
            bail!("Rust adapter requires an executable Rust policy");
        }
        if plan.runtime.max_functions == 0
            || plan.runtime.max_active_calls == 0
            || plan.runtime.shutdown_timeout_ms == 0
        {
            bail!("invalid resolved Rust runtime limits");
        }
        if plan.traces.enabled {
            let export = plan
                .trace_export
                .as_ref()
                .ok_or_else(|| anyhow::anyhow!("missing resolved Rust trace export"))?;
            quux_otelc_config::validate_endpoint(&export.endpoint)?;
            if export.protocol != "http/protobuf"
                || !(1..=60000).contains(&export.timeout_ms)
                || !plan.traces.root_sample_ratio.is_finite()
                || !(0.0..=1.0).contains(&plan.traces.root_sample_ratio)
                || !(1..=65536).contains(&plan.traces.max_active_traces)
                || !(1..=65536).contains(&plan.traces.max_spans_per_trace)
                || plan
                    .traces
                    .max_active_traces
                    .checked_mul(plan.traces.max_spans_per_trace)
                    .is_none_or(|slots| slots > 1_048_576)
                || !(1..=64).contains(&plan.export.max_queued_batches)
            {
                bail!("invalid resolved Rust trace settings");
            }
        }
        Ok(())
    }
}
