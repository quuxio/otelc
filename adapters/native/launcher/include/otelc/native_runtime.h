#ifndef OTELC_NATIVE_RUNTIME_H
#define OTELC_NATIVE_RUNTIME_H

#ifdef __cplusplus
extern "C" {
#define OTELC_NATIVE_NOEXCEPT noexcept
#else
#define OTELC_NATIVE_NOEXCEPT
#endif
/* Called by tooling-generated startup/exit code, never application source. */
int otelc_native_start_v1(int plan_descriptor) OTELC_NATIVE_NOEXCEPT;
void otelc_native_finish_v1(void) OTELC_NATIVE_NOEXCEPT;
#ifdef __cplusplus
}
#endif
#undef OTELC_NATIVE_NOEXCEPT
#endif
