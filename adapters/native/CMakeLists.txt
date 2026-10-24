cmake_minimum_required(VERSION 3.22)
project(otelc_native_controls LANGUAGES C CXX)

include(FetchContent)
set(OTELCPP_WITH_STL CXX17 CACHE STRING "" FORCE)
set(OTELCPP_BUILD_TESTING OFF CACHE BOOL "" FORCE)
set(OTELCPP_WITH_EXAMPLES OFF CACHE BOOL "" FORCE)
set(OTELCPP_WITH_BENCHMARK OFF CACHE BOOL "" FORCE)
set(OTELCPP_WITH_CONFIGURATION OFF CACHE BOOL "" FORCE)
set(OTELCPP_INSTALL OFF CACHE BOOL "" FORCE)
FetchContent_Declare(otelcpp
  URL https://codeload.github.com/open-telemetry/opentelemetry-cpp/tar.gz/c7b64ec660c1b6c65dff0932b3d1c3483bc6db64
  TLS_VERIFY ON
  URL_HASH SHA256=2e1df07f6217833e13819d12716f52fa93103dfe6b8826c76fb2868da339712f)
FetchContent_MakeAvailable(otelcpp)

add_library(otelc_native_controls STATIC src/metric_control.cpp)
target_include_directories(otelc_native_controls PUBLIC include)
target_compile_features(otelc_native_controls PUBLIC cxx_std_17)
target_link_libraries(otelc_native_controls PUBLIC opentelemetry_metrics opentelemetry_trace)
target_compile_options(otelc_native_controls PRIVATE -Wall -Wextra -Werror)

option(OTELC_NATIVE_COVERAGE "Profile otelc controls with LLVM tools" OFF)
if(OTELC_NATIVE_COVERAGE)
  target_compile_options(otelc_native_controls PRIVATE -fprofile-instr-generate -fcoverage-mapping)
  target_link_options(otelc_native_controls INTERFACE -fprofile-instr-generate)
endif()
enable_testing()
add_executable(otelc_native_controls_test tests/metric_control_test.cpp)
target_link_libraries(otelc_native_controls_test PRIVATE otelc_native_controls)
target_compile_options(otelc_native_controls_test PRIVATE -UNDEBUG -Wall -Wextra -Werror)
add_test(NAME native_metric_controls COMMAND otelc_native_controls_test)
set_tests_properties(native_metric_controls PROPERTIES TIMEOUT 20)
