use super::*;
fn policy() -> CommonConfig {
    toml::from_str(include_str!("../../../../examples/common.toml")).unwrap()
}
#[test]
fn one_document_resolves_all_languages_with_shared_policy() {
    let config = policy();
    config.validate().unwrap();
    for language in &config.languages {
        let resolved = config.resolve(*language).unwrap();
        assert_eq!(resolved.schema_version, 2);
        assert_eq!(resolved.language, *language);
        assert_eq!(resolved.sources.include, config.sources.include);
        assert_eq!(resolved.functions.include, config.functions.include);
        assert_eq!(resolved.export.endpoint, config.export.endpoint);
        assert_eq!(
            resolved.metrics.histogram_boundaries_seconds,
            config.metrics.histogram_boundaries_seconds
        );
        assert_eq!(resolved.resource.service_name, config.resource.service_name);
        assert_eq!(
            resolved.execution_available,
            matches!(
                language,
                Language::C
                    | Language::Cpp
                    | Language::Python
                    | Language::JavaScript
                    | Language::TypeScript
                    | Language::Java
                    | Language::Go
                    | Language::Rust
            )
        );
        assert_eq!(language.to_string().parse::<Language>().unwrap(), *language);
    }
    assert!("c++".parse::<Language>().is_err());
}
#[test]
fn rust_traces_resolve_independent_signal_settings() {
    let mut config = policy();
    config.traces.enabled = true;
    config
        .apply_environment(|key| match key {
            "OTEL_EXPORTER_OTLP_ENDPOINT" => Some("http://127.0.0.1:4318".into()),
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT" => Some("http://127.0.0.1:55682/metrics".into()),
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" => Some("http://127.0.0.1:55681/traces".into()),
            "OTEL_EXPORTER_OTLP_TIMEOUT" => Some("777".into()),
            "OTEL_EXPORTER_OTLP_METRICS_TIMEOUT" => Some("222".into()),
            "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT" => Some("333".into()),
            _ => None,
        })
        .unwrap();
    let resolved = config.resolve(Language::Rust).unwrap();
    assert!(resolved.execution_available);
    assert_eq!(resolved.metrics_endpoint, "http://127.0.0.1:55682/metrics");
    assert_eq!(resolved.export.timeout_ms, 222);
    let trace = resolved.trace_export.unwrap();
    assert_eq!(trace.endpoint, "http://127.0.0.1:55681/traces");
    assert_eq!(trace.timeout_ms, 333);
    assert!(config.resolve(Language::Cpp).unwrap().execution_available);
}

#[test]
fn java_traces_consume_the_common_independent_signal_policy() {
    let mut config = policy();
    config.traces.enabled = true;
    config.traces.root_sample_ratio = 0.5;
    config.export.timeout_ms = 111;
    config
        .apply_environment(|key| match key {
            "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT" => Some("555".into()),
            _ => None,
        })
        .unwrap();
    let resolved = config.resolve(Language::Java).unwrap();
    assert!(resolved.execution_available);
    assert_eq!(resolved.trace_export.unwrap().timeout_ms, 555);
    assert_eq!(resolved.export.timeout_ms, 111);
    assert_eq!(resolved.traces.root_sample_ratio, 0.5);
    assert!(config.resolve(Language::Cpp).unwrap().execution_available);
}

#[test]
fn managed_language_traces_resolve_with_independent_signal_settings() {
    let mut config = policy();
    config.traces.enabled = true;
    config.export.timeout_ms = 111;
    config
        .apply_environment(|key| match key {
            "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT" => Some("555".into()),
            _ => None,
        })
        .unwrap();
    for language in [Language::JavaScript, Language::TypeScript, Language::Go] {
        let resolved = config.resolve(language).unwrap();
        assert!(resolved.execution_available);
        assert_eq!(resolved.export.timeout_ms, 111);
        assert_eq!(resolved.trace_export.unwrap().timeout_ms, 555);
    }
}
#[test]
fn python_traces_share_the_independent_signal_policy() {
    let mut config = policy();
    config.traces.enabled = true;
    config
        .apply_environment(|key| match key {
            "OTEL_EXPORTER_OTLP_METRICS_TIMEOUT" => Some("111".into()),
            "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT" => Some("555".into()),
            _ => None,
        })
        .unwrap();
    let python = config.resolve(Language::Python).unwrap();
    assert!(python.execution_available);
    assert_eq!(python.export.timeout_ms, 111);
    assert_eq!(python.trace_export.unwrap().timeout_ms, 555);
    assert!(config.resolve(Language::Cpp).unwrap().execution_available);
}
#[test]
fn native_projection_uses_shared_policy_and_owned_buffer_settings() {
    let config = policy();
    let native = config.for_native(Language::Cpp).unwrap();
    assert_eq!(native.build.backend, "llvm");
    assert_eq!(native.build.include, config.sources.include);
    assert_eq!(native.runtime.max_functions, config.runtime.max_functions);
    assert_eq!(native.runtime.max_threads, 32);
    assert_eq!(native.runtime.queue_capacity, 65536);
    assert_eq!(
        native.runtime.max_active_calls,
        config.runtime.max_active_calls
    );
    assert!(native.objects.classes.is_empty());
    assert!(!native.traces.enabled);
    assert!(config.for_native(Language::Java).is_err());
}
#[test]
fn defaults_and_backend_preferences_are_deterministic() {
    let config: CommonConfig = toml::from_str("schema_version=2\nlanguages=['c','cpp','rust','typescript','javascript','java','python','go']").unwrap();
    for (language, backend) in [
        (Language::C, "llvm"),
        (Language::Cpp, "llvm"),
        (Language::Rust, "compiler"),
        (Language::TypeScript, "source"),
        (Language::JavaScript, "loader"),
        (Language::Java, "agent"),
        (Language::Python, "profile"),
        (Language::Go, "compile"),
    ] {
        assert_eq!(config.resolve(language).unwrap().backend, backend);
    }
    assert!(
        !Selection::new(&config.functions.include, &config.functions.exclude, false)
            .unwrap()
            .accepts("anything")
    );
    let mut config = config;
    config.adapters.insert(
        Language::C,
        Adapter {
            backend: "callbacks".into(),
            native: None,
        },
    );
    assert_eq!(
        config.for_native(Language::C).unwrap().build.backend,
        "callbacks"
    );
}
#[test]
fn unsupported_requested_features_are_never_silently_ignored() {
    for feature in 0..4 {
        let mut config = policy();
        match feature {
            0 => {
                config.annotations.read_existing = true;
                config.adapters.get_mut(&Language::Cpp).unwrap().backend = "callbacks".into();
            }
            1 => config.annotations.inject_generated = true,
            2 => config.lifetimes.enabled = true,
            _ => {
                config.traces.enabled = true;
                config.adapters.get_mut(&Language::Cpp).unwrap().backend = "callbacks".into();
            }
        }
        config.validate().unwrap();
        let resolved = config.resolve(Language::Cpp).unwrap();
        assert!(!resolved.execution_available);
        assert_eq!(resolved.unavailable.len(), 1);
        assert!(config.for_native(Language::Cpp).is_err());
    }
    let mut config = policy();
    config.adapters.get_mut(&Language::Cpp).unwrap().backend = "source".into();
    assert!(!config.resolve(Language::Cpp).unwrap().execution_available);
}
#[test]
fn invalid_language_lists_and_disabled_adapters() {
    let mut config = policy();
    config.languages.clear();
    assert!(config.validate().is_err());
    config.languages = vec![Language::Cpp, Language::Cpp];
    assert!(config.validate().is_err());
    config.languages = vec![Language::Cpp];
    assert!(config.validate().is_err());
    config
        .adapters
        .retain(|language, _| *language == Language::Cpp);
    config.validate().unwrap();
    assert!(config.resolve(Language::C).is_err());
    config.schema_version = 1;
    assert!(config.validate().is_err());
}
#[test]
fn backend_and_native_setting_validation() {
    let mut config = policy();
    config.adapters.get_mut(&Language::Java).unwrap().backend = "llvm".into();
    assert!(config.validate().is_err());
    config.adapters.get_mut(&Language::Java).unwrap().backend = "agent".into();
    config.adapters.get_mut(&Language::Java).unwrap().native = Some(NativeLimits::default());
    assert!(config.validate().is_err());
    config.adapters.get_mut(&Language::Java).unwrap().native = None;
    config
        .adapters
        .get_mut(&Language::Cpp)
        .unwrap()
        .native
        .as_mut()
        .unwrap()
        .queue_capacity = 65;
    assert!(config.validate().is_err());
}
#[test]
fn lifetime_and_trace_contract_is_validated_without_claiming_support() {
    let mut config = policy();
    config.lifetimes.boundary = "destructor-ish".into();
    assert!(config.validate().is_err());
    for boundary in ["object", "resource", "collection"] {
        config.lifetimes.boundary = boundary.into();
        config.validate().unwrap();
    }
    config.runtime.max_live_lifetimes = 0;
    assert!(config.validate().is_err());
    config.runtime.max_live_lifetimes = 4096;
    config.traces.max_active_traces = 0;
    assert!(config.validate().is_err());
    config.traces.max_active_traces = 512;
    config.traces.max_spans_per_trace = 65537;
    assert!(config.validate().is_err());
}
#[test]
fn strict_keys_and_language_identifiers() {
    for invalid in [
        "schema_version=2\nlanguages=['c']\nunknown=true",
        "schema_version=2\nlanguages=['scala']",
        "schema_version=2\nlanguages=['c']\n[annotations]\nmagic=true",
        "schema_version=2\nlanguages=['c']\n[adapters.c]\nexecute='anything'",
        "schema_version=2\nlanguages=['c']\n[adapters.scala]\nbackend='auto'",
    ] {
        assert!(toml::from_str::<CommonConfig>(invalid).is_err());
    }
}
#[test]
fn load_shared_file_and_legacy_file_through_the_native_loader() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../examples/common.toml");
    CommonConfig::load(&path, false).unwrap();
    let native = Config::load_for_language(&path, false, Some(Language::Cpp)).unwrap();
    assert_eq!(native.resource.service_name, "otelc-common-example");
    assert!(Config::load_for_language(&path, false, Some(Language::Python)).is_err());
    let legacy = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../examples/local.toml");
    assert_eq!(
        Config::load(&legacy, false).unwrap().build.backend,
        "callbacks"
    );
    assert!(CommonConfig::load(&legacy, false).is_err());
}

#[test]
fn common_signal_endpoint_and_service_overrides_are_resolved_once() {
    let mut config = policy();
    config
        .apply_environment(|key| match key {
            "OTEL_EXPORTER_OTLP_ENDPOINT" => Some("https://collector.example/base".into()),
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT" => Some("https://metrics.example/custom".into()),
            "OTEL_SERVICE_NAME" => Some("service-override".into()),
            _ => None,
        })
        .unwrap();
    config.validate().unwrap();
    let resolved = config.resolve(Language::Java).unwrap();
    assert_eq!(resolved.export.endpoint, "https://collector.example/base");
    assert_eq!(resolved.metrics_endpoint, "https://metrics.example/custom");
    assert_eq!(resolved.resource.service_name, "service-override");
    assert_eq!(
        config.for_native(Language::Cpp).unwrap().metrics_endpoint(),
        resolved.metrics_endpoint
    );
    let mut config = policy();
    config
        .apply_environment(|key| {
            if key == "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT" {
                Some("http://remote.example/metrics".into())
            } else {
                None
            }
        })
        .unwrap();
    assert!(config.validate().is_err());
    assert_eq!(
        policy().resolve(Language::Cpp).unwrap().metrics_endpoint,
        "http://127.0.0.1:4318/v1/metrics"
    );
}
#[test]
fn annotation_and_live_control_capabilities_follow_backend_selection() {
    let mut config = policy();
    config.annotations.read_existing = true;
    config.runtime.control_socket = Some("build/control/metrics.sock".into());
    assert!(config.resolve(Language::Cpp).unwrap().execution_available);
    assert!(config.for_native(Language::Cpp).unwrap().read_annotations);
    config.adapters.get_mut(&Language::Cpp).unwrap().backend = "callbacks".into();
    assert!(!config.resolve(Language::Cpp).unwrap().execution_available);
    assert!(config.for_native(Language::Cpp).is_err());
    config.runtime.control_socket = Some("".into());
    assert!(config.validate().is_err());
}
#[test]
fn trace_environment_defaults_validation_and_disabled_signal_are_independent() {
    let mut config = policy();
    config.traces.enabled = true;
    config
        .apply_environment(|key| match key {
            "OTEL_EXPORTER_OTLP_ENDPOINT" => Some("http://localhost:4318/base/".into()),
            "OTEL_EXPORTER_OTLP_METRICS_TIMEOUT" => Some("500".into()),
            _ => None,
        })
        .unwrap();
    let trace = config
        .resolve(Language::Rust)
        .unwrap()
        .trace_export
        .unwrap();
    assert_eq!(trace.endpoint, "http://localhost:4318/base/v1/traces");
    assert_eq!(trace.timeout_ms, 1000);
    assert_eq!(config.export.timeout_ms, 500);
    for (key, value) in [
        ("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "grpc"),
        (
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "http://remote.example",
        ),
        ("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "0"),
        ("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "60001"),
    ] {
        let mut config = policy();
        config.traces.enabled = true;
        config
            .apply_environment(|candidate| (key == candidate).then(|| value.into()))
            .unwrap();
        assert!(config.validate().is_err());
    }
    let mut config = policy();
    config.traces.enabled = true;
    assert!(config
        .apply_environment(
            |key| (key == "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT").then(|| "invalid".into())
        )
        .is_err());
    let mut config = policy();
    config.traces.enabled = true;
    config.traces.max_active_traces = 65536;
    config.traces.max_spans_per_trace = 65536;
    assert!(config.validate().is_err());
    let mut config = policy();
    config
        .apply_environment(|key| {
            key.starts_with("OTEL_EXPORTER_OTLP_TRACES_")
                .then(|| "invalid".into())
        })
        .unwrap();
    config.validate().unwrap();
    assert!(config
        .resolve(Language::Rust)
        .unwrap()
        .trace_export
        .is_none());
}
