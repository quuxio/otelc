#include <atomic>
#include <cstdio>
#include <thread>

struct Payload { int code; };
static Payload payload{7};
static std::atomic<int> base_cleanup{0}, order_cleanup{0};
int choose_value(int value) {
    if (value < 0) throw &payload;
    return value;
}
struct Base {
    Base() {}
    ~Base() { ++base_cleanup; }
};
struct Order : Base {
    int value;
    explicit Order(int input) : value(choose_value(input)) {}
    Order() : Order(3) {}
    ~Order() { ++order_cleanup; }
};
int build_order(int mode) {
    try {
        if (mode == 2) { Order order; return order.value; }
        Order order(mode < 0 ? -1 : 4);
        return order.value;
    } catch (Payload *error) {
        if (error != &payload || error->code != 7) std::terminate();
        return error->code;
    }
}
int main() {
    int result = build_order(0) + build_order(-1) + build_order(2);
    std::thread worker([&result] { result += build_order(0); });
    worker.join();
    std::printf("result=%d base_cleanup=%d order_cleanup=%d size=%zu\n",
        result, base_cleanup.load(), order_cleanup.load(), sizeof(Order));
    return result != 18 || base_cleanup != 4 || order_cleanup != 3;
}
