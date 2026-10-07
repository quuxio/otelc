use super::*;
fn config() -> Config {
    toml::from_str("schema_version=1").unwrap()
}
#[test]
fn strict_schema() {
    assert!(toml::from_str::<Config>("schema_version=1\nunknown=true").is_err());
    assert!(toml::from_str::<Config>("schema_version=1\n[runtime]\nunknown=1").is_err());
    assert!(toml::from_str::<Config>("").is_err());
}
#[test]
fn defaults_and_budget() {
    let c = config();
    c.validate().unwrap();
    assert_eq!(c.memory_bytes().unwrap(), 34 * 1024 * 1024);
}
#[test]
fn selection() {
    let s = Selection::new(&["OrderBook::*".into()], &["*::operator*".into()], false).unwrap();
    assert!(s.accepts("OrderBook::add(int)"));
    assert!(!s.accepts("OrderBook::operator+(int)"));
    assert!(!s.accepts("Other::add()"));
    assert!(!Selection::new(&[], &[], false).unwrap().accepts("any"));
}
#[test]
fn paths() {
    let s = Selection::new(&["src/**/*.c".into()], &["src/vendor/**".into()], true).unwrap();
    assert!(s.accepts("src/main.c"));
    assert!(s.accepts("src/sub/main.c"));
    assert!(!s.accepts("src/vendor/main.c"));
}
#[test]
fn invalid_limits() {
    let mut c = config();
    c.runtime.queue_capacity = 65;
    assert!(c.validate().is_err());
    c.runtime.queue_capacity = 64;
    c.runtime.max_threads = 0;
    assert!(c.validate().is_err());
    c.runtime.max_threads = 4096;
    c.runtime.stack_depth = 4096;
    c.runtime.queue_capacity = 65536;
    assert!(c.validate().is_err());
}
#[test]
fn invalid_capabilities_and_buckets() {
    let mut c = config();
    c.traces.enabled = true;
    assert!(c.validate().is_err());
    c.traces.enabled = false;
    c.metrics.histogram_boundaries_seconds = vec![1.0, 1.0];
    assert!(c.validate().is_err());
}
#[test]
fn endpoints() {
    for v in [
        "http://localhost:4318",
        "http://127.0.0.1:4318",
        "https://collector.example",
    ] {
        validate_endpoint(v).unwrap();
    }
    for v in [
        "http://remote.example",
        "https://user:secret@collector.example",
        "https://collector.example?token=secret",
    ] {
        assert!(validate_endpoint(v).is_err());
    }
}
#[test]
fn environment_precedence() {
    let mut c = config();
    c.apply_environment(|key| match key {
        "OTEL_SERVICE_NAME" => Some("service".into()),
        "OTEL_EXPORTER_OTLP_ENDPOINT" => Some("https://base".into()),
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT" => Some("https://signal".into()),
        _ => None,
    })
    .unwrap();
    assert_eq!(c.export.endpoint, "https://signal");
    assert_eq!(c.resource.service_name, "service");
}

#[test]
fn resource_and_timeout_overrides() {
    let mut c = config();
    c.apply_environment(|key| match key {
        "OTEL_RESOURCE_ATTRIBUTES" => Some(
            "service.name=base,service.version=one,deployment.environment.name=test%20env".into(),
        ),
        "OTEL_SERVICE_NAME" => Some("override".into()),
        "OTEL_EXPORTER_OTLP_TIMEOUT" => Some("200".into()),
        "OTEL_EXPORTER_OTLP_METRICS_TIMEOUT" => Some("50".into()),
        _ => None,
    })
    .unwrap();
    c.validate().unwrap();
    assert_eq!(c.resource.service_name, "override");
    assert_eq!(c.resource.service_version, "one");
    assert_eq!(
        c.resource.attributes["deployment.environment.name"],
        "test env"
    );
    assert_eq!(c.export.timeout_ms, 50);
    assert!(c
        .apply_environment(|key| if key == "OTEL_EXPORTER_OTLP_TIMEOUT" {
            Some("invalid".into())
        } else {
            None
        })
        .is_err());
}
#[test]
fn invalid_resource_and_empty_identity() {
    let mut c = config();
    c.resource
        .attributes
        .insert("service.name".into(), "bad".into());
    assert!(c.validate().is_err());
    c.resource.attributes.clear();
    c.resource.service_name.clear();
    assert!(c.validate().is_err());
}

#[test]
fn signal_endpoint_path_does_not_depend_on_the_process_environment() {
    let mut c = config();
    c.apply_environment(|key| {
        if key == "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT" {
            Some("https://metrics.example/custom".into())
        } else {
            None
        }
    })
    .unwrap();
    assert_eq!(c.metrics_endpoint(), "https://metrics.example/custom");
    c.apply_environment(|key| {
        if key == "OTEL_EXPORTER_OTLP_ENDPOINT" {
            Some("https://collector.example/base".into())
        } else {
            None
        }
    })
    .unwrap();
    assert_eq!(
        c.metrics_endpoint(),
        "https://collector.example/base/v1/metrics"
    );
}
#[test]
fn annotation_opt_in_cannot_override_exclusions() {
    let selection = Selection::new(&[], &["blocked*".into()], false).unwrap();
    assert!(!selection.accepts("ordinary"));
    assert!(selection.accepts_with_annotation("ordinary", true));
    assert!(!selection.accepts_with_annotation("blocked_function", true));
}
