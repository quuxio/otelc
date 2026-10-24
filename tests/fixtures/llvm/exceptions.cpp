#include <atomic>
#include <cstdio>
#include <thread>
static std::atomic<int> cleanup{0};
struct Cleanup { ~Cleanup() { ++cleanup; } };
__attribute__((noinline)) int selected_throw(int depth, bool fail) {
    Cleanup guard;
    if(depth) return selected_throw(depth-1,fail)+1;
    if(fail) throw 7;
    return 0;
}
__attribute__((noinline)) int selected_catch() {
    try { selected_throw(0,true); } catch(int e) { return e; }
    return 0;
}
__attribute__((noinline)) void selected_rethrow() {
    try { selected_throw(0,true); } catch(int) { throw; }
}
static void scenario() {
    if(selected_throw(3,false)!=3) std::terminate();
    try { selected_throw(3,true); } catch(int e) { if(e!=7)std::terminate(); }
    if(selected_catch()!=7) std::terminate();
    try { selected_rethrow(); } catch(int e) { if(e!=7) std::terminate(); }
    if(selected_throw(0,false)!=0) std::terminate();
}
int main(int argc,char**) {
    scenario();
    if(argc>1) { std::thread thread(scenario); thread.join(); }
    int expected=argc>1 ? 22 : 11;
    std::printf("result=%d cleanup=%d\n",cleanup.load()==expected ? 0:1,cleanup.load());
    return cleanup.load()!=expected;
}
