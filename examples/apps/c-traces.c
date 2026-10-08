#include <errno.h>
#include <pthread.h>
#include <stdio.h>

__attribute__((noinline)) int child_order(int value) { return value + 1; }
__attribute__((noinline)) int parent_order(int value) { return child_order(value); }
__attribute__((noinline)) int recursive_order(int depth) {
    return depth ? recursive_order(depth - 1) + 1 : 0;
}
static void *work(void *unused) { (void)unused; child_order(2); return NULL; }
int main(void) {
    errno = EDOM;
    int result = parent_order(4) + recursive_order(3);
    if (errno != EDOM) return 2;
    pthread_t thread;
    if (pthread_create(&thread, NULL, work, NULL)) return 3;
    pthread_join(thread, NULL);
    printf("result=%d\n", result);
    return result == 8 ? 0 : 1;
}
