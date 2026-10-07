use opentelemetry_sdk::{
    error::OTelSdkResult,
    metrics::{
        data::ResourceMetrics, reader::MetricReader, InstrumentKind, ManualReader, Pipeline,
        Temporality,
    },
};
use std::{
    sync::{Arc, Weak},
    time::Duration,
};

/// Version-pinned SDK pull reader, shared with our deadline-bounded worker.
#[derive(Clone, Debug)]
pub(crate) struct Reader(pub Arc<ManualReader>);
impl MetricReader for Reader {
    fn register_pipeline(&self, pipeline: Weak<Pipeline>) {
        self.0.register_pipeline(pipeline);
    }
    fn collect(&self, metrics: &mut ResourceMetrics) -> OTelSdkResult {
        self.0.collect(metrics)
    }
    fn force_flush(&self) -> OTelSdkResult {
        self.0.force_flush()
    }
    fn shutdown_with_timeout(&self, timeout: Duration) -> OTelSdkResult {
        self.0.shutdown_with_timeout(timeout)
    }
    fn temporality(&self, kind: InstrumentKind) -> Temporality {
        self.0.temporality(kind)
    }
}
