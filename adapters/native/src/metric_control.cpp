#include "otelc/metric_control.h"

#include <algorithm>
#include <stdexcept>

#include "opentelemetry/sdk/instrumentationscope/scope_configurator.h"
#include "opentelemetry/sdk/metrics/meter_config.h"

namespace otelc::native {
namespace metrics = opentelemetry::sdk::metrics;
namespace common = opentelemetry::sdk::common;
namespace scope = opentelemetry::sdk::instrumentationscope;
namespace {
using Clock = std::chrono::steady_clock;
using Micros = std::chrono::microseconds;

Clock::time_point Deadline(Micros timeout) {
  auto now = Clock::now();
  auto available = std::chrono::duration_cast<Micros>(Clock::time_point::max() - now);
  if (timeout >= available) return Clock::time_point::max();
  return now + std::max(timeout, Micros::zero());
}

Micros Remaining(Clock::time_point deadline) {
  if (deadline == Clock::time_point::max()) return Micros::max();
  return std::max(std::chrono::duration_cast<Micros>(deadline - Clock::now()), Micros::zero());
}

std::unique_ptr<scope::ScopeConfigurator<metrics::MeterConfig>> Configuration(bool enabled) {
  using Configurator = scope::ScopeConfigurator<metrics::MeterConfig>;
  return std::make_unique<Configurator>(Configurator::Builder(
      enabled ? metrics::MeterConfig::Enabled() : metrics::MeterConfig::Disabled()).Build());
}

bool Lock(std::unique_lock<std::timed_mutex> &lock, Clock::time_point deadline) {
  if (deadline == Clock::time_point::max()) {
    lock.lock();
    return true;
  }
  return lock.try_lock_until(deadline);
}
}  // namespace

MetricState::MetricState(bool collection, bool emission)
    : collection_(collection), status_{collection, emission, false, false, 0, 0, 0, 0, 0, 0} {}

MetricStatus MetricState::Status() const {
  std::lock_guard<std::mutex> guard(mutex_);
  return status_;
}

bool MetricState::SetEmission(bool enabled) {
  std::lock_guard<std::mutex> guard(mutex_);
  if (status_.closing) return false;
  status_.emission = enabled;
  return true;
}

MetricController::MetricController(std::shared_ptr<metrics::MeterProvider> provider,
                                   std::shared_ptr<MetricState> state)
    : provider_(std::move(provider)), state_(std::move(state)) {
  if (!provider_ || !state_) throw std::invalid_argument("native metric controls require a provider and state");
  if (!SetCollection(state_->collection_.load()))
    throw std::invalid_argument("native metric controls require an open state");
}

bool MetricController::SetCollection(bool enabled) {
  std::lock_guard<std::timed_mutex> transition(state_->control_);
  {
    std::lock_guard<std::mutex> guard(state_->mutex_);
    if (state_->status_.closing) return false;
    if (!enabled) state_->collection_.store(false, std::memory_order_release);
  }
  // SDK callbacks can inspect status. Never hold its lock across an SDK call.
  provider_->UpdateMeterConfigurator(Configuration(enabled));
  bool accepted;
  {
    std::lock_guard<std::mutex> guard(state_->mutex_);
    accepted = !state_->status_.closing;
    state_->collection_.store(enabled && accepted, std::memory_order_release);
    state_->status_.collection = enabled && accepted;
  }
  if (!accepted) provider_->UpdateMeterConfigurator(Configuration(false));
  return accepted;
}

std::unique_ptr<metrics::MetricFilter> MetricController::Filter() const {
  return metrics::MetricFilter::Create(
      [state = state_](const auto &, auto, const auto &, auto) {
        return state->collection_.load(std::memory_order_acquire)
            ? metrics::MetricFilter::MetricFilterResult::kAccept
            : metrics::MetricFilter::MetricFilterResult::kDrop;
      }, [](const auto &, auto, const auto &, auto, const auto &) {
        return metrics::MetricFilter::AttributesFilterResult::kAccept;
      });
}

GatedMetricExporter::GatedMetricExporter(std::unique_ptr<metrics::PushMetricExporter> delegate,
                                       std::shared_ptr<MetricState> state)
    : delegate_(std::move(delegate)), state_(std::move(state)) {
  if (!delegate_ || !state_) throw std::invalid_argument("native metric exporter requires a delegate and state");
}

common::ExportResult GatedMetricExporter::Export(const metrics::ResourceMetrics &data) noexcept {
  {
    std::lock_guard<std::mutex> guard(state_->mutex_);
    ++state_->status_.queued;
  }
  std::unique_lock<std::timed_mutex> operation(operation_);
  {
    std::lock_guard<std::mutex> guard(state_->mutex_);
    --state_->status_.queued;
    if (state_->status_.closing) return common::ExportResult::kFailure;
    if (!state_->status_.collection || !state_->status_.emission) {
      ++state_->status_.suppressed;
      return common::ExportResult::kSuccess;
    }
    ++state_->status_.in_flight;
    ++state_->status_.attempted;
  }
  auto result = delegate_->Export(data);
  {
    std::lock_guard<std::mutex> guard(state_->mutex_);
    --state_->status_.in_flight;
    ++state_->status_.completed;
    if (result != common::ExportResult::kSuccess) ++state_->status_.failed;
  }
  return result;
}

metrics::AggregationTemporality GatedMetricExporter::GetAggregationTemporality(metrics::InstrumentType) const noexcept {
  return metrics::AggregationTemporality::kCumulative;
}

bool GatedMetricExporter::ForceFlush(Micros timeout) noexcept {
  auto deadline = Deadline(timeout);
  std::unique_lock<std::timed_mutex> operation(operation_, std::defer_lock);
  if (!Lock(operation, deadline)) return false;
  {
    std::lock_guard<std::mutex> guard(state_->mutex_);
    if (state_->status_.closing) return false;
    if (!state_->status_.collection || !state_->status_.emission) return true;
    ++state_->status_.in_flight;
  }
  bool result = delegate_->ForceFlush(Remaining(deadline));
  {
    std::lock_guard<std::mutex> guard(state_->mutex_);
    --state_->status_.in_flight;
  }
  return result;
}

bool GatedMetricExporter::Shutdown(Micros timeout) noexcept {
  auto deadline = Deadline(timeout);
  {
    std::lock_guard<std::mutex> guard(state_->mutex_);
    state_->status_.closing = true;
  }
  std::unique_lock<std::timed_mutex> transition(state_->control_, std::defer_lock);
  if (!Lock(transition, deadline)) return false;
  transition.unlock();
  std::unique_lock<std::timed_mutex> operation(operation_, std::defer_lock);
  if (!Lock(operation, deadline)) return false;
  {
    std::lock_guard<std::mutex> guard(state_->mutex_);
    if (state_->status_.closed) return true;
    ++state_->status_.in_flight;
  }
  bool result = delegate_->Shutdown(Remaining(deadline));
  {
    std::lock_guard<std::mutex> guard(state_->mutex_);
    --state_->status_.in_flight;
    state_->status_.closed = result;
  }
  return result;
}
}  // namespace otelc::native
