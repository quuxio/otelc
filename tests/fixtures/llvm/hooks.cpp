#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include "function_abi.h"
#ifdef OTELC_FUNCTION_REGISTRY_V1
static otelc_function_descriptor_v1 descriptors[1024];
static uint64_t descriptor_count = 0;
extern "C" void otelc_function_register_v1(const otelc_function_descriptor_v1 *rows, uint64_t count) noexcept {
 if (!rows || !count || count > 1024 - descriptor_count) std::abort();
 for (uint64_t i=0;i<count;++i) {
  if (!rows[i].address || !rows[i].name || !*rows[i].name) std::abort();
  for (uint64_t j=0;j<descriptor_count;++j) if (descriptors[j].address==rows[i].address) std::abort();
  descriptors[descriptor_count++]=rows[i];
 }
}
#endif
static std::atomic<uint64_t> entered{0},normal{0},unwind{0},invalid{0};
static std::atomic<bool> live[1024]{};
extern "C" uint64_t otelc_function_enter_v1(const void* address) noexcept {
#ifdef OTELC_FUNCTION_REGISTRY_V1
 bool found=false;for(uint64_t i=0;i<descriptor_count;++i)found|=descriptors[i].address==address;
 if(!found)std::abort();
#else
 (void)address;
#endif
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
#ifdef OTELC_FUNCTION_REGISTRY_V1
 if(auto* path=std::getenv("REGISTRY_REPORT")){
  auto* output=std::fopen(path,"w");if(!output)std::abort();
  for(uint64_t i=0;i<descriptor_count;++i)std::fprintf(output,"%s\n",descriptors[i].name);
  std::fclose(output);
 }
#endif
}
struct Register{Register(){std::atexit(report);}};
static Register registration;
