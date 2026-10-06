#define _GNU_SOURCE
#include <stdint.h>
#include <stdlib.h>
#include <errno.h>
#include <pthread.h>
#ifdef __APPLE__
#include <mach-o/dyld.h>
#else
#include <link.h>
#endif

extern void otelc_initialize(void);
extern void otelc_shutdown(void);
extern intptr_t otelc_register_thread(void);
extern void otelc_retire_thread(intptr_t slot);
extern uint64_t otelc_record_token_enter(intptr_t slot, uintptr_t address);
extern void otelc_record_token_exit(intptr_t slot, uint64_t token, uint32_t kind);
extern uint64_t otelc_record_object_begin(const char *name);
extern void otelc_record_object_end(intptr_t slot, uint64_t token);
extern void otelc_record_enter(intptr_t slot, uintptr_t address);
extern void otelc_record_denied(uintptr_t address);
extern void otelc_record_exit(intptr_t slot, uintptr_t address);
static _Thread_local intptr_t thread_slot = -2;
static _Thread_local int busy;
static pthread_key_t retirement_key;
static int key_ready;

/* Retire after the owning thread has stopped producing observations. */
static void retire(void *value) {
    if (value) otelc_retire_thread((intptr_t)value - 1);
}
/* No lifecycle work is initiated from application probes. */
__attribute__((constructor(101))) static void initialize(void) {
    key_ready = pthread_key_create(&retirement_key, retire) == 0;
    if (key_ready) {
        otelc_initialize();
        atexit(otelc_shutdown);
    }
}
/* Cache the slot in native TLS; registration is a cold-path operation. */
static intptr_t slot(void) {
    if (thread_slot == -2) {
        thread_slot = otelc_register_thread();
        if (thread_slot >= 0 && pthread_setspecific(retirement_key, (void *)(thread_slot + 1))) {
            otelc_retire_thread(thread_slot);
            thread_slot = -1;
        }
    }
    return thread_slot;
}
void __cyg_profile_func_enter(void *function, void *caller) {
    (void)caller;
    int saved_errno = errno;
    if (!busy && key_ready) {
        busy = 1;
        intptr_t index = slot();
        if (index >= 0) otelc_record_enter(index, (uintptr_t)function);
        else otelc_record_denied((uintptr_t)function);
        busy = 0;
    }
    errno = saved_errno;
}
void __cyg_profile_func_exit(void *function, void *caller) {
    (void)caller;
    int saved_errno = errno;
    if (!busy && thread_slot >= 0) {
        busy = 1;
        otelc_record_exit(thread_slot, (uintptr_t)function);
        busy = 0;
    }
    errno = saved_errno;
}
uint64_t otelc_function_enter_v1(void *function) {
    int saved_errno = errno;
    uint64_t token = 0;
    if (!busy && key_ready) {
        busy = 1;
        intptr_t index = slot();
        if (index >= 0) token = otelc_record_token_enter(index, (uintptr_t)function);
        else otelc_record_denied((uintptr_t)function);
        busy = 0;
    }
    errno = saved_errno;
    return token;
}
void otelc_function_leave_v1(uint64_t token, uint32_t kind) {
    int saved_errno = errno;
    if (token && !busy && thread_slot >= 0) {
        busy = 1;
        otelc_record_token_exit(thread_slot, token, kind);
        busy = 0;
    }
    errno = saved_errno;
}
uint64_t otelc_object_begin_v1(const char *name) {
    int saved_errno = errno;
    uint64_t token = 0;
    if (!busy && key_ready) { busy = 1; token = otelc_record_object_begin(name); busy = 0; }
    errno = saved_errno; return token;
}
void otelc_object_end_v1(uint64_t token) {
    int saved_errno = errno;
    if (token && !busy && key_ready) { busy = 1; otelc_record_object_end(slot(), token); busy = 0; }
    errno = saved_errno;
}
#ifdef __APPLE__
intptr_t otelc_image_slide(void) { return _dyld_get_image_vmaddr_slide(0); }
#else
static int slide_callback(struct dl_phdr_info *info, size_t size, void *data) {
    (void)size;
    if (!info->dlpi_name || !*info->dlpi_name) {
        *(intptr_t *)data = (intptr_t)info->dlpi_addr;
        return 1;
    }
    return 0;
}
intptr_t otelc_image_slide(void) { intptr_t slide = 0; dl_iterate_phdr(slide_callback, &slide); return slide; }
#endif
