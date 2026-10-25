#include "otelc/native_runtime.h"
#include "otelc/function_runtime.h"
#include "otelc/object_runtime.h"
#include "otelc/deadline.h"
#include "resource_abi.h"

#include <arpa/inet.h>
#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>
#include <atomic>
#include <chrono>
#include <cerrno>
#include <cstring>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <set>
#include <future>

#include <nlohmann/json.hpp>
#include "opentelemetry/exporters/otlp/otlp_http_exporter_factory.h"
#include "opentelemetry/exporters/otlp/otlp_http_exporter_options.h"
#include "opentelemetry/exporters/otlp/otlp_http_metric_exporter_factory.h"
#include "opentelemetry/exporters/otlp/otlp_http_metric_exporter_options.h"
#include "opentelemetry/sdk/metrics/export/periodic_exporting_metric_reader.h"
#include "opentelemetry/sdk/trace/batch_span_processor.h"
#include "opentelemetry/sdk/trace/samplers/parent.h"
#include "opentelemetry/sdk/trace/samplers/trace_id_ratio.h"
#include "opentelemetry/sdk/trace/tracer_provider.h"
#include "opentelemetry/sdk/common/global_log_handler.h"

namespace {
using Json = nlohmann::json;
using Micros = std::chrono::microseconds;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
namespace native = otelc::native;
struct SavedErrno {
  const int value = errno;
  ~SavedErrno() noexcept { errno = value; }
};
namespace metrics = opentelemetry::sdk::metrics;
namespace traces = opentelemetry::sdk::trace;
namespace otlp = opentelemetry::exporter::otlp;
using Result = opentelemetry::sdk::common::ExportResult;

class Descriptor {
 public:
  explicit Descriptor(int value = -1) : value_(value) {}
  ~Descriptor() { Reset(); }
  Descriptor(const Descriptor &) = delete;
  Descriptor &operator=(const Descriptor &) = delete;
  int Get() const { return value_; }
  void Reset(int value = -1) { if (value_ >= 0) ::close(value_); value_ = value; }
 private:
  int value_;
};

class DiagnosticLog final : public opentelemetry::sdk::common::internal_log::LogHandler {
 public:
  std::atomic<std::uint64_t> dropped{0};
  explicit DiagnosticLog(int directory)
      : descriptor_(::openat(directory, "agent.log", O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600)) {
    if (descriptor_.Get() < 0) throw std::runtime_error("native diagnostic log cannot be created");
  }
  void Handle(opentelemetry::sdk::common::internal_log::LogLevel, const char *, int,
              const char *message, const opentelemetry::sdk::common::AttributeMap &) noexcept override {
    std::scoped_lock lock(mutex_);
    std::size_t length = message ? ::strnlen(message, 65536) : 0;
    if (bytes_ + length + 1 > 1024 * 1024) { ++dropped; return; }
    std::size_t offset = 0;
    while (offset < length) {
      auto count = ::write(descriptor_.Get(), message + offset, length - offset);
      if (count < 0 && errno == EINTR) continue;
      if (count <= 0) { ++dropped; return; }
      offset += std::size_t(count);
    }
    if (::write(descriptor_.Get(), "\n", 1) != 1) ++dropped;
    bytes_ += length + 1;
  }
 private:
  Descriptor descriptor_;
  std::mutex mutex_;
  std::size_t bytes_ = 0;
};

std::string Decimal(std::uint64_t value) { return std::to_string(value); }
void Require(bool condition, const char *message) { if (!condition) throw std::runtime_error(message); }
bool WriteAll(int fd, const std::string &value, bool socket = false) {
  std::size_t offset = 0;
  while (offset < value.size()) {
    int flags = 0;
#ifdef MSG_NOSIGNAL
    flags = MSG_NOSIGNAL;
#endif
    auto count = socket ? ::send(fd, value.data() + offset, value.size() - offset, flags)
                        : ::write(fd, value.data() + offset, value.size() - offset);
    if (count < 0 && errno == EINTR) continue;
    if (count <= 0) return false;
    offset += std::size_t(count);
  }
  return true;
}

struct ExportStats {
  mutable std::mutex mutex;
  std::uint64_t batches = 0, succeeded = 0, failed = 0, pending = 0;
  std::uint64_t units = 0, delivered = 0, rejected = 0;
  void Begin(std::uint64_t count) {
    std::scoped_lock lock(mutex); ++batches; ++pending; units += count;
  }
  void End(std::uint64_t count, Result result) {
    std::scoped_lock lock(mutex); --pending;
    if (result == Result::kSuccess) { ++succeeded; delivered += count; }
    else { ++failed; rejected += count; }
  }
  Json Status(const char *unit, bool observed = true) const {
    std::scoped_lock lock(mutex);
    return {{"exporters_observed", observed ? 1 : 0}, {"unit", unit},
            {"batches_started", Decimal(batches)}, {"batches_succeeded", Decimal(succeeded)},
            {"batches_failed", Decimal(failed)}, {"batches_pending", Decimal(pending)},
            {"units_started", Decimal(units)}, {"units_succeeded", Decimal(delivered)},
            {"units_failed", Decimal(rejected)}};
  }
};

class MetricExporter final : public metrics::PushMetricExporter {
 public:
  MetricExporter(std::unique_ptr<metrics::PushMetricExporter> delegate, ExportStats &stats)
      : delegate_(std::move(delegate)), stats_(stats) {}
  Result Export(const metrics::ResourceMetrics &data) noexcept override {
    std::uint64_t points = 0;
    for (const auto &scope : data.scope_metric_data_)
      for (const auto &metric : scope.metric_data_) points += metric.point_data_attr_.size();
    stats_.Begin(points); auto result = delegate_->Export(data); stats_.End(points, result); return result;
  }
  metrics::AggregationTemporality GetAggregationTemporality(metrics::InstrumentType type) const noexcept override {
    return delegate_->GetAggregationTemporality(type);
  }
  bool ForceFlush(Micros timeout) noexcept override { return delegate_->ForceFlush(timeout); }
  bool Shutdown(Micros timeout) noexcept override { return delegate_->Shutdown(timeout); }
 private:
  std::unique_ptr<metrics::PushMetricExporter> delegate_;
  ExportStats &stats_;
};

class SpanExporter final : public traces::SpanExporter {
 public:
  SpanExporter(std::unique_ptr<traces::SpanExporter> delegate, ExportStats &stats)
      : delegate_(std::move(delegate)), stats_(stats) {}
  std::unique_ptr<traces::Recordable> MakeRecordable() noexcept override { return delegate_->MakeRecordable(); }
  Result Export(const opentelemetry::nostd::span<std::unique_ptr<traces::Recordable>> &batch) noexcept override {
    stats_.Begin(batch.size()); auto result = delegate_->Export(batch); stats_.End(batch.size(), result); return result;
  }
  bool ForceFlush(Micros timeout) noexcept override { return delegate_->ForceFlush(timeout); }
  bool Shutdown(Micros timeout) noexcept override { return delegate_->Shutdown(timeout); }
 private:
  std::unique_ptr<traces::SpanExporter> delegate_;
  ExportStats &stats_;
};

class Processor final : public traces::SpanProcessor {
 public:
  std::atomic<std::uint64_t> started{0}, ended{0};
  explicit Processor(std::unique_ptr<traces::SpanProcessor> delegate) : delegate_(std::move(delegate)) {}
  std::unique_ptr<traces::Recordable> MakeRecordable() noexcept override { return delegate_->MakeRecordable(); }
  void OnStart(traces::Recordable &span, const opentelemetry::trace::SpanContext &parent) noexcept override {
    ++started; delegate_->OnStart(span, parent);
  }
  void OnEnd(std::unique_ptr<traces::Recordable> &&span) noexcept override { ++ended; delegate_->OnEnd(std::move(span)); }
  bool ForceFlush(Micros timeout) noexcept override { return delegate_->ForceFlush(timeout); }
  bool Shutdown(Micros timeout) noexcept override { return delegate_->Shutdown(timeout); }
 private:
  std::unique_ptr<traces::SpanProcessor> delegate_;
};

class Runtime {
 public:
  std::unique_ptr<native::FunctionRuntime> functions;
  std::unique_ptr<native::ObjectRuntime> objects;
  native::LifetimeBoundary lifetime_boundary = native::LifetimeBoundary::destructor_entry;
  ExportStats metric_stats, trace_stats;
  std::shared_ptr<native::MetricState> state;
  std::shared_ptr<metrics::MeterProvider> meters;
  std::unique_ptr<native::MetricController> controller;
  std::shared_ptr<traces::TracerProvider> tracer;
  Processor *processor = nullptr;
  Json plan;
  std::atomic<std::uint64_t> &metadata_errors;
  explicit Runtime(Json value, std::atomic<std::uint64_t> &errors)
      : plan(std::move(value)), metadata_errors(errors) {
    const auto &policy = plan.at("settings");
    auto resource = opentelemetry::sdk::resource::Resource::Create({
      {"service.name", policy.at("service_name").get<std::string>()},
      {"otelc.language", plan.at("language").get<std::string>()}});
    directory_.Reset(::open(plan.at("runtime").get<std::string>().c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC));
    struct stat info{};
    Require(directory_.Get() >= 0 && ::fstat(directory_.Get(), &info) == 0 && info.st_uid == ::getuid()
            && (info.st_mode & 0777) == 0700, "native runtime directory must be owned and private");
    diagnostics_ = std::make_shared<DiagnosticLog>(directory_.Get());
    opentelemetry::sdk::common::internal_log::GlobalLogHandler::SetLogHandler(diagnostics_);
    state = std::make_shared<native::MetricState>(policy.at("collection").get<bool>(), policy.at("emission").get<bool>());
    meters = std::make_shared<metrics::MeterProvider>(std::make_unique<metrics::ViewRegistry>(), resource);
    controller = std::make_unique<native::MetricController>(meters, state);
    native::ConfigureFunctionMetrics(*meters, 4096, policy.at("boundaries").get<std::vector<double>>());
    bool object_enabled = false;
    std::uint32_t object_limit = 0;
    const bool resources = policy.contains("resource_lifetimes");
    Require(!(resources && policy.contains("object_lifetimes")), "native lifetime profiles cannot be combined");
    const char *lifetime_key = resources ? "resource_lifetimes" : "object_lifetimes";
    if (policy.contains(lifetime_key)) {
      const auto &object = policy.at(lifetime_key);
      Require(object.is_object() && object.size() == 2 && object.at("enabled").is_boolean()
              && object.at("max_live").is_number_integer(), "invalid native object settings");
      auto limit = object.at("max_live").get<std::int64_t>();
      Require(limit > 0 && limit <= 1048576, "invalid native object live limit");
      object_enabled = object.at("enabled").get<bool>();
      Require(!object_enabled || plan.at("language") == (resources ? "c" : "cpp"),
              "native lifetime settings do not match the application language");
      lifetime_boundary = resources ? native::LifetimeBoundary::free_entry : native::LifetimeBoundary::destructor_entry;
      object_limit = static_cast<std::uint32_t>(limit);
      if (object_enabled)
        native::ConfigureObjectMetrics(*meters, 4096, policy.at("boundaries").get<std::vector<double>>());
    }
    auto endpoint = policy.at("endpoint").get<std::string>();
    otlp::OtlpHttpMetricExporterOptions metric_options(nullptr);
    metric_options.content_type = otlp::HttpRequestContentType::kBinary;
    metric_options.url = endpoint + "/v1/metrics";
    metric_options.timeout = std::chrono::milliseconds(policy.at("timeout_ms").get<int>());
    auto observed = std::make_unique<MetricExporter>(otlp::OtlpHttpMetricExporterFactory::Create(metric_options), metric_stats);
    auto gate = std::make_unique<native::GatedMetricExporter>(std::move(observed), state);
    metrics::PeriodicExportingMetricReaderOptions read_options;
    read_options.export_interval_millis = std::chrono::milliseconds(policy.at("interval_ms").get<int>());
    // This is the reader's collection budget, separate from the HTTP timeout.
    // The upstream reader silently replaces equal/longer budgets with defaults.
    // Owned function instruments are synchronous, including at a 1ms interval.
    read_options.export_timeout_millis = std::min(std::chrono::milliseconds(policy.at("timeout_ms").get<int>()),
        read_options.export_interval_millis / 2);
    meters->AddMetricReader(std::make_shared<metrics::PeriodicExportingMetricReader>(std::move(gate), read_options), controller->Filter());
    opentelemetry::nostd::shared_ptr<opentelemetry::trace::Tracer> selected;
    if (policy.at("traces").get<bool>()) {
      otlp::OtlpHttpExporterOptions options(nullptr);
      options.content_type = otlp::HttpRequestContentType::kBinary;
      options.url = endpoint + "/v1/traces";
      options.timeout = metric_options.timeout;
      traces::BatchSpanProcessorOptions batch;
      batch.max_queue_size = 65536; batch.max_export_batch_size = 512;
      batch.schedule_delay_millis = read_options.export_interval_millis;
      batch.export_timeout = std::chrono::milliseconds(policy.at("timeout_ms").get<int>());
      auto sink = std::make_unique<SpanExporter>(otlp::OtlpHttpExporterFactory::Create(options), trace_stats);
      auto observed_processor = std::make_unique<Processor>(std::make_unique<traces::BatchSpanProcessor>(std::move(sink), batch));
      processor = observed_processor.get();
      tracer = std::make_shared<traces::TracerProvider>(std::move(observed_processor), resource,
          std::make_unique<traces::ParentBasedSampler>(std::make_shared<traces::TraceIdRatioBasedSampler>(policy.at("sample_ratio").get<double>())));
      selected = tracer->GetTracer(native::FunctionScopeName);
    }
    functions = std::make_unique<native::FunctionRuntime>(state, meters->GetMeter(native::FunctionScopeName), selected,
        native::FunctionLimits{4096, 512, native::FunctionContext::owned});
    if (object_enabled)
      objects = std::make_unique<native::ObjectRuntime>(state, meters->GetMeter(native::FunctionScopeName), selected,
          native::ObjectLimits{4096, object_limit, lifetime_boundary});
  }

  ~Runtime() { StopControl(); }

  Json Status() const {
    auto status = state->Status();
    return {{"schema_version", 1}, {"pid", ::getpid()}, {"collection", status.collection},
            {"emission", status.emission}, {"effective_emission_off", (!status.collection || !status.emission)
                && !status.queued && !status.in_flight}};
  }
  bool Flush(Clock::time_point deadline) {
    ++flush_attempts_;
    flush_.store("pending");
    bool metric_ok = native::detail::BeforeDeadline(deadline, [&](Micros remaining) {
      return meters->ForceFlush(remaining);
    });
    bool trace_ok = !tracer || native::detail::BeforeDeadline(deadline, [&](Micros remaining) {
      return tracer->ForceFlush(remaining);
    });
    bool result = metric_ok && trace_ok;
    if (!result) ++flush_failed_;
    flush_.store(result ? "succeeded" : "failed");
    return result;
  }
  Json Health(bool final = false) const {
    auto function = functions->Status();
    auto started = processor ? processor->started.load() : 0;
    auto ended = processor ? processor->ended.load() : 0;
    Json queue_pending = nullptr, queue_dropped = nullptr;
    auto exported = trace_stats.Status("spans", bool(tracer));
    const char *shutdown = deadline_expired_.load() ? "timed_out" : shutdown_.load();
    bool drained = final && std::strcmp(shutdown, "succeeded") == 0;
    if (drained) {
      auto count = std::stoull(exported.at("units_started").get<std::string>());
      queue_pending = "0"; queue_dropped = Decimal(ended > count ? ended - count : 0);
    }
    Json result = {{"schema_version", 1}, {"kind", "otelc.runtime-health"}, {"language", plan.at("language")},
      {"scope", "sdk_pipeline"}, {"pid", ::getpid()}, {"phase", final ? "final" : "running"},
      {"sdk_attached", true}, {"policy_sha256", plan.at("policy_sha256")}, {"selection_sha256", plan.at("selection_sha256")},
      {"controls", Status()}, {"lifecycle", {{"flush", flush_.load()}, {"shutdown", shutdown}}},
      {"metric_flush", {{"attempts", Decimal(flush_attempts_.load())}, {"failed_attempts", Decimal(flush_failed_.load())}}},
      {"span_accounting", {{"processors_observed", processor ? 1 : 0}, {"batch_processors_observed", processor ? 1 : 0},
        {"sampled_started", Decimal(started)}, {"sampled_ended", Decimal(ended)},
        {"sampled_active", Decimal(started > ended ? started - ended : 0)}, {"queue_pending", queue_pending},
        {"queue_dropped", queue_dropped}, {"queue_evidence", drained ? "derived_after_shutdown" : "unavailable"}}},
      {"trace_exports", exported}, {"metric_exports", metric_stats.Status("metric_points")},
      {"diagnostics", {{"dropped", Decimal(diagnostics_->dropped.load())}}},
      {"function_accounting", {{"registered", Decimal(function.registered)}, {"started", Decimal(function.started)},
        {"ended", Decimal(function.ended)}, {"active", Decimal(function.active)}, {"lost", Decimal(function.lost)},
        {"invalid", Decimal(function.invalid)}, {"closed", function.closed},
        {"metadata_errors", Decimal(metadata_errors.load())}}},
      {"source_complete", false}, {"downstream_receipt", "unavailable"}, {"otlp_partial_rejection", "unavailable"}};
    if (objects) {
      const auto object = objects->Status();
      const bool resources = lifetime_boundary == native::LifetimeBoundary::free_entry;
      result[resources ? "resource_accounting" : "object_accounting"] = {{"types", Decimal(object.types)}, {"admitted", Decimal(object.admitted)},
        {"completed", Decimal(object.completed)}, {"live", Decimal(object.live)},
        {resources ? "releasing" : "destroying", Decimal(object.destroying)}, {"ending", Decimal(object.ending)},
        {"censored", Decimal(object.censored)}, {"lost", Decimal(object.lost)},
        {"duplicates", Decimal(object.duplicates)}, {"invalid", Decimal(object.invalid)},
        {"closing", object.closing}, {"closed", object.closed}};
    }
    return result;
  }

  void StartControl() {
    listener_ = ::socket(AF_INET, SOCK_STREAM, 0);
    Require(listener_ >= 0, "native control socket failed");
    ::fcntl(listener_, F_SETFD, FD_CLOEXEC);
    sockaddr_in address{}; address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    Require(::bind(listener_, reinterpret_cast<sockaddr *>(&address), sizeof(address)) == 0
            && ::listen(listener_, 8) == 0, "native control bind failed");
    socklen_t length = sizeof(address);
    Require(::getsockname(listener_, reinterpret_cast<sockaddr *>(&address), &length) == 0, "native control address failed");
    File("control.json", {{"schema_version", 1}, {"kind", "otelc.control.tcp"}, {"host", "127.0.0.1"},
      {"port", ntohs(address.sin_port)}, {"pid", ::getpid()}, {"token", plan.at("token")}});
    worker_ = std::thread([this] { ControlLoop(); });
  }
  void StopControl() {
    stopped_ = true;
    if (listener_ >= 0) ::shutdown(listener_, SHUT_RDWR);
    if (worker_.joinable()) worker_.join();
    if (listener_ >= 0) { ::close(listener_); listener_ = -1; }
  }
  void Finish() {
    auto deadline = Clock::now() + std::chrono::milliseconds(plan.at("settings").at("shutdown_timeout_ms").get<int>());
    stopped_ = true;
    if (listener_ >= 0) ::shutdown(listener_, SHUT_RDWR);
    auto completed = std::make_shared<std::promise<void>>();
    auto result = completed->get_future();
    std::thread drain([this, deadline, completed] {
      try { Drain(deadline); }
      catch (...) { shutdown_.store("failed"); }
      completed->set_value();
    });
    if (result.wait_until(deadline) == std::future_status::ready) {
      drain.join();
    } else {
      // Some official SDK shutdown paths join an HTTP worker beyond their
      // supplied timeout. The process-owned runtime outlives this detached
      // drain; no provider or active frame is freed underneath it.
      drain.detach();
      deadline_expired_.store(true);
      shutdown_.store("timed_out");
    }
    File("health.json", Health(true));
    directory_.Reset();
  }

 private:
  void Drain(Clock::time_point deadline) {
    StopControl();
    bool quiescent = functions->Close();
    while (!quiescent && Clock::now() < deadline) { std::this_thread::sleep_for(1ms); quiescent = functions->Close(); }
    if (quiescent && objects) {
      quiescent = objects->Close();
      while (!quiescent && Clock::now() < deadline) {
        std::this_thread::sleep_for(1ms);
        quiescent = objects->Close();
      }
    }
    if (quiescent) {
      bool flushed = Flush(deadline);
      bool trace_ok = !tracer || native::detail::BeforeDeadline(deadline, [&](Micros remaining) {
        return tracer->Shutdown(remaining);
      });
      bool metric_ok = native::detail::BeforeDeadline(deadline, [&](Micros remaining) {
        return meters->Shutdown(remaining);
      });
      shutdown_.store(flushed && trace_ok && metric_ok ? "succeeded" : "failed");
    } else { shutdown_.store("timed_out"); }
  }

  Descriptor directory_;
  std::shared_ptr<DiagnosticLog> diagnostics_;
  int listener_ = -1;
  std::atomic<bool> stopped_{false};
  std::atomic<bool> deadline_expired_{false};
  std::thread worker_;
  std::atomic<std::uint64_t> flush_attempts_{0}, flush_failed_{0};
  std::atomic<const char *> flush_{"not_started"}, shutdown_{"not_started"};
  void File(const char *name, const Json &value) {
    int fd = ::openat(directory_.Get(), name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    Require(fd >= 0, "native runtime output already exists or cannot be created");
    bool written = WriteAll(fd, value.dump() + "\n"); ::close(fd);
    Require(written, "native runtime output write failed");
  }
  Json Command(const std::string &name) {
    bool accepted = true;
    if (name == "health") return Health();
    if (name == "flush") accepted = Flush(Clock::now() + 2s);
    else if (name == "on" || name == "off") {
      accepted = controller->SetCollection(name == "on") && state->SetEmission(name == "on");
    } else if (name == "collection on" || name == "collection off") accepted = controller->SetCollection(name == "collection on");
    else if (name == "emission on" || name == "emission off") accepted = state->SetEmission(name == "emission on");
    else if (name != "status") accepted = false;
    auto status = Status(); if (!accepted) status["error"] = "native control request failed";
    return status;
  }
  void Connection(int client) {
#ifdef SO_NOSIGPIPE
    int enabled = 1; ::setsockopt(client, SOL_SOCKET, SO_NOSIGPIPE, &enabled, sizeof(enabled));
#endif
    std::string request; char buffer[512];
    auto deadline = Clock::now() + 2s;
    while (!stopped_ && Clock::now() < deadline && request.size() < 4096) {
      pollfd ready{client, POLLIN, 0};
      if (::poll(&ready, 1, 50) <= 0) continue;
      auto count = ::recv(client, buffer, sizeof(buffer), 0);
      if (count <= 0) return;
      request.append(buffer, std::size_t(count));
      if (request.back() != '\n') continue;
      try {
        std::set<std::string> keys;
        auto data = Json::parse(request, [&keys](int depth, Json::parse_event_t event, Json &parsed) {
          if (event == Json::parse_event_t::key)
            Require(depth == 1 && keys.insert(parsed.get<std::string>()).second, "duplicate or nested control key");
          return true;
        });
        Require(data.is_object() && data.size() == 2 && data.at("token") == plan.at("token"), "native control authentication failed");
        auto response = Command(data.at("command").get<std::string>()).dump() + "\n";
        WriteAll(client, response, true);
      } catch (const std::exception &) { WriteAll(client, "{\"error\":\"invalid native control request\"}\n", true); }
      return;
    }
  }
  void ControlLoop() {
    while (!stopped_) {
      pollfd ready{listener_, POLLIN, 0};
      if (::poll(&ready, 1, 50) <= 0) continue;
      int client = ::accept(listener_, nullptr, nullptr);
      if (client < 0) continue;
      ::fcntl(client, F_SETFD, FD_CLOEXEC);
      Connection(client); ::close(client);
    }
  }
};

struct Registry {
  std::mutex mutex;
  std::map<const void *, std::string> pending;
  Runtime *runtime = nullptr;
  std::atomic<std::uint64_t> errors{0};
  bool starting = false, finished = false;
};
Registry &Global() { static auto *value = new Registry; return *value; }
Runtime *Installed() { auto &state = Global(); std::scoped_lock lock(state.mutex); return state.runtime; }
Json ReadPlan(int fd) {
  struct stat info{};
  Require(::fstat(fd, &info) == 0 && S_ISREG(info.st_mode) && info.st_uid == ::getuid()
          && (info.st_mode & 0777) == 0600 && info.st_size > 0 && info.st_size <= 65536, "invalid native plan descriptor");
  std::string raw(std::size_t(info.st_size), '\0');
  auto count = ::pread(fd, raw.data(), raw.size(), 0);
  Require(count == info.st_size, "incomplete native plan");
  auto plan = Json::parse(raw);
  Require(plan.at("schema_version") == 1 && (plan.at("language") == "c" || plan.at("language") == "cpp"), "unsupported native plan");
  return plan;
}
}  // namespace

extern "C" void otelc_function_register_v1(const otelc_function_descriptor_v1 *items, std::uint64_t count) noexcept {
  auto &global = Global();
  try {
    std::scoped_lock lock(global.mutex);
    Require(items && count <= 4096, "invalid native registry batch");
    if (global.runtime) { if (!global.runtime->functions->Register(items, count)) ++global.errors; return; }
    auto pending = global.pending;
    for (std::uint64_t index = 0; index < count; ++index) {
      Require(items[index].address && items[index].name, "invalid native descriptor");
      auto length = ::strnlen(items[index].name, 4097);
      Require(length > 0 && length <= 4096, "invalid native function name");
      auto [found, added] = pending.emplace(items[index].address, std::string(items[index].name, length));
      Require((added || found->second == items[index].name) && pending.size() <= 4096, "conflicting native descriptors");
    }
    global.pending.swap(pending);
  } catch (...) { ++global.errors; }
}
extern "C" std::uint64_t otelc_function_enter_v1(const void *address) noexcept {
  if (auto *runtime = Installed()) return runtime->functions->Enter(address);
  auto &state = Global(); std::scoped_lock lock(state.mutex); ++state.errors; return 0;
}
extern "C" void otelc_function_leave_v1(std::uint64_t token, std::int32_t unwinding) noexcept {
  if (auto *runtime = Installed()) runtime->functions->Leave(token, unwinding);
}
extern "C" void otelc_object_created_v1(const void *address, const char *type, const char *name) noexcept {
  if (auto *runtime = Installed(); runtime && runtime->objects) {
    runtime->objects->Admit(address, type, name, runtime->functions->CurrentContext());
  } else {
    ++Global().errors;
  }
}
extern "C" std::uint64_t otelc_object_destroying_v1(const void *address, const char *type) noexcept {
  if (auto *runtime = Installed(); runtime && runtime->objects)
    return runtime->objects->Destroying(address, type);
  ++Global().errors;
  return 0;
}
extern "C" void otelc_object_destroyed_v1(std::uint64_t token, std::int32_t unwinding) noexcept {
  if (auto *runtime = Installed(); runtime && runtime->objects)
    runtime->objects->Destroyed(token, unwinding);
  else
    ++Global().errors;
}
extern "C" void otelc_resource_allocated_v1(const void *address, const char *type, const char *name) noexcept {
  const SavedErrno saved;
  if (auto *runtime = Installed(); runtime && runtime->objects &&
      runtime->lifetime_boundary == native::LifetimeBoundary::free_entry)
    runtime->objects->Admit(address, type, name, runtime->functions->CurrentContext());
  else
    ++Global().errors;
}
extern "C" std::uint64_t otelc_resource_releasing_v1(const void *address) noexcept {
  const SavedErrno saved;
  if (auto *runtime = Installed(); runtime && runtime->objects &&
      runtime->lifetime_boundary == native::LifetimeBoundary::free_entry)
    return runtime->objects->Releasing(address);
  ++Global().errors;
  return 0;
}
extern "C" void otelc_resource_released_v1(std::uint64_t token) noexcept {
  const SavedErrno saved;
  if (auto *runtime = Installed(); runtime && runtime->objects &&
      runtime->lifetime_boundary == native::LifetimeBoundary::free_entry)
    runtime->objects->Destroyed(token, 0);
  else
    ++Global().errors;
}
extern "C" int otelc_native_start_v1(int descriptor) noexcept {
  auto &global = Global();
  Descriptor input(descriptor);
  try {
    { std::scoped_lock lock(global.mutex); Require(!global.starting, "native startup repeated"); global.starting = true; }
    auto plan = ReadPlan(input.Get()); input.Reset();
    auto runtime = std::make_unique<Runtime>(std::move(plan), global.errors);
    {
      std::scoped_lock lock(global.mutex);
      std::vector<otelc_function_descriptor_v1> descriptors;
      for (const auto &item : global.pending) descriptors.push_back({item.first, item.second.c_str()});
      Require(!global.errors && runtime->functions->Register(descriptors.data(), descriptors.size()), "native registration failed");
      runtime->StartControl();
      global.runtime = runtime.release();
    }
    return 0;
  } catch (...) { return 78; }
}
extern "C" void otelc_native_finish_v1() noexcept {
  auto &global = Global(); Runtime *runtime;
  { std::scoped_lock lock(global.mutex); if (global.finished) return; global.finished = true; runtime = global.runtime; }
  if (runtime) try { runtime->Finish(); } catch (...) { /* A failed final report must not throw into application teardown. */ }
}
