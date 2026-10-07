//! Shared, strict configuration and selection rules.
#[cfg(unix)]
pub mod control;
use anyhow::{bail, Context, Result};
use globset::{GlobBuilder, GlobSet, GlobSetBuilder};
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, path::Path};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub schema_version: u32,
    #[serde(skip)]
    pub metrics_endpoint_is_full: bool,
    #[serde(skip)]
    pub read_annotations: bool,
    #[serde(default)]
    pub build: Build,
    #[serde(default)]
    pub functions: Functions,
    #[serde(default)]
    pub objects: Objects,
    #[serde(default)]
    pub runtime: Runtime,
    #[serde(default)]
    pub metrics: Metrics,
    #[serde(default)]
    pub traces: Traces,
    #[serde(default)]
    pub export: Export,
    #[serde(default)]
    pub resource: Resource,
}
macro_rules! config_section {
    ($name:ident { $($field:ident : $type:ty = $value:expr),* $(,)? }) => {
        #[derive(Clone, Debug, Deserialize, Serialize)]
        #[serde(default, deny_unknown_fields)]
        pub struct $name { $(pub $field: $type),* }
        impl Default for $name { fn default() -> Self { Self { $($field: $value),* } } }
    }
}
pub mod common;
config_section!(Build { backend: String = "callbacks".into(), include: Vec<String> = vec![], exclude: Vec<String> = vec![] });
config_section!(Functions { include: Vec<String> = vec![], exclude: Vec<String> = vec![] });
config_section!(Objects { classes: Vec<String> = vec![], max_live: usize = 4096 });
config_section!(Runtime {
    max_functions: usize = 4096,
    max_active_calls: usize = 4096,
    max_threads: usize = 128,
    stack_depth: usize = 256,
    queue_capacity: usize = 4096,
    shutdown_timeout_ms: u64 = 2000,
    control_socket: Option<String> = None
});
config_section!(Metrics { enabled: bool = true, histogram_boundaries_seconds: Vec<f64> = vec![0.000001,0.00001,0.0001,0.001,0.01,0.1,1.0] });
config_section!(Traces {
    enabled: bool = false,
    root_sample_ratio: f64 = 0.01,
    max_active_traces: usize = 512,
    max_spans_per_trace: usize = 1024
});
config_section!(Export {
    protocol: String = "http/protobuf".into(),
    endpoint: String = "http://localhost:4318".into(),
    interval_ms: u64 = 5000,
    timeout_ms: u64 = 1000,
    max_queued_batches: usize = 8
});
config_section!(Resource {
    service_name: String = "unknown_service".into(),
    service_version: String = "".into(),
    attributes: BTreeMap<String,String> = BTreeMap::new()
});

#[derive(Clone)]
pub struct Selection {
    include: GlobSet,
    exclude: GlobSet,
}
impl Selection {
    pub fn regexes(values: &[String], paths: bool) -> Result<Vec<String>> {
        values
            .iter()
            .map(|value| {
                let pattern = if paths {
                    value.clone()
                } else {
                    value
                        .chars()
                        .map(|c| match c {
                            '[' | '{' | '}' | ']' | '\\' => format!("\\{c}"),
                            _ => c.to_string(),
                        })
                        .collect()
                };
                Ok(GlobBuilder::new(&pattern)
                    .literal_separator(paths)
                    .backslash_escape(true)
                    .build()?
                    .regex()
                    .to_owned())
            })
            .collect()
    }
    pub fn new(include: &[String], exclude: &[String], paths: bool) -> Result<Self> {
        fn compile(values: &[String], paths: bool) -> Result<GlobSet> {
            let mut builder = GlobSetBuilder::new();
            for pattern in values {
                // Function patterns permit only * and ?, with all other characters literal.
                let pattern = if paths {
                    pattern.clone()
                } else {
                    pattern
                        .chars()
                        .map(|c| match c {
                            '[' | '{' | '}' | ']' | '\\' => format!("\\{c}"),
                            _ => c.to_string(),
                        })
                        .collect()
                };
                builder.add(
                    GlobBuilder::new(&pattern)
                        .literal_separator(paths)
                        .backslash_escape(true)
                        .build()?,
                );
            }
            Ok(builder.build()?)
        }
        Ok(Self {
            include: compile(include, paths)?,
            exclude: compile(exclude, paths)?,
        })
    }
    pub fn accepts(&self, name: &str) -> bool {
        self.accepts_with_annotation(name, false)
    }
    pub fn accepts_with_annotation(&self, name: &str, annotated: bool) -> bool {
        (annotated || self.include.is_match(name)) && !self.exclude.is_match(name)
    }
    pub fn reason(&self, name: &str) -> &'static str {
        if self.exclude.is_match(name) {
            "excluded by rule"
        } else if self.include.is_match(name) {
            "included by rule"
        } else {
            "no include rule"
        }
    }
}
impl Config {
    pub fn load(path: &Path, environment: bool) -> Result<Self> {
        Self::load_for_language(path, environment, None)
    }
    pub fn load_for_language(
        path: &Path,
        environment: bool,
        language: Option<common::Language>,
    ) -> Result<Self> {
        let text = std::fs::read_to_string(path).context("read configuration")?;
        let document: toml::Value =
            toml::from_str(&text).map_err(|_| anyhow::anyhow!("invalid TOML configuration"))?;
        if document
            .get("schema_version")
            .and_then(toml::Value::as_integer)
            == Some(2)
        {
            let language = if let Some(language) = language {
                language
            } else {
                std::env::var("OTELC_LANGUAGE")
                    .context("schema 2 requires --language or OTELC_LANGUAGE")?
                    .parse()?
            };
            return common::CommonConfig::from_text(&text, environment)?.for_native(language);
        }
        let mut config: Self = toml::from_str(&text).map_err(|_| {
            anyhow::anyhow!("invalid TOML or configuration schema; unknown keys are rejected")
        })?;
        if environment {
            config.apply_environment(|key| std::env::var(key).ok())?;
        }
        config.validate()?;
        Ok(config)
    }
    pub fn apply_environment(&mut self, get: impl Fn(&str) -> Option<String>) -> Result<()> {
        if let Some(value) = get("OTEL_RESOURCE_ATTRIBUTES") {
            for part in value.split(',').filter(|p| !p.trim().is_empty()) {
                let (key, value) = part
                    .trim()
                    .split_once('=')
                    .context("invalid resource attributes")?;
                self.resource.attributes.insert(
                    key.into(),
                    percent_encoding::percent_decode_str(value)
                        .decode_utf8()
                        .context("invalid resource encoding")?
                        .into(),
                );
            }
            if let Some(name) = self.resource.attributes.remove("service.name") {
                self.resource.service_name = name;
            }
            if let Some(version) = self.resource.attributes.remove("service.version") {
                self.resource.service_version = version;
            }
            self.resource.attributes.remove("service.instance.id");
        }
        for key in [
            "OTEL_EXPORTER_OTLP_TIMEOUT",
            "OTEL_EXPORTER_OTLP_METRICS_TIMEOUT",
        ] {
            if let Some(value) = get(key) {
                self.export.timeout_ms = value.parse().context("invalid OTLP timeout")?;
            }
        }
        if let Some(v) = get("OTEL_SERVICE_NAME") {
            self.resource.service_name = v;
        }
        if let Some(v) = get("OTEL_EXPORTER_OTLP_ENDPOINT") {
            self.export.endpoint = v;
            self.metrics_endpoint_is_full = false;
        }
        if let Some(v) = get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT") {
            self.export.endpoint = v;
            self.metrics_endpoint_is_full = true;
        }
        if let Some(v) = get("OTEL_EXPORTER_OTLP_PROTOCOL") {
            self.export.protocol = v;
        }
        if let Some(v) = get("OTEL_EXPORTER_OTLP_METRICS_PROTOCOL") {
            self.export.protocol = v;
        }
        Ok(())
    }
    pub fn validate(&self) -> Result<()> {
        if self.schema_version != 1 || !matches!(self.build.backend.as_str(), "callbacks" | "llvm")
        {
            bail!("only schema_version=1 and backend=callbacks or llvm are supported");
        }
        if let Some(path) = &self.runtime.control_socket {
            if self.build.backend != "llvm"
                || path.is_empty()
                || path.len() > 90
                || path.as_bytes().contains(&0)
            {
                bail!("live metrics control requires LLVM and a nonempty socket path of at most 90 bytes");
            }
        }
        if self.objects.max_live == 0
            || self.objects.max_live > 65536
            || self.objects.classes.len() > 128
            || self
                .objects
                .classes
                .iter()
                .any(|name| name.is_empty() || name.len() > 255 || name.as_bytes().contains(&0))
        {
            bail!("object classes/pool exceed limits");
        }
        let unique: std::collections::HashSet<_> = self.objects.classes.iter().collect();
        if unique.len() != self.objects.classes.len() {
            bail!("object classes must be unique");
        }
        if self.traces.enabled {
            bail!("span export is not implemented");
        }
        for (name, value, max) in [
            ("max_functions", self.runtime.max_functions, 65536),
            ("max_active_calls", self.runtime.max_active_calls, 65536),
            ("max_threads", self.runtime.max_threads, 4096),
            ("stack_depth", self.runtime.stack_depth, 4096),
            ("queue_capacity", self.runtime.queue_capacity, 65536),
            ("max_queued_batches", self.export.max_queued_batches, 64),
        ] {
            if value == 0 || value > max {
                bail!("{name} must be between 1 and {max}");
            }
        }
        if self.runtime.queue_capacity < 64 || !self.runtime.queue_capacity.is_power_of_two() {
            bail!("queue_capacity must be a power of two, at least 64");
        }
        for value in [
            self.runtime.shutdown_timeout_ms,
            self.export.timeout_ms,
            self.export.interval_ms,
        ] {
            if value == 0 || value > 60000 {
                bail!("time budgets must be between 1 and 60000 ms");
            }
        }
        if self.memory_bytes()? > 1024 * 1024 * 1024 {
            bail!("runtime pools exceed 1 GiB");
        }
        let buckets = &self.metrics.histogram_boundaries_seconds;
        if buckets.len() > 64
            || buckets.is_empty()
            || buckets.iter().any(|v| !v.is_finite() || *v <= 0.0)
            || buckets.windows(2).any(|p| p[0] >= p[1])
        {
            bail!(
                "histogram boundaries must be positive, finite, sorted and unique (1..64 buckets)"
            );
        }
        if !self.traces.root_sample_ratio.is_finite()
            || !(0.0..=1.0).contains(&self.traces.root_sample_ratio)
        {
            bail!("root_sample_ratio must be between 0 and 1");
        }
        if self.export.protocol != "http/protobuf" {
            bail!("only http/protobuf is supported");
        }
        validate_endpoint(&self.export.endpoint)?;
        if self.resource.service_name.is_empty()
            || self.resource.service_name.len() > 1024
            || self.resource.service_version.len() > 1024
        {
            bail!("invalid service identity");
        }
        if self.resource.attributes.len() > 32
            || self.resource.attributes.iter().any(|(k, v)| {
                k.is_empty() || k.len() > 256 || v.len() > 1024 || k.starts_with("service.")
            })
        {
            bail!("resource attributes exceed limits or override reserved service identity");
        }
        self.path_selection()?;
        self.function_selection()?;
        Ok(())
    }
    pub fn memory_bytes(&self) -> Result<usize> {
        self.runtime
            .stack_depth
            .checked_add(self.runtime.queue_capacity)
            .and_then(|n| n.checked_mul(64))
            .and_then(|n| n.checked_mul(self.runtime.max_threads))
            .and_then(|n| {
                let objects = if self.objects.classes.is_empty() {
                    0
                } else {
                    self.objects.max_live.checked_mul(64)?
                };
                n.checked_add(objects)
            })
            .context("runtime memory budget overflow")
    }
    pub fn path_selection(&self) -> Result<Selection> {
        Selection::new(&self.build.include, &self.build.exclude, true)
    }
    pub fn function_selection(&self) -> Result<Selection> {
        let mut excludes = self.functions.exclude.clone();
        excludes.extend(["otelc_*".into(), "__cyg_profile_*".into()]);
        Selection::new(&self.functions.include, &excludes, false)
    }
    pub fn metrics_endpoint(&self) -> String {
        if self.metrics_endpoint_is_full {
            self.export.endpoint.clone()
        } else {
            format!("{}/v1/metrics", self.export.endpoint.trim_end_matches('/'))
        }
    }
}
pub fn validate_endpoint(endpoint: &str) -> Result<()> {
    let parsed = url::Url::parse(endpoint).context("invalid OTLP endpoint")?;
    if !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.query().is_some()
        || parsed.fragment().is_some()
    {
        bail!("OTLP endpoint must not contain credentials, query or fragment");
    }
    let local = matches!(parsed.host_str(), Some("localhost" | "127.0.0.1" | "[::1]"));
    if parsed.scheme() != "https" && !(parsed.scheme() == "http" && local) {
        bail!("remote OTLP endpoints require HTTPS");
    }
    Ok(())
}
/// Read only supported OTLP header variables; never store headers in a manifest.
pub fn export_headers() -> Result<BTreeMap<String, String>> {
    export_signal_headers("OTEL_EXPORTER_OTLP_METRICS_HEADERS")
}
pub fn export_signal_headers(signal: &str) -> Result<BTreeMap<String, String>> {
    let value = std::env::var(signal)
        .or_else(|_| std::env::var("OTEL_EXPORTER_OTLP_HEADERS"))
        .unwrap_or_default();
    if value.len() > 8192 {
        bail!("OTLP headers exceed 8192 bytes");
    }
    let mut result = BTreeMap::new();
    for part in value.split(',').filter(|v| !v.trim().is_empty()) {
        let (key, value) = part
            .trim()
            .split_once('=')
            .context("invalid OTLP header format")?;
        let decoded = percent_encoding::percent_decode_str(value)
            .decode_utf8()
            .context("invalid OTLP header encoding")?
            .to_string();
        if key.is_empty()
            || !key.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-')
            || decoded.contains(['\r', '\n'])
        {
            bail!("invalid OTLP header");
        }
        result.insert(key.into(), decoded);
    }
    Ok(result)
}
#[cfg(test)]
#[path = "tests/config.rs"]
mod tests;
