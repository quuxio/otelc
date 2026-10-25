cmake_minimum_required(VERSION 3.22)
project(otelc_native_launch LANGUAGES C CXX)

include(FetchContent)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(OTELCPP_WITH_OTLP_HTTP ON CACHE BOOL "" FORCE)
set(OTELCPP_WITH_OTLP_GRPC OFF CACHE BOOL "" FORCE)
set(OTELCPP_WITH_ASYNC_EXPORT_PREVIEW OFF CACHE BOOL "" FORCE)
set(JSON_BuildTests OFF CACHE BOOL "" FORCE)
set(JSON_Install OFF CACHE BOOL "" FORCE)
FetchContent_Declare(opentelemetry-proto
  URL https://codeload.github.com/open-telemetry/opentelemetry-proto/tar.gz/790608c4d51e6ffc12210b541e8514cbed9e91a4
  TLS_VERIFY ON
  URL_HASH SHA256=b8331fd497a17008da3efdc2ccf25da19aa4653b62cfc49cf181f22373573bc8)
FetchContent_Declare(nlohmann_json
  URL https://codeload.github.com/nlohmann/json/tar.gz/55f93686c01528224f448c19128836e7df245f72
  TLS_VERIFY ON
  URL_HASH SHA256=67f4cdd9ca930c9c1e130af4a437c7fc98fab77a2846fc2d2a14b4943831f8ef)
FetchContent_MakeAvailable(nlohmann_json)
add_subdirectory(.. native-core EXCLUDE_FROM_ALL)

add_library(otelc_native_runtime SHARED src/runtime.cpp)
target_compile_features(otelc_native_runtime PRIVATE cxx_std_17)
target_compile_options(otelc_native_runtime PRIVATE -Wall -Wextra -Werror)
target_link_libraries(otelc_native_runtime PRIVATE otelc_native_controls
  opentelemetry_exporter_otlp_http opentelemetry_exporter_otlp_http_metric
  nlohmann_json::nlohmann_json)
target_include_directories(otelc_native_runtime PUBLIC include)

option(OTELC_LAUNCH_COVERAGE "Profile native launch runtime with LLVM" OFF)
if(OTELC_LAUNCH_COVERAGE)
  target_compile_options(otelc_native_runtime PRIVATE -fprofile-instr-generate -fcoverage-mapping -fprofile-update=atomic)
  target_link_options(otelc_native_runtime PRIVATE -fprofile-instr-generate)
endif()
