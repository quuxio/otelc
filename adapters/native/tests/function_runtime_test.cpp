#include "otelc/function_runtime.h"

#include <cassert>
#include <array>
#include <algorithm>
#include <cstdio>
#include <cerrno>
#include <functional>
#include <future>
#include <map>
#include <limits>
#include <set>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "opentelemetry/sdk/metrics/metric_reader.h"
#include "opentelemetry/sdk/trace/simple_processor.h"
#include "opentelemetry/sdk/trace/samplers/always_on.h"
#include "opentelemetry/sdk/trace/samplers/parent.h"
#include "opentelemetry/sdk/trace/span_data.h"
#include "opentelemetry/sdk/trace/tracer_provider.h"
#include "opentelemetry/trace/context.h"
#include "opentelemetry/trace/scope.h"

namespace native = otelc::native;
namespace metrics = opentelemetry::sdk::metrics;
namespace traces = opentelemetry::sdk::trace;
namespace trace = opentelemetry::trace;
using namespace std::chrono_literals;
using Values = std::map<std::string, std::uint64_t>;
#ifdef OTELC_HAVE_LLVM_FIXTURES
extern "C" int selected_c_transform(int value);
extern "C" void selected_c_bridge(void (*callback)(void));
int selected_cpp(int value);
int selected_cpp_outer(int value);
void selected_throw();
#endif

namespace engine_test {
int outer_address, inner_address, extra_address;

#ifdef OTELC_HAVE_LLVM_FIXTURES
// Internal tooling bootstrap. The application fixture files contain no probes,
// SDK calls, annotations, imports or guards. Their compiler-generated tables
// arrive before main, then this harness supplies the owned official SDK.
std::array<otelc_function_descriptor_v1, 32> descriptors;
std::uint64_t descriptor_count = 0;
native::FunctionRuntime *installed_runtime = nullptr;
extern "C" void otelc_function_register_v1(const otelc_function_descriptor_v1 *items, std::uint64_t count) noexcept {
  assert(descriptor_count + count <= descriptors.size());
  for (std::uint64_t index = 0; index < count; ++index) descriptors[descriptor_count++] = items[index];
}
extern "C" std::uint64_t otelc_function_enter_v1(const void *address) noexcept {
  assert(installed_runtime);
  return installed_runtime->Enter(address);
}
extern "C" void otelc_function_leave_v1(std::uint64_t token, std::int32_t unwinding) noexcept {
  assert(installed_runtime && installed_runtime->Leave(token, unwinding));
}
#endif

Values Counts(const metrics::ResourceMetrics &data) {
  Values values;
  for (const auto &scope : data.scope_metric_data_) for (const auto &metric : scope.metric_data_) {
    for (const auto &point : metric.point_data_attr_) {
      if (opentelemetry::nostd::holds_alternative<metrics::SumPointData>(point.point_data)) {
        values[metric.instrument_descriptor.name_] += opentelemetry::nostd::get<int64_t>(
            opentelemetry::nostd::get<metrics::SumPointData>(point.point_data).value_);
      } else {
        const auto &histogram = opentelemetry::nostd::get<metrics::HistogramPointData>(point.point_data);
        assert(opentelemetry::nostd::get<double>(histogram.sum_) >= 0);
        values[metric.instrument_descriptor.name_] += histogram.count_;
      }
    }
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

struct Reader : metrics::MetricReader {
  metrics::AggregationTemporality GetAggregationTemporality(metrics::InstrumentType) const noexcept override {
    return metrics::AggregationTemporality::kCumulative;
  }
  bool OnForceFlush(std::chrono::microseconds) noexcept override { return true; }
  bool OnShutDown(std::chrono::microseconds) noexcept override { return true; }
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
    std::lock_guard<std::mutex> guard(mutex);
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
  explicit World(bool collection = true, bool emission = true, bool configure = true,
                 std::unique_ptr<traces::Sampler> sampler = std::make_unique<traces::AlwaysOnSampler>())
      : state(std::make_shared<native::MetricState>(collection, emission)), controller(meters, state),
        metric_sink(new MetricSink), exporter(std::unique_ptr<MetricSink>(metric_sink), state),
        span_sink(new SpanSink), traces(std::make_shared<traces::TracerProvider>(
            std::make_unique<traces::SimpleSpanProcessor>(std::unique_ptr<SpanSink>(span_sink)),
            opentelemetry::sdk::resource::Resource::GetDefault(), std::move(sampler))) {
    if (configure) native::ConfigureFunctionMetrics(*meters);
    meters->AddMetricReader(reader, controller.Filter());
  }
  std::unique_ptr<native::FunctionRuntime> Engine(native::FunctionLimits limits = {}, bool tracing = true) {
    return std::make_unique<native::FunctionRuntime>(state, meters->GetMeter(native::FunctionScopeName),
        tracing ? traces->GetTracer("io.quux.otelc.native") : nullptr, limits);
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

void Register(native::FunctionRuntime &engine) {
  const otelc_function_descriptor_v1 functions[] = {{&outer_address, "outer"}, {&inner_address, "inner"}};
  assert(engine.Register(functions, 2));
}

void BalancedScopesErrorsAndApplicationParents() {
  World world;
  auto engine = world.Engine(); Register(*engine);
  auto tracer = world.traces->GetTracer("application");
  auto application = tracer->StartSpan("application");
  {
    trace::Scope app_scope(application);
    auto outer = engine->Enter(&outer_address);
    assert(outer && trace::GetSpan(opentelemetry::context::RuntimeContext::GetCurrent())->IsRecording());
    auto outer_context = trace::GetSpan(opentelemetry::context::RuntimeContext::GetCurrent())->GetContext();
    auto external = tracer->StartSpan("application-child");
    {
      trace::Scope child_scope(external);
      auto inner = engine->Enter(&inner_address);
      assert(inner && inner != outer);
      assert(!engine->Leave(outer, 0) && !engine->Leave(inner, 2));
      assert(engine->Leave(inner, 1));
      assert(trace::GetSpan(opentelemetry::context::RuntimeContext::GetCurrent())->GetContext() == external->GetContext());
    }
    assert(trace::GetSpan(opentelemetry::context::RuntimeContext::GetCurrent())->GetContext() == outer_context);
    external->End();
    assert(engine->Leave(outer, 0));
    assert(trace::GetSpan(opentelemetry::context::RuntimeContext::GetCurrent())->GetContext() == application->GetContext());
    assert(application->IsRecording());
  }
  application->End();
  assert(world.span_sink->spans.size() == 4);
  auto &inner = *world.span_sink->spans[0]; auto &child = *world.span_sink->spans[1];
  auto &outer = *world.span_sink->spans[2];
  assert(inner.GetName() == "inner" && inner.GetStatus() == trace::StatusCode::kError);
  assert(opentelemetry::nostd::get<std::string>(inner.GetAttributes().at("error.type")) == "exception");
  assert(inner.GetParentSpanId() == child.GetSpanId() && child.GetParentSpanId() == outer.GetSpanId());
  assert(outer.GetParentSpanId() == application->GetContext().span_id());
  assert(outer.GetTraceId() == inner.GetTraceId() && inner.GetTraceId() == application->GetContext().trace_id());
  assert(world.Collect() == (Values{{"otelc.function.calls", 2}, {"otelc.function.errors", 1},
                                  {"otelc.function.duration", 2}}));
  auto status = engine->Status();
  assert(status.started == 2 && status.ended == 2 && !status.active && !status.lost && status.invalid == 2);
  assert(engine->Close() && engine->Status().closed);
}

void OwnedParentsDoNotReadOrChangeAmbientSDKScopes() {
  World world;
  auto engine = world.Engine({4096, 512, native::FunctionContext::owned}); Register(*engine);
  auto application = world.traces->GetTracer("application")->StartSpan("application");
  {
    trace::Scope scope(application);
    auto outer = engine->Enter(&outer_address);
    auto inner = engine->Enter(&inner_address);
    assert(outer && inner && engine->Leave(inner, 0) && engine->Leave(outer, 0));
    assert(trace::GetSpan(opentelemetry::context::RuntimeContext::GetCurrent())->GetContext() == application->GetContext());
  }
  application->End();
  assert(world.span_sink->spans.size() == 3);
  const auto &inner = *world.span_sink->spans[0], &outer = *world.span_sink->spans[1];
  assert(inner.GetParentSpanId() == outer.GetSpanId());
  assert(inner.GetTraceId() == outer.GetTraceId() && outer.GetTraceId() != application->GetContext().trace_id());
  assert(!outer.GetParentSpanId().IsValid());
  assert(engine->Close());
}

trace::SpanContext RemoteContext(bool sampled = true) {
  std::uint8_t trace_bytes[16] = {1};
  std::uint8_t span_bytes[8] = {2};
  return {trace::TraceId(trace_bytes), trace::SpanId(span_bytes),
          trace::TraceFlags(sampled ? 1 : 0), true, trace::TraceState::FromHeader("quux=task")};
}

void OwnedContextWorkerCase(bool named_parent, bool named_child, bool collection, bool emission) {
  World world(collection, emission);
  auto engine = world.Engine({4, 512, native::FunctionContext::owned, 1}); Register(*engine);
  auto parent = named_parent ? engine->EnterNamed("src/app.rs::outer") : engine->Enter(&outer_address);
  auto captured = engine->CurrentContext();
  assert(captured.IsValid() && engine->Leave(parent, 0));
  assert(!engine->CurrentContext().IsValid());
  auto child_context = trace::SpanContext::GetInvalid();
  auto nested_context = trace::SpanContext::GetInvalid();
  auto fresh_context = trace::SpanContext::GetInvalid();
  std::thread worker([&] {
    auto ambient = world.traces->GetTracer("application")->StartSpan("unrelated-worker");
    {
      trace::Scope scope(ambient);
      errno = EDOM;
      auto lease = engine->AttachContext(captured);
      assert(lease && errno == EDOM && engine->CurrentContext() == captured);
      assert(errno == EDOM);
      auto child = named_child ? engine->EnterNamed("src/app.rs::inner") : engine->Enter(&inner_address);
      child_context = engine->CurrentContext();
      auto nested = engine->Enter(&outer_address);
      nested_context = engine->CurrentContext();
      assert(child && nested && engine->Leave(nested, 0) && engine->Leave(child, 0));
      assert(engine->CurrentContext() == captured && engine->DetachContext(lease));
      assert(!engine->CurrentContext().IsValid());
      auto fresh = engine->Enter(&inner_address);
      fresh_context = engine->CurrentContext();
      assert(fresh && engine->Leave(fresh, 0));
      assert(trace::GetSpan(opentelemetry::context::RuntimeContext::GetCurrent())->GetContext()
             == ambient->GetContext() && ambient->IsRecording());
    }
    ambient->End();
  });
  worker.join();
  assert(world.span_sink->spans.size() == 5);
  auto find = [&](const trace::SpanContext &context) -> const traces::SpanData & {
    auto row = std::find_if(world.span_sink->spans.begin(), world.span_sink->spans.end(),
        [&](const auto &span) { return span->GetSpanId() == context.span_id(); });
    assert(row != world.span_sink->spans.end());
    return **row;
  };
  assert(find(child_context).GetParentSpanId() == captured.span_id());
  assert(find(nested_context).GetParentSpanId() == child_context.span_id());
  assert(child_context.trace_id() == captured.trace_id() && nested_context.trace_id() == captured.trace_id());
  assert(!find(fresh_context).GetParentSpanId().IsValid() && fresh_context.trace_id() != captured.trace_id());
  auto status = engine->ContextStatus();
  assert(status.admitted == 1 && status.released == 1 && !status.active && !status.lost && !status.invalid);
  auto values = world.Collect();
  assert(collection ? values.at("otelc.function.calls") == 4 : values.empty());
  assert(world.metric_sink->exports == unsigned(collection && emission));
  assert(engine->Status().registered == unsigned(2 + named_parent + named_child));
  assert(engine->Close() && engine->ContextStatus().closed);
}

void OwnedContextTransfersToAReusedWorkerWithoutAmbientMutation() {
  for (bool collection : {false, true}) for (bool emission : {false, true})
    OwnedContextWorkerCase(false, false, collection, emission);
}

void NamedAndAddressContextsShareParentageAndBudgets() {
  for (bool named_parent : {false, true}) for (bool named_child : {false, true})
    if (named_parent || named_child)
      for (bool collection : {false, true}) for (bool emission : {false, true})
        OwnedContextWorkerCase(named_parent, named_child, collection, emission);
}

void OwnedContextOrderingCapacityAndIncompleteClose() {
  World world;
  auto engine = world.Engine({4096, 512, native::FunctionContext::owned, 2}); Register(*engine);
  auto outer = engine->Enter(&outer_address);
  auto original = engine->CurrentContext();
  auto remote = RemoteContext();
  auto first = engine->AttachContext(remote);
  assert(first && engine->CurrentContext() == remote && !engine->Leave(outer, 0));
  auto inner = engine->Enter(&inner_address);
  assert(inner && !engine->DetachContext(first) && engine->Leave(inner, 0));
  auto second = engine->AttachContext(original);
  assert(second && engine->CurrentContext() == original);
  assert(!engine->AttachContext(remote) && !engine->DetachContext(first));
  std::thread foreign([&] {
    errno = ECHILD;
    assert(!engine->DetachContext(second) && errno == ECHILD);
    assert(!engine->CurrentContext().IsValid());
  });
  foreign.join();
  assert(!engine->Close() && !engine->Status().closed && !engine->ContextStatus().closed);
  assert(engine->DetachContext(second) && engine->CurrentContext() == remote);
  assert(!engine->AttachContext(remote));
  assert(!engine->DetachContext(second));
  assert(engine->DetachContext(first) && engine->CurrentContext() == original);
  assert(engine->Leave(outer, 0) && engine->Close());
  auto status = engine->ContextStatus();
  assert(status.admitted == 2 && status.released == 2 && !status.active);
  assert(status.lost == 2 && status.invalid == 4 && status.closing && status.closed);
  assert(engine->Status().invalid == 1 && !engine->Status().lost);
  errno = ERANGE;
  assert(engine->DetachContext(0) && errno == ERANGE);
}

void ConcurrentOwnedContextsKeepEachThreadsParentAndCloseWaitsForAllLeases() {
  World world;
  auto engine = world.Engine({4096, 512, native::FunctionContext::owned}); Register(*engine);
  std::vector<trace::SpanContext> parents(8, trace::SpanContext::GetInvalid());
  std::vector<trace::SpanContext> children(8, trace::SpanContext::GetInvalid());
  for (auto &parent : parents) {
    auto call = engine->Enter(&outer_address);
    parent = engine->CurrentContext();
    assert(call && engine->Leave(call, 0));
  }
  std::vector<std::promise<void>> ready(parents.size());
  std::promise<void> release;
  auto released = release.get_future().share();
  std::vector<std::thread> workers;
  for (std::size_t index = 0; index < parents.size(); ++index) workers.emplace_back([&, index] {
    auto lease = engine->AttachContext(parents[index]);
    auto call = engine->Enter(&inner_address);
    children[index] = engine->CurrentContext();
    assert(lease && call && children[index].trace_id() == parents[index].trace_id());
    ready[index].set_value();
    assert(released.wait_for(5s) == std::future_status::ready);
    assert(engine->Leave(call, 0) && engine->DetachContext(lease));
    assert(!engine->CurrentContext().IsValid());
  });
  for (auto &item : ready) assert(item.get_future().wait_for(5s) == std::future_status::ready);
  assert(engine->Status().active == 8 && engine->ContextStatus().active == 8);
  assert(!engine->CurrentContext().IsValid() && !engine->Close());
  release.set_value();
  for (auto &worker : workers) worker.join();
  assert(world.span_sink->spans.size() == 16 && engine->Close());
  for (std::size_t index = 0; index < children.size(); ++index) {
    auto child = std::find_if(world.span_sink->spans.begin(), world.span_sink->spans.end(),
        [&](const auto &span) { return span->GetSpanId() == children[index].span_id(); });
    assert(child != world.span_sink->spans.end() && (*child)->GetParentSpanId() == parents[index].span_id());
  }
  auto status = engine->ContextStatus();
  assert(status.admitted == 8 && status.released == 8 && !status.active && !status.lost && !status.invalid);
}

void OwnedContextPreservesRemoteSamplingTraceStateAndMetricsOnlyParents() {
  for (bool sampled : {false, true}) {
    World world(true, true, true,
        std::make_unique<traces::ParentBasedSampler>(std::make_shared<traces::AlwaysOnSampler>()));
    auto engine = world.Engine({4096, 512, native::FunctionContext::owned}); Register(*engine);
    auto parent = RemoteContext(sampled);
    auto lease = engine->AttachContext(parent);
    auto captured = engine->CurrentContext();
    assert(lease && captured == parent && captured.IsRemote());
    assert(captured.trace_state()->ToHeader() == "quux=task");
    auto call = engine->Enter(&inner_address);
    auto child = engine->CurrentContext();
    assert(call && child.IsValid() && child.trace_id() == parent.trace_id());
    assert(child.IsSampled() == sampled && child.trace_state()->ToHeader() == "quux=task");
    assert(engine->Leave(call, 0) && engine->DetachContext(lease));
    assert(world.span_sink->spans.size() == unsigned(sampled));
    if (sampled) assert(world.span_sink->spans[0]->GetParentSpanId() == parent.span_id());
    assert(world.Collect().at("otelc.function.calls") == 1 && engine->Close());
  }
  World world;
  auto engine = world.Engine({4096, 512, native::FunctionContext::owned}, false); Register(*engine);
  auto parent = RemoteContext(false);
  auto lease = engine->AttachContext(parent);
  auto call = engine->Enter(&inner_address);
  assert(lease && call && engine->CurrentContext() == parent);
  assert(engine->Leave(call, 0) && engine->DetachContext(lease) && engine->Close());
  assert(world.span_sink->spans.empty() && world.Collect().at("otelc.function.calls") == 1);
}

void OwnedContextRefusalsAndSDKReentryKeepErrnoAndTheValidLease() {
  World world;
  auto ambient = world.Engine();
  auto parent = RemoteContext();
  assert(!ambient->AttachContext(parent) && ambient->ContextStatus().invalid == 1 && ambient->Close());
  auto engine = world.Engine({4096, 512, native::FunctionContext::owned}); Register(*engine);
  assert(!engine->AttachContext(trace::SpanContext::GetInvalid()));
  auto lease = engine->AttachContext(parent);
  auto expected = parent;
  auto callback = [&] {
    errno = EOVERFLOW;
    assert(!engine->AttachContext(parent) && !engine->DetachContext(lease));
    assert(engine->CurrentContext() == expected && errno == EOVERFLOW);
    assert(engine->ContextStatus().active == 1);
  };
  world.span_sink->create_callback = callback;
  errno = EINTR;
  auto call = engine->Enter(&inner_address);
  assert(call && errno == EINTR);
  expected = engine->CurrentContext();
  world.span_sink->create_callback = {};
  world.span_sink->callback = callback;
  errno = ECHILD;
  assert(engine->Leave(call, 0) && errno == ECHILD);
  world.span_sink->callback = {};
  assert(engine->DetachContext(lease) && engine->Close());
  auto status = engine->ContextStatus();
  assert(status.admitted == 1 && status.released == 1 && status.invalid == 5 && !status.lost);
}

void OwnedContextFinalTraceStateReleaseRunsWithoutTheRuntimeLock() {
  World world;
  auto engine = world.Engine({4096, 512, native::FunctionContext::owned}); Register(*engine);
  std::uint64_t lease = 0;
  unsigned destroyed = 0;
  auto original = RemoteContext();
  {
    std::shared_ptr<trace::TraceState> observed(trace::TraceState::GetDefault().get(), [&](trace::TraceState *) {
      std::fputs("native owned context: final-reference deleter entered\n", stderr);
      ++destroyed;
      auto status = engine->ContextStatus();
      assert(status.active == 1 && status.released == 0 && !status.closed);
      assert(!engine->CurrentContext().IsValid());
      errno = ERANGE;
      assert(!engine->AttachContext(original) && !engine->DetachContext(lease));
      assert(!engine->Enter(&inner_address) && errno == ERANGE);
      assert(!engine->Close());
    });
    auto parent = trace::SpanContext(original.trace_id(), original.span_id(), original.trace_flags(),
                                     true, observed);
    lease = engine->AttachContext(parent);
    assert(lease && !destroyed);
  }
  errno = EDOM;
  assert(engine->DetachContext(lease) && destroyed == 1 && errno == EDOM);
  auto status = engine->ContextStatus();
  assert(status.admitted == 1 && status.released == 1 && !status.active && status.invalid == 2);
  assert(!status.lost && engine->Status().lost == 1 && !engine->Status().active && engine->Close());
}

void AllLiveStatesAndEntryExitWindows() {
  for (bool collection : {false, true}) for (bool emission : {false, true}) {
    World world(collection, emission);
    auto engine = world.Engine(); Register(*engine);
    auto first = engine->Enter(&outer_address); assert(first && engine->Leave(first, 0));
    auto values = world.Collect();
    assert(collection ? values.at("otelc.function.calls") == 1 : values.empty());
    assert(world.metric_sink->exports == unsigned(collection && emission));
    assert(world.controller.SetCollection(true) && world.state->SetEmission(true));
    auto second = engine->Enter(&outer_address); assert(second && engine->Leave(second, 0));
    world.Collect();
    auto expected = std::uint64_t(collection ? 2 : 1);
    assert(world.metric_sink->values.at("otelc.function.calls") == expected);
    assert(world.state->SetEmission(false));
    auto silent = engine->Enter(&outer_address); assert(silent && engine->Leave(silent, 0));
    world.Collect();
    assert(world.metric_sink->values.at("otelc.function.calls") == expected);
    assert(world.state->SetEmission(true)); world.Collect();
    assert(world.metric_sink->values.at("otelc.function.calls") == ++expected);
    auto on_to_off = engine->Enter(&outer_address);
    assert(world.controller.SetCollection(false) && engine->Leave(on_to_off, 0));
    auto off_to_on = engine->Enter(&outer_address);
    assert(world.controller.SetCollection(true) && engine->Leave(off_to_on, 0));
    assert(world.Collect().at("otelc.function.calls") == expected);
    assert(world.span_sink->spans.size() == 5 && engine->Close());
  }
}

void ThreadOwnershipCapacityAndIncompleteClose() {
  World world; auto engine = world.Engine({2, 2}); Register(*engine);
  auto main_token = engine->Enter(&outer_address);
  std::promise<void> ready, release; auto released = release.get_future();
  std::thread worker([&] {
    auto token = engine->Enter(&inner_address);
    assert(token && !engine->Leave(main_token, 0));
    ready.set_value(); assert(released.wait_for(5s) == std::future_status::ready);
    assert(engine->Leave(token, 0));
  });
  assert(ready.get_future().wait_for(5s) == std::future_status::ready);
  assert(!engine->Enter(&extra_address) && engine->Leave(0, 0));
  assert(!engine->Close() && engine->Status().closing && !engine->Status().closed);
  assert(engine->Status().active == 2 && !engine->Enter(&outer_address));
  release.set_value(); worker.join(); assert(engine->Leave(main_token, 0));
  assert(!engine->Leave(main_token, 0) && engine->Close());
  auto status = engine->Status();
  assert(status.closed && status.started == 2 && status.ended == 2 && status.lost == 2 && status.invalid == 2);
  assert(world.Collect().at("otelc.function.calls") == 2 && world.span_sink->spans.size() == 2);
  assert(world.span_sink->spans[0]->GetParentSpanId() == trace::SpanId{});
}

void RegistrationIsBoundedAtomicAndOwnsNames() {
  World world; auto engine = world.Engine({2, 2});
  assert(!engine->Register(nullptr, 1) && engine->Register(nullptr, 0));
  std::string huge(4097, 'x');
  const otelc_function_descriptor_v1 bad[] = {{&outer_address, "valid"}, {&inner_address, huge.c_str()}};
  assert(!engine->Register(bad, 2) && !engine->Status().registered);
  const otelc_function_descriptor_v1 empty[] = {{nullptr, "name"}, {&outer_address, ""}};
  assert(!engine->Register(empty, 1) && !engine->Register(empty + 1, 1));
  const otelc_function_descriptor_v1 conflict[] = {{&outer_address, "one"}, {&outer_address, "two"}};
  assert(!engine->Register(conflict, 2) && !engine->Status().registered);
  char name[] = "owned";
  const otelc_function_descriptor_v1 original[] = {{&outer_address, name}, {&inner_address, "\xcf\x80"}};
  assert(engine->Register(original, 2) && engine->Register(original, 2));
  name[0] = 'x';
  const otelc_function_descriptor_v1 extra[] = {{&extra_address, "extra"}};
  assert(!engine->Register(extra, 1) && !engine->Register(original, 2));
  assert(!engine->Register(original, 3));
  assert(!engine->Enter(&extra_address));
  auto token = engine->Enter(&outer_address); assert(token && engine->Leave(token, 0));
  assert(world.span_sink->spans[0]->GetName() == "owned");
  token = engine->Enter(&inner_address); assert(token && engine->Leave(token, 0));
  assert(world.span_sink->spans[1]->GetName() == "\xcf\x80");
  assert(engine->Status().registered == 2 && engine->Close() && !engine->Register(extra, 1));
}

void TracingOffAndExporterReentry() {
  World world; auto metrics_only = world.Engine({}, false); Register(*metrics_only);
  auto token = metrics_only->Enter(&outer_address); assert(token && metrics_only->Leave(token, 0));
  assert(world.span_sink->spans.empty() && world.Collect().at("otelc.function.calls") == 1);
  assert(metrics_only->Close());
  auto engine = world.Engine(); Register(*engine);
  token = engine->Enter(&outer_address);
  world.span_sink->callback = [&] {
    auto status = engine->Status();
    assert(status.active == 1 && !status.ended && !engine->Enter(&inner_address));
    assert(!engine->Close());
    assert(!engine->Leave(token, 0));
  };
  assert(engine->Leave(token, 0)); world.span_sink->callback = {};
  assert(engine->Status().lost == 1 && engine->Status().invalid == 1 && engine->Close());
}

void NamedFunctionsOwnNamesAndShareTheRegistrationBudget() {
  World world; auto engine = world.Engine({2, 4});
  const otelc_function_descriptor_v1 native[] = {{&outer_address, "native"}};
  assert(engine->Register(native, 1));
  std::string name = "src/app.rs::workload";
  auto token = engine->EnterNamed(name);
  assert(token && engine->Status().registered == 2);
  name.assign("changed after entry");
  assert(engine->Leave(token, 0));
  assert(world.span_sink->spans[0]->GetName() == "src/app.rs::workload");
  token = engine->EnterNamed("src/app.rs::workload");
  assert(token && engine->Leave(token, 1) && engine->Status().registered == 2);
  assert(!engine->EnterNamed("another"));
  const otelc_function_descriptor_v1 extra[] = {{&extra_address, "another native"}};
  assert(!engine->Register(extra, 1));
  token = engine->Enter(&outer_address); assert(token && engine->Leave(token, 0));
  auto values = world.Collect();
  assert(values.at("otelc.function.calls") == 3 && values.at("otelc.function.errors") == 1);
  assert(engine->Status().lost == 1 && engine->Status().invalid == 1 && engine->Close());
  assert(!engine->EnterNamed("src/app.rs::workload"));
  assert(!engine->EnterNamed("after close") && engine->Status().registered == 2);
}

void NamedFunctionsValidateNamesAndRetainLiveStates() {
  for (bool collection : {false, true}) for (bool emission : {false, true}) {
    World world(collection, emission); auto engine = world.Engine();
    auto first = engine->EnterNamed("src/app.rs::\xcf\x80");
    assert(first && engine->Leave(first, 0));
    auto values = world.Collect();
    assert(collection ? values.at("otelc.function.calls") == 1 : values.empty());
    assert(world.metric_sink->exports == unsigned(collection && emission));
    assert(world.controller.SetCollection(true) && world.state->SetEmission(false));
    auto second = engine->EnterNamed("src/app.rs::\xcf\x80");
    assert(second && engine->Leave(second, 0)); world.Collect();
    assert(world.metric_sink->exports == unsigned(collection && emission));
    assert(world.state->SetEmission(true)); world.Collect();
    assert(world.metric_sink->values.at("otelc.function.calls") == unsigned(collection ? 2 : 1));
    assert(world.span_sink->spans.size() == 2 && engine->Status().registered == 1 && engine->Close());
  }
  World world; auto engine = world.Engine({1, 1});
  assert(!engine->EnterNamed({}) && !engine->EnterNamed(std::string("a\0b", 3)));
  assert(!engine->EnterNamed(std::string(4097, 'x')) && !engine->Status().registered);
  auto token = engine->EnterNamed(std::string(4096, 'x'));
  assert(token && engine->Leave(token, 0));
  assert(world.span_sink->spans[0]->GetName().size() == 4096);
  assert(engine->Status().invalid == 3 && engine->Close());
  World metrics_only; auto no_traces = metrics_only.Engine({}, false);
  token = no_traces->EnterNamed("src/app.rs::metrics_only");
  assert(token && no_traces->Leave(token, 0));
  assert(metrics_only.span_sink->spans.empty());
  assert(metrics_only.Collect().at("otelc.function.calls") == 1 && no_traces->Close());
}

void NamedRegistrationIsConcurrentAndRefusesExporterReentry() {
  World world; auto engine = world.Engine({1, 8});
  std::vector<std::thread> workers;
  for (unsigned worker = 0; worker < 4; ++worker) workers.emplace_back([&] {
    for (unsigned call = 0; call < 8; ++call) {
      auto token = engine->EnterNamed("src/app.rs::shared");
      assert(token && engine->Leave(token, 0));
    }
  });
  for (auto &worker : workers) worker.join();
  assert(engine->Status().registered == 1 && engine->Status().ended == 32);
  assert(!engine->Status().invalid && !engine->Status().lost);
  auto token = engine->EnterNamed("src/app.rs::shared");
  world.span_sink->callback = [&] { assert(!engine->EnterNamed("exporter callback")); };
  assert(token && engine->Leave(token, 0)); world.span_sink->callback = {};
  assert(engine->Status().registered == 1 && engine->Status().lost == 1);
  assert(world.span_sink->spans.size() == 33 && engine->Close());
  World closing_world; auto closing = closing_world.Engine({2, 2});
  token = closing->EnterNamed("outstanding"); assert(token && !closing->Close());
  assert(!closing->EnterNamed("outstanding") && !closing->EnterNamed("not registered"));
  assert(closing->Status().registered == 1 && closing->Status().lost == 2);
  assert(closing->Leave(token, 0) && closing->Close());
}

void InvalidInputs() {
  World world;
  const double nan = std::numeric_limits<double>::quiet_NaN();
  const double infinity = std::numeric_limits<double>::infinity();
  for (const std::vector<double> &bounds : {std::vector<double>{}, {0.}, {-1.}, {nan}, {infinity},
                                          {.2, .1}, {.1, .1}, std::vector<double>(129, 1.)}) {
    bool refused = false;
    try { native::ConfigureFunctionMetrics(*world.meters, 4096, bounds); }
    catch (const std::invalid_argument &) { refused = true; }
    assert(refused);
  }
  for (auto limit : {0u, 4097u}) {
    bool refused = false;
    try { native::ConfigureFunctionMetrics(*world.meters, limit); }
    catch (const std::invalid_argument &) { refused = true; }
    assert(refused);
  }
  for (native::FunctionLimits limits : {native::FunctionLimits{0, 1}, {4097, 1}, {1, 0}, {1, 513},
      {1, 1, native::FunctionContext::owned, 0}, {1, 1, native::FunctionContext::owned, 513}}) {
    bool refused = false;
    try { auto engine = world.Engine(limits); } catch (const std::invalid_argument &) { refused = true; }
    assert(refused);
  }
  bool refused = false;
  try { native::FunctionRuntime engine(nullptr, world.meters->GetMeter("test"), nullptr); }
  catch (const std::invalid_argument &) { refused = true; }
  assert(refused);
  refused = false;
  try { native::FunctionRuntime engine(world.state, nullptr, nullptr); }
  catch (const std::invalid_argument &) { refused = true; }
  assert(refused && world.exporter.Shutdown(1s));
  refused = false;
  try { auto engine = world.Engine(); } catch (const std::invalid_argument &) { refused = true; }
  assert(refused);
}

void EntryAndCompletionPreserveApplicationErrno() {
  for (bool named : {false, true}) for (bool collection : {false, true})
      for (bool emission : {false, true}) for (int unwinding : {0, 1}) {
    bool created = false, exported = false;
    World world(collection, emission);
    auto engine = world.Engine(); Register(*engine);
    world.span_sink->create_callback = [&] { created = true; errno = ERANGE; };
    world.span_sink->callback = [&] {
      exported = true;
      errno = EOVERFLOW;
      // Refused recursive probes must preserve the callback's own error too.
      assert(engine->EnterNamed("exporter-reentry") == 0 && errno == EOVERFLOW);
      assert(!engine->Enter(&inner_address) && errno == EOVERFLOW);
      assert(!engine->Leave(42, 0) && errno == EOVERFLOW);
    };
    errno = EINTR;
    auto token = named ? engine->EnterNamed("errno.rs::workload") : engine->Enter(&outer_address);
    assert(token && created && errno == EINTR);
    errno = EDOM;
    assert(engine->Leave(token, unwinding) && exported && errno == EDOM);
    errno = ECHILD;
    assert(!engine->Leave(token, unwinding) && errno == ECHILD);
    assert(!engine->EnterNamed("") && errno == ECHILD);
    assert(!engine->Enter(nullptr) && errno == ECHILD);
    assert(engine->Leave(0, 0) && errno == ECHILD);
    assert(!engine->Status().active && engine->Close());
  }
}

void ConfiguredHistogramUsesTheExactSecondsPolicy() {
  World world(true, true, false);
  const std::vector<double> bounds = {.00005, .02, 5.};
  native::ConfigureFunctionMetrics(*world.meters, 2, bounds);
  auto engine = world.Engine({2, 2}, false); Register(*engine);
  auto token = engine->Enter(&outer_address); assert(token && engine->Leave(token, 0));
  bool histogram = false;
  assert(world.reader->Collect([&](metrics::ResourceMetrics &data) {
    for (const auto &scope : data.scope_metric_data_) for (const auto &metric : scope.metric_data_) {
      if (metric.instrument_descriptor.name_ != "otelc.function.duration") continue;
      for (const auto &point : metric.point_data_attr_) {
        assert(opentelemetry::nostd::get<metrics::HistogramPointData>(point.point_data).boundaries_ == bounds);
        histogram = true;
      }
    }
    return true;
  }));
  assert(histogram && engine->Close());
}

void CompilerInsertedCallsUseTheSDK() {
#ifdef OTELC_HAVE_LLVM_FIXTURES
  assert(descriptor_count == 5);
  World world; auto engine = world.Engine();
  assert(engine->Register(descriptors.data(), descriptor_count));
  installed_runtime = engine.get();
  assert(selected_cpp_outer(10) == 21);
  bool caught = false;
  try { selected_c_bridge(selected_throw); }
  catch (const std::runtime_error &error) { caught = std::string(error.what()) == "original exception"; }
  assert(caught);
  std::thread worker([] { assert(selected_cpp(3) == 6); }); worker.join();
  auto status = engine->Status();
  assert(status.registered == 5 && status.started == 8 && status.ended == 8);
  assert(!status.active && !status.lost && !status.invalid && engine->Close());
  assert(world.Collect() == (Values{{"otelc.function.calls", 8}, {"otelc.function.errors", 2},
                                  {"otelc.function.duration", 8}}));
  assert(world.span_sink->spans.size() == 8);
  unsigned roots = 0, errors = 0;
  for (const auto &span : world.span_sink->spans) {
    roots += span->GetParentSpanId() == trace::SpanId{};
    errors += span->GetStatus() == trace::StatusCode::kError;
  }
  assert(roots == 3 && errors == 2);
  for (const auto &span : world.span_sink->spans) if (span->GetParentSpanId() != trace::SpanId{}) {
    auto parent = std::find_if(world.span_sink->spans.begin(), world.span_sink->spans.end(),
        [&](const auto &candidate) { return candidate->GetSpanId() == span->GetParentSpanId(); });
    assert(parent != world.span_sink->spans.end() && (*parent)->GetTraceId() == span->GetTraceId());
  }
  installed_runtime = nullptr;
#endif
}

bool WideSelectionKeepsEachSeriesAndSecondBuckets() {
  World world; auto engine = world.Engine({}, false);
  std::array<int, 4096> addresses{};
  std::vector<std::string> names;
  std::vector<otelc_function_descriptor_v1> items;
  names.reserve(addresses.size()); items.reserve(addresses.size());
  for (std::size_t index = 0; index < addresses.size(); ++index) {
    names.push_back("function." + std::to_string(index));
    items.push_back({&addresses[index], names.back().c_str()});
  }
  assert(engine->Register(items.data(), items.size()));
  for (auto &address : addresses) { auto token = engine->Enter(&address); assert(token && engine->Leave(token, 1)); }
  std::map<std::string, std::set<std::string>> labels;
  unsigned overflow = 0;
  bool buckets = true;
  const std::vector<double> expected = {1e-6, 1e-5, 1e-4, .001, .01, .1, 1.};
  assert(world.reader->Collect([&](metrics::ResourceMetrics &data) {
    for (const auto &scope : data.scope_metric_data_) for (const auto &metric : scope.metric_data_) {
      for (const auto &point : metric.point_data_attr_) {
        auto function = point.attributes.find("function");
        if (function == point.attributes.end()) { ++overflow; continue; }
        labels[metric.instrument_descriptor.name_].insert(opentelemetry::nostd::get<std::string>(function->second));
        if (metric.instrument_descriptor.name_ == "otelc.function.duration") {
          const auto &histogram = opentelemetry::nostd::get<metrics::HistogramPointData>(point.point_data);
          buckets &= histogram.boundaries_ == expected && histogram.count_ == 1;
        } else {
          assert(opentelemetry::nostd::get<int64_t>(opentelemetry::nostd::get<metrics::SumPointData>(point.point_data).value_) == 1);
        }
      }
    }
    return true;
  }));
  assert(!engine->Status().lost && !engine->Status().active && engine->Close());
  std::printf("native wide selection: calls=%zu errors=%zu durations=%zu overflow=%u second_buckets=%d\n",
      labels["otelc.function.calls"].size(), labels["otelc.function.errors"].size(),
      labels["otelc.function.duration"].size(), overflow, buckets);
  return !overflow && buckets && labels.size() == 3
      && std::all_of(labels.begin(), labels.end(), [&](const auto &row) { return row.second.size() == addresses.size(); });
}

}  // namespace engine_test

int main() {
  using namespace engine_test;
  BalancedScopesErrorsAndApplicationParents();
  OwnedParentsDoNotReadOrChangeAmbientSDKScopes();
  OwnedContextTransfersToAReusedWorkerWithoutAmbientMutation();
  OwnedContextOrderingCapacityAndIncompleteClose();
  ConcurrentOwnedContextsKeepEachThreadsParentAndCloseWaitsForAllLeases();
  OwnedContextPreservesRemoteSamplingTraceStateAndMetricsOnlyParents();
  OwnedContextRefusalsAndSDKReentryKeepErrnoAndTheValidLease();
  OwnedContextFinalTraceStateReleaseRunsWithoutTheRuntimeLock();
  std::puts("native owned context: 6 SDK groups passed");
  NamedAndAddressContextsShareParentageAndBudgets();
  std::puts("native owned context: mixed address/named SDK groups passed");
  AllLiveStatesAndEntryExitWindows();
  ThreadOwnershipCapacityAndIncompleteClose();
  RegistrationIsBoundedAtomicAndOwnsNames();
  NamedFunctionsOwnNamesAndShareTheRegistrationBudget();
  NamedFunctionsValidateNamesAndRetainLiveStates();
  NamedRegistrationIsConcurrentAndRefusesExporterReentry();
  TracingOffAndExporterReentry();
  EntryAndCompletionPreserveApplicationErrno();
  InvalidInputs();
  CompilerInsertedCallsUseTheSDK();
  ConfiguredHistogramUsesTheExactSecondsPolicy();
  if (!WideSelectionKeepsEachSeriesAndSecondBuckets()) return 1;
  std::puts("native SDK function and metric checks passed");
#ifdef OTELC_HAVE_LLVM_FIXTURES
  std::puts("native SDK function engine: compiler-linked C/C++ group passed");
#endif
}
