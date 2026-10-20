// Rust compiler hooks retain original source spans and use the Rust SDK.
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/StringMap.h"
#include "llvm/ADT/StringSet.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/Instructions.h"
#include "llvm/IR/PassManager.h"
#include "llvm/IR/Verifier.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Plugins/PassPlugin.h"
#include "llvm/Support/ErrorHandling.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/FormatVariadic.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Transforms/Utils/Local.h"
#include <cstdlib>
#include <fstream>
using namespace llvm;

namespace {
constexpr uint64_t StorageBytes = 256;
constexpr uint64_t StorageAlign = 16;

struct Definition {
  std::string File, Name, Leaf, Suffix, Unsupported, SourceHash;
  uint64_t HeaderStart, HeaderEnd, BodyStartLine, BodyStartColumn, BodyEndLine, BodyEndColumn;
  uint64_t DeclarationStartLine, DeclarationStartColumn, DeclarationEndLine, DeclarationEndColumn;
  bool Selected, Startup, Emitted;
  std::string Symbol;
};

SmallVector<Definition> definitions() {
  const char *Path = std::getenv("OTELC_RUST_LLVM_INVENTORY");
  if (!Path) report_fatal_error("otelc Rust: compiler inventory is missing");
  auto Buffer = MemoryBuffer::getFile(Path);
  if (!Buffer || (*Buffer)->getBufferSize() > 4 * 1024 * 1024)
    report_fatal_error("otelc Rust: compiler inventory is unavailable or too large");
  auto Document = json::parse((*Buffer)->getBuffer());
  auto *Object = Document ? Document->getAsObject() : nullptr;
  if (!Object || Object->getInteger("schema_version") != 3)
    report_fatal_error("otelc Rust: invalid compiler inventory version");
  auto *Entries = Object->getArray("functions");
  auto *Sources = Object->getObject("sources");
  if (!Sources || !Entries || Entries->size() > 65536)
    report_fatal_error("otelc Rust: invalid compiler function inventory");
  SmallVector<Definition> Result;
  for (const auto &Entry : *Entries) {
    auto *Row = Entry.getAsObject();
    if (!Row) report_fatal_error("otelc Rust: invalid compiler function definition");
    auto Text = [&](StringRef Key) {
      auto Value = Row->getString(Key);
      if (!Value || Value->size() > 16384)
        report_fatal_error("otelc Rust: invalid compiler definition text");
      return Value->str();
    };
    auto Number = [&](StringRef Key) -> uint64_t {
      auto Value = Row->getInteger(Key);
      if (!Value || *Value < 1)
        report_fatal_error("otelc Rust: invalid compiler definition position");
      return static_cast<uint64_t>(*Value);
    };
    auto Flag = [&](StringRef Key) {
      auto Value = Row->getBoolean(Key);
      if (!Value) report_fatal_error("otelc Rust: invalid compiler definition flag");
      return *Value;
    };
    Definition Item{Text("file"), Text("name"), Text("leaf"), Text("suffix"), Text("unsupported"), "",
      Number("header_start"), Number("header_end"), Number("body_start_line"),
      Number("body_start_column"), Number("body_end_line"), Number("body_end_column"),
      Number("declaration_start_line"), Number("declaration_start_column"),
      Number("declaration_end_line"), Number("declaration_end_column"),
      Flag("selected"), Flag("startup"), Flag("emitted"), Text("compiled_symbol")};
    if (Item.Emitted != !Item.Symbol.empty() || (Item.Startup && !Item.Emitted)
        || Item.Name.empty() || Item.Name.size() > 1024 || Item.HeaderEnd < Item.HeaderStart
        || std::pair(Item.BodyEndLine, Item.BodyEndColumn) < std::pair(Item.BodyStartLine, Item.BodyStartColumn)
        || std::pair(Item.DeclarationEndLine, Item.DeclarationEndColumn) < std::pair(Item.DeclarationStartLine, Item.DeclarationStartColumn))
      report_fatal_error("otelc Rust: invalid compiler definition bounds");
    auto Digest = Sources->getString(Item.File);
    if (!Digest || Digest->size() != 64 || !llvm::all_of(*Digest, [](char Value) { return (Value >= '0' && Value <= '9') || (Value >= 'a' && Value <= 'f'); }))
      report_fatal_error("otelc Rust: source fingerprint is missing or invalid");
    Item.SourceHash = Digest->str();
    Result.push_back(std::move(Item));
  }
  return Result;
}

struct RustPass : PassInfoMixin<RustPass> {
  PreservedAnalyses run(Module &Module, ModuleAnalysisManager &) {
    if (Module.getNamedMetadata("otelc.rust.instrumented"))
      report_fatal_error("otelc Rust: compiler module is already instrumented");
    auto Definitions = definitions();
    StringMap<const Definition *> Symbols;
    for (const auto &Definition : Definitions)
      if (Definition.Emitted && !Symbols.try_emplace(Definition.Symbol, &Definition).second)
        report_fatal_error("otelc Rust: duplicate compiled symbol identity");
    StringSet<> Matched;
    auto &Context = Module.getContext();
    auto *Pointer = PointerType::getUnqual(Context);
    auto *Void = Type::getVoidTy(Context);
    auto *Size = Module.getDataLayout().getIntPtrType(Context);
    for (StringRef Name : {"otelc_rust_enter_v1", "otelc_rust_leave_v1", "otelc_rust_start_v1", "otelc_rust_shutdown_v1"})
      if (Module.getNamedValue(Name)) report_fatal_error("otelc Rust: application collides with a compiler SDK symbol");
    auto Enter = Module.getOrInsertFunction("otelc_rust_enter_v1", Void, Pointer, Size, Pointer);
    auto Leave = Module.getOrInsertFunction("otelc_rust_leave_v1", Void, Pointer);
    auto Start = Module.getOrInsertFunction("otelc_rust_start_v1", Void);
    auto Stop = Module.getOrInsertFunction("otelc_rust_shutdown_v1", Void);
    for (auto Callee : {Enter, Leave, Start, Stop})
      cast<Function>(Callee.getCallee())->addFnAttr(Attribute::NoUnwind);
    SmallVector<std::pair<Function *, const Definition *>> Selected;
    for (auto &Function : Module) {
      if (Function.isDeclaration()) continue;
      const auto *Definition = Symbols.lookup(Function.getName());
      if (Definition && (Definition->Selected || Definition->Startup)) {
        if (!Definition->Unsupported.empty()) report_fatal_error(Twine("otelc Rust: ") + Definition->Unsupported);
        if (Function.hasFnAttribute(Attribute::Naked)) report_fatal_error("otelc Rust: naked compiler bodies are unsupported");
        if (Function.hasPersonalityFn()) {
          const auto *Personality = dyn_cast<llvm::Function>(Function.getPersonalityFn()->stripPointerCasts());
          if (!Personality || Personality->getName() != "rust_eh_personality")
            report_fatal_error("otelc Rust: unsupported compiler exception personality");
        }
        Selected.push_back({&Function, Definition});
        Matched.insert(Function.getName());
      }
    }
    for (const auto &Definition : Definitions)
      if (Definition.Emitted && (Definition.Selected || Definition.Startup) && !Matched.contains(Definition.Symbol))
        report_fatal_error("otelc Rust: selected compiled symbol did not resolve to its LLVM body");
    bool ExpectedStartup = llvm::any_of(Definitions, [](const Definition &Item) { return Item.Startup; });
    bool FoundStartup = llvm::any_of(Selected, [](const auto &Item) { return Item.second->Startup; });
    if (ExpectedStartup && !FoundStartup)
      report_fatal_error("otelc Rust: executable main did not resolve to its source definition");
    json::Array Resolved;
    for (const auto &Item : Selected) {
      const auto &Definition = *Item.second;
      Resolved.push_back(json::Object{
        {"name", Definition.Name}, {"file", Definition.File}, {"source_sha256", Definition.SourceHash},
        {"compiled_symbol", Item.first->getName().str()}, {"kind", Definition.Leaf == "<closure>" ? "closure" : "function"},
        {"selected", Definition.Selected}, {"startup", Definition.Startup},
        {"body_start_line", int64_t(Definition.BodyStartLine)}, {"body_start_column", int64_t(Definition.BodyStartColumn)},
        {"body_end_line", int64_t(Definition.BodyEndLine)}, {"body_end_column", int64_t(Definition.BodyEndColumn)}});
    }
    for (const auto &Item : Selected) {
      auto *Function = Item.first;
      const auto *Definition = Item.second;
      SmallVector<Instruction *> Exits;
      SmallVector<CallInst *> Calls;
      for (auto &Block : *Function) for (auto &Instruction : Block) {
        if (isa<ReturnInst>(Instruction) || isa<ResumeInst>(Instruction)) Exits.push_back(&Instruction);
        if (isa<CatchSwitchInst>(Instruction) || isa<CatchPadInst>(Instruction) || isa<CleanupPadInst>(Instruction))
          report_fatal_error("otelc Rust: compiler exception funclets are unsupported");
        if (auto *Call = dyn_cast<CallInst>(&Instruction)) {
          if (Call->isMustTailCall()) report_fatal_error("otelc Rust: compiler musttail is unsupported");
          Call->setTailCallKind(CallInst::TCK_None);
          if (auto *Target = Call->getCalledFunction(); Target && Target->getName().starts_with("llvm.coro."))
            report_fatal_error("otelc Rust: coroutine compiler bodies need lifetime qualification");
          if (!Call->doesNotThrow() && !Call->isInlineAsm()) Calls.push_back(Call);
        }
      }
      IRBuilder<> Entry(&*Function->getEntryBlock().getFirstInsertionPt());
      Value *Storage = nullptr;
      if (Definition->Startup) Entry.CreateCall(Start);
      if (Definition->Selected) {
        auto *Allocation = Entry.CreateAlloca(ArrayType::get(Type::getInt8Ty(Context), StorageBytes));
        Allocation->setAlignment(Align(StorageAlign));
        Storage = Allocation;
        auto *Name = Entry.CreateGlobalString(Definition->Name);
        Entry.CreateCall(Enter, {Name, ConstantInt::get(Size, Definition->Name.size()), Storage});
      }
      auto Finish = [&](IRBuilder<> &Builder) {
        if (Storage) Builder.CreateCall(Leave, {Storage});
        if (Definition->Startup) Builder.CreateCall(Stop);
      };
      for (auto *Exit : Exits) { IRBuilder<> Builder(Exit); Finish(Builder); }
      if (!Calls.empty()) {
        if (!Function->hasPersonalityFn()) {
          auto Personality = Module.getOrInsertFunction("rust_eh_personality", FunctionType::get(Type::getInt32Ty(Context), true));
          Function->setPersonalityFn(cast<Constant>(Personality.getCallee()));
        }
        auto *Cleanup = BasicBlock::Create(Context, "otelc.rust.unwind", Function);
        IRBuilder<> Builder(Cleanup);
        auto *Landing = Builder.CreateLandingPad(StructType::get(Pointer, Type::getInt32Ty(Context)), 0);
        Landing->setCleanup(true);
        Finish(Builder);
        Builder.CreateResume(Landing);
        for (auto *Call : Calls) changeToInvokeAndSplitBasicBlock(Call, Cleanup);
      }
    }
    Module.getOrInsertNamedMetadata("otelc.rust.instrumented");
    if (verifyModule(Module, &errs())) report_fatal_error("otelc Rust: invalid compiler instrumentation module");
    const char *Output = std::getenv("OTELC_RUST_LLVM_RESOLVED");
    if (!Output) report_fatal_error("otelc Rust: resolved compiler output path is missing");
    json::Value Document = json::Object{{"schema_version", 1}, {"phase", "llvm-pipeline-start"},
      {"module", Module.getModuleIdentifier()}, {"functions", std::move(Resolved)}};
    auto Text = formatv("{0:2}", Document).str();
    if (Text.size() > 4 * 1024 * 1024) report_fatal_error("otelc Rust: resolved compiler inventory exceeds 4 MiB");
    std::ofstream File(Output, std::ios::binary | std::ios::trunc);
    File.write(Text.data(), Text.size());
    File.close();
    if (!File) report_fatal_error("otelc Rust: cannot write resolved compiler provenance");
    return PreservedAnalyses::none();
  }
};
}

extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "otelc-rust", "0.1.0", [](PassBuilder &Builder) {
    Builder.registerPipelineStartEPCallback([](ModulePassManager &Manager, OptimizationLevel) {
      Manager.addPass(RustPass());
    });
  }};
}
