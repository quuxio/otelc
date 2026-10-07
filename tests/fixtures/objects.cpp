#include "otelc/lifetime.hpp"
#include <cstdio>
#include <thread>
#include <utility>
struct OrderBook {
    otelc::ObjectLifetime lifetime;
    explicit OrderBook(bool fail=false) { if(fail)throw 9; lifetime.start("OrderBook"); }
};
int main() {
    { OrderBook local; }
    try { OrderBook failed(true); } catch(int) {}
    OrderBook* cross=new OrderBook;
    std::thread thread([cross]{delete cross;});thread.join();
    { otelc::ObjectLifetime first("OrderBook"); auto second=std::move(first); }
    { otelc::ObjectLifetime first("OrderBook"),second("OrderBook"); second=std::move(first); }
    try { OrderBook unwind; throw 7; } catch(int) {}
    { otelc::ObjectLifetime unselected("NotSelected"); }
    std::printf("result=0\n");
}
