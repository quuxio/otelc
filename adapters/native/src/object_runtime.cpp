#include "otelc/object_runtime.h"
#include "otelc/function_runtime.h"
#include <cerrno>
#include <chrono>
#include <cmath>
#include <map>
#include <stdexcept>
#include <string>
#include <utility>
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
thread_local bool inside_object_runtime = false;
struct SavedErrno {
  SavedErrno() = default;
  SavedErrno(const SavedErrno &) = delete;
  SavedErrno &operator=(const SavedErrno &) = delete;
  const int value = errno;
  ~SavedErrno() noexcept { errno = value; }
};
struct Guard {
  Guard() { inside_object_runtime = true; }
  ~Guard() { inside_object_runtime = false; }
};
std::string name(const char *value) {
  std::size_t size = 0;
  if (value) while (size <= 4096 && value[size]) ++size;
  if (!size || size > 4096) throw std::invalid_argument("bounded object identity required");
  return {value, size};
}
}  // namespace

void ConfigureObjectMetrics(opentelemetry::sdk::metrics::MeterProvider &provider,
    std::uint32_t types, const std::vector<double> &boundaries) {
  if (!types || types > 4096 || boundaries.empty() || boundaries.size() > 128)
    throw std::invalid_argument("bounded object metric series and histogram required");
  double previous = 0;
  for (double boundary : boundaries) {
    if (!std::isfinite(boundary) || boundary <= previous)
      throw std::invalid_argument("object histogram boundaries must increase and be finite");
    previous = boundary;
  }
  namespace sdk = opentelemetry::sdk::metrics;
  auto histogram = std::make_shared<sdk::HistogramAggregationConfig>(2 * types + 1);
  histogram->boundaries_ = boundaries;
  for (const auto *instrument : {"otelc.lifetime.admitted", "otelc.lifetime.completed",
      "otelc.lifetime.incomplete", "otelc.lifetime.duration", "otelc.lifetime.live"}) {
    bool duration = std::string(instrument) == "otelc.lifetime.duration";
    bool live = std::string(instrument) == "otelc.lifetime.live";
    provider.AddView(std::make_unique<sdk::InstrumentSelector>(duration ? sdk::InstrumentType::kHistogram :
        live ? sdk::InstrumentType::kObservableGauge : sdk::InstrumentType::kCounter,
        instrument, duration ? "s" : "{object}", sdk::PredicateType::kExact),
        std::make_unique<sdk::MeterSelector>(FunctionScopeName, "", ""),
        std::make_unique<sdk::View>(instrument, "", duration ? sdk::AggregationType::kHistogram :
            live ? sdk::AggregationType::kLastValue : sdk::AggregationType::kSum,
            duration ? std::static_pointer_cast<sdk::AggregationConfig>(histogram) :
                std::make_shared<sdk::AggregationConfig>(2 * types + 1)));
  }
}

struct ObjectRuntime::Impl {
  struct Type { std::string name; std::uint32_t id; std::uint64_t live = 0; };
  struct Observation {
    Type *type;
    std::uintptr_t address;
    std::uint64_t token;
    Clock::time_point start, end;
    std::chrono::system_clock::time_point epoch;
    trace::SpanContext creation = trace::SpanContext::GetInvalid();
    bool collection, destroying = false;
  };
  std::shared_ptr<MetricState> state;
  opentelemetry::nostd::shared_ptr<trace::Tracer> tracer;
  opentelemetry::nostd::unique_ptr<metrics::Counter<std::uint64_t>> admitted_metric, completed_metric, incomplete;
  opentelemetry::nostd::unique_ptr<metrics::Histogram<double>> duration;
  opentelemetry::nostd::shared_ptr<metrics::ObservableInstrument> live_metric;
  ObjectLimits limits;
  mutable std::mutex mutex;
  std::map<std::string, Type> types;
  std::map<std::uint64_t, Observation> observations;
  using Index = std::map<std::pair<std::uintptr_t, std::uint32_t>, std::uint64_t>;
  Index index;
  std::atomic<std::uint64_t> lost{0}, invalid{0}, duplicates{0};
  std::uint64_t next = 1, admitted = 0, completed = 0, censored = 0, ending = 0;
  bool closing = false, closed = false, finalising = false;

  Impl(std::shared_ptr<MetricState> selected_state,
      opentelemetry::nostd::shared_ptr<metrics::Meter> meter,
      opentelemetry::nostd::shared_ptr<trace::Tracer> selected_tracer, ObjectLimits selected_limits)
      : state(std::move(selected_state)), tracer(std::move(selected_tracer)), limits(selected_limits) {
    if (!state || !meter || state->Status().closing || !limits.types || limits.types > 4096 ||
        !limits.live || limits.live > 1000000 ||
        (limits.boundary != LifetimeBoundary::destructor_entry && limits.boundary != LifetimeBoundary::free_entry &&
         limits.boundary != LifetimeBoundary::drop_entry))
      throw std::invalid_argument("object observations require open SDK inputs and bounded limits");
    admitted_metric = meter->CreateUInt64Counter("otelc.lifetime.admitted", "Successful selected initialisations", "{object}");
    completed_metric = meter->CreateUInt64Counter("otelc.lifetime.completed", "Observed selected release entries", "{object}");
    incomplete = meter->CreateUInt64Counter("otelc.lifetime.incomplete", "Observations censored at shutdown", "{object}");
    duration = meter->CreateDoubleHistogram("otelc.lifetime.duration", "Time until selected release entry or censoring", "s");
    live_metric = meter->CreateInt64ObservableGauge("otelc.lifetime.live", "Selected observations before release entry", "{object}");
    live_metric->AddCallback(Observe, this);
  }
  ~Impl() { live_metric->RemoveCallback(Observe, this); }
  const char *Boundary() const {
    if (limits.boundary == LifetimeBoundary::free_entry) return "free_entry";
    return limits.boundary == LifetimeBoundary::drop_entry ? "drop_entry" : "destructor_entry";
  }
  // The caller holds mutex. Remove the live-address entry before releasing its
  // storage, so reuse can start a new generation while this completion drains.
  std::uint64_t Claim(Index::iterator found, Clock::time_point end) {
    if (closed || finalising || found == index.end()) return 0;
    auto &item = observations.at(found->second);
    item.destroying = true;
    item.end = end;
    --item.type->live;
    index.erase(found);
    return item.token;
  }
  static void Observe(metrics::ObserverResult result, void *context) noexcept {
    auto &self = *static_cast<Impl *>(context);
    if (inside_object_runtime) { ++self.lost; return; }
    Guard guard;
    try {
      std::map<std::string, std::uint64_t> values;
      {
        std::lock_guard<std::mutex> lock(self.mutex);
        for (const auto &[_, type] : self.types) values[type.name] += type.live;
      }
      auto observer = opentelemetry::nostd::get<opentelemetry::nostd::shared_ptr<metrics::ObserverResultT<int64_t>>>(result);
      for (const auto &[name, count] : values)
        observer->Observe(static_cast<int64_t>(count), {{"type", name}, {"boundary", self.Boundary()}});
    } catch (...) { ++self.lost; }
  }
  void Emit(const Observation &item, bool complete, bool unwinding = false) noexcept {
    try {
      const char *boundary = complete ? Boundary() : "shutdown_censored";
      if (item.collection && state->Collecting()) {
        (complete ? completed_metric : incomplete)->Add(1, {{"type", item.type->name}, {"boundary", boundary}});
        duration->Record(std::chrono::duration<double>(item.end - item.start).count(),
            {{"type", item.type->name}, {"boundary", boundary}}, opentelemetry::context::Context{});
      }
      if (!tracer) return;
      trace::StartSpanOptions options;
      options.parent = opentelemetry::context::Context{}.SetValue(trace::kIsRootSpanKey, true);
      options.start_system_time = item.epoch;
      options.start_steady_time = item.start;
      std::vector<std::pair<trace::SpanContext, std::map<std::string, opentelemetry::common::AttributeValue>>> links;
      if (item.creation.IsValid()) links.emplace_back(item.creation, std::map<std::string, opentelemetry::common::AttributeValue>{});
      std::map<std::string, opentelemetry::common::AttributeValue> labels{
          {"type", item.type->name}, {"boundary", boundary}, {"otelc.lifetime.complete", complete}};
      auto instance = std::to_string(item.token);
      labels.emplace("otelc.lifetime.instance", instance);
      auto span = tracer->StartSpan(item.type->name + " lifetime", labels, links, options);
      if (complete && limits.boundary == LifetimeBoundary::destructor_entry)
        span->SetAttribute("otelc.destructor.exception_exit", unwinding);
      if (complete && limits.boundary == LifetimeBoundary::drop_entry)
        span->SetAttribute("otelc.drop.exception_exit", unwinding);
      trace::EndSpanOptions end;
      end.end_steady_time = item.end;
      span->End(end);
    } catch (...) { ++lost; }
  }
};

ObjectRuntime::ObjectRuntime(std::shared_ptr<MetricState> state,
    opentelemetry::nostd::shared_ptr<metrics::Meter> meter,
    opentelemetry::nostd::shared_ptr<trace::Tracer> tracer, ObjectLimits limits)
    : impl_(std::make_unique<Impl>(std::move(state), std::move(meter), std::move(tracer), limits)) {}
ObjectRuntime::~ObjectRuntime() { Close(); }

bool ObjectRuntime::Admit(const void *address, const char *type_usr, const char *type_name,
                         const trace::SpanContext &creation) noexcept {
  const SavedErrno saved;
  if (inside_object_runtime) { ++impl_->lost; return false; }
  Guard guard;
  auto epoch = std::chrono::system_clock::now();
  auto start = Clock::now();
  try {
    if (!address) { ++impl_->invalid; return false; }
    auto id = name(type_usr), label = name(type_name);
    bool collection = impl_->state->Collecting();
    {
      std::lock_guard<std::mutex> lock(impl_->mutex);
      if (impl_->closing || impl_->observations.size() + impl_->ending >= impl_->limits.live || !impl_->next) {
        ++impl_->lost; return false;
      }
      auto type = impl_->types.find(id);
      if (type == impl_->types.end()) {
        if (impl_->types.size() >= impl_->limits.types) { ++impl_->lost; return false; }
        type = impl_->types.emplace(id, Impl::Type{label, static_cast<std::uint32_t>(impl_->types.size())}).first;
      } else if (type->second.name != label) { ++impl_->invalid; return false; }
      auto key = std::make_pair(reinterpret_cast<std::uintptr_t>(address), type->second.id);
      auto existing = impl_->index.lower_bound({key.first, 0});
      if (impl_->index.count(key) || (impl_->limits.boundary == LifetimeBoundary::free_entry &&
          existing != impl_->index.end() && existing->first.first == key.first)) {
        ++impl_->duplicates; ++impl_->lost; return false;
      }
      auto token = impl_->next++;
      impl_->observations.emplace(token, Impl::Observation{&type->second, key.first, token, start, {}, epoch, creation, collection});
      try { impl_->index.emplace(key, token); }
      catch (...) { impl_->observations.erase(token); throw; }
      ++type->second.live;
      ++impl_->admitted;
    }
    if (collection && impl_->state->Collecting())
      impl_->admitted_metric->Add(1, {{"type", label}, {"boundary", impl_->Boundary()}});
    return true;
  } catch (...) { ++impl_->lost; return false; }
}

bool ObjectRuntime::Relocate(const void *source, const void *destination, const char *type_usr) noexcept {
  const SavedErrno saved;
  if (inside_object_runtime) { ++impl_->lost; return false; }
  Guard guard;
  try {
    if (!source || !destination) { ++impl_->invalid; return false; }
    auto id = name(type_usr);
    std::lock_guard<std::mutex> lock(impl_->mutex);
    if (impl_->limits.boundary != LifetimeBoundary::drop_entry || impl_->closing) {
      ++impl_->invalid; return false;
    }
    auto type = impl_->types.find(id);
    if (type == impl_->types.end()) return false;
    auto previous = std::make_pair(reinterpret_cast<std::uintptr_t>(source), type->second.id);
    auto found = impl_->index.find(previous);
    if (found == impl_->index.end()) return false;
    auto next = std::make_pair(reinterpret_cast<std::uintptr_t>(destination), type->second.id);
    if (previous == next) return true;
    if (impl_->index.count(next)) { ++impl_->duplicates; ++impl_->lost; return false; }
    auto &item = impl_->observations.at(found->second);
    // Extracting/reinserting a node in the same map needs no new index storage.
    // The mutex keeps the vacant destination and live generation stable.
    auto node = impl_->index.extract(found);
    node.key() = next;
    impl_->index.insert(std::move(node));
    item.address = next.first;
    return true;
  } catch (...) { ++impl_->invalid; return false; }
}

std::uint64_t ObjectRuntime::Destroying(const void *address, const char *type_usr) noexcept {
  const SavedErrno saved;
  if (inside_object_runtime) { ++impl_->lost; return 0; }
  Guard guard;
  try {
    auto end = Clock::now();
    auto id = name(type_usr);
    std::lock_guard<std::mutex> lock(impl_->mutex);
    if (impl_->closed || impl_->finalising) return 0;
    auto type = impl_->types.find(id);
    if (type == impl_->types.end()) return 0;
    auto key = std::make_pair(reinterpret_cast<std::uintptr_t>(address), type->second.id);
    auto found = impl_->index.find(key);
    return impl_->Claim(found, end);
  } catch (...) { ++impl_->invalid; return 0; }
}

std::uint64_t ObjectRuntime::Releasing(const void *address) noexcept {
  const SavedErrno saved;
  if (inside_object_runtime) { ++impl_->lost; return 0; }
  Guard guard;
  if (impl_->limits.boundary != LifetimeBoundary::free_entry) { ++impl_->invalid; return 0; }
  if (!address) return 0;
  try {
    auto end = Clock::now();
    auto key = reinterpret_cast<std::uintptr_t>(address);
    std::lock_guard<std::mutex> lock(impl_->mutex);
    auto found = impl_->index.lower_bound({key, 0});
    if (found == impl_->index.end() || found->first.first != key) return 0;
    return impl_->Claim(found, end);
  } catch (...) { ++impl_->invalid; return 0; }
}

bool ObjectRuntime::Destroyed(std::uint64_t token, std::int32_t unwinding) noexcept {
  const SavedErrno saved;
  if (!token) return true;
  if (inside_object_runtime) { ++impl_->invalid; return false; }
  Guard guard;
  Impl::Observation item{};
  {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    auto found = impl_->observations.find(token);
    if (found == impl_->observations.end() || !found->second.destroying || (unwinding != 0 && unwinding != 1) ||
        (impl_->limits.boundary == LifetimeBoundary::free_entry && unwinding != 0)) {
      ++impl_->invalid; return false;
    }
    item = std::move(found->second);
    impl_->observations.erase(found);
    ++impl_->ending;
  }
  impl_->Emit(item, true, unwinding != 0);
  std::lock_guard<std::mutex> lock(impl_->mutex);
  --impl_->ending;
  ++impl_->completed;
  return true;
}

ObjectStatus ObjectRuntime::Status() const {
  std::lock_guard<std::mutex> lock(impl_->mutex);
  return {impl_->types.size(), impl_->admitted, impl_->completed, impl_->index.size(),
      impl_->observations.size() - impl_->index.size(), impl_->ending, impl_->censored,
      impl_->lost.load(), impl_->duplicates.load(), impl_->invalid.load(), impl_->closing, impl_->closed};
}

bool ObjectRuntime::Close() noexcept {
  const SavedErrno saved;
  if (inside_object_runtime) return false;
  Guard guard;
  std::unique_lock<std::mutex> lock(impl_->mutex);
  impl_->closing = true;
  if (impl_->closed) return true;
  if (impl_->finalising || impl_->ending || impl_->observations.size() != impl_->index.size()) return false;
  impl_->finalising = true;
  const auto end = Clock::now();
  while (!impl_->observations.empty()) {
    auto found = impl_->observations.begin();
    auto item = std::move(found->second);
    item.end = end;
    --item.type->live;
    impl_->index.erase({item.address, item.type->id});
    impl_->observations.erase(found);
    ++impl_->ending;
    lock.unlock();
    impl_->Emit(item, false);
    lock.lock();
    --impl_->ending;
    ++impl_->censored;
  }
  impl_->finalising = false;
  impl_->closed = true;
  return true;
}
}  // namespace otelc::native
