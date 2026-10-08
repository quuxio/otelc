#include <stdio.h>
__attribute__((noinline)) int selected_child(int value) { return value + 1; }
__attribute__((noinline)) int selected_parent(void) {
    char command[32];
    puts("holding");
    fflush(stdout);
    if (!fgets(command, sizeof(command), stdin)) return -1;
    return selected_child(1);
}
int main(void) {
    int result = selected_parent() + selected_child(2);
    printf("result=%d\n", result);
    return result == 5 ? 0 : 1;
}
