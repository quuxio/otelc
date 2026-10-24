#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
static std::atomic<uint64_t> entered{0},normal{0},unwind{0},invalid{0};
static std::atomic<bool> live[1024]{};
extern "C" uint64_t otelc_function_enter_v1(void*) noexcept {
 auto id=entered.fetch_add(1)+1;if(id>=1024){invalid++;return 0;}live[id]=true;return id;
}
extern "C" void otelc_function_leave_v1(uint64_t id,int32_t error) noexcept {
 if(id==0||id>=1024||!live[id].exchange(false)){invalid++;return;}
 if(error)unwind++;else normal++;
}
static void report(){
 for(auto& value:live)if(value.load())invalid++;
 auto* file=std::fopen(std::getenv("PROBE_REPORT"),"w");if(!file)std::abort();
 std::fprintf(file,"{\"entries\":%llu,\"normal\":%llu,\"unwind\":%llu,\"invalid\":%llu}\n",(unsigned long long)entered.load(),(unsigned long long)normal.load(),(unsigned long long)unwind.load(),(unsigned long long)invalid.load());std::fclose(file);
}
struct Register{Register(){std::atexit(report);}};
static Register registration;
