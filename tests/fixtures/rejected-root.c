#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <unistd.h>
static _Atomic int ready;
static _Atomic int release_holder;
static pthread_t holder;
__attribute__((noinline)) int selected_holder(void) {
    atomic_store(&ready, 1);
    while (!atomic_load(&release_holder)) usleep(1000);
    return 1;
}
static void *hold(void *unused) { (void)unused; selected_holder(); return NULL; }
__attribute__((noinline)) int selected_child(int value) { return value + 1; }
__attribute__((noinline)) int selected_outer(void) {
    int result = selected_child(1);
    atomic_store(&release_holder, 1);
    pthread_join(holder, NULL);
    return result + selected_child(2);
}
int main(void) {
    if (pthread_create(&holder, NULL, hold, NULL)) return 2;
    while (!atomic_load(&ready)) usleep(1000);
    int result = selected_outer() + selected_child(3);
    printf("result=%d\n", result);
    return result == 9 ? 0 : 1;
}
