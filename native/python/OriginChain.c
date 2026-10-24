/* Bind the loaded interpreter and the exact native compiler emission path. */
#define _POSIX_C_SOURCE 200809L
#include <Python.h>
#include <stdint.h>
#include "OriginEntry.h"
#include "OriginChain.h"

#if OTELC_ORIGIN_CHAIN_AVAILABLE
#include <dlfcn.h>
#include <execinfo.h>
#include <limits.h>
#include <mach-o/loader.h>
#include <stdlib.h>
#include <string.h>

static const void *python_base;

static int matching_uuid(const struct mach_header_64 *image) {
    if (image->magic != MH_MAGIC_64 || image->cputype != CPU_TYPE_ARM64 ||
            image->ncmds > 4096 || image->sizeofcmds > 1024U * 1024U) return 0;
    const unsigned char *cursor = (const unsigned char *)(image + 1);
    const unsigned char *end = cursor + image->sizeofcmds;
    unsigned found = 0;
    for (uint32_t index = 0; index < image->ncmds; ++index) {
        if ((size_t)(end - cursor) < sizeof(struct load_command)) return 0;
        const struct load_command *command = (const struct load_command *)cursor;
        if (command->cmdsize < sizeof(*command) || command->cmdsize % 8 ||
                command->cmdsize > (size_t)(end - cursor)) return 0;
        if (command->cmd == LC_UUID) {
            if (command->cmdsize != sizeof(struct uuid_command) || ++found != 1 ||
                    memcmp(((const struct uuid_command *)command)->uuid, origin_uuid, 16)) return 0;
        }
        cursor += command->cmdsize;
    }
    return cursor == end && found == 1;
}

int otelc_py_origin_chain_ready(void) {
    Dl_info location;
    char actual[PATH_MAX];
    if (!dladdr((const void *)Py_GetVersion, &location) || !location.dli_fbase ||
            !location.dli_fname || !realpath(location.dli_fname, actual) ||
            strcmp(actual, origin_library) || !matching_uuid(location.dli_fbase)) return -1;
    python_base = location.dli_fbase;
    return 0;
}

__attribute__((noinline)) int otelc_py_origin_chain_matches(void) {
    void *frames[32];
    int length = backtrace(frames, 32);
    size_t count = sizeof(origin_chain) / sizeof(origin_chain[0]);
    /* This function and its direct native audit callback precede the chain.
       A matching outer frame cannot excuse an intervening re-entrant caller. */
    if (!python_base || length < (int)count + 2) return 0;
    for (size_t index = 0; index < count; ++index) {
        Dl_info actual;
        if (!dladdr(frames[index + 2], &actual) || actual.dli_fbase != python_base) return 0;
        uintptr_t offset = (uintptr_t)frames[index + 2] - (uintptr_t)python_base;
        OriginFrame expected = origin_chain[index];
        if (offset < expected.start || offset >= expected.end ||
                (expected.pc && offset != expected.pc)) return 0;
    }
    return 1;
}
#else
int otelc_py_origin_chain_ready(void) { return -1; }
int otelc_py_origin_chain_matches(void) { return 0; }
#endif
