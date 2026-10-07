use super::*;
#[test]
fn histogram_and_proto() {
    let config: Config = toml_config();
    let mut a = Aggregate::new(
        "work".into(),
        config.metrics.histogram_boundaries_seconds.len(),
    );
    a.observe(0.000001, &config.metrics.histogram_boundaries_seconds);
    a.observe(2.0, &config.metrics.histogram_boundaries_seconds);
    assert_eq!(a.buckets[0], 1);
    assert_eq!(a.buckets.last(), Some(&1));
    let data = encode(&config, (&[a], &[]), &[("queue", 3)], 1, 10, 20, "test");
    let decoded = ExportMetricsServiceRequest::decode(data.as_slice()).unwrap();
    assert_eq!(
        decoded.resource_metrics[0].scope_metrics[0].metrics.len(),
        5
    );
}
fn toml_config() -> Config {
    Config {
        schema_version: 1,
        metrics_endpoint_is_full: false,
        read_annotations: false,
        build: Default::default(),
        functions: Default::default(),
        objects: Default::default(),
        runtime: Default::default(),
        metrics: Default::default(),
        traces: Default::default(),
        trace_export: None,
        export: Default::default(),
        resource: Default::default(),
    }
}
