#include "otelc/metric_control.h"

#include <cassert>
#include <condition_variable>
#include <cstdio>
#include <future>
#include <map>
#include <stdexcept>
#include <string>
#include <thread>

#include "opentelemetry/metrics/observer_result.h"
#include "opentelemetry/sdk/metrics/metric_reader.h"
#include "opentelemetry/sdk/trace/simple_processor.h"
#include "opentelemetry/sdk/trace/span_data.h"
#include "opentelemetry/sdk/trace/tracer_provider.h"

namespace control = otelc::native;
namespace metrics = opentelemetry::sdk::metrics;
namespace traces = opentelemetry::sdk::trace;
namespace common = opentelemetry::sdk::common;
using namespace std::chrono_literals;

std::map<std::string, int64_t> Sums(const metrics::ResourceMetrics &resource) {
  std::map<std::string, int64_t> result;
  for (const auto &scope : resource.scope_metric_data_) for (const auto &metric : scope.metric_data_) {
    for (const auto &point : metric.point_data_attr_) {
      if (opentelemetry::nostd::holds_alternative<metrics::SumPointData>(point.point_data)) {
        const auto &sum = opentelemetry::nostd::get<metrics::SumPointData>(point.point_data);
        result[metric.instrument_descriptor.name_] = opentelemetry::nostd::get<int64_t>(sum.value_);
      }
    }
  }
  return result;
}

struct Sink : metrics::PushMetricExporter {
  std::atomic<unsigned> exports{0}, flushes{0}, shutdowns{0}, active{0};
  bool success = true, shutdown_success = true, flush_success = true;
  std::mutex mutex;
  std::condition_variable condition;
  bool block = false, entered = false;
  std::map<std::string, int64_t> last;
  common::ExportResult Export(const metrics::ResourceMetrics &data) noexcept override {
    assert(active.fetch_add(1) == 0);
    {
      std::unique_lock<std::mutex> guard(mutex);
      entered = true;
      ++exports;
      condition.notify_all();
      assert(condition.wait_for(guard, 5s, [&] { return !block; }));
      last = Sums(data);
    }
    --active;
    return success ? common::ExportResult::kSuccess : common::ExportResult::kFailure;
  }
  metrics::AggregationTemporality GetAggregationTemporality(metrics::InstrumentType) const noexcept override {
    return metrics::AggregationTemporality::kDelta;
  }
  bool ForceFlush(std::chrono::microseconds timeout) noexcept override {
    assert(timeout >= 0us && active.fetch_add(1) == 0);
    ++flushes; --active; return flush_success;
  }
  bool Shutdown(std::chrono::microseconds timeout) noexcept override {
    assert(timeout >= 0us && active.fetch_add(1) == 0);
    ++shutdowns; --active; return shutdown_success;
  }
  void Wait() {
    std::unique_lock<std::mutex> guard(mutex);
    assert(condition.wait_for(guard, 5s, [&] { return entered; }));
  }
  void Release() {
    std::lock_guard<std::mutex> guard(mutex);
    block = false; condition.notify_all();
  }
};

struct Reader : metrics::MetricReader {
  metrics::AggregationTemporality GetAggregationTemporality(metrics::InstrumentType) const noexcept override {
    return metrics::AggregationTemporality::kCumulative;
  }
  bool OnForceFlush(std::chrono::microseconds) noexcept override { return true; }
  bool OnShutDown(std::chrono::microseconds) noexcept override { return true; }
};

struct Pipeline {
  std::shared_ptr<metrics::MeterProvider> provider = std::make_shared<metrics::MeterProvider>();
  std::shared_ptr<control::MetricState> state;
  control::MetricController controller;
  std::shared_ptr<Reader> reader = std::make_shared<Reader>();
  Sink *sink;
  control::GatedMetricExporter exporter;
  explicit Pipeline(bool collection, bool emission)
      : state(std::make_shared<control::MetricState>(collection, emission)),
        controller(provider, state), sink(new Sink), exporter(std::unique_ptr<Sink>(sink), state) {
    provider->AddMetricReader(reader, controller.Filter());
  }
  std::map<std::string, int64_t> Collect() {
    std::map<std::string, int64_t> values;
    assert(reader->Collect([&](metrics::ResourceMetrics &resource) {
      values = Sums(resource);
      exporter.Export(resource);
      return true;
    }));
    return values;
  }
};

void FourStatesAndCachedInstruments() {
  for (bool collection : {false, true}) for (bool emission : {false, true}) {
    Pipeline pipeline(collection, emission);
    auto meter = pipeline.provider->GetMeter("otelc.native.test");
    auto counter = meter->CreateUInt64Counter("calls");
    int callbacks = 0;
    auto observable = meter->CreateInt64ObservableGauge("observed");
    observable->AddCallback([](opentelemetry::metrics::ObserverResult result, void *state) {
      ++*static_cast<int *>(state);
      opentelemetry::nostd::get<opentelemetry::nostd::shared_ptr<
          opentelemetry::metrics::ObserverResultT<int64_t>>>(result)->Observe(7);
    }, &callbacks);
    counter->Add(1);
    auto first = pipeline.Collect();
    assert(collection ? first.at("calls") == 1 : first.empty());
    assert(callbacks == (collection ? 1 : 0));
    assert(pipeline.sink->exports == (collection && emission ? 1u : 0u));
    assert(pipeline.state->SetEmission(true));
    assert(pipeline.state->Status().collection == collection);
    assert(pipeline.controller.SetCollection(true));
    counter->Add(2);
    pipeline.Collect();
    int64_t expected = (collection ? 1 : 0) + 2;
    assert(pipeline.sink->last.at("calls") == expected);

    assert(pipeline.state->SetEmission(false));
    auto sent = pipeline.sink->exports.load();
    counter->Add(5);
    assert(pipeline.Collect().at("calls") == expected + 5);
    assert(pipeline.sink->exports == sent);
    assert(pipeline.exporter.ForceFlush(1s) && pipeline.sink->flushes == 0);
    assert(pipeline.state->SetEmission(true));
    pipeline.Collect();
    assert(pipeline.sink->last.at("calls") == expected + 5);

    assert(pipeline.controller.SetCollection(false));
    int before = callbacks;
    sent = pipeline.sink->exports;
    auto late = pipeline.provider->GetMeter("otelc.native.late")->CreateUInt64Counter("late");
    counter->Add(1000); late->Add(1000);
    assert(pipeline.Collect().empty() && callbacks == before && pipeline.sink->exports == sent);
    assert(pipeline.state->Status().emission);
    assert(pipeline.controller.SetCollection(true));
    late->Add(3);
    pipeline.Collect();
    assert(pipeline.sink->last.at("calls") == expected + 5 && pipeline.sink->last.at("late") == 3);
    assert(pipeline.exporter.GetAggregationTemporality(metrics::InstrumentType::kCounter) ==
           metrics::AggregationTemporality::kCumulative);
    assert(pipeline.provider->Shutdown(1s));
    assert(pipeline.exporter.Shutdown(1s));
  }
}

void ExportFailureRetainsCumulativeValues() {
  Pipeline pipeline(true, true);
  auto counter = pipeline.provider->GetMeter("otelc.native.retry")->CreateUInt64Counter("calls");
  counter->Add(12);
  pipeline.sink->success = false;
  pipeline.Collect();
  auto failed = pipeline.state->Status();
  assert(failed.attempted == 1 && failed.completed == 1 && failed.failed == 1 && failed.in_flight == 0);
  pipeline.sink->success = true;
  pipeline.Collect();
  assert(pipeline.sink->last.at("calls") == 12);
  assert(pipeline.state->Status().attempted == 2 && pipeline.state->Status().failed == 1);
  pipeline.sink->flush_success = false;
  assert(!pipeline.exporter.ForceFlush(1s));
  pipeline.sink->flush_success = true;
  assert(pipeline.exporter.ForceFlush() && pipeline.sink->flushes == 2);
  pipeline.sink->shutdown_success = false;
  assert(!pipeline.exporter.Shutdown(1s) && !pipeline.state->Status().closed);
  pipeline.sink->shutdown_success = true;
  assert(pipeline.exporter.Shutdown() && pipeline.state->Status().closed);
  assert(pipeline.exporter.Shutdown() && pipeline.sink->shutdowns == 2);
  assert(!pipeline.exporter.ForceFlush(1s));
  assert(!pipeline.state->SetEmission(true) && !pipeline.controller.SetCollection(true));
}

void InFlightAndQueuedCallsCannotBypassDisableOrShutdown() {
  auto state = std::make_shared<control::MetricState>(true, true);
  auto sink = std::make_unique<Sink>(); auto *raw = sink.get(); raw->block = true;
  control::GatedMetricExporter exporter(std::move(sink), state);
  metrics::ResourceMetrics data;
  std::thread first([&] { assert(exporter.Export(data) == common::ExportResult::kSuccess); });
  raw->Wait();
  assert(state->Status().in_flight == 1 && state->Status().attempted == 1);
  assert(state->SetEmission(false));
  std::thread queued([&] { assert(exporter.Export(data) == common::ExportResult::kFailure); });
  assert(!exporter.ForceFlush(0us));
  assert(!exporter.Shutdown(0us));
  assert(state->Status().closing && !state->Status().closed && state->Status().in_flight == 1);
  raw->Release(); first.join(); queued.join();
  assert(raw->exports == 1 && state->Status().in_flight == 0);
  assert(exporter.Shutdown(1s) && raw->shutdowns == 1);
}

void QueuedExportRechecksEmissionWithoutClosing() {
  auto state = std::make_shared<control::MetricState>(true, true);
  auto sink = std::make_unique<Sink>(); auto *raw = sink.get(); raw->block = true;
  control::GatedMetricExporter exporter(std::move(sink), state);
  metrics::ResourceMetrics data;
  std::thread first([&] { assert(exporter.Export(data) == common::ExportResult::kSuccess); });
  raw->Wait();
  std::thread second([&] {
    assert(exporter.Export(data) == common::ExportResult::kSuccess);
  });
  const auto deadline = std::chrono::steady_clock::now() + 5s;
  while (state->Status().queued != 1 && std::chrono::steady_clock::now() < deadline)
    std::this_thread::yield();
  assert(state->Status().queued == 1);
  assert(state->SetEmission(false) && !state->Status().closing);
  raw->Release(); first.join(); second.join();
  auto status = state->Status();
  assert(raw->exports == 1 && status.attempted == 1 && status.suppressed == 1 &&
         status.in_flight == 0 && status.queued == 0);
  assert(exporter.ForceFlush(1s) && raw->flushes == 0);
  assert(state->SetEmission(true));
  assert(exporter.Export(data) == common::ExportResult::kSuccess && raw->exports == 2);
  assert(exporter.Shutdown(1s));
}

void ConcurrentCollectionUpdatesPreserveStatusCallbacks() {
  Pipeline pipeline(true, true);
  auto meter = pipeline.provider->GetMeter("otelc.native.concurrent");
  auto counter = meter->CreateUInt64Counter("calls");
  auto observable = meter->CreateInt64ObservableGauge("status");
  observable->AddCallback([](opentelemetry::metrics::ObserverResult result, void *state) {
    auto snapshot = static_cast<control::MetricState *>(state)->Status();
    opentelemetry::nostd::get<opentelemetry::nostd::shared_ptr<
        opentelemetry::metrics::ObserverResultT<int64_t>>>(result)->Observe(snapshot.collection ? 1 : 0);
  }, pipeline.state.get());
  std::atomic<bool> start{false};
  std::thread recording([&] {
    while (!start.load()) std::this_thread::yield();
    for (unsigned i = 0; i < 1000; ++i) counter->Add(1);
  });
  std::thread collecting([&] {
    while (!start.load()) std::this_thread::yield();
    for (unsigned i = 0; i < 100; ++i) pipeline.Collect();
  });
  start = true;
  for (unsigned i = 0; i < 100; ++i) assert(pipeline.controller.SetCollection(i % 2 == 0));
  recording.join(); collecting.join();
  assert(pipeline.controller.SetCollection(true));
  auto values = pipeline.Collect();
  assert(values.empty() || (values.at("calls") >= 0 && values.at("calls") <= 1000));
  assert(pipeline.provider->Shutdown(1s) && pipeline.exporter.Shutdown(1s));
}

void ShutdownBudgetIncludesPendingCollectionUpdates() {
  Pipeline pipeline(true, true);
  std::mutex mutex;
  std::condition_variable condition;
  bool entered = false, released = false;
  using Configurator = opentelemetry::sdk::instrumentationscope::ScopeConfigurator<metrics::MeterConfig>;
  auto builder = Configurator::Builder(metrics::MeterConfig::Enabled());
  builder.AddCondition([&](const auto &) {
    std::unique_lock<std::mutex> guard(mutex);
    entered = true; condition.notify_all();
    assert(condition.wait_for(guard, 5s, [&] { return released; }));
    return true;
  }, metrics::MeterConfig::Enabled());
  pipeline.provider->UpdateMeterConfigurator(std::make_unique<Configurator>(builder.Build()));
  std::thread SDK([&] { pipeline.provider->GetMeter("blocked.sdk.configuration"); });
  {
    std::unique_lock<std::mutex> guard(mutex);
    assert(condition.wait_for(guard, 5s, [&] { return entered; }));
  }
  auto filter = pipeline.controller.Filter();
  auto scope = opentelemetry::sdk::instrumentationscope::InstrumentationScope::Create("probe");
  std::thread updating([&] { assert(!pipeline.controller.SetCollection(false)); });
  auto deadline = std::chrono::steady_clock::now() + 5s;
  while (filter->TestMetric(*scope, "probe", metrics::InstrumentType::kCounter, "1") !=
             metrics::MetricFilter::MetricFilterResult::kDrop && std::chrono::steady_clock::now() < deadline)
    std::this_thread::yield();
  assert(filter->TestMetric(*scope, "probe", metrics::InstrumentType::kCounter, "1") ==
         metrics::MetricFilter::MetricFilterResult::kDrop);
  auto shutdown = std::async(std::launch::async, [&] { return pipeline.exporter.Shutdown(2ms); });
  assert(shutdown.wait_for(1s) == std::future_status::ready && !shutdown.get());
  assert(pipeline.state->Status().closing && !pipeline.state->Status().closed);
  {
    std::lock_guard<std::mutex> guard(mutex);
    released = true; condition.notify_all();
  }
  SDK.join(); updating.join();
  assert(!pipeline.state->Status().collection);
  assert(pipeline.exporter.Shutdown(1s) && pipeline.sink->shutdowns == 1);
}

struct SpanSink : traces::SpanExporter {
  unsigned spans = 0;
  std::unique_ptr<traces::Recordable> MakeRecordable() noexcept override { return std::make_unique<traces::SpanData>(); }
  common::ExportResult Export(const opentelemetry::nostd::span<std::unique_ptr<traces::Recordable>> &batch) noexcept override {
    spans += batch.size(); return common::ExportResult::kSuccess;
  }
  bool ForceFlush(std::chrono::microseconds) noexcept override { return true; }
  bool Shutdown(std::chrono::microseconds) noexcept override { return true; }
};

void TracingIsIndependent() {
  auto sink = std::make_unique<SpanSink>(); auto *raw = sink.get();
  traces::TracerProvider provider(std::make_unique<traces::SimpleSpanProcessor>(std::move(sink)));
  auto tracer = provider.GetTracer("otelc.native.trace-test");
  Pipeline metrics(true, true);
  for (bool collection : {false, true}) for (bool emission : {false, true}) {
    assert(metrics.controller.SetCollection(collection) && metrics.state->SetEmission(emission));
    auto span = tracer->StartSpan("unaffected");
    assert(span->IsRecording()); span->End();
  }
  assert(raw->spans == 4 && provider.Shutdown(1s));
}

template<class Callable> void Invalid(Callable callable) {
  bool rejected = false;
  try { callable(); } catch (const std::invalid_argument &) { rejected = true; }
  assert(rejected);
}

void InvalidConstructionRefuses() {
  auto state = std::make_shared<control::MetricState>(false, false);
  auto provider = std::make_shared<metrics::MeterProvider>();
  Invalid([&] { control::MetricController value(nullptr, state); });
  Invalid([&] { control::MetricController value(provider, nullptr); });
  Invalid([&] { control::GatedMetricExporter value(nullptr, state); });
  Invalid([&] { control::GatedMetricExporter value(std::make_unique<Sink>(), nullptr); });
  control::GatedMetricExporter exporter(std::make_unique<Sink>(), state);
  assert(exporter.Shutdown(0us));
  Invalid([&] { control::MetricController value(provider, state); });
}

int main() {
  FourStatesAndCachedInstruments();
  ExportFailureRetainsCumulativeValues();
  InFlightAndQueuedCallsCannotBypassDisableOrShutdown();
  QueuedExportRechecksEmissionWithoutClosing();
  ConcurrentCollectionUpdatesPreserveStatusCallbacks();
  ShutdownBudgetIncludesPendingCollectionUpdates();
  TracingIsIndependent();
  InvalidConstructionRefuses();
  std::puts("native metric controls: eight contract groups passed");
}
