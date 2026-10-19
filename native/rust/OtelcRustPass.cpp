// Rust compiler hooks retain original source spans and use the Rust SDK.
#include "llvm/ADT/SmallVector.h"
#include "llvm/Demangle/Demangle.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/DebugInfoMetadata.h"
#include "llvm/IR/DebugProgramInstruction.h"
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
#include <filesystem>
#include <fstream>
using namespace llvm;

namespace {
constexpr uint64_t StorageBytes = 256;
constexpr uint64_t StorageAlign = 16;

struct Definition {
  std::string File, Name, Leaf, Suffix, Unsupported, SourceHash;
  uint64_t HeaderStart, HeaderEnd, BodyStartLine, BodyStartColumn, BodyEndLine, BodyEndColumn;
  uint64_t DeclarationStartLine, DeclarationStartColumn, DeclarationEndLine, DeclarationEndColumn;
  bool Selected, Startup;
};

std::string normalPath(StringRef Directory, StringRef File) {
  return (std::filesystem::path(Directory.str()) / File.str()).lexically_normal().string();
}

SmallVector<Definition> definitions() {
  const char *Path = std::getenv("OTELC_RUST_LLVM_INVENTORY");
  if (!Path) report_fatal_error("otelc Rust: compiler inventory is missing");
  auto Buffer = MemoryBuffer::getFile(Path);
  if (!Buffer || (*Buffer)->getBufferSize() > 4 * 1024 * 1024)
    report_fatal_error("otelc Rust: compiler inventory is unavailable or too large");
  auto Document = json::parse((*Buffer)->getBuffer());
  auto *Object = Document ? Document->getAsObject() : nullptr;
  if (!Object || Object->getInteger("schema_version") != 2)
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
      Flag("selected"), Flag("startup")};
    if (Item.Name.empty() || Item.Name.size() > 1024 || Item.HeaderEnd < Item.HeaderStart
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

bool closureDeclaration(const Function &Function, const Definition &Definition) {
  auto Begin = std::pair(Definition.DeclarationStartLine, Definition.DeclarationStartColumn);
  auto End = std::pair(Definition.DeclarationEndLine, Definition.DeclarationEndColumn);
  for (const auto &Block : Function) for (const auto &Instruction : Block)
    for (const auto &Record : Instruction.getDbgRecordRange()) {
      const auto *Variable = dyn_cast<DbgVariableRecord>(&Record);
      if (!Variable || Variable->getVariable()->getArg() != 1) continue;
      const auto &Location = Record.getDebugLoc();
      if (!Location || cast<DILocalScope>(Location.getScope())->getSubprogram() != Function.getSubprogram()) continue;
      auto Position = std::pair(uint64_t(Location.getLine()), uint64_t(Location.getCol()));
      if (Begin <= Position && Position <= End) return true;
    }
  return false;
}

std::string withoutGenerics(StringRef Name) {
  std::string Result;
  unsigned Depth = 0;
  for (char Value : Name) {
    if (Value == '<') ++Depth;
    else if (Value == '>') { if (Depth) --Depth; }
    else if (!Depth && Value != ' ') Result.push_back(Value);
  }
  for (size_t Index = 0; (Index = Result.find("::", Index)) != std::string::npos;)
    Result.replace(Index, 2, ".");
  for (size_t Index = 0; (Index = Result.find("r#", Index)) != std::string::npos;)
    Result.erase(Index, 2);
  return Result;
}

bool bodyLocation(const Function &Function, const Definition &Definition) {
  for (const auto &Block : Function) for (const auto &Instruction : Block) {
    const auto &Location = Instruction.getDebugLoc();
    if (!Location || cast<DILocalScope>(Location.getScope())->getSubprogram() != Function.getSubprogram()) continue;
    uint64_t Line = Location.getLine(), Column = Location.getCol();
    if (!Column || Line < Definition.BodyStartLine || Line > Definition.BodyEndLine) continue;
    if (Line == Definition.BodyStartLine && Column < Definition.BodyStartColumn) continue;
    if (Line == Definition.BodyEndLine && Column > Definition.BodyEndColumn) continue;
    return true;
  }
  return false;
}

const Definition *definitionFor(const Function &Function, ArrayRef<Definition> Definitions) {
  const auto *Subprogram = Function.getSubprogram();
  if (!Subprogram || !Subprogram->getFile()) return nullptr;
  const auto *File = Subprogram->getFile();
  auto Path = normalPath(File->getDirectory(), File->getFilename());
  auto Leaf = withoutGenerics(Subprogram->getName());
  SmallVector<const Definition *> Candidates;
  for (const auto &Definition : Definitions) {
    if (Definition.File != Path || Subprogram->getLine() < Definition.HeaderStart
        || Subprogram->getLine() > Definition.HeaderEnd) continue;
    if (Definition.Leaf == "<closure>" ? !Subprogram->getName().starts_with("{closure")
                                       : withoutGenerics(Definition.Leaf) != Leaf) continue;
    Candidates.push_back(&Definition);
  }
  if (!Candidates.empty() && Candidates.front()->Leaf == "<closure>") {
    // Constant/zero-sized closure returns can share the same body-end debug
    // location. Their compiler-owned environment argument identifies the
    // original closure prefix, including move/parameter tokens.
    SmallVector<const Definition *> Specific;
    for (const auto *Definition : Candidates)
      if (closureDeclaration(Function, *Definition)) Specific.push_back(Definition);
    if (Specific.empty()) report_fatal_error("otelc Rust: compiled closure declaration did not resolve");
    Candidates = std::move(Specific);
  }
  if (Candidates.size() > 1) {
    SmallVector<const Definition *> Specific;
    for (const auto *Definition : Candidates)
      if (bodyLocation(Function, *Definition)) Specific.push_back(Definition);
    Candidates = std::move(Specific);
    if (Candidates.empty()) report_fatal_error("otelc Rust: compiled function source position did not resolve");
  }
  if (Candidates.size() > 1) {
    // A nested same-name function's body is contained in its parent's source
    // range, although its instructions belong to a separate compiler scope.
    SmallVector<const Definition *> Inner;
    for (const auto *Candidate : Candidates) {
      auto Begin = std::pair(Candidate->BodyStartLine, Candidate->BodyStartColumn);
      auto End = std::pair(Candidate->BodyEndLine, Candidate->BodyEndColumn);
      bool ContainsAnother = false;
      for (const auto *Other : Candidates) {
        if (Other == Candidate) continue;
        auto OtherBegin = std::pair(Other->BodyStartLine, Other->BodyStartColumn);
        auto OtherEnd = std::pair(Other->BodyEndLine, Other->BodyEndColumn);
        ContainsAnother |= Begin <= OtherBegin && OtherEnd <= End && (Begin != OtherBegin || End != OtherEnd);
      }
      if (!ContainsAnother) Inner.push_back(Candidate);
    }
    Candidates = std::move(Inner);
  }
  if (Candidates.size() > 1) {
    auto Symbol = withoutGenerics(llvm::demangle(Function.getName().str()));
    SmallVector<const Definition *> Specific;
    for (const auto *Definition : Candidates)
      if (StringRef(Symbol).ends_with(withoutGenerics(Definition->Suffix))) Specific.push_back(Definition);
    if (!Specific.empty()) Candidates = std::move(Specific);
  }
  if (Candidates.size() > 1)
    report_fatal_error("otelc Rust: compiled function identity is ambiguous");
  return Candidates.empty() ? nullptr : Candidates.front();
}

struct RustPass : PassInfoMixin<RustPass> {
  PreservedAnalyses run(Module &Module, ModuleAnalysisManager &) {
    if (Module.getNamedMetadata("otelc.rust.instrumented"))
      report_fatal_error("otelc Rust: compiler module is already instrumented");
    auto Definitions = definitions();
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
      const auto *Definition = definitionFor(Function, Definitions);
      if (Definition && (Definition->Selected || Definition->Startup)) {
        if (!Definition->Unsupported.empty()) report_fatal_error(Twine("otelc Rust: ") + Definition->Unsupported);
        if (Function.hasFnAttribute(Attribute::Naked)) report_fatal_error("otelc Rust: naked compiler bodies are unsupported");
        if (Function.hasPersonalityFn()) {
          const auto *Personality = dyn_cast<llvm::Function>(Function.getPersonalityFn()->stripPointerCasts());
          if (!Personality || Personality->getName() != "rust_eh_personality")
            report_fatal_error("otelc Rust: unsupported compiler exception personality");
        }
        Selected.push_back({&Function, Definition});
      }
    }
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
