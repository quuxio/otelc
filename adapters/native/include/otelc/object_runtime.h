#pragma once
#include <cstdint>
#include <memory>
#include <vector>
#include "object_abi.h"
#include "otelc/metric_control.h"
#include "opentelemetry/metrics/meter.h"
#include "opentelemetry/trace/tracer.h"

namespace otelc::native {
void ConfigureObjectMetrics(opentelemetry::sdk::metrics::MeterProvider &provider,
    std::uint32_t types = 4096,
    const std::vector<double> &boundaries = {1e-6, 1e-5, 1e-4, .001, .01, .1, 1.});
struct ObjectLimits { std::uint32_t types = 4096, live = 4096; };
struct ObjectStatus {
  std::uint64_t types, admitted, completed, live, destroying, ending, censored;
  std::uint64_t lost, duplicates, invalid;
  bool closing, closed;
};
// The observation starts after successful initialisation and ends at the first
// destructor entry. A generation token separately tracks cleanup completion.
// Keep this engine and its providers alive until Close returns true.
class ObjectRuntime {
 public:
  ObjectRuntime(std::shared_ptr<MetricState> state,
      opentelemetry::nostd::shared_ptr<opentelemetry::metrics::Meter> meter,
      opentelemetry::nostd::shared_ptr<opentelemetry::trace::Tracer> tracer,
      ObjectLimits limits = {});
  ~ObjectRuntime();
  ObjectRuntime(const ObjectRuntime &) = delete;
  ObjectRuntime &operator=(const ObjectRuntime &) = delete;
  bool Admit(const void *address, const char *type_usr, const char *name,
      const opentelemetry::trace::SpanContext &creation =
          opentelemetry::trace::SpanContext::GetInvalid()) noexcept;
  std::uint64_t Destroying(const void *address, const char *type_usr) noexcept;
  bool Destroyed(std::uint64_t token, std::int32_t unwinding) noexcept;
  ObjectStatus Status() const;
  // Refuse new admission, wait for already claimed cleanup and SDK callbacks,
  // then finish remaining observations as censored, never as destroyed.
  bool Close() noexcept;
 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
}  // namespace otelc::native
