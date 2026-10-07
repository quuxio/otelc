// Itanium C++ EH instrumentation. Probes cannot throw or inspect arguments.
#include "llvm/IR/IRBuilder.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/Analysis/ValueTracking.h"
#include "llvm/Demangle/Demangle.h"
#include "llvm/Support/JSON.h"
#include "llvm/TargetParser/Triple.h"
#include "llvm/IR/Instructions.h"
#include "llvm/IR/PassManager.h"
#include "llvm/IR/Verifier.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Plugins/PassPlugin.h"
#include "llvm/Support/ErrorHandling.h"
#include "llvm/Transforms/Utils/Local.h"
#include "llvm/Transforms/Utils/ModuleUtils.h"
#include <cstdlib>
using namespace llvm;
namespace {
// Only * and ? are wildcards; C++ punctuation remains literal.
bool matches(StringRef Pattern, StringRef Name) {
  size_t P=0,N=0,Star=StringRef::npos,Retry=0;
  while(N<Name.size()) {
    if(P<Pattern.size() && (Pattern[P]=='?' || Pattern[P]==Name[N])) { ++P;++N; }
    else if(P<Pattern.size() && Pattern[P]=='*') { Star=P++;Retry=N; }
    else if(Star!=StringRef::npos) { P=Star+1;N=++Retry; }
    else return false;
  }
  while(P<Pattern.size() && Pattern[P]=='*') ++P;
  return P==Pattern.size();
}
SmallVector<std::string> patterns(const char *Key) {
  const char *Value=std::getenv(Key);
  if(!Value) report_fatal_error("otelc: selection configuration missing");
  auto Parsed=json::parse(Value);
  if(!Parsed || !Parsed->getAsArray()) report_fatal_error("otelc: invalid selection configuration");
  SmallVector<std::string> Result;
  for(auto &Item:*Parsed->getAsArray()) {
    auto Text=Item.getAsString(); if(!Text) report_fatal_error("otelc: invalid selection pattern");
    Result.push_back(Text->str());
  }
  return Result;
}
// Clang supplies semantic annotation metadata after preprocessing and parsing.
// Preserve unrelated annotations and never scan source text for attribute spelling.
DenseMap<Function *, unsigned> annotations(Module &M) {
  DenseMap<Function *, unsigned> Result;
  const char *Enabled = std::getenv("OTELC_READ_ANNOTATIONS");
  if (!Enabled || StringRef(Enabled) != "1") return Result;
  auto *Global = M.getGlobalVariable("llvm.global.annotations");
  if (!Global || !Global->hasInitializer()) return Result;
  for (auto &Operand : Global->getInitializer()->operands()) {
    auto *Record = dyn_cast<ConstantStruct>(Operand.get());
    if (!Record || Record->getNumOperands() < 2) continue;
    StringRef Text;
    if (!getConstantStringInfo(Record->getOperand(1), Text) || !Text.starts_with("otelc.")) continue;
    auto *Target = dyn_cast<Function>(Record->getOperand(0)->stripPointerCasts());
    if (!Target) report_fatal_error("otelc: only function annotations are supported");
    if (Text == "otelc.instrument") Result[Target] |= 1;
    else if (Text == "otelc.exclude") Result[Target] |= 2;
    else report_fatal_error("otelc: unknown otelc function annotation");
  }
  return Result;
}
struct OtelcPass : PassInfoMixin<OtelcPass> {
  PreservedAnalyses run(Module &M, ModuleAnalysisManager &) {
    if (M.getNamedMetadata("otelc.instrumented"))
      report_fatal_error("otelc: module already instrumented");
    auto &C = M.getContext();
    auto *Ptr = PointerType::getUnqual(C);
    auto *I64 = Type::getInt64Ty(C);
    auto *I32 = Type::getInt32Ty(C);
    auto Enter = M.getOrInsertFunction("otelc_function_enter_v1", I64, Ptr);
    auto Leave = M.getOrInsertFunction("otelc_function_leave_v1", Type::getVoidTy(C), I64, I32);
    for (auto Callee : {Enter, Leave}) {
      auto *F = cast<Function>(Callee.getCallee());
      F->addFnAttr(Attribute::NoUnwind);
      F->addFnAttr(Attribute::WillReturn);
    }
    bool Cpp = std::getenv("OTELC_LLVM_CPP") != nullptr;
    auto Includes=patterns("OTELC_FUNCTION_INCLUDE");
    auto Excludes=patterns("OTELC_FUNCTION_EXCLUDE");
    auto Annotations = annotations(M);
    SmallVector<Function *> Functions;
    for (auto &F : M) {
      if (F.isDeclaration() || F.getName().starts_with("otelc_") ||
          F.getName().starts_with("__cyg_profile_func_") ||
          F.hasFnAttribute("no-instrument-function"))
        continue;
      auto Name=llvm::demangle(F.getName().str());
      bool Selected = (Annotations.lookup(&F) & 1) != 0;
      for(auto &Pattern:Includes) Selected |= matches(Pattern,Name);
      for(auto &Pattern:Excludes) if(matches(Pattern,Name)) Selected=false;
      if (Annotations.lookup(&F) == 3) errs() << "otelc: otelc.exclude overrides otelc.instrument on " << Name << "\n";
      if (Annotations.lookup(&F) & 2) Selected = false;
      if(!Selected) continue;
      if (F.hasPersonalityFn()) {
        auto *P = dyn_cast<Function>(F.getPersonalityFn()->stripPointerCasts());
        if (!P || (P->getName() != "__gxx_personality_v0" && P->getName() != "__gcc_personality_v0"))
          report_fatal_error("otelc: unsupported exception personality");
      }
      for (auto &BB : F) for (auto &I : BB) {
        if (isa<CatchSwitchInst>(I) || isa<CatchPadInst>(I) || isa<CleanupPadInst>(I))
          report_fatal_error("otelc: Windows exception funclets are unsupported");
        if (auto *Call = dyn_cast<CallInst>(&I)) {
          if (Call->isMustTailCall()) report_fatal_error("otelc: musttail is unsupported");
          if (auto *Target = Call->getCalledFunction(); Target && Target->getName().starts_with("llvm.coro."))
            report_fatal_error("otelc: coroutines are unsupported");
        }
      }
      Functions.push_back(&F);
    }
    SmallVector<Constant *> Inventory, AnnotatedInventory;
    for (auto *F : Functions) {
      SmallVector<Instruction *> Exits;
      SmallVector<CallInst *> Calls;
      for (auto &BB : *F) for (auto &I : BB) {
        if (isa<ReturnInst>(I) || isa<ResumeInst>(I)) Exits.push_back(&I);
        if (auto *Call = dyn_cast<CallInst>(&I)) {
          Call->setTailCallKind(CallInst::TCK_None);
          // Existing invokes retain their catches/cleanup. Only otherwise
          // unprotected throwing calls need a new cleanup landing pad.
          if (Cpp && !F->doesNotThrow() && !Call->doesNotThrow() && !Call->isInlineAsm())
            Calls.push_back(Call);
        }
      }
      IRBuilder<> Entry(&*F->getEntryBlock().getFirstInsertionPt());
      auto *Token = Entry.CreateCall(Enter, {F}, "otelc.token");
      Token->setDoesNotThrow();
      for (auto *Exit : Exits) {
        IRBuilder<> Builder(Exit);
        Builder.CreateCall(Leave, {Token, Builder.getInt32(isa<ResumeInst>(Exit) ? 1 : 0)})->setDoesNotThrow();
      }
      if (!Calls.empty()) {
        if (!F->hasPersonalityFn()) {
          auto Personality = M.getOrInsertFunction("__gxx_personality_v0", FunctionType::get(I32, true));
          F->setPersonalityFn(cast<Constant>(Personality.getCallee()));
        }
        auto *Cleanup = BasicBlock::Create(C, "otelc.unwind", F);
        IRBuilder<> Builder(Cleanup);
        auto *Landing = Builder.CreateLandingPad(StructType::get(Ptr, I32), 0);
        Landing->setCleanup(true);
        Builder.CreateCall(Leave, {Token, Builder.getInt32(1)})->setDoesNotThrow();
        Builder.CreateResume(Landing);
        for (auto *Call : Calls) changeToInvokeAndSplitBasicBlock(Call, Cleanup);
      }
      Inventory.push_back(F);
      if (Annotations.lookup(F) & 1) AnnotatedInventory.push_back(F);
    }
    auto Retain = [&](ArrayRef<Constant *> Values, StringRef Name, StringRef Section) {
      if (Values.empty()) return;
      auto *Array = ConstantArray::get(ArrayType::get(Ptr, Values.size()), Values);
      auto *Global = new GlobalVariable(M, Array->getType(), true, GlobalValue::PrivateLinkage, Array, Name);
      Global->setSection(Section);
      appendToUsed(M, {Global});
    };
    bool Darwin = M.getTargetTriple().isOSDarwin();
    Retain(Inventory, "otelc.inventory", Darwin ? "__DATA,__otelc" : ".otelc");
    Retain(AnnotatedInventory, "otelc.annotations", Darwin ? "__DATA,__otela" : ".otela");
    M.getOrInsertNamedMetadata("otelc.instrumented");
    if (verifyModule(M, &errs())) report_fatal_error("otelc: invalid instrumented module");
    return PreservedAnalyses::none();
  }
};
}
extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "otelc", "0.1.0", [](PassBuilder &PB) {
    PB.registerPipelineStartEPCallback([](ModulePassManager &PM, OptimizationLevel) {
      PM.addPass(OtelcPass());
    });
  }};
}
