#include <cstdio>
struct OrderBook {
    __attribute__((noinline)) int add(int depth) {
        volatile int result = depth ? add(depth - 1) + 1 : 0;
        return result;
    }
};
int main() { OrderBook book; int result = book.add(2); std::printf("result=%d\n", result); return result != 2; }
