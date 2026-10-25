#ifndef OTELC_OBJECT_ABI_H
#define OTELC_OBJECT_ABI_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#define OTELC_OBJECT_NOEXCEPT noexcept
#else
#define OTELC_OBJECT_NOEXCEPT
#endif
/* Compiler-owned strings remain valid for the image lifetime. No application
   object is retained or dereferenced by these observation hooks. */
void otelc_object_created_v1(const void *address, const char *type_usr,
                             const char *name) OTELC_OBJECT_NOEXCEPT;
uint64_t otelc_object_destroying_v1(const void *address,
                                  const char *type_usr) OTELC_OBJECT_NOEXCEPT;
void otelc_object_destroyed_v1(uint64_t token, int32_t unwinding) OTELC_OBJECT_NOEXCEPT;
/* Private drop-profile hooks. Relocation moves only SDK metadata; it never
   reads or writes application storage. Bad compiler identities are visible. */
void otelc_object_relocated_v1(const void *source, const void *destination,
                             const char *type_usr) OTELC_OBJECT_NOEXCEPT;
void otelc_object_metadata_error_v1(void) OTELC_OBJECT_NOEXCEPT;
#ifdef __cplusplus
}
#endif
#undef OTELC_OBJECT_NOEXCEPT
#endif
