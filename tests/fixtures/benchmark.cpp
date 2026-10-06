#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <thread>
#include <vector>
__attribute__((noinline)) unsigned long long selected_hot(unsigned long long value) {
    for(int j=0;j<16;++j) {
        value ^= value >> 30; value *= 0xbf58476d1ce4e5b9ULL;
        value ^= value >> 27; value *= 0x94d049bb133111ebULL;
        value ^= value >> 31;
    }
    return value;
}
int main(int argc,char**argv) {
    const int iterations=argc>1 ? std::atoi(argv[1]):100000;
    const int threads=argc>2 ? std::atoi(argv[2]):1;
    if(iterations<1 || iterations>10000000 || threads<1 || threads>16)return 2;
    unsigned long long warm=0;
    for(int i=0;i<1000;++i)warm^=selected_hot(i);
    std::atomic<unsigned long long> checksum{warm};
    std::atomic<bool> start{false};
    std::atomic<int> ready{0};
    std::vector<std::thread> workers;
    for(int t=0;t<threads;++t) workers.emplace_back([&,t] {
        ++ready;
        while(!start.load(std::memory_order_acquire)) std::this_thread::yield();
        unsigned long long sum=0;
        for(int i=0;i<iterations;++i) sum^=selected_hot(static_cast<unsigned long long>(i)+static_cast<unsigned long long>(t)*iterations);
        checksum.fetch_xor(sum,std::memory_order_relaxed);
    });
    while(ready.load()!=threads)std::this_thread::yield();
    const auto begin=std::chrono::steady_clock::now();
    start.store(true,std::memory_order_release);
    for(auto &worker:workers)worker.join();
    const auto elapsed=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-begin).count();
    std::printf("elapsed_ns=%lld checksum=%llu calls=%lld\n",static_cast<long long>(elapsed),checksum.load(),static_cast<long long>(iterations)*threads+1000);
}
