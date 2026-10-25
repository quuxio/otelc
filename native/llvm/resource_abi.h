#ifndef OTELC_RESOURCE_ABI_H
#define OTELC_RESOURCE_ABI_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#define OTELC_RESOURCE_NOEXCEPT noexcept
#else
#define OTELC_RESOURCE_NOEXCEPT
#endif
/* Observe addresses only. The original allocator and free still own storage. */
void otelc_resource_allocated_v1(const void *address, const char *type_usr,
                                 const char *name) OTELC_RESOURCE_NOEXCEPT;
uint64_t
otelc_resource_releasing_v1(const void *address) OTELC_RESOURCE_NOEXCEPT;
void otelc_resource_released_v1(uint64_t token) OTELC_RESOURCE_NOEXCEPT;
#ifdef __cplusplus
}
#endif
#undef OTELC_RESOURCE_NOEXCEPT
#endif
