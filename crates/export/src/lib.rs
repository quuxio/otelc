//! Cumulative metrics encoded using the upstream OpenTelemetry protobuf types.
pub mod traces;
use anyhow::{bail, Result};
use opentelemetry_proto::tonic::{
    collector::metrics::v1::{ExportMetricsServiceRequest, ExportMetricsServiceResponse},
    common::v1::{any_value, AnyValue, InstrumentationScope, KeyValue},
    metrics::v1::{
        metric, number_data_point, AggregationTemporality, Histogram, HistogramDataPoint, Metric,
        NumberDataPoint, ResourceMetrics, ScopeMetrics, Sum,
    },
    resource::v1::Resource,
};
use prost::Message;
use quux_otelc_config::Config;
use std::{collections::BTreeMap, time::Duration};

#[derive(Clone, Debug)]
pub struct Aggregate {
    pub name: String,
    pub count: u64,
    pub unwinds: u64,
    pub sum: f64,
    pub min: f64,
    pub max: f64,
    pub buckets: Vec<u64>,
}
impl Aggregate {
    pub fn new(name: String, bucket_count: usize) -> Self {
        Self {
            name,
            count: 0,
            unwinds: 0,
            sum: 0.0,
            min: f64::INFINITY,
            max: 0.0,
            buckets: vec![0; bucket_count + 1],
        }
    }
    pub fn observe(&mut self, seconds: f64, boundaries: &[f64]) {
        self.count = self.count.saturating_add(1);
        self.sum += seconds;
        self.min = self.min.min(seconds);
        self.max = self.max.max(seconds);
        let index = boundaries.partition_point(|v| seconds > *v);
        if let Some(bucket) = self.buckets.get_mut(index) {
            *bucket = bucket.saturating_add(1);
        }
    }
}
fn attr(key: &str, value: &str) -> KeyValue {
    KeyValue {
        key: key.into(),
        value: Some(AnyValue {
            value: Some(any_value::Value::StringValue(value.into())),
        }),
        ..Default::default()
    }
}
fn counter(name: &str, points: Vec<NumberDataPoint>) -> Metric {
    Metric {
        name: name.into(),
        unit: if name == "otelc.function.calls" {
            "{call}"
        } else {
            "{observation}"
        }
        .into(),
        data: Some(metric::Data::Sum(Sum {
            data_points: points,
            aggregation_temporality: AggregationTemporality::Cumulative as i32,
            is_monotonic: true,
        })),
        ..Default::default()
    }
}
fn number(value: u64, attributes: Vec<KeyValue>, start: u64, now: u64) -> NumberDataPoint {
    NumberDataPoint {
        attributes,
        start_time_unix_nano: start,
        time_unix_nano: now,
        value: Some(number_data_point::Value::AsInt(
            value.min(i64::MAX as u64) as i64
        )),
        ..Default::default()
    }
}
pub fn encode(
    config: &Config,
    collections: (&[Aggregate], &[Aggregate]),
    losses: &[(&str, u64)],
    export_loss: u64,
    start: u64,
    now: u64,
    instance: &str,
) -> Vec<u8> {
    let (aggregates, objects) = collections;
    let calls = aggregates
        .iter()
        .filter(|a| a.count > 0)
        .map(|a| {
            number(
                a.count,
                vec![attr("code.function.name", &a.name)],
                start,
                now,
            )
        })
        .collect();
    let histograms = aggregates
        .iter()
        .filter(|a| a.count > 0)
        .map(|a| HistogramDataPoint {
            attributes: vec![attr("code.function.name", &a.name)],
            start_time_unix_nano: start,
            time_unix_nano: now,
            count: a.count,
            sum: Some(a.sum),
            bucket_counts: a.buckets.clone(),
            explicit_bounds: config.metrics.histogram_boundaries_seconds.clone(),
            min: Some(a.min),
            max: Some(a.max),
            ..Default::default()
        })
        .collect();
    let mut metrics = vec![
        counter("otelc.function.calls", calls),
        counter(
            "otelc.function.unwinds",
            aggregates
                .iter()
                .filter(|a| a.unwinds > 0)
                .map(|a| {
                    number(
                        a.unwinds,
                        vec![attr("code.function.name", &a.name)],
                        start,
                        now,
                    )
                })
                .collect(),
        ),
        Metric {
            name: "otelc.function.duration".into(),
            unit: "s".into(),
            data: Some(metric::Data::Histogram(Histogram {
                data_points: histograms,
                aggregation_temporality: AggregationTemporality::Cumulative as i32,
            })),
            ..Default::default()
        },
        counter(
            "otelc.runtime.dropped_observations",
            losses
                .iter()
                .map(|(reason, value)| number(*value, vec![attr("reason", reason)], start, now))
                .collect(),
        ),
    ];
    if !objects.is_empty() {
        metrics.push(counter(
            "otelc.object.lifetimes",
            objects
                .iter()
                .filter(|a| a.count > 0)
                .map(|a| number(a.count, vec![attr("code.object.type", &a.name)], start, now))
                .collect(),
        ));
        metrics.push(Metric {
            name: "otelc.object.lifetime.duration".into(),
            unit: "s".into(),
            data: Some(metric::Data::Histogram(Histogram {
                data_points: objects
                    .iter()
                    .filter(|a| a.count > 0)
                    .map(|a| HistogramDataPoint {
                        attributes: vec![attr("code.object.type", &a.name)],
                        start_time_unix_nano: start,
                        time_unix_nano: now,
                        count: a.count,
                        sum: Some(a.sum),
                        bucket_counts: a.buckets.clone(),
                        explicit_bounds: config.metrics.histogram_boundaries_seconds.clone(),
                        min: Some(a.min),
                        max: Some(a.max),
                        ..Default::default()
                    })
                    .collect(),
                aggregation_temporality: AggregationTemporality::Cumulative as i32,
            })),
            ..Default::default()
        });
    }
    let mut dropped = counter(
        "otelc.export.dropped_batches",
        vec![number(export_loss, vec![], start, now)],
    );
    dropped.unit = "{batch}".into();
    metrics.push(dropped);
    let resource = Resource {
        attributes: vec![
            attr("service.name", &config.resource.service_name),
            attr("service.version", &config.resource.service_version),
            attr("service.instance.id", instance),
        ]
        .into_iter()
        .chain(config.resource.attributes.iter().map(|(k, v)| attr(k, v)))
        .collect(),
        ..Default::default()
    };
    ExportMetricsServiceRequest {
        resource_metrics: vec![ResourceMetrics {
            resource: Some(resource),
            scope_metrics: vec![ScopeMetrics {
                scope: Some(InstrumentationScope {
                    name: "quux.otelc".into(),
                    version: env!("CARGO_PKG_VERSION").into(),
                    ..Default::default()
                }),
                metrics,
                schema_url: String::new(),
            }],
            schema_url: String::new(),
        }],
    }
    .encode_to_vec()
}
/// Append bounded native whole-tree loss accounting on the telemetry worker.
pub fn append_trace_losses(
    payload: Vec<u8>,
    losses: &[(&str, u64)],
    start: u64,
    now: u64,
) -> Vec<u8> {
    let mut request = ExportMetricsServiceRequest::decode(payload.as_slice())
        .expect("internally encoded native metrics");
    let points = losses
        .iter()
        .map(|(reason, count)| number(*count, vec![attr("reason", reason)], start, now))
        .collect();
    let mut metric = counter("otelc.trace.dropped_trees", points);
    metric.unit = "{tree}".into();
    request.resource_metrics[0].scope_metrics[0]
        .metrics
        .push(metric);
    request.encode_to_vec()
}

/// Up to three transient retries within one total deadline. Partial success,
/// permanent rejection and invalid response bodies are never retried.
pub fn send(
    endpoint: &str,
    headers: &BTreeMap<String, String>,
    payload: &[u8],
    timeout: Duration,
) -> Result<()> {
    send_signal(endpoint, headers, payload, timeout, false)
}
/// Send an OTLP trace request and validate the trace-specific acknowledgement.
pub fn send_traces(
    endpoint: &str,
    headers: &BTreeMap<String, String>,
    payload: &[u8],
    timeout: Duration,
) -> Result<()> {
    send_signal(endpoint, headers, payload, timeout, true)
}
fn send_signal(
    endpoint: &str,
    headers: &BTreeMap<String, String>,
    payload: &[u8],
    timeout: Duration,
    traces: bool,
) -> Result<()> {
    let deadline = std::time::Instant::now() + timeout;
    for attempt in 0..3 {
        let remaining = deadline.saturating_duration_since(std::time::Instant::now());
        if remaining.is_zero() {
            bail!("OTLP retry deadline exceeded");
        }
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .timeout_global(Some(remaining))
            .http_status_as_error(false)
            .max_redirects(0)
            .build()
            .into();
        let mut request = agent
            .post(endpoint)
            .header("Content-Type", "application/x-protobuf");
        for (key, value) in headers {
            request = request.header(key, value);
        }
        let mut delay = Duration::from_millis(10 << attempt);
        match request.send(payload) {
            Ok(mut response) => {
                let status = response.status().as_u16();
                if status == 200 {
                    let body = response
                        .body_mut()
                        .with_config()
                        .limit(65536)
                        .read_to_vec()
                        .map_err(|_| anyhow::anyhow!("invalid OTLP response"))?;
                    let rejected = if traces {
                        opentelemetry_proto::tonic::collector::trace::v1::ExportTraceServiceResponse::decode(body.as_slice())
                            .map_err(|_|anyhow::anyhow!("invalid OTLP trace protobuf response"))?
                            .partial_success.is_some_and(|partial| partial.rejected_spans > 0)
                    } else {
                        ExportMetricsServiceResponse::decode(body.as_slice())
                            .map_err(|_| anyhow::anyhow!("invalid OTLP protobuf response"))?
                            .partial_success
                            .is_some_and(|partial| partial.rejected_data_points > 0)
                    };
                    if rejected {
                        bail!("OTLP partially rejected the batch");
                    }
                    return Ok(());
                }
                if !matches!(status, 429 | 502 | 503 | 504) {
                    bail!("OTLP rejected the batch (HTTP {status})");
                }
                if let Some(value) = response
                    .headers()
                    .get("retry-after")
                    .and_then(|v| v.to_str().ok())
                {
                    delay = if let Ok(seconds) = value.parse::<u64>() {
                        Duration::from_secs(seconds)
                    } else if let Ok(date) = httpdate::parse_http_date(value) {
                        date.duration_since(std::time::SystemTime::now())
                            .unwrap_or_default()
                    } else {
                        bail!("invalid OTLP retry delay");
                    };
                }
            }
            Err(ureq::Error::Io(_) | ureq::Error::Timeout(_)) => {}
            Err(_) => bail!("OTLP transport failed"),
        }
        if attempt == 2 || delay >= deadline.saturating_duration_since(std::time::Instant::now()) {
            bail!("OTLP retry budget exhausted");
        }
        std::thread::sleep(delay);
    }
    bail!("OTLP retry budget exhausted")
}
#[cfg(test)]
#[path = "tests/metrics.rs"]
mod tests;

#[cfg(test)]
#[path = "tests/transport.rs"]
mod transport_tests;
