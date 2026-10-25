// Explicit constructor/destructor boundary component. Runtime ownership is separate.
#include "object_pass.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/BinaryFormat/Dwarf.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/DebugInfoMetadata.h"
#include "llvm/IR/Instructions.h"
#include "llvm/IR/PassManager.h"
#include "llvm/IR/Verifier.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Plugins/PassPlugin.h"
#include "llvm/Support/ErrorHandling.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/Path.h"
#include "llvm/Transforms/Utils/BasicBlockUtils.h"
#include "llvm/Transforms/Utils/Local.h"
#include <cstdlib>
#include <map>
#include <set>
#include <string>

using namespace llvm;
namespace {
std::string text(const json::Object &Row, StringRef Key) {
  auto Value = Row.getString(Key);
  if (!Value) report_fatal_error("otelc: incomplete object semantic string");
  return Value->str();
}
const json::Array &array(const json::Object &Row, StringRef Key) {
  auto *Value = Row.getArray(Key);
  if (!Value) report_fatal_error("otelc: incomplete object semantic array");
  return *Value;
}
const json::Object &object(const json::Value &Value) {
  auto *Result = Value.getAsObject();
  if (!Result) report_fatal_error("otelc: invalid object semantic row");
  return *Result;
}
std::string path(StringRef Name, StringRef Root) {
  SmallString<256> Value(Name);
  if (!sys::path::is_absolute(Value)) {
    Value = Root;
    sys::path::append(Value, Name);
  }
  sys::path::remove_dots(Value, true);
  return Value.str().str();
}
struct ObjectType {
  std::string Id, Name;
  std::string CompleteDestructor, BaseDestructor;
  bool TrivialDestructor = false;
};
struct Site {
  ObjectType *Record;
  std::set<std::string> Constructors, Callers;
  std::string File;
  unsigned Line, Column;
  bool Global = false;
  bool Hit = false;
};

std::set<Function *> reachable(SmallVector<Function *> Pending) {
  std::set<Function *> Result;
  while (!Pending.empty()) {
    auto *F = Pending.pop_back_val();
    if (!Result.insert(F).second) continue;
    for (auto &Block : *F) for (auto &Instruction : Block)
      if (auto *Call = dyn_cast<CallBase>(&Instruction))
        if (auto *Target = dyn_cast<Function>(Call->getCalledOperand()->stripPointerCastsAndAliases())) Pending.push_back(Target);
  }
  return Result;
}

std::set<Function *> initialisers(Module &M) {
  SmallVector<Function *> Pending;
  if (auto *Entries = M.getGlobalVariable("llvm.global_ctors")) {
    if (auto *Array = dyn_cast_or_null<ConstantArray>(Entries->hasInitializer() ? Entries->getInitializer() : nullptr)) {
      for (const auto &Entry : Array->operands()) {
        auto *Row = dyn_cast<ConstantStruct>(Entry);
        if (Row && Row->getNumOperands() >= 2)
          if (auto *F = dyn_cast<Function>(Row->getOperand(1)->stripPointerCastsAndAliases())) Pending.push_back(F);
      }
    }
  }
  return reachable(std::move(Pending));
}
FunctionCallee probe(Module &M, StringRef Name, FunctionType *Signature) {
  if (auto *Existing = M.getNamedValue(Name)) {
    auto *Function = dyn_cast<llvm::Function>(Existing);
    if (!Function || !Function->isDeclaration() || Function->getFunctionType() != Signature ||
        Function->getCallingConv() != CallingConv::C || Function->getAttributes().hasAttributesAtIndex(AttributeList::ReturnIndex))
      report_fatal_error("otelc: application conflicts with the object probe ABI");
    for (unsigned Index = 0; Index < Function->arg_size(); ++Index)
      if (Function->getAttributes().hasParamAttrs(Index))
        report_fatal_error("otelc: application conflicts with object parameter attributes");
    for (auto Attribute : Function->getAttributes().getFnAttrs())
      if (!Attribute.isEnumAttribute() || (Attribute.getKindAsEnum() != Attribute::NoUnwind &&
          Attribute.getKindAsEnum() != Attribute::WillReturn))
        report_fatal_error("otelc: application conflicts with object probe attributes");
  }
  auto Result = M.getOrInsertFunction(Name, Signature);
  auto *Function = cast<llvm::Function>(Result.getCallee());
  Function->addFnAttr(Attribute::NoUnwind);
  Function->addFnAttr(Attribute::WillReturn);
  return Result;
}
struct ObjectPass : PassInfoMixin<ObjectPass> {
  PreservedAnalyses run(Module &M, ModuleAnalysisManager &) {
    if (const char *Mode = std::getenv("OTELC_OBJECT_C_UNIT"); Mode && StringRef(Mode) == "1") {
      if (sys::path::extension(M.getSourceFileName()) != ".c")
        report_fatal_error("otelc: C object exclusion does not match the source unit");
      for (auto *Unit : M.debug_compile_units()) {
        const auto Language = Unit->getSourceLanguage();
        const auto Name = Language.getName();
        bool IsC = Language.hasVersionedName() ? Name == dwarf::DW_LNAME_C :
            Name == dwarf::DW_LANG_C || Name == dwarf::DW_LANG_C89 || Name == dwarf::DW_LANG_C99 ||
            Name == dwarf::DW_LANG_C11 || Name == dwarf::DW_LANG_C17;
        if (!IsC)
          report_fatal_error("otelc: C object exclusion cannot hide another source language");
      }
      return PreservedAnalyses::all();
    }
    if (M.getNamedMetadata("otelc.objects.instrumented"))
      report_fatal_error("otelc: object boundaries already instrumented");
    const char *Filename = std::getenv("OTELC_OBJECT_INVENTORY");
    if (!Filename) report_fatal_error("otelc: object inventory missing");
    auto Buffer = MemoryBuffer::getFile(Filename);
    if (!Buffer || (*Buffer)->getBufferSize() > 64 * 1024 * 1024)
      report_fatal_error("otelc: object inventory unavailable or oversized");
    auto Parsed = json::parse((*Buffer)->getBuffer());
    if (!Parsed) report_fatal_error("otelc: invalid object inventory JSON");
    const auto &Inventory = object(*Parsed);
    auto Version = Inventory.getInteger("schema_version");
    if ((Version != 1 && Version != 2) ||
        Inventory.getString("kind") != "otelc.native.lifetime-inventory")
      report_fatal_error("otelc: unsupported object inventory");
    if (Version == 2 && (Inventory.getString("profile") != "clang22-cxx-context-v1" ||
        !Inventory.getObject("compile_context")))
      report_fatal_error("otelc: explicit object inventory context missing");
    auto Root = text(Inventory, "root");
    SmallString<256> Canonical;
    if (sys::fs::real_path(M.getSourceFileName(), Canonical))
      report_fatal_error("otelc: original object source unavailable");
    std::map<std::string, ObjectType> Types;
    SmallVector<Site> Sites;
    unsigned Units = 0;
    for (const auto &UnitValue : array(Inventory, "translation_units")) {
      const auto &Unit = object(UnitValue);
      if (text(Unit, "source") != Canonical.str()) continue;
      ++Units;
      for (const auto &Value : array(Unit, "types")) {
        const auto &Row = object(Value);
        ObjectType Record{text(Row, "usr"), text(Row, "name"), {}, {}, Row.getBoolean("trivial_destructor").value_or(true)};
        for (const auto &MethodValue : array(Row, "methods")) {
          const auto &Method = object(MethodValue);
          if (text(Method, "kind") == "CXXDestructorDecl") {
            Record.CompleteDestructor = text(Method, "symbol");
            Record.BaseDestructor = text(Method, "base_symbol");
          }
        }
        if (Record.Id.empty() || !Types.emplace(Record.Id, std::move(Record)).second)
          report_fatal_error("otelc: duplicate object type identity");
      }
      for (const auto &Value : array(Unit, "constructions")) {
        const auto &Row = object(Value);
        std::set<std::string> Callers{text(Row, "caller"), text(Row, "caller_base")};
        SmallVector<std::string> Aliases;
        bool Excluded = false;
        for (const auto &Name : Callers) {
          auto *Value = M.getNamedValue(Name);
          auto *Caller = Value ? dyn_cast<Function>(Value->stripPointerCastsAndAliases()) : nullptr;
          Excluded |= Caller && Caller->hasFnAttribute(Attribute::NoProfile);
          if (Caller) Aliases.push_back(Caller->getName().str());
          if (Caller && Row.getBoolean("thread_local_initialiser").value_or(false))
            for (auto *Helper : reachable({Caller})) Aliases.push_back(Helper->getName().str());
        }
        Callers.insert(Aliases.begin(), Aliases.end());
        if (Excluded) continue;
        if (Row.getString("kind") == "InitListExpr")
          report_fatal_error("otelc: object materialisation or trivial destruction needs a separate boundary profile");
        auto Kind = text(Row, "construction_kind");
        if (Kind == "delegating" || Kind == "non-virtual base" || Kind == "virtual base") continue;
        auto Found = Types.find(text(Row, "type_usr"));
        if (Found == Types.end()) report_fatal_error("otelc: unbound object type");
        if (Kind != "complete" || Row.getBoolean("trivial").value_or(true) || Found->second.TrivialDestructor)
          report_fatal_error("otelc: object materialisation or trivial destruction needs a separate boundary profile");
        auto *Location = Row.getObject("location");
        if (!Location) report_fatal_error("otelc: object source location missing");
        auto Line = Location->getInteger("debug_line"), Column = Location->getInteger("debug_column");
        if (!Line || !Column || *Line <= 0 || *Column <= 0)
          report_fatal_error("otelc: object debug location missing");
        Sites.push_back({&Found->second, {text(Row, "constructor_symbol"), text(Row, "constructor_base_symbol")},
                        std::move(Callers),
                        path(text(*Location, "debug_file"), Root), static_cast<unsigned>(*Line), static_cast<unsigned>(*Column),
                        Row.getBoolean("global_initialiser").value_or(false)});
      }
    }
    if (Units != 1) report_fatal_error("otelc: expected one bound object translation unit");
    auto &C = M.getContext();
    auto *Pointer = PointerType::getUnqual(C);
    auto *I64 = Type::getInt64Ty(C);
    auto *Void = Type::getVoidTy(C);
    auto Born = probe(M, "otelc_object_created_v1", FunctionType::get(Void, {Pointer, Pointer, Pointer}, false));
    auto Destroy = probe(M, "otelc_object_destroying_v1", FunctionType::get(I64, {Pointer, Pointer}, false));
    auto End = probe(M, "otelc_object_destroyed_v1", FunctionType::get(Void, {I64, Type::getInt32Ty(C)}, false));
    SmallVector<std::pair<CallBase *, Site *>> Creations;
    auto Initialisers = initialisers(M);
    for (auto &F : M) for (auto &Block : F) for (auto &Instruction : Block) {
      auto *Call = dyn_cast<CallBase>(&Instruction);
      if (!Call || !Call->getDebugLoc()) continue;
      auto *Target = dyn_cast<Function>(Call->getCalledOperand()->stripPointerCastsAndAliases());
      if (!Target) continue;
      auto *Location = Call->getDebugLoc().get();
      auto *File = Location->getFile();
      if (!File) continue;
      auto DebugFile = path(File->getFilename(), File->getDirectory());
      Site *Match = nullptr;
      for (auto &Site : Sites) {
        if ((!Site.Callers.count(F.getName().str()) && !(Site.Global && Initialisers.count(&F))) ||
            !Site.Constructors.count(Target->getName().str()) ||
            DebugFile != Site.File || Location->getLine() != Site.Line || Location->getColumn() != Site.Column) continue;
        if ((!isa<CallInst>(Call) && !isa<InvokeInst>(Call)) || Call->arg_empty() || !Call->getArgOperand(0)->getType()->isPointerTy())
          report_fatal_error("otelc: unsupported object constructor call");
        if (Match) report_fatal_error("otelc: ambiguous compiler object boundary");
        Match = &Site;
      }
      if (Match) {
        Creations.emplace_back(Call, Match);
        Match->Hit = true;
      }
    }
    for (const auto &Site : Sites) {
      if (Site.Hit) continue;
      std::string Message = "otelc: selected constructor has no matching compiler boundary at " + Site.File +
          ":" + std::to_string(Site.Line) + ":" + std::to_string(Site.Column) + " for " + Site.Record->Name + " in";
      for (const auto &Caller : Site.Callers) Message += " " + Caller;
      M.getContext().emitError(Message);
      return PreservedAnalyses::all();
    }
    for (auto [Call, Site] : Creations) {
      Instruction *Insertion;
      if (auto *Invoke = dyn_cast<InvokeInst>(Call))
        Insertion = SplitEdge(Invoke->getParent(), Invoke->getNormalDest())->getTerminator();
      else {
        auto *Plain = cast<CallInst>(Call);
        Plain->setTailCallKind(CallInst::TCK_None);
        Insertion = Plain->getNextNode();
      }
      IRBuilder<> Builder(Insertion);
      Builder.CreateCall(Born, {Call->getArgOperand(0), Builder.CreateGlobalString(Site->Record->Id),
                               Builder.CreateGlobalString(Site->Record->Name)})->setDoesNotThrow();
    }
    for (auto &[Id, Record] : Types) {
      // A complete destructor can be an alias of its base implementation.
      // Instrument that actual implementation once. If a complete wrapper
      // exists, leave its internal base call alone so it cannot claim a new
      // object placed at the same address during the old cleanup.
      auto Resolve = [&](const std::string &Name) -> Function * {
        auto *Value = M.getNamedValue(Name);
        return Value ? dyn_cast<Function>(Value->stripPointerCastsAndAliases()) : nullptr;
      };
      auto *F = Resolve(Record.CompleteDestructor);
      if (!F || F->isDeclaration()) F = Resolve(Record.BaseDestructor);
      if (!F || F->isDeclaration()) continue;
      SmallVector<Instruction *> Exits;
      SmallVector<CallInst *> Calls;
      for (auto &Block : *F) for (auto &Instruction : Block) {
        if (isa<ReturnInst>(Instruction) || isa<ResumeInst>(Instruction)) Exits.push_back(&Instruction);
        if (auto *Call = dyn_cast<CallInst>(&Instruction); Call && !F->doesNotThrow() && !Call->doesNotThrow() && !Call->isInlineAsm())
          Calls.push_back(Call);
      }
      IRBuilder<> Entry(&*F->getEntryBlock().getFirstInsertionPt());
      auto *Token = Entry.CreateCall(Destroy, {F->getArg(0), Entry.CreateGlobalString(Id)}, "otelc.object.token");
      Token->setDoesNotThrow();
      for (auto *Exit : Exits) {
        IRBuilder<> Builder(Exit);
        Builder.CreateCall(End, {Token, Builder.getInt32(isa<ResumeInst>(Exit))})->setDoesNotThrow();
      }
      if (!Calls.empty()) {
        if (!F->hasPersonalityFn()) {
          auto Personality = M.getOrInsertFunction("__gxx_personality_v0", FunctionType::get(Type::getInt32Ty(C), true));
          F->setPersonalityFn(cast<Constant>(Personality.getCallee()));
        }
        auto *Cleanup = BasicBlock::Create(C, "otelc.object.unwind", F);
        IRBuilder<> Builder(Cleanup);
        auto *Landing = Builder.CreateLandingPad(StructType::get(Pointer, Type::getInt32Ty(C)), 0);
        Landing->setCleanup(true);
        Builder.CreateCall(End, {Token, Builder.getInt32(1)})->setDoesNotThrow();
        Builder.CreateResume(Landing);
        for (auto *Call : Calls) changeToInvokeAndSplitBasicBlock(Call, Cleanup);
      }
    }
    M.getOrInsertNamedMetadata("otelc.objects.instrumented");
    if (verifyModule(M, &errs())) report_fatal_error("otelc: invalid object boundary module");
    return PreservedAnalyses::none();
  }
};
}
void otelc::InstrumentObjectBoundaries(Module &M, ModuleAnalysisManager &Manager) {
  ObjectPass().run(M, Manager);
}
#ifndef OTELC_OBJECT_WITH_FUNCTIONS_V1
extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "otelc-objects", "0.1.0", [](PassBuilder &PB) {
    PB.registerPipelineStartEPCallback([](ModulePassManager &PM, OptimizationLevel) { PM.addPass(ObjectPass()); });
  }};
}

#endif
