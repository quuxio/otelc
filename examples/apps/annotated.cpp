#include <stdio.h>
#include <stdexcept>

__attribute__((annotate("otelc.instrument")))
int process_order(int value) { return value * 2; }

__attribute__((annotate("otelc.exclude")))
int audit_order(int value) { return value + 1; }

__attribute__((annotate("otelc.instrument")))
int blocked_order(int value) { return value + 2; }

__attribute__((annotate("vendor.audit")))
int vendor_order(int value) { return value + 3; }

__attribute__((annotate("otelc.instrument"), annotate("otelc.exclude")))
int conflicted_order(int value) { return value + 4; }

int configured_order(int value) { return value + 5; }

__attribute__((annotate("otelc.instrument")))
int throw_order(int value) {
    if (value < 0) throw std::runtime_error("invalid order");
    return value;
}

int main(void) {
    int result = 0;
    for (int i = 0; i < 4; ++i)
        result += process_order(i) + audit_order(i) + blocked_order(i)
            + vendor_order(i) + conflicted_order(i) + configured_order(i);
    try { throw_order(-1); }
    catch (const std::runtime_error &) { result += 7; }
    printf("result=%d\n", result);
    return 0;
}
