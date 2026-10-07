#include <stdio.h>

int process_order(int value) { return value * 2; }
int audit_order(int value) { return value + 1; }

int main(void) {
    int result = 0;
    for (int i = 0; i < 4; ++i)
        result += process_order(i) + audit_order(i);
    printf("result=%d\n", result);
    return 0;
}
