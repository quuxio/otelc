/* Check parser boundaries without mutating a loaded interpreter image. */
#include <Python.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include "OriginChain.c"
#include "OriginLoader.h"

#if OTELC_ORIGIN_CHAIN_AVAILABLE
typedef union {
    max_align_t alignment;
    unsigned char bytes[sizeof(struct mach_header_64) + 2 * sizeof(struct uuid_command)];
} Image;

static struct mach_header_64 *image(Image *storage) {
    memset(storage, 0, sizeof(*storage));
    struct mach_header_64 *header = (void *)storage->bytes;
    header->magic = MH_MAGIC_64;
    header->cputype = CPU_TYPE_ARM64;
    header->ncmds = 1;
    header->sizeofcmds = sizeof(struct uuid_command);
    struct uuid_command *command = (void *)(header + 1);
    command->cmd = LC_UUID;
    command->cmdsize = sizeof(*command);
    memcpy(command->uuid, origin_uuid, 16);
    return header;
}

int main(void) {
    Image storage;
    struct mach_header_64 *header = image(&storage);
    if (!matching_uuid(header) || otelc_py_origin_chain_ready() || otelc_py_origin_chain_matches()) return 1;
    for (unsigned change = 0; change < 12; ++change) {
        header = image(&storage);
        struct uuid_command *command = (void *)(header + 1);
        switch (change) {
            case 0: header->magic = 0; break;
            case 1: header->cputype = 0; break;
            case 2: header->ncmds = 4097; break;
            case 3: header->sizeofcmds = 2 * 1024 * 1024; break;
            case 4: command->cmdsize = 7; break;
            case 5: command->cmdsize += 8; break;
            case 6: command->uuid[0] ^= 255; break;
            case 7: command->cmdsize -= 8; break;
            case 8: command->cmd = 0; break;
            case 9: header->ncmds = 2; break;
            case 10:
                header->ncmds = 2;
                header->sizeofcmds *= 2;
                memcpy(command + 1, command, sizeof(*command));
                break;
            case 11: header->sizeofcmds = 1; break;
        }
        if (matching_uuid(header)) return 10 + (int)change;
    }
    puts("supported");
    return 0;
}
#else
int main(void) {
    if (otelc_py_origin_chain_ready() != -1 || otelc_py_origin_chain_matches() ||
            otelc_py_origin_entry_install() || otelc_py_origin_entry_ready(0) != -1 ||
            otelc_py_origin_entry_active()) return 1;
    otelc_py_origin_entry_read(NULL, NULL, 0, 0);
    otelc_py_origin_entry_cancel();
    if (otelc_py_origin_module(NULL, NULL, 0, 0, 0, 0) != -1 ||
            otelc_py_origin_local_callback(NULL, 0, 0, NULL) != -1 ||
            otelc_py_origin_loader_ready() != -1 || otelc_py_origin_loader_active() ||
            otelc_py_origin_loader_start(NULL) || otelc_py_origin_loader_read(NULL, NULL, 0)) return 2;
    otelc_py_origin_loader_cancel();
    puts("unavailable");
    return 0;
}
#endif
