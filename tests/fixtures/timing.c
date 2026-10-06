#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

__attribute__((noinline)) int selected_recursive(int depth) {
    volatile int result = depth ? selected_recursive(depth - 1) + 1 : 0;
    return result;
}
__attribute__((noinline)) int excluded_work(void) { return 17; }
__attribute__((noinline)) void selected_work(void) { usleep(1000); }
static void *thread_work(void *unused) {
    (void)unused;
    for (int i = 0; i < 10; ++i) selected_work();
    return NULL;
}
int main(int argc, char **argv) {
    errno = EDOM;
    int result = selected_recursive(3);
    if (errno != EDOM || result != 3 || excluded_work() != 17) return 2;
    if (argc > 1 && strcmp(argv[1], "threads") == 0) {
        pthread_t threads[4];
        for (int i = 0; i < 4; ++i) if (pthread_create(&threads[i], NULL, thread_work, NULL)) return 3;
        for (int i = 0; i < 4; ++i) pthread_join(threads[i], NULL);
    }
    if (argc > 1 && strcmp(argv[1], "overload") == 0) {
        for (int i = 0; i < 100000; ++i) selected_recursive(0);
    }
    printf("result=%d\n", result);
    return argc > 1 && strcmp(argv[1], "exit7") == 0 ? 7 : 0;
}
