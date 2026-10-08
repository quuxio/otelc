#include <setjmp.h>
#include <stdio.h>
static jmp_buf destination;
__attribute__((noinline)) void selected_skip(void) { longjmp(destination, 1); }
__attribute__((noinline)) int selected_outer(void) {
    if (!setjmp(destination)) selected_skip();
    return 3;
}
__attribute__((noinline)) int selected_healthy(void) { return 4; }
int main(void) {
    int result = selected_outer() + selected_healthy();
    printf("result=%d\n", result);
    return result == 7 ? 0 : 1;
}
