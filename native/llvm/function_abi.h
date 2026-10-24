#ifndef OTELC_FUNCTION_ABI_H
#define OTELC_FUNCTION_ABI_H

#include <stdint.h>

/* Tool-generated descriptors remain valid for their loaded image's lifetime. */
typedef struct otelc_function_descriptor_v1 {
  const void *address;
  const char *name;
} otelc_function_descriptor_v1;

#ifdef __cplusplus
extern "C" {
#define OTELC_FUNCTION_NOEXCEPT noexcept
#else
#define OTELC_FUNCTION_NOEXCEPT
#endif

uint64_t otelc_function_enter_v1(const void *address) OTELC_FUNCTION_NOEXCEPT;
void otelc_function_leave_v1(uint64_t token, int32_t unwinding) OTELC_FUNCTION_NOEXCEPT;
/* Required only by the explicitly selected registered profile (probe ABI2). */
void otelc_function_register_v1(const otelc_function_descriptor_v1 *functions,
                                uint64_t count) OTELC_FUNCTION_NOEXCEPT;

#ifdef __cplusplus
}
#endif
#undef OTELC_FUNCTION_NOEXCEPT

#endif
