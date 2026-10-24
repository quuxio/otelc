/* Force preceding startup stages ready; require real cache-failure cancellation. */
#include <stdlib.h>
#include "OriginEntry.h"
#include "OriginLoader.h"

static int entry_ready_calls, loader_ready_calls, entry_cancels, loader_cancels;

int otelc_py_origin_entry_ready(int optimization) {
    (void)optimization;
    entry_ready_calls++;
    return 0;
}
int otelc_py_origin_loader_ready(void) {
    loader_ready_calls++;
    return 0;
}
void otelc_py_origin_entry_cancel(void) { entry_cancels++; }
void otelc_py_origin_loader_cancel(void) { loader_cancels++; }

static void verify_cleanup(void) {
    if (entry_ready_calls != 1 || loader_ready_calls != 1 ||
            entry_cancels != 1 || loader_cancels != 1) _Exit(99);
}
__attribute__((constructor)) static void require_cleanup(void) {
    if (atexit(verify_cleanup)) _Exit(98);
}
