/* Force SDK span-name allocation to hold the store until the test releases it. */
#include <errno.h>
#include <malloc/malloc.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static _Atomic int armed;
static _Atomic int blocked;
void *malloc(size_t size) {
    if (size == 127 && atomic_load(&armed)) {
        char name[64] = {0};
        pthread_getname_np(pthread_self(), name, sizeof(name));
        if (strcmp(name, "otelc-traces") == 0) {
            atomic_store(&blocked, 1);
            char byte;
            while (read(STDIN_FILENO, &byte, 1) < 0 && errno == EINTR) {}
            atomic_store(&armed, 0);
        }
    }
    return malloc_zone_malloc(malloc_default_zone(), size);
}
__attribute__((noinline)) int SELECTED_NAME(void) {
    atomic_store(&armed, 1);
    return 42;
}
int main(void) {
    int result = SELECTED_NAME();
    for (int attempt = 0; attempt < 10000 && !atomic_load(&blocked); ++attempt) {
        usleep(1000);
    }
    if (!atomic_load(&blocked)) return 2;
    puts("sdk allocation blocked");
    fflush(stdout);
    return result == 42 ? 0 : 1;
}
