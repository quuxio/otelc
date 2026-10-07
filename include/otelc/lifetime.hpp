#pragma once
#include <cstdint>
#include <utility>
extern "C" std::uint64_t otelc_object_begin_v1(const char *class_name) noexcept;
extern "C" void otelc_object_end_v1(std::uint64_t token) noexcept;
namespace otelc {
// Place first in a class to destroy last. Call start at the end of a successful
// constructor when failed construction must not count as an object lifetime.
class ObjectLifetime {
    std::uint64_t token_ = 0;
public:
    ObjectLifetime() noexcept = default;
    explicit ObjectLifetime(const char *class_name) noexcept { start(class_name); }
    ObjectLifetime(const ObjectLifetime &) = delete;
    ObjectLifetime &operator=(const ObjectLifetime &) = delete;
    ObjectLifetime(ObjectLifetime &&other) noexcept : token_(std::exchange(other.token_, 0)) {}
    ObjectLifetime &operator=(ObjectLifetime &&other) noexcept {
        if (this != &other) { finish(); token_ = std::exchange(other.token_, 0); }
        return *this;
    }
    ~ObjectLifetime() noexcept { finish(); }
    void start(const char *class_name) noexcept { finish(); token_ = otelc_object_begin_v1(class_name); }
    void finish() noexcept { otelc_object_end_v1(std::exchange(token_, 0)); }
};
}
