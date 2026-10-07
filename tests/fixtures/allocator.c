/* The application's allocator can also be called by the telemetry runtime. */
#include <errno.h>
#include <stdlib.h>
#ifdef __APPLE__
#include <malloc/malloc.h>
void *malloc(size_t size) {
    return malloc_zone_malloc(malloc_default_zone(), size);
}
#else
/* This fixture qualifies the macOS runner, without interposing glibc internals. */
#error allocator fixture requires the macOS zone allocator
#endif
static void *volatile allocation;
int main(void) {
    errno = EDOM;
    allocation = malloc(17);
    int unchanged = errno == EDOM;
    free(allocation);
    return !unchanged;
}
