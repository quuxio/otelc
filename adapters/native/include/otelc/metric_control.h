#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>
#include <memory>
#include <mutex>

#include "opentelemetry/sdk/metrics/meter_provider.h"
#include "opentelemetry/sdk/metrics/push_metric_exporter.h"

namespace otelc::native {

// These are SDK-call observations, not downstream HTTP receipt or queue-loss proof.
struct MetricStatus {
  bool collection;
  bool emission;
  bool closing;
  bool closed;
  std::uint64_t queued;
  std::uint64_t in_flight;
  std::uint64_t attempted;
  std::uint64_t completed;
  std::uint64_t failed;
  std::uint64_t suppressed;
};

class MetricState {
 public:
  MetricState(bool collection, bool emission);
  MetricStatus Status() const;
  bool SetEmission(bool enabled);

 private:
  friend class MetricController;
  friend class GatedMetricExporter;
  std::atomic<bool> collection_;
  std::timed_mutex control_;
  mutable std::mutex mutex_;
  MetricStatus status_;
};

// Apply the official SDK's live meter updates to existing and future instruments.
class MetricController {
 public:
  MetricController(std::shared_ptr<opentelemetry::sdk::metrics::MeterProvider> provider,
                   std::shared_ptr<MetricState> state);
  bool SetCollection(bool enabled);
  std::unique_ptr<opentelemetry::sdk::metrics::MetricFilter> Filter() const;

 private:
  std::shared_ptr<opentelemetry::sdk::metrics::MeterProvider> provider_;
  std::shared_ptr<MetricState> state_;
};

// Gate the official exporter, keeping SDK aggregation cumulative during silence.
class GatedMetricExporter final : public opentelemetry::sdk::metrics::PushMetricExporter {
 public:
  GatedMetricExporter(std::unique_ptr<opentelemetry::sdk::metrics::PushMetricExporter> delegate,
                      std::shared_ptr<MetricState> state);
  opentelemetry::sdk::common::ExportResult Export(
      const opentelemetry::sdk::metrics::ResourceMetrics &data) noexcept override;
  opentelemetry::sdk::metrics::AggregationTemporality GetAggregationTemporality(
      opentelemetry::sdk::metrics::InstrumentType type) const noexcept override;
  bool ForceFlush(std::chrono::microseconds timeout = std::chrono::microseconds::max()) noexcept override;
  bool Shutdown(std::chrono::microseconds timeout = std::chrono::microseconds::max()) noexcept override;

 private:
  std::unique_ptr<opentelemetry::sdk::metrics::PushMetricExporter> delegate_;
  std::shared_ptr<MetricState> state_;
  // The SDK forbids concurrent Export calls on one exporter. Flush and shutdown
  // use the same operation lock, while status/control never wait on network I/O.
  std::timed_mutex operation_;
};

}  // namespace otelc::native
