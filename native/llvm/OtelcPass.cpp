// Itanium C++ EH instrumentation. Probes cannot throw or inspect arguments.
#include "function_abi.h"
#ifdef OTELC_OBJECT_WITH_FUNCTIONS_V1
#include "object_pass.h"
#endif
#ifdef OTELC_RESOURCE_WITH_FUNCTIONS_V1
#include "resource_pass.h"
#endif
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/CallingConv.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/Analysis/ValueTracking.h"
#include "llvm/Demangle/Demangle.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/ConvertUTF.h"
#include "llvm/TargetParser/Triple.h"
#include "llvm/IR/Instructions.h"
#include "llvm/IR/PassManager.h"
#include "llvm/IR/Verifier.h"
#include "llvm/IR/DebugInfoMetadata.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Plugins/PassPlugin.h"
#include "llvm/Support/ErrorHandling.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Transforms/Utils/Local.h"
#include "llvm/Transforms/Utils/ModuleUtils.h"
#include <cstdlib>
#include <cstddef>
#include <algorithm>
static_assert(sizeof(otelc_function_descriptor_v1) == 2 * sizeof(void *));
static_assert(offsetof(otelc_function_descriptor_v1, name) == sizeof(void *));
using namespace llvm;
namespace {
// Only * and ? are wildcards; C++ punctuation remains literal.
size_t nextCharacter(StringRef Name, size_t Offset) {
  auto *Start = reinterpret_cast<const UTF8 *>(Name.data() + Offset);
  unsigned Size = getUTF8SequenceSize(Start,
      reinterpret_cast<const UTF8 *>(Name.data() + Name.size()));
  if (!Size) report_fatal_error("otelc: invalid UTF-8 function name");
  return Offset + Size;
}
bool matches(StringRef Pattern, StringRef Name) {
  size_t P=0,N=0,Star=StringRef::npos,Retry=0;
  while(N<Name.size()) {
    if(P<Pattern.size() && Pattern[P]=='*') { Star=P++;Retry=N; }
    else if(P<Pattern.size() && Pattern[P]=='?') { ++P;N=nextCharacter(Name,N); }
    else if(P<Pattern.size() && Pattern[P]==Name[N]) { ++P;++N; }
    else if(Star!=StringRef::npos) { P=Star+1;Retry=nextCharacter(Name,Retry);N=Retry; }
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
#ifdef OTELC_SOURCE_PROFILE_V1
// Same slash-aware *, **, **/ and Unicode ? semantics as the common policy.
// A small NFA avoids recursive/exponential wildcard backtracking.
bool sourceMatches(StringRef Pattern, StringRef Name) {
  enum Kind { Literal, Any, Star, Globstar, Directory, DirectoryBody };
  struct Token { Kind kind; std::string text; };
  SmallVector<Token> Tokens;
  for (size_t P = 0; P < Pattern.size();) {
    if (Pattern.substr(P).starts_with("**/")) {
      Tokens.push_back({Directory, ""}); Tokens.push_back({DirectoryBody, ""}); P += 3;
    } else if (Pattern.substr(P).starts_with("**")) { Tokens.push_back({Globstar, ""}); P += 2; }
    else if (Pattern[P] == '*') { Tokens.push_back({Star, ""}); ++P; }
    else if (Pattern[P] == '?') { Tokens.push_back({Any, ""}); ++P; }
    else { auto End = nextCharacter(Pattern, P); Tokens.push_back({Literal, Pattern.slice(P, End).str()}); P = End; }
  }
  SmallVector<unsigned char> States(Tokens.size() + 1, 0), Next(States.size(), 0);
  States[0] = 1;
  auto Closure = [&] {
    for (size_t I = 0; I < Tokens.size(); ++I) if (States[I]) {
      if (Tokens[I].kind == Star || Tokens[I].kind == Globstar) States[I + 1] = 1;
      else if (Tokens[I].kind == Directory) States[I + 2] = 1;
    }
  };
  for (size_t N = 0; N < Name.size();) {
    Closure();
    std::fill(Next.begin(), Next.end(), 0);
    auto End = nextCharacter(Name, N);
    auto Character = Name.slice(N, End);
    for (size_t I = 0; I < Tokens.size(); ++I) if (States[I]) {
      auto Kind = Tokens[I].kind;
      if (Kind == Literal && Character == Tokens[I].text) Next[I + 1] = 1;
      else if (Kind == Any && Character != "/") Next[I + 1] = 1;
      else if (Kind == Star && Character != "/") Next[I] = 1;
      else if (Kind == Globstar) Next[I] = 1;
      else if (Kind == Directory || Kind == DirectoryBody) {
        Next[Kind == Directory ? I + 1 : I] = 1;
        if (Character == "/") Next[Kind == Directory ? I + 2 : I + 1] = 1;
      }
    }
    States.swap(Next); N = End;
  }
  Closure();
  return States.back();
}

struct SourcePolicy {
  std::string Root;
  SmallVector<std::string> Include = patterns("OTELC_SOURCE_INCLUDE"), Exclude = patterns("OTELC_SOURCE_EXCLUDE");
  SourcePolicy(Module &M) {
    const char *Value = std::getenv("OTELC_SOURCE_ROOT");
    if (!Value || !sys::path::is_absolute(Value)) report_fatal_error("otelc: source profile requires an absolute source root");
    SmallString<256> Normal(Value);
    sys::path::remove_dots(Normal, true);
    Root = Normal.str().str();
    if (!M.getNamedMetadata("llvm.dbg.cu")) report_fatal_error("otelc: source profile requires compiler debug metadata (-g)");
  }
  std::string selectedFile(Function &F) const {
    auto *Scope = F.getSubprogram();
    // Compiler-generated helpers without a source definition are not application
    // declarations. The public source profile never claims whole-source proof.
    if (!Scope || Scope->isArtificial() || !Scope->getFile()) return "";
    auto *File = Scope->getFile();
    SmallString<256> Path(File->getFilename());
    if (!sys::path::is_absolute(Path)) {
      Path = File->getDirectory(); sys::path::append(Path, File->getFilename());
    }
    sys::path::remove_dots(Path, true);
    SmallString<256> Canonical;
    if (sys::fs::real_path(Path, Canonical)) return "";
    StringRef Relative(Canonical);
    StringRef Prefix(Root);
    if (!Relative.consume_front(Prefix) || !Relative.consume_front("/") || Relative.empty()) return "";
    bool Selected = false;
    for (const auto &Pattern : Include) Selected |= sourceMatches(Pattern, Relative);
    for (const auto &Pattern : Exclude) if (sourceMatches(Pattern, Relative)) Selected = false;
    return Selected ? Relative.str() : "";
  }
};
#endif
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
#ifdef OTELC_FUNCTION_REGISTRY_V1
void registerFunctions(Module &M, ArrayRef<Function *> Functions, FunctionCallee Register,
                       const DenseMap<Function *, std::string> &Labels) {
  if (Functions.empty()) return;
  if (auto *Ctors = M.getNamedGlobal("llvm.global_ctors"); Ctors && Ctors->hasInitializer()) {
    for (auto &Operand : Ctors->getInitializer()->operands()) {
      auto *Row = dyn_cast<ConstantStruct>(Operand.get());
      if (Row && cast<ConstantInt>(Row->getOperand(0))->isZero())
        report_fatal_error("otelc: application initialiser uses the reserved registry priority");
    }
  }
  auto &C = M.getContext();
  auto *Ptr = PointerType::getUnqual(C);
  auto *Descriptor = StructType::get(Ptr, Ptr);
  SmallVector<Constant *> Records;
  for (auto *F : Functions) {
    auto *Text = ConstantDataArray::getString(C, Labels.lookup(F), true);
    auto *Name = new GlobalVariable(M, Text->getType(), true, GlobalValue::PrivateLinkage,
                                    Text, "otelc.function.name");
    Name->setUnnamedAddr(GlobalValue::UnnamedAddr::Global);
    Records.push_back(ConstantStruct::get(Descriptor, {F, Name}));
  }
  auto *Array = ConstantArray::get(ArrayType::get(Descriptor, Records.size()), Records);
  auto *Table = new GlobalVariable(M, Array->getType(), true, GlobalValue::PrivateLinkage,
                                   Array, "otelc.function.descriptors");
  auto *Init = Function::Create(FunctionType::get(Type::getVoidTy(C), false),
                                GlobalValue::InternalLinkage, "otelc.function.registration", M);
  Init->addFnAttr(Attribute::NoUnwind);
  Init->addFnAttr(Attribute::WillReturn);
  IRBuilder<> Builder(BasicBlock::Create(C, "entry", Init));
  Builder.CreateCall(Register, {Table, Builder.getInt64(Records.size())})->setDoesNotThrow();
  Builder.CreateRetVoid();
  // Implementation-reserved priority: no application initialiser needs to enrol probes.
  appendToGlobalCtors(M, Init, 0);
}
#endif
struct OtelcPass : PassInfoMixin<OtelcPass> {
  PreservedAnalyses run(Module &M, ModuleAnalysisManager &Manager) {
    if (M.getNamedMetadata("otelc.instrumented"))
      report_fatal_error("otelc: module already instrumented");
    (void)Manager;
#ifdef OTELC_OBJECT_WITH_FUNCTIONS_V1
    otelc::InstrumentObjectBoundaries(M, Manager);
#endif
#ifdef OTELC_RESOURCE_WITH_FUNCTIONS_V1
    otelc::PlanResourceBoundaries(M, Manager);
#endif
    auto &C = M.getContext();
    auto *Ptr = PointerType::getUnqual(C);
    auto *I64 = Type::getInt64Ty(C);
    auto *I32 = Type::getInt32Ty(C);
    auto Probe = [&](StringRef Name, FunctionType *Type) {
      if (auto *Existing = M.getNamedValue(Name)) {
        auto *Function = dyn_cast<llvm::Function>(Existing);
        if (!Function || !Function->isDeclaration() || Function->getFunctionType() != Type ||
            Function->getCallingConv() != CallingConv::C)
          report_fatal_error("otelc: application conflicts with the reserved probe ABI");
        // A type-compatible declaration can still change the ABI or erase probes
        // (for example byval, noreturn or memory(none)). Accept only the fixed
        // contract's neutral attributes, not arbitrary application promises.
        auto Attributes = Function->getAttributes();
        bool Compatible = !Attributes.getRetAttrs().hasAttributes();
        for (unsigned Arg = 0; Arg < Function->arg_size(); ++Arg)
          Compatible &= !Attributes.hasParamAttrs(Arg);
        for (auto Attribute : Attributes.getFnAttrs())
          Compatible &= Attribute.isEnumAttribute() &&
              (Attribute.getKindAsEnum() == Attribute::NoUnwind ||
               Attribute.getKindAsEnum() == Attribute::WillReturn);
        if (!Compatible)
          report_fatal_error("otelc: application conflicts with the reserved probe ABI attributes");
      }
      return M.getOrInsertFunction(Name, Type);
    };
    auto Enter = Probe("otelc_function_enter_v1", FunctionType::get(I64, {Ptr}, false));
    auto Leave = Probe("otelc_function_leave_v1", FunctionType::get(Type::getVoidTy(C), {I64, I32}, false));
    SmallVector<FunctionCallee> Probes{Enter, Leave};
#ifdef OTELC_FUNCTION_REGISTRY_V1
    auto Register = Probe("otelc_function_register_v1", FunctionType::get(Type::getVoidTy(C), {Ptr, I64}, false));
    Probes.push_back(Register);
#endif
    for (auto Callee : Probes) {
      auto *F = cast<Function>(Callee.getCallee());
      F->addFnAttr(Attribute::NoUnwind);
      F->addFnAttr(Attribute::WillReturn);
    }
    bool Cpp = std::getenv("OTELC_LLVM_CPP") != nullptr;
    auto Includes=patterns("OTELC_FUNCTION_INCLUDE");
    auto Excludes=patterns("OTELC_FUNCTION_EXCLUDE");
    auto Annotations = annotations(M);
#ifdef OTELC_SOURCE_PROFILE_V1
    SourcePolicy Sources(M);
#endif
    SmallVector<Function *> Functions;
    DenseMap<Function *, std::string> Labels;
    for (auto &F : M) {
      if (F.isDeclaration() || F.hasFnAttribute("no-instrument-function") ||
          F.hasFnAttribute(Attribute::NoProfile))
        continue;
      auto Name=llvm::demangle(F.getName().str());
      bool Selected = (Annotations.lookup(&F) & 1) != 0;
      for(auto &Pattern:Includes) Selected |= matches(Pattern,Name);
      for(auto &Pattern:Excludes) if(matches(Pattern,Name)) Selected=false;
      if (Annotations.lookup(&F) == 3) errs() << "otelc: otelc.exclude overrides otelc.instrument on " << Name << "\n";
      if (Annotations.lookup(&F) & 2) Selected = false;
      if(!Selected) continue;
      std::string Label = Name;
#ifdef OTELC_SOURCE_PROFILE_V1
      auto File = Sources.selectedFile(F);
      if (File.empty()) continue;
      Label = File + "::" + Name;
      if (Label.size() > 4096) report_fatal_error("otelc: source function identity exceeds the native name budget");
#endif
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
      Labels[&F] = std::move(Label);
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
#ifdef OTELC_FUNCTION_REGISTRY_V1
    registerFunctions(M, Functions, Register, Labels);
#endif
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
#ifdef OTELC_RESOURCE_WITH_FUNCTIONS_V1
    PB.registerOptimizerLastEPCallback([](ModulePassManager &PM, OptimizationLevel, ThinOrFullLTOPhase) {
      PM.addPass(otelc::ResourceBoundaryPass());
    });
#endif
  }};
}
