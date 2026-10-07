//! Language-neutral policy. Adapters consume one resolved, versioned document.
use crate::*;
use std::{fmt, str::FromStr};

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "lowercase")]
pub enum Language {
    C,
    Cpp,
    Rust,
    TypeScript,
    JavaScript,
    Java,
    Python,
    Go,
}
impl fmt::Display for Language {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::C => "c",
            Self::Cpp => "cpp",
            Self::Rust => "rust",
            Self::TypeScript => "typescript",
            Self::JavaScript => "javascript",
            Self::Java => "java",
            Self::Python => "python",
            Self::Go => "go",
        })
    }
}
impl FromStr for Language {
    type Err = anyhow::Error;
    fn from_str(value: &str) -> Result<Self> {
        match value {
            "c" => Ok(Self::C), "cpp" => Ok(Self::Cpp), "rust" => Ok(Self::Rust),
            "typescript" => Ok(Self::TypeScript), "javascript" => Ok(Self::JavaScript),
            "java" => Ok(Self::Java), "python" => Ok(Self::Python), "go" => Ok(Self::Go),
            _ => bail!("unknown language; expected c, cpp, rust, typescript, javascript, java, python or go"),
        }
    }
}
config_section!(Annotations {
    read_existing: bool = false,
    inject_generated: bool = false
});
config_section!(Lifetimes {
    enabled: bool = false,
    boundary: String = "object".into(),
    include: Vec<String> = vec![],
    exclude: Vec<String> = vec![]
});
config_section!(SharedRuntime {
    max_functions: usize = 4096,
    max_active_calls: usize = 4096,
    max_live_lifetimes: usize = 4096,
    shutdown_timeout_ms: u64 = 2000,
    control_socket: Option<String> = None
});
config_section!(NativeLimits {
    max_threads: usize = 128,
    stack_depth: usize = 256,
    queue_capacity: usize = 4096
});
config_section!(Adapter { backend: String = "auto".into(), native: Option<NativeLimits> = None });

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CommonConfig {
    pub schema_version: u32,
    pub languages: Vec<Language>,
    #[serde(default)]
    pub sources: Functions,
    #[serde(default)]
    pub functions: Functions,
    #[serde(default)]
    pub lifetimes: Lifetimes,
    #[serde(default)]
    pub annotations: Annotations,
    #[serde(default)]
    pub runtime: SharedRuntime,
    #[serde(default)]
    pub metrics: Metrics,
    #[serde(default)]
    pub traces: Traces,
    #[serde(default)]
    pub export: Export,
    #[serde(skip)]
    metrics_endpoint_override: Option<String>,
    #[serde(default)]
    pub resource: Resource,
    #[serde(default)]
    pub adapters: BTreeMap<Language, Adapter>,
}
#[derive(Debug, Serialize)]
pub struct Matchers {
    pub include: Vec<String>,
    pub exclude: Vec<String>,
}
impl Matchers {
    fn new(selection: &Functions, paths: bool) -> Result<Self> {
        Ok(Self {
            include: Selection::regexes(&selection.include, paths)?,
            exclude: Selection::regexes(&selection.exclude, paths)?,
        })
    }
}
#[derive(Debug, Serialize)]
pub struct ResolvedConfig {
    pub schema_version: u32,
    pub language: Language,
    pub backend: String,
    pub sources: Functions,
    pub functions: Functions,
    pub source_matchers: Matchers,
    pub function_matchers: Matchers,
    pub lifetime_matchers: Matchers,
    pub lifetimes: Lifetimes,
    pub annotations: Annotations,
    pub runtime: SharedRuntime,
    pub native: Option<NativeLimits>,
    pub metrics: Metrics,
    pub traces: Traces,
    pub export: Export,
    pub resource: Resource,
    pub metrics_endpoint: String,
    pub execution_available: bool,
    pub unavailable: Vec<String>,
}
impl CommonConfig {
    pub fn load(path: &Path, environment: bool) -> Result<Self> {
        let text = std::fs::read_to_string(path).context("read common configuration")?;
        Self::from_text(&text, environment)
    }
    pub fn from_text(text: &str, environment: bool) -> Result<Self> {
        let mut config: Self = toml::from_str(text).map_err(|_| {
            anyhow::anyhow!("invalid common configuration; unknown keys are rejected")
        })?;
        if environment {
            config.apply_environment(|key| std::env::var(key).ok())?;
        }
        config.validate()?;
        Ok(config)
    }
    pub fn apply_environment(&mut self, get: impl Fn(&str) -> Option<String>) -> Result<()> {
        let mut shared = self.native_template(NativeLimits::default());
        shared.apply_environment(&get)?;
        self.metrics_endpoint_override = get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT");
        if self.metrics_endpoint_override.is_some() {
            shared.export.endpoint =
                get("OTEL_EXPORTER_OTLP_ENDPOINT").unwrap_or_else(|| self.export.endpoint.clone());
        }
        self.export = shared.export;
        self.resource = shared.resource;
        Ok(())
    }
    fn native_template(&self, native: NativeLimits) -> Config {
        Config {
            schema_version: 1,
            metrics_endpoint_is_full: false,
            read_annotations: self.annotations.read_existing,
            build: Build {
                backend: "llvm".into(),
                include: self.sources.include.clone(),
                exclude: self.sources.exclude.clone(),
            },
            functions: self.functions.clone(),
            objects: Objects {
                classes: vec![],
                max_live: self.runtime.max_live_lifetimes,
            },
            runtime: Runtime {
                max_functions: self.runtime.max_functions,
                max_threads: native.max_threads,
                stack_depth: native.stack_depth,
                queue_capacity: native.queue_capacity,
                shutdown_timeout_ms: self.runtime.shutdown_timeout_ms,
                control_socket: self.runtime.control_socket.clone(),
            },
            metrics: self.metrics.clone(),
            // Schema validation and adapter capability checks are separate.
            traces: Traces {
                enabled: false,
                ..self.traces.clone()
            },
            export: self.export.clone(),
            resource: self.resource.clone(),
        }
    }
    pub fn validate(&self) -> Result<()> {
        if self.runtime.max_active_calls == 0 || self.runtime.max_active_calls > 65536 {
            bail!("max_active_calls must be between 1 and 65536");
        }
        if self.schema_version != 2 || self.languages.is_empty() {
            bail!("common configuration requires schema_version=2 and nonempty languages");
        }
        let unique: std::collections::BTreeSet<_> = self.languages.iter().collect();
        if unique.len() != self.languages.len() {
            bail!("languages must be unique");
        }
        if !matches!(
            self.lifetimes.boundary.as_str(),
            "object" | "resource" | "collection"
        ) {
            bail!("lifetimes.boundary must be object, resource or collection");
        }
        Selection::new(&self.lifetimes.include, &self.lifetimes.exclude, false)?;
        for value in [
            self.traces.max_active_traces,
            self.traces.max_spans_per_trace,
        ] {
            if value == 0 || value > 65536 {
                bail!("trace limits must be between 1 and 65536");
            }
        }
        self.native_template(NativeLimits::default()).validate()?;
        if let Some(endpoint) = &self.metrics_endpoint_override {
            validate_endpoint(endpoint)?;
        }
        for (language, adapter) in &self.adapters {
            if !self.languages.contains(language) {
                bail!("adapter {language} is not in languages");
            }
            let allowed: &[&str] = match language {
                Language::C | Language::Cpp => &["auto", "callbacks", "llvm", "source"],
                Language::Rust => &["auto", "compiler", "source"],
                Language::TypeScript | Language::JavaScript => &["auto", "source", "loader"],
                Language::Java => &["auto", "agent", "aspectj"],
                Language::Python => &["auto", "import", "profile", "source"],
                Language::Go => &["auto", "compile"],
            };
            if !allowed.contains(&adapter.backend.as_str()) {
                bail!("invalid backend for {language}");
            }
            if let Some(native) = &adapter.native {
                if !matches!(language, Language::C | Language::Cpp) {
                    bail!("native buffer settings are not applicable to {language}");
                }
                self.native_template(native.clone()).validate()?;
            }
        }
        Ok(())
    }
    pub fn resolve(&self, language: Language) -> Result<ResolvedConfig> {
        self.validate()?;
        if !self.languages.contains(&language) {
            bail!("language {language} is not enabled");
        }
        let adapter = self.adapters.get(&language).cloned().unwrap_or_default();
        let backend = if adapter.backend == "auto" {
            match language {
                Language::C | Language::Cpp => "llvm",
                Language::Rust => "compiler",
                Language::TypeScript => "source",
                Language::JavaScript => "loader",
                Language::Java => "agent",
                Language::Python => "profile",
                Language::Go => "compile",
            }
            .into()
        } else {
            adapter.backend
        };
        let native = matches!(language, Language::C | Language::Cpp)
            .then(|| adapter.native.unwrap_or_default());
        let mut unavailable = Vec::new();
        if language == Language::Python {
            if backend != "profile" {
                unavailable.push("Python requires the profile backend".into());
            }
            if self.annotations.inject_generated {
                unavailable.push("Python monitoring does not inject annotations".into());
            }
            if self.lifetimes.enabled {
                unavailable.push("automatic Python lifetimes are not implemented".into());
            }
            if self.traces.enabled {
                unavailable.push("Python span export is not implemented".into());
            }
        } else if matches!(language, Language::JavaScript | Language::TypeScript) {
            let required = if language == Language::TypeScript {
                "source"
            } else {
                "loader"
            };
            if backend != required {
                unavailable.push(format!("{language} requires the {required} backend"));
            }
            if self.lifetimes.enabled {
                unavailable.push(format!(
                    "automatic {language} lifetimes are not implemented"
                ));
            }
            if self.traces.enabled {
                unavailable.push(format!("{language} span export is not implemented"));
            }
        } else if language == Language::Java {
            if backend != "agent" {
                unavailable.push("Java requires the agent backend".into());
            }
            if self.lifetimes.enabled {
                unavailable.push("automatic Java lifetimes are not implemented".into());
            }
            if self.traces.enabled {
                unavailable.push("Java span export is not implemented".into());
            }
        } else if language == Language::Go {
            if backend != "compile" {
                unavailable.push("Go requires the compile backend".into());
            }
            if self.lifetimes.enabled {
                unavailable.push("automatic Go lifetimes are not implemented".into());
            }
            if self.traces.enabled {
                unavailable.push("Go span export is not implemented".into());
            }
        } else if language == Language::Rust {
            if backend != "compiler" {
                unavailable.push("Rust requires the compiler backend".into());
            }
            if self.lifetimes.enabled {
                unavailable.push("automatic Rust lifetimes are not implemented".into());
            }
            if self.traces.enabled {
                unavailable.push("Rust span export is not implemented".into());
            }
        } else {
            if backend == "source" {
                unavailable.push("source-processing backend is not implemented".into());
            }
            if self.runtime.control_socket.is_some() && backend != "llvm" {
                unavailable.push("live metrics control requires the LLVM backend".into());
            }
            if self.annotations.read_existing && backend != "llvm" {
                unavailable.push("reading annotations requires the LLVM backend".into());
            }
            if self.annotations.inject_generated {
                unavailable.push("annotation injection is not implemented".into());
            }
            if self.lifetimes.enabled {
                unavailable.push("automatic lifetime instrumentation is not implemented".into());
            }
            if self.traces.enabled {
                unavailable.push("span export is not implemented".into());
            }
        }
        Ok(ResolvedConfig {
            schema_version: 2,
            language,
            backend,
            native,
            sources: self.sources.clone(),
            functions: self.functions.clone(),
            source_matchers: Matchers::new(&self.sources, true)?,
            function_matchers: Matchers::new(&self.functions, false)?,
            lifetime_matchers: Matchers::new(
                &Functions {
                    include: self.lifetimes.include.clone(),
                    exclude: self.lifetimes.exclude.clone(),
                },
                false,
            )?,
            lifetimes: self.lifetimes.clone(),
            annotations: self.annotations.clone(),
            runtime: self.runtime.clone(),
            metrics: self.metrics.clone(),
            traces: self.traces.clone(),
            export: self.export.clone(),
            resource: self.resource.clone(),
            metrics_endpoint: self.metrics_endpoint_override.clone().unwrap_or_else(|| {
                format!("{}/v1/metrics", self.export.endpoint.trim_end_matches('/'))
            }),
            execution_available: unavailable.is_empty(),
            unavailable,
        })
    }
    pub fn for_native(&self, language: Language) -> Result<Config> {
        let resolved = self.resolve(language)?;
        if !resolved.execution_available {
            bail!("{}", resolved.unavailable.join("; "));
        }
        let mut config = self.native_template(resolved.native.context("native adapter required")?);
        config.build.backend = resolved.backend;
        if let Some(endpoint) = &self.metrics_endpoint_override {
            config.export.endpoint = endpoint.clone();
            config.metrics_endpoint_is_full = true;
        }
        config.validate()?;
        Ok(config)
    }
}
#[cfg(test)]
#[path = "tests/common.rs"]
mod tests;
