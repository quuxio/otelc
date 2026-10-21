#include <stdio.h>
#include <unistd.h>

__attribute__((noinline)) int selected_work(void) { return 42; }

int main(int argc, char **argv) {
    /* Ordinary application startup must not spend the telemetry shutdown budget. */
    usleep(2100000);
    int result = selected_work();
    if (argc == 2) {
        FILE *ready = fopen(argv[1], "w");
        if (!ready) return 2;
        int written = fputs("ready\n", ready);
        if (fclose(ready) != 0 || written < 0) return 3;
        if (getchar() != 'r') return 4;
    }
    printf("result=%d\n", result);
    return result == 42 ? 0 : 5;
}
