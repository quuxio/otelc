#include "otelc/object_runtime.h"
#include "otelc/function_runtime.h"
#include <cassert>
#include <array>
#include <chrono>
#include <cerrno>
#include <cstdio>
#include <functional>
#include <future>
#include <map>
#include <set>
#include <thread>
#include <vector>
#include "opentelemetry/sdk/metrics/metric_reader.h"
#include "opentelemetry/sdk/trace/simple_processor.h"
#include "opentelemetry/sdk/trace/span_data.h"
#include "opentelemetry/sdk/trace/tracer_provider.h"

namespace native = otelc::native;
namespace metrics = opentelemetry::sdk::metrics;
namespace traces = opentelemetry::sdk::trace;
using namespace std::chrono_literals;
using Values = std::map<std::string, std::uint64_t>;
#ifdef OTELC_HAVE_OBJECT_FIXTURES
extern "C" int object_fixture_run();
extern "C" int object_fixture_reuse();
extern "C" int object_fixture_failed();
extern "C" int object_fixture_unwind();
extern "C" void *object_fixture_make();
extern "C" void object_fixture_delete(void *);
extern "C" int object_fixture_throwing_destructor();
#endif
namespace {
#ifdef OTELC_HAVE_OBJECT_FIXTURES
native::ObjectRuntime *installed_objects = nullptr;
native::FunctionRuntime *installed_functions = nullptr;
#ifdef OTELC_OBJECT_FUNCTION_PROFILE
std::array<otelc_function_descriptor_v1, 64> function_descriptors;
std::uint64_t descriptor_count = 0;
extern "C" void otelc_function_register_v1(const otelc_function_descriptor_v1 *items, std::uint64_t count) noexcept {
  assert(items && descriptor_count + count <= function_descriptors.size());
  for (std::uint64_t index = 0; index < count; ++index) function_descriptors[descriptor_count++] = items[index];
}
extern "C" std::uint64_t otelc_function_enter_v1(const void *address) noexcept {
  assert(installed_functions);
  return installed_functions->Enter(address);
}
extern "C" void otelc_function_leave_v1(std::uint64_t token, std::int32_t unwinding) noexcept {
  assert(installed_functions && installed_functions->Leave(token, unwinding));
}
#endif
extern "C" void otelc_object_created_v1(const void *address, const char *type, const char *name) noexcept {
  assert(installed_objects && installed_functions);
  assert(installed_objects->Admit(address, type, name, installed_functions->CurrentContext()));
}
extern "C" std::uint64_t otelc_object_destroying_v1(const void *address, const char *type) noexcept {
  assert(installed_objects);
  return installed_objects->Destroying(address, type);
}
extern "C" void otelc_object_destroyed_v1(std::uint64_t token, std::int32_t unwinding) noexcept {
  assert(installed_objects && installed_objects->Destroyed(token, unwinding));
}
#endif
struct Reader : metrics::MetricReader {
  metrics::AggregationTemporality GetAggregationTemporality(metrics::InstrumentType) const noexcept override {
    return metrics::AggregationTemporality::kCumulative;
  }
  bool OnForceFlush(std::chrono::microseconds) noexcept override { return true; }
  bool OnShutDown(std::chrono::microseconds) noexcept override { return true; }
};
Values Counts(const metrics::ResourceMetrics &data) {
  Values values;
  for (const auto &scope : data.scope_metric_data_) for (const auto &metric : scope.metric_data_)
    for (const auto &point : metric.point_data_attr_) {
      const auto &name = metric.instrument_descriptor.name_;
      if (opentelemetry::nostd::holds_alternative<metrics::SumPointData>(point.point_data))
        values[name] += opentelemetry::nostd::get<int64_t>(opentelemetry::nostd::get<metrics::SumPointData>(point.point_data).value_);
      else if (opentelemetry::nostd::holds_alternative<metrics::LastValuePointData>(point.point_data))
        values[name] += opentelemetry::nostd::get<int64_t>(opentelemetry::nostd::get<metrics::LastValuePointData>(point.point_data).value_);
      else {
        const auto &histogram = opentelemetry::nostd::get<metrics::HistogramPointData>(point.point_data);
        assert(opentelemetry::nostd::get<double>(histogram.sum_) >= 0);
        values[name] += histogram.count_;
      }
      for (const auto &[key, _] : point.attributes) assert(key != "address" && key != "otelc.lifetime.instance");
    }
  return values;
}
struct MetricSink : metrics::PushMetricExporter {
  Values values;
  unsigned exports = 0;
  opentelemetry::sdk::common::ExportResult Export(const metrics::ResourceMetrics &data) noexcept override {
    values = Counts(data); ++exports;
    return opentelemetry::sdk::common::ExportResult::kSuccess;
  }
  metrics::AggregationTemporality GetAggregationTemporality(metrics::InstrumentType) const noexcept override {
    return metrics::AggregationTemporality::kCumulative;
  }
  bool ForceFlush(std::chrono::microseconds) noexcept override { return true; }
  bool Shutdown(std::chrono::microseconds) noexcept override { return true; }
};
struct SpanSink : traces::SpanExporter {
  std::mutex mutex;
  std::vector<std::unique_ptr<traces::SpanData>> spans;
  std::function<void()> callback;
  std::function<void()> create_callback;
  std::unique_ptr<traces::Recordable> MakeRecordable() noexcept override {
    if (create_callback) create_callback();
    return std::make_unique<traces::SpanData>();
  }
  opentelemetry::sdk::common::ExportResult Export(
      const opentelemetry::nostd::span<std::unique_ptr<traces::Recordable>> &batch) noexcept override {
    if (callback) callback();
    std::lock_guard<std::mutex> lock(mutex);
    for (auto &item : batch) spans.emplace_back(static_cast<traces::SpanData *>(item.release()));
    return opentelemetry::sdk::common::ExportResult::kSuccess;
  }
  bool ForceFlush(std::chrono::microseconds) noexcept override { return true; }
  bool Shutdown(std::chrono::microseconds) noexcept override { return true; }
};
struct World {
  std::shared_ptr<metrics::MeterProvider> meters = std::make_shared<metrics::MeterProvider>();
  std::shared_ptr<native::MetricState> state;
  native::MetricController controller;
  std::shared_ptr<Reader> reader = std::make_shared<Reader>();
  MetricSink *metric_sink;
  native::GatedMetricExporter exporter;
  SpanSink *span_sink;
  std::shared_ptr<traces::TracerProvider> traces;
  explicit World(bool collection = true, bool emission = true)
      : state(std::make_shared<native::MetricState>(collection, emission)), controller(meters, state),
        metric_sink(new MetricSink), exporter(std::unique_ptr<MetricSink>(metric_sink), state),
        span_sink(new SpanSink), traces(std::make_shared<traces::TracerProvider>(
            std::make_unique<traces::SimpleSpanProcessor>(std::unique_ptr<SpanSink>(span_sink)))) {
    native::ConfigureObjectMetrics(*meters);
    native::ConfigureFunctionMetrics(*meters);
    meters->AddMetricReader(reader, controller.Filter());
  }
  std::unique_ptr<native::ObjectRuntime> Engine(native::ObjectLimits limits = {}, bool tracing = true) {
    return std::make_unique<native::ObjectRuntime>(state, meters->GetMeter(native::FunctionScopeName),
        tracing ? traces->GetTracer(native::FunctionScopeName) : nullptr, limits);
  }
  Values Collect() {
    Values values;
    assert(reader->Collect([&](metrics::ResourceMetrics &data) {
      values = Counts(data); exporter.Export(data); return true;
    }));
    return values;
  }
  ~World() { assert(exporter.Shutdown(1s) && traces->Shutdown(1s) && meters->Shutdown(1s)); }
};
void Finish(native::ObjectRuntime &engine, const void *address, const char *type = "usr:Item") {
  auto token = engine.Destroying(address, type);
  assert(token && engine.Destroyed(token, 0));
}
void CompletedSpanHasCreationLinkAndEndsBeforeCleanup() {
  World world;
  auto engine = world.Engine();
  int object = 0;
  auto parent = world.traces->GetTracer("application")->StartSpan("creator");
  const auto creation = parent->GetContext();
  const auto before_admission = std::chrono::steady_clock::now();
  assert(engine->Admit(&object, "usr:Item", "demo::Item", creation));
  parent->End();
  auto before = world.Collect();
  assert(before.at("otelc.lifetime.live") == 1);
  auto token = engine->Destroying(&object, "usr:Item");
  const auto entry_upper_bound = std::chrono::steady_clock::now() - before_admission;
  assert(token && engine->Status().destroying == 1 && engine->Status().live == 0);
  assert(world.span_sink->spans.size() == 1);
  assert(world.Collect().at("otelc.lifetime.live") == 0);
  // Keep cleanup open longer than the entire observed pre-cleanup interval.
  // Use the same monotonic clock as the SDK duration, not wall-clock alignment.
  std::this_thread::sleep_for(entry_upper_bound + 1ms);
  assert(engine->Destroyed(token, 0));
  assert(world.span_sink->spans.size() == 2);
  const auto &span = *world.span_sink->spans.back();
  assert(span.GetName() == "demo::Item lifetime");
  assert(!span.GetParentSpanId().IsValid());
  assert(span.GetLinks().size() == 1 && span.GetLinks()[0].GetSpanContext() == creation);
  assert(span.GetDuration().count() >= 0);
  assert(span.GetDuration() <= entry_upper_bound);
  const auto &attributes = span.GetAttributes();
  assert(opentelemetry::nostd::get<std::string>(attributes.at("boundary")) == "destructor_entry");
  assert(opentelemetry::nostd::get<bool>(attributes.at("otelc.lifetime.complete")));
  auto values = world.Collect();
  assert(values.at("otelc.lifetime.admitted") == 1 && values.at("otelc.lifetime.completed") == 1);
  assert(values.at("otelc.lifetime.duration") == 1);
  assert(engine->Close() && engine->Close());
}
void GenerationsNestedVariantsAndCrossThreadDestruction() {
  World world;
  auto engine = world.Engine();
  int object = 0;
  assert(engine->Admit(&object, "usr:Item", "Item"));
  auto old = engine->Destroying(&object, "usr:Item");
  assert(old && engine->Destroying(&object, "usr:Item") == 0 && engine->Destroyed(0, 0));
  assert(engine->Admit(&object, "usr:Item", "Item"));
  assert(engine->Destroyed(old, 0));
  assert(engine->Status().live == 1);
  std::uint64_t next = 0;
  std::thread other([&] { next = engine->Destroying(&object, "usr:Item"); });
  other.join();
  assert(next != 0 && next != old && engine->Destroyed(next, 1));
  assert(engine->Status().completed == 2 && engine->Status().lost == 0);
  assert(world.span_sink->spans.size() == 2);
  std::set<std::string> instances;
  for (const auto &span : world.span_sink->spans)
    instances.insert(opentelemetry::nostd::get<std::string>(span->GetAttributes().at("otelc.lifetime.instance")));
  assert(instances.size() == 2);
}
void EqualDisplayNamesAggregateLiveObjectsWithoutMergingIdentities() {
  World world;
  auto engine = world.Engine();
  int first = 0, second = 0;
  assert(engine->Admit(&first, "usr:first:Local", "Local"));
  assert(engine->Admit(&second, "usr:second:Local", "Local"));
  assert(world.Collect().at("otelc.lifetime.live") == 2);
  Finish(*engine, &first, "usr:first:Local");
  assert(world.Collect().at("otelc.lifetime.live") == 1);
  Finish(*engine, &second, "usr:second:Local");
  assert(world.Collect().at("otelc.lifetime.live") == 0);
  assert(engine->Status().types == 2 && engine->Status().completed == 2);
}
void CloseWaitsForClaimedCleanupThenCensorsOnlyOpenObservations() {
  World world;
  auto engine = world.Engine();
  int first = 0, second = 0, refused = 0;
  assert(engine->Admit(&first, "usr:Item", "Item") && engine->Admit(&second, "usr:Item", "Item"));
  auto token = engine->Destroying(&first, "usr:Item");
  assert(!engine->Close());
  assert(!engine->Admit(&refused, "usr:Item", "Item"));
  assert(engine->Destroyed(token, 0) && engine->Close());
  const auto status = engine->Status();
  assert(status.admitted == 2 && status.completed == 1 && status.censored == 1);
  assert(status.live == 0 && status.destroying == 0 && status.ending == 0 && status.closed);
  assert(engine->Destroying(&second, "usr:Item") == 0);
  assert(world.span_sink->spans.size() == 2);
  const auto &attributes = world.span_sink->spans.back()->GetAttributes();
  assert(!opentelemetry::nostd::get<bool>(attributes.at("otelc.lifetime.complete")));
  assert(opentelemetry::nostd::get<std::string>(attributes.at("boundary")) == "shutdown_censored");
  auto values = world.Collect();
  assert(values.at("otelc.lifetime.completed") == 1 && values.at("otelc.lifetime.incomplete") == 1);
}
void PendingSDKExportStillOccupiesTheObservationBudget() {
  World world;
  auto engine = world.Engine({1, 1});
  int first = 0, second = 0;
  std::promise<void> entered, release;
  auto entered_future = entered.get_future();
  auto released = release.get_future();
  world.span_sink->callback = [&] { entered.set_value(); released.wait(); };
  assert(engine->Admit(&first, "usr:Item", "Item"));
  std::thread worker([&] { Finish(*engine, &first); });
  assert(entered_future.wait_for(2s) == std::future_status::ready);
  assert(engine->Status().ending == 1);
  assert(!engine->Admit(&second, "usr:Item", "Item"));
  release.set_value();
  worker.join();
  world.span_sink->callback = {};
  assert(engine->Admit(&second, "usr:Item", "Item"));
  Finish(*engine, &second);
  assert(engine->Status().completed == 2 && engine->Status().lost == 1);
  assert(engine->Close());
}
void AllLiveStatesAndStartupEnableKeepTracesIndependent() {
  for (bool collecting : {false, true}) for (bool emitting : {false, true}) {
    World world(collecting, emitting);
    auto engine = world.Engine();
    int object = 0;
    auto run = [&] { assert(engine->Admit(&object, "usr:Item", "Item")); Finish(*engine, &object); };
    run();
    auto values = world.Collect();
    assert(collecting ? values.at("otelc.lifetime.completed") == 1 : values.empty());
    assert(world.metric_sink->exports == unsigned(collecting && emitting));
    assert(world.span_sink->spans.size() == 1);
    assert(world.controller.SetCollection(true) && world.state->SetEmission(true));
    run();
    world.Collect();
    auto expected = unsigned(collecting) + 1;
    assert(world.metric_sink->values.at("otelc.lifetime.completed") == expected);
    assert(world.state->SetEmission(false));
    run(); world.Collect();
    assert(world.metric_sink->values.at("otelc.lifetime.completed") == expected);
    assert(world.state->SetEmission(true)); world.Collect();
    assert(world.metric_sink->values.at("otelc.lifetime.completed") == expected + 1);
    assert(world.controller.SetCollection(false));
    run(); assert(world.Collect().empty());
    assert(world.controller.SetCollection(true));
    world.Collect();
    assert(world.metric_sink->values.at("otelc.lifetime.completed") == expected + 1);
    assert(world.span_sink->spans.size() == 4);
    assert(engine->Status().admitted == 4 && engine->Status().completed == 4);
    assert(engine->Close());
  }
}
void LimitsInvalidInputsAndExporterReentryAreVisible() {
  World world;
  auto engine = world.Engine({1, 2});
  int first = 0, second = 0, third = 0;
  assert(engine->Admit(&first, "usr:Item", "Item"));
  assert(!engine->Admit(&first, "usr:Item", "Item"));
  assert(engine->Status().duplicates == 1);
  assert(!engine->Admit(&second, "usr:Item", "wrong label"));
  assert(!engine->Admit(&second, "usr:Other", "Other"));
  assert(engine->Admit(&second, "usr:Item", "Item"));
  assert(!engine->Admit(&third, "usr:Item", "Item"));
  assert(!engine->Admit(nullptr, "usr:Item", "Item"));
  assert(!engine->Admit(&third, nullptr, "Item"));
  assert(!engine->Admit(&third, "usr:Item", ""));
  auto token = engine->Destroying(&first, "usr:Item");
  assert(!engine->Destroyed(token, 2));
  assert(!engine->Destroyed(1000000, 0));
  assert(engine->Destroying(&first, nullptr) == 0);
  assert(engine->Destroying(&first, "usr:Other") == 0);
  assert(engine->Destroying(&first, "usr:Item") == 0);
  world.span_sink->callback = [&] {
    assert(!engine->Admit(&third, "usr:Item", "Item"));
    assert(!engine->Close());
    assert(!engine->Destroyed(42, 0));
    assert(engine->Destroying(&second, "usr:Item") == 0);
    assert(engine->Status().ending == 1);
  };
  assert(engine->Destroyed(token, 0));
  world.span_sink->callback = {};
  Finish(*engine, &second);
  assert(engine->Status().lost >= 6 && engine->Status().invalid >= 4);
  assert(engine->Close());
  for (auto limits : {native::ObjectLimits{0,1}, native::ObjectLimits{4097,1}, native::ObjectLimits{1,0}, native::ObjectLimits{1,1000001}}) {
    bool refused = false;
    try { auto bad = world.Engine(limits); } catch (const std::invalid_argument &) { refused = true; }
    assert(refused);
  }
  for (auto boundaries : {std::vector<double>{}, {0.0}, {1.0, 0.5}, {1.0, 1.0}}) {
    bool refused = false;
    try { native::ConfigureObjectMetrics(*world.meters, 1, boundaries); }
    catch (const std::invalid_argument &) { refused = true; }
    assert(refused);
  }
  auto no_traces = world.Engine({}, false);
  assert(no_traces->Admit(&third, "usr:NoTrace", "NoTrace"));
  Finish(*no_traces, &third, "usr:NoTrace");
  assert(no_traces->Close());
}
void ObjectProbesPreserveErrnoIncludingCensoredShutdown() {
  for (bool collection : {false, true}) for (bool emission : {false, true}) {
    for (int unwinding : {0, 1}) {
      World world(collection, emission);
      auto engine = world.Engine();
      int first = 0, second = 0, recursive = 0;
      unsigned created = 0, exported = 0;
      world.span_sink->create_callback = [&] { ++created; errno = ERANGE; };
      world.span_sink->callback = [&] {
        ++exported;
        errno = EOVERFLOW;
        assert(!engine->Admit(&recursive, "usr:Item", "Item") && errno == EOVERFLOW);
        assert(!engine->Destroying(&recursive, "usr:Item") && errno == EOVERFLOW);
        assert(!engine->Destroyed(42, 0) && errno == EOVERFLOW);
        assert(!engine->Close() && errno == EOVERFLOW);
      };
      errno = EINTR;
      assert(engine->Admit(&first, "usr:Item", "Item") && errno == EINTR);
      errno = EDOM;
      auto token = engine->Destroying(&first, "usr:Item");
      assert(token && errno == EDOM);
      errno = ECHILD;
      assert(engine->Destroyed(token, unwinding) && errno == ECHILD);
      assert(created == 1 && exported == 1);
      errno = EBUSY;
      assert(!engine->Destroyed(token, 0) && errno == EBUSY);
      assert(!engine->Admit(nullptr, "usr:Item", "Item") && errno == EBUSY);
      assert(engine->Destroyed(0, 0) && errno == EBUSY);
      assert(engine->Admit(&second, "usr:Item", "Item") && errno == EBUSY);
      errno = E2BIG;
      assert(engine->Close() && errno == E2BIG);
      assert(created == 2 && exported == 2);
      auto status = engine->Status();
      assert(status.closed && status.admitted == 2 && status.completed == 1 && status.censored == 1);
    }
  }
}

void CompilerInsertedObjectBoundariesUseTheSDK() {
#ifdef OTELC_HAVE_OBJECT_FIXTURES
  World world;
  auto objects = world.Engine();
  native::FunctionRuntime functions(world.state, world.meters->GetMeter(native::FunctionScopeName),
      world.traces->GetTracer(native::FunctionScopeName), {16, 16, native::FunctionContext::owned});
  int marker = 0;
  otelc_function_descriptor_v1 descriptor{&marker, "fixture creator"};
  assert(functions.Register(&descriptor, 1));
#ifdef OTELC_OBJECT_FUNCTION_PROFILE
  assert(descriptor_count == 7 && functions.Register(function_descriptors.data(), descriptor_count));
#endif
  assert(!functions.CurrentContext().IsValid());
  installed_objects = objects.get();
  installed_functions = &functions;
  auto token = functions.Enter(&marker);
  auto context = functions.CurrentContext();
  assert(token && context.IsValid());
  assert(object_fixture_run() == 8);
  assert(objects->Status().admitted == 6 && objects->Status().completed == 6);
  assert(object_fixture_reuse() == 3);
  assert(objects->Status().admitted == 8 && objects->Status().completed == 8);
  assert(object_fixture_failed() == 7);
  assert(objects->Status().admitted == 8);
  assert(object_fixture_unwind() == 7);
  auto *object = object_fixture_make();
  std::thread worker([&] { object_fixture_delete(object); });
  worker.join();
  assert(object_fixture_throwing_destructor() == 13);
  assert(functions.Leave(token, 0) && !functions.CurrentContext().IsValid());
  auto status = objects->Status();
  assert(status.admitted == 11 && status.completed == 11 && status.live == 0);
  assert(status.lost == 0 && status.invalid == 0 && status.duplicates == 0);
  unsigned lifetimes = 0;
  std::map<std::string, unsigned> creation_functions;
  for (const auto &span : world.span_sink->spans) if (span->GetName().find(" lifetime") != std::string::npos) {
    ++lifetimes;
    assert(span->GetName() == "native_object_fixture::Item lifetime" ||
           span->GetName() == "native_object_fixture::ThrowingItem lifetime");
    assert(opentelemetry::nostd::get<bool>(span->GetAttributes().at("otelc.destructor.exception_exit")) ==
           (span->GetName() == "native_object_fixture::ThrowingItem lifetime"));
    assert(span->GetLinks().size() == 1);
#ifdef OTELC_OBJECT_FUNCTION_PROFILE
    const auto &link = span->GetLinks()[0].GetSpanContext();
    unsigned matches = 0;
    for (const auto &creator : world.span_sink->spans)
      if (creator->GetTraceId() == link.trace_id() && creator->GetSpanId() == link.span_id()) {
        ++matches;
        ++creation_functions[std::string(creator->GetName())];
      }
    assert(matches == 1);
#else
    assert(span->GetLinks()[0].GetSpanContext() == context);
#endif
    assert(!span->GetParentSpanId().IsValid());
  }
  assert(lifetimes == 11);
#ifdef OTELC_OBJECT_FUNCTION_PROFILE
  assert(world.span_sink->spans.size() == 19);
  const std::map<std::string, unsigned> expected_creators{
      {"native-objects.cpp::object_fixture_run", 6}, {"native-objects.cpp::object_fixture_reuse", 2},
      {"native-objects.cpp::object_fixture_unwind", 1}, {"native-objects.cpp::object_fixture_make", 1},
      {"native-objects.cpp::object_fixture_throwing_destructor", 1}};
  assert(creation_functions == expected_creators);
#else
  assert(world.span_sink->spans.size() == 12);
#endif
  assert(objects->Close() && functions.Close());
  installed_objects = nullptr;
  installed_functions = nullptr;
  std::puts("native SDK objects: compiler-linked unchanged C++ fixture passed");
#endif
}
}  // namespace
int main() {
  CompletedSpanHasCreationLinkAndEndsBeforeCleanup();
  GenerationsNestedVariantsAndCrossThreadDestruction();
  EqualDisplayNamesAggregateLiveObjectsWithoutMergingIdentities();
  CloseWaitsForClaimedCleanupThenCensorsOnlyOpenObservations();
  PendingSDKExportStillOccupiesTheObservationBudget();
  AllLiveStatesAndStartupEnableKeepTracesIndependent();
  LimitsInvalidInputsAndExporterReentryAreVisible();
  ObjectProbesPreserveErrnoIncludingCensoredShutdown();
  CompilerInsertedObjectBoundariesUseTheSDK();
  std::puts("native SDK object lifetime checks passed");
}
