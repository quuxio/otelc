#pragma once

#include <chrono>

namespace otelc::native::detail {

// Parts of the SDK interpret zero as an unlimited wait. Take one budget
// snapshot and pass that same positive value, or do not start the operation.
template <typename Clock = std::chrono::steady_clock, typename Operation>
bool BeforeDeadline(typename Clock::time_point deadline, Operation operation) {
  using Micros = std::chrono::microseconds;
  const auto timeout = deadline == Clock::time_point::max()
      ? Micros::max()
      : std::chrono::duration_cast<Micros>(deadline - Clock::now());
  return timeout > Micros::zero() && operation(timeout);
}

}  // namespace otelc::native::detail
