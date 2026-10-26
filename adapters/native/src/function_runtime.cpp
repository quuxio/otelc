#include "otelc/function_runtime.h"

#include <array>
#include <chrono>
#include <cerrno>
#include <cmath>
#include <map>
#include <stdexcept>
#include <string>
#include <thread>

#include "opentelemetry/trace/scope.h"
#include "opentelemetry/trace/context.h"
#include "opentelemetry/sdk/metrics/aggregation/aggregation_config.h"
#include "opentelemetry/sdk/metrics/view/instrument_selector.h"
#include "opentelemetry/sdk/metrics/view/meter_selector.h"
#include "opentelemetry/sdk/metrics/view/view.h"

namespace otelc::native {
namespace trace = opentelemetry::trace;
namespace metrics = opentelemetry::metrics;
namespace {
using Clock = std::chrono::steady_clock;
constexpr std::uint32_t MaxFunctions = 4096;
constexpr std::uint32_t MaxActive = 512;
constexpr std::uint32_t MaxContexts = 512;
constexpr std::size_t MaxName = 4096;
struct SavedErrno {
  SavedErrno() = default;
  SavedErrno(const SavedErrno &) = delete;
  SavedErrno &operator=(const SavedErrno &) = delete;
  const int value = errno;
  ~SavedErrno() noexcept { errno = value; }
};
// SDK processors/exporters may call selected application code. Refuse those
// recursive probes visibly instead of recursing into the instrumentation engine.
thread_local bool inside_runtime = false;
struct ProbeGuard {
  ProbeGuard() { inside_runtime = true; }
  ~ProbeGuard() { inside_runtime = false; }
};
}  // namespace

void ConfigureFunctionMetrics(opentelemetry::sdk::metrics::MeterProvider &provider,
    std::uint32_t functions, const std::vector<double> &boundaries) {
  if (!functions || functions > MaxFunctions || boundaries.empty() || boundaries.size() > 128)
    throw std::invalid_argument("native function metrics require bounded series and histogram limits");
  double previous = 0;
  for (double boundary : boundaries) {
    if (!std::isfinite(boundary) || boundary <= previous)
      throw std::invalid_argument("native function histogram boundaries must be finite, positive and increasing");
    previous = boundary;
  }
  namespace sdk = opentelemetry::sdk::metrics;
  // Leave room for an SDK overflow bucket without reducing the admitted function
  // denominator. These views affect only the tooling's exact scope/instruments.
  auto sum = std::make_shared<sdk::AggregationConfig>(functions + 1);
  auto histogram = std::make_shared<sdk::HistogramAggregationConfig>(functions + 1);
  histogram->boundaries_ = boundaries;
  for (const auto *name : {"otelc.function.calls", "otelc.function.errors", "otelc.function.duration"}) {
    bool duration = std::string(name) == "otelc.function.duration";
    provider.AddView(std::make_unique<sdk::InstrumentSelector>(
        duration ? sdk::InstrumentType::kHistogram : sdk::InstrumentType::kCounter,
        name, duration ? "s" : (std::string(name) == "otelc.function.calls" ? "{call}" : "{error}"),
        sdk::PredicateType::kExact),
        std::make_unique<sdk::MeterSelector>(FunctionScopeName, "", ""),
        std::make_unique<sdk::View>(name, "", duration ? sdk::AggregationType::kHistogram : sdk::AggregationType::kSum,
            duration ? std::static_pointer_cast<sdk::AggregationConfig>(histogram) : sum));
  }
}

struct FunctionRuntime::Impl {
  enum class Phase { free, entering, active, leaving };
  struct Frame {
    Phase phase = Phase::free;
    std::uint64_t token = 0;
    std::thread::id thread;
    const std::string *name = nullptr;
    Clock::time_point start;
    bool collection = false;
    opentelemetry::nostd::shared_ptr<trace::Span> span;
    std::unique_ptr<trace::Scope> scope;
  };
  struct Parent {
    std::uint64_t token = 0;
    std::thread::id thread;
    trace::SpanContext context = trace::SpanContext::GetInvalid();
  };

  std::shared_ptr<MetricState> state;
  opentelemetry::nostd::shared_ptr<trace::Tracer> tracer;
  opentelemetry::nostd::unique_ptr<metrics::Counter<std::uint64_t>> calls, errors;
  opentelemetry::nostd::unique_ptr<metrics::Histogram<double>> duration;
  FunctionLimits limits;
  mutable std::mutex mutex;
  std::map<const void *, std::string> functions;
  std::array<Frame, MaxActive> frames;
  std::array<Parent, MaxContexts> parents;
  std::atomic<std::uint64_t> lost{0}, invalid{0};
  std::atomic<std::uint64_t> context_lost{0}, context_invalid{0};
  std::uint64_t next = 1, started = 0, ended = 0, active = 0;
  std::uint64_t context_admitted = 0, context_released = 0, context_active = 0;
  bool closing = false;

  Impl(std::shared_ptr<MetricState> selected_state,
       opentelemetry::nostd::shared_ptr<metrics::Meter> meter,
       opentelemetry::nostd::shared_ptr<trace::Tracer> selected_tracer, FunctionLimits selected_limits)
      : state(std::move(selected_state)), tracer(std::move(selected_tracer)), limits(selected_limits) {
    if (!state || !meter || state->Status().closing || limits.functions == 0
        || limits.functions > MaxFunctions || limits.active == 0 || limits.active > MaxActive
        || limits.contexts == 0 || limits.contexts > MaxContexts
        || (limits.context != FunctionContext::ambient && limits.context != FunctionContext::owned))
      throw std::invalid_argument("native function engine requires open SDK inputs and bounded limits");
    calls = meter->CreateUInt64Counter("otelc.function.calls", "Completed selected calls", "{call}");
    errors = meter->CreateUInt64Counter("otelc.function.errors", "Selected exception exits", "{error}");
    duration = meter->CreateDoubleHistogram("otelc.function.duration", "Selected call duration", "s");
  }

  Frame *Top(std::thread::id thread) {
    Frame *result = nullptr;
    for (auto &frame : frames) {
      if (frame.phase != Phase::free && frame.thread == thread
          && (!result || frame.token > result->token)) result = &frame;
    }
    return result;
  }

  Parent *TopParent(std::thread::id thread) {
    if (!context_active) return nullptr;
    Parent *result = nullptr;
    for (auto &parent : parents) {
      if (parent.token && parent.thread == thread
          && (!result || parent.token > result->token)) result = &parent;
    }
    return result;
  }

  trace::SpanContext Current(std::thread::id thread) {
    auto *frame = Top(thread);
    auto *parent = TopParent(thread);
    if (parent && (!frame || parent->token > frame->token)) return parent->context;
    if (frame && frame->span) return frame->span->GetContext();
    if (parent) return parent->context;
    return trace::SpanContext::GetInvalid();
  }
};

FunctionRuntime::FunctionRuntime(std::shared_ptr<MetricState> state,
    opentelemetry::nostd::shared_ptr<metrics::Meter> meter,
    opentelemetry::nostd::shared_ptr<trace::Tracer> tracer, FunctionLimits limits)
    : impl_(std::make_unique<Impl>(std::move(state), std::move(meter), std::move(tracer), limits)) {}
FunctionRuntime::~FunctionRuntime() = default;

bool FunctionRuntime::Register(const otelc_function_descriptor_v1 *functions, std::uint64_t count) noexcept {
  try {
    if (count > impl_->limits.functions || (!functions && count)) {
      ++impl_->invalid;
      return false;
    }
    // Build complete owning nodes before changing the live map. merge transfers
    // them without allocation and preserves names referenced by active frames.
    std::map<const void *, std::string> incoming;
    for (std::uint64_t index = 0; index < count; ++index) {
      const auto &item = functions[index];
      std::size_t length = 0;
      if (item.name) while (length <= MaxName && item.name[length]) ++length;
      if (!item.address || !length || length > MaxName) {
        ++impl_->invalid;
        return false;
      }
      std::string name(item.name, length);
      auto inserted = incoming.emplace(item.address, name);
      if (!inserted.second && inserted.first->second != name) {
        ++impl_->invalid;
        return false;
      }
    }
    std::lock_guard<std::mutex> guard(impl_->mutex);
    std::size_t additions = 0;
    for (const auto &item : incoming) {
      auto found = impl_->functions.find(item.first);
      if (found == impl_->functions.end()) ++additions;
      else if (found->second != item.second) {
        ++impl_->invalid;
        return false;
      }
    }
    if (impl_->closing || impl_->functions.size() + additions > impl_->limits.functions) {
      ++impl_->invalid;
      return false;
    }
    impl_->functions.merge(incoming);
    return true;
  } catch (...) {
    ++impl_->lost;
    return false;
  }
}

std::uint64_t FunctionRuntime::Enter(const void *address) noexcept {
  const SavedErrno saved;
  if (inside_runtime) {
    ++impl_->lost;
    return 0;
  }
  ProbeGuard probe;
  Impl::Frame *slot = nullptr;
  auto parent = trace::SpanContext::GetInvalid();
  try {
    {
      std::lock_guard<std::mutex> guard(impl_->mutex);
      auto function = impl_->functions.find(address);
      if (impl_->closing || impl_->active >= impl_->limits.active || !impl_->next
          || function == impl_->functions.end()) {
        ++impl_->lost;
        return 0;
      }
      if (impl_->limits.context == FunctionContext::owned) {
        parent = impl_->Current(std::this_thread::get_id());
      }
      for (auto &frame : impl_->frames) if (frame.phase == Impl::Phase::free) { slot = &frame; break; }
      slot->phase = Impl::Phase::entering;
      slot->thread = std::this_thread::get_id();
      slot->token = impl_->next++;
      slot->name = &function->second;
      slot->collection = impl_->state->Collecting();
      slot->start = Clock::now();
      ++impl_->active;
    }
    // Ambient mode joins application SDK operations. Owned mode also works in
    // global/TLS destructors after the SDK's thread-local stack has been freed.
    // No bookkeeping lock spans a processor or exporter call.
    if (impl_->tracer) {
      if (impl_->limits.context == FunctionContext::owned) {
        trace::StartSpanOptions options;
        // An invalid SpanContext alone falls back to ambient context in the SDK.
        options.parent = opentelemetry::context::Context{}.SetValue(trace::kIsRootSpanKey, true);
        if (parent.IsValid()) options.parent = parent;
        slot->span = impl_->tracer->StartSpan(*slot->name, {{"function", *slot->name}}, options);
      } else {
        slot->span = impl_->tracer->StartSpan(*slot->name, {{"function", *slot->name}});
        slot->scope = std::make_unique<trace::Scope>(slot->span);
      }
    }
    std::lock_guard<std::mutex> guard(impl_->mutex);
    slot->phase = Impl::Phase::active;
    ++impl_->started;
    return slot->token;
  } catch (...) {
    ++impl_->lost;
    if (slot) {
      slot->scope.reset();
      if (slot->span) slot->span->End();
      slot->span = nullptr;
      std::lock_guard<std::mutex> guard(impl_->mutex);
      slot->phase = Impl::Phase::free;
      --impl_->active;
    }
    return 0;
  }
}

bool FunctionRuntime::Leave(std::uint64_t token, std::int32_t unwinding) noexcept {
  const SavedErrno saved;
  if (!token) return true;
  if (inside_runtime) { ++impl_->invalid; return false; }
  ProbeGuard probe;
  Impl::Frame *slot;
  {
    std::lock_guard<std::mutex> guard(impl_->mutex);
    auto thread = std::this_thread::get_id();
    slot = impl_->Top(thread);
    auto *parent = impl_->TopParent(thread);
    if (!slot || slot->token != token || slot->phase != Impl::Phase::active
        || (unwinding != 0 && unwinding != 1) || (parent && parent->token > token)) {
      ++impl_->invalid;
      return false;
    }
    slot->phase = Impl::Phase::leaving;
  }
  if (slot->collection && impl_->state->Collecting()) {
    const auto &name = *slot->name;
    impl_->calls->Add(1, {{"function", name}});
    if (unwinding) impl_->errors->Add(1, {{"function", name}, {"error.type", "exception"}});
    impl_->duration->Record(std::chrono::duration<double>(Clock::now() - slot->start).count(),
                           {{"function", name}}, opentelemetry::context::Context{});
  }
  slot->scope.reset();
  if (slot->span) {
    if (unwinding) {
      slot->span->SetAttribute("error.type", "exception");
      slot->span->SetStatus(trace::StatusCode::kError);
    }
    slot->span->End();
    slot->span = nullptr;
  }
  std::lock_guard<std::mutex> guard(impl_->mutex);
  slot->phase = Impl::Phase::free;
  --impl_->active;
  ++impl_->ended;
  return true;
}

FunctionStatus FunctionRuntime::Status() const {
  std::lock_guard<std::mutex> guard(impl_->mutex);
  return {impl_->functions.size(), impl_->started, impl_->ended, impl_->active,
          impl_->lost.load(), impl_->invalid.load(), impl_->closing,
          impl_->closing && !impl_->active && !impl_->context_active};
}

trace::SpanContext FunctionRuntime::CurrentContext() const noexcept {
  const SavedErrno saved;
  try {
    std::lock_guard<std::mutex> guard(impl_->mutex);
    return impl_->Current(std::this_thread::get_id());
  } catch (...) {}
  return trace::SpanContext::GetInvalid();
}

std::uint64_t FunctionRuntime::AttachContext(const trace::SpanContext &parent) noexcept {
  const SavedErrno saved;
  if (inside_runtime || impl_->limits.context != FunctionContext::owned || !parent.IsValid()) {
    ++impl_->context_invalid;
    return 0;
  }
  try {
    std::lock_guard<std::mutex> guard(impl_->mutex);
    if (impl_->closing || impl_->context_active >= impl_->limits.contexts || !impl_->next) {
      ++impl_->context_lost;
      return 0;
    }
    for (auto &slot : impl_->parents) if (!slot.token) {
      slot.context = parent;
      slot.thread = std::this_thread::get_id();
      slot.token = impl_->next++;
      ++impl_->context_admitted;
      ++impl_->context_active;
      return slot.token;
    }
  } catch (...) {}
  ++impl_->context_lost;
  return 0;
}

bool FunctionRuntime::DetachContext(std::uint64_t token) noexcept {
  const SavedErrno saved;
  if (!token) return true;
  if (inside_runtime) { ++impl_->context_invalid; return false; }
  try {
    std::lock_guard<std::mutex> guard(impl_->mutex);
    auto thread = std::this_thread::get_id();
    auto *parent = impl_->TopParent(thread);
    auto *frame = impl_->Top(thread);
    if (!parent || parent->token != token || (frame && frame->token > token)) {
      ++impl_->context_invalid;
      return false;
    }
    parent->context = trace::SpanContext::GetInvalid();
    parent->token = 0;
    --impl_->context_active;
    ++impl_->context_released;
    return true;
  } catch (...) {
    ++impl_->context_invalid;
    return false;
  }
}

FunctionContextStatus FunctionRuntime::ContextStatus() const {
  std::lock_guard<std::mutex> guard(impl_->mutex);
  return {impl_->context_admitted, impl_->context_released, impl_->context_active,
          impl_->context_lost.load(), impl_->context_invalid.load(), impl_->closing,
          impl_->closing && !impl_->active && !impl_->context_active};
}

bool FunctionRuntime::Close() {
  std::lock_guard<std::mutex> guard(impl_->mutex);
  impl_->closing = true;
  return !impl_->active && !impl_->context_active;
}
}  // namespace otelc::native
