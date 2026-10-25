#pragma once

#include <cstdint>
#include <memory>
#include <string_view>
#include <vector>

#include "function_abi.h"
#include "otelc/metric_control.h"
#include "opentelemetry/metrics/meter.h"
#include "opentelemetry/trace/tracer.h"

namespace otelc::native {

inline constexpr char FunctionScopeName[] = "io.quux.otelc.native";

// Install once on the tooling-owned provider, before function instruments exist.
// Reader-level cardinality setters are not enforced in the pinned SDK; views are.
void ConfigureFunctionMetrics(opentelemetry::sdk::metrics::MeterProvider &provider,
    std::uint32_t functions = 4096,
    const std::vector<double> &boundaries = {1e-6, 1e-5, 1e-4, .001, .01, .1, 1.});

enum class FunctionContext { ambient, owned };

struct FunctionLimits {
  std::uint32_t functions = 4096;
  std::uint32_t active = 512;
  // Owned mode uses explicit probe parentage without SDK thread-local scopes.
  FunctionContext context = FunctionContext::ambient;
};

struct FunctionStatus {
  std::uint64_t registered;
  std::uint64_t started;
  std::uint64_t ended;
  std::uint64_t active;
  std::uint64_t lost;
  std::uint64_t invalid;
  bool closing;
  bool closed;
};

// Tooling owns this engine and its providers. Keep it alive until Close succeeds;
// SDK scopes must be detached by the thread that entered their function.
class FunctionRuntime {
 public:
  FunctionRuntime(std::shared_ptr<MetricState> state,
                  opentelemetry::nostd::shared_ptr<opentelemetry::metrics::Meter> meter,
                  opentelemetry::nostd::shared_ptr<opentelemetry::trace::Tracer> tracer,
                  FunctionLimits limits = {});
  ~FunctionRuntime();
  FunctionRuntime(const FunctionRuntime &) = delete;
  FunctionRuntime &operator=(const FunctionRuntime &) = delete;

  bool Register(const otelc_function_descriptor_v1 *functions, std::uint64_t count) noexcept;
  std::uint64_t Enter(const void *address) noexcept;
  // Compiler-owned identities need not have a reifiable native function
  // address (for example generic Rust bodies). Names are copied and share the
  // same registration budget as address-backed functions.
  std::uint64_t EnterNamed(std::string_view name) noexcept;
  bool Leave(std::uint64_t token, std::int32_t unwinding) noexcept;
  FunctionStatus Status() const;
  // Snapshot the selected function's explicit context without reading SDK TLS.
  opentelemetry::trace::SpanContext CurrentContext() const noexcept;
  // Stop admission immediately. Existing calls can still leave on their owner
  // threads; false means the caller must retain the engine and try again later.
  bool Close();

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace otelc::native
