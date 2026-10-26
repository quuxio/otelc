// C allocation/free observation; original calls and application storage stay owned by C.
#include "resource_pass.h"
#include "llvm/BinaryFormat/Dwarf.h"
#include "llvm/IR/DebugInfoMetadata.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/Instructions.h"
#include "llvm/IR/PassManager.h"
#include "llvm/IR/Verifier.h"
#include "llvm/Support/ErrorHandling.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Transforms/Utils/BasicBlockUtils.h"
#include <cstdlib>
#include <cstdint>
#include <string>
#include <vector>

using namespace llvm;
namespace {
const json::Object &object(const json::Value &Value) {
  auto *Row = Value.getAsObject();
  if (!Row) report_fatal_error("otelc: invalid C resource record");
  return *Row;
}
std::string text(const json::Object &Row, StringRef Key) {
  auto Value = Row.getString(Key);
  if (!Value || Value->empty() || Value->size() > 8192)
    report_fatal_error("otelc: missing C resource string");
  return Value->str();
}
const json::Array &array(const json::Object &Row, StringRef Key) {
  auto *Value = Row.getArray(Key);
  if (!Value) report_fatal_error("otelc: incomplete C resource array");
  return *Value;
}
std::string path(StringRef Name, StringRef Directory) {
  SmallString<256> Value(Name);
  if (!sys::path::is_absolute(Value)) {
    Value = Directory;
    sys::path::append(Value, Name);
  }
  sys::path::remove_dots(Value, true);
  return Value.str().str();
}
FunctionCallee probe(Module &M, StringRef Name, FunctionType *Signature) {
  if (auto *Existing = M.getNamedValue(Name)) {
    auto *F = dyn_cast<Function>(Existing);
    if (!F || !F->isDeclaration() || F->getFunctionType() != Signature ||
        F->getCallingConv() != CallingConv::C || F->getAttributes().hasAttributesAtIndex(AttributeList::ReturnIndex))
      report_fatal_error("otelc: application conflicts with the C resource ABI");
    for (unsigned Index = 0; Index < F->arg_size(); ++Index)
      if (F->getAttributes().hasParamAttrs(Index))
        report_fatal_error("otelc: application conflicts with C resource parameter attributes");
    for (auto Attribute : F->getAttributes().getFnAttrs())
      if (!Attribute.isEnumAttribute() || (Attribute.getKindAsEnum() != Attribute::NoUnwind &&
          Attribute.getKindAsEnum() != Attribute::WillReturn))
        report_fatal_error("otelc: application conflicts with C resource function attributes");
  }
  auto Result = M.getOrInsertFunction(Name, Signature);
  auto *F = cast<Function>(Result.getCallee());
  F->addFnAttr(Attribute::NoUnwind);
  F->addFnAttr(Attribute::WillReturn);
  return Result;
}
struct Site {
  std::string Caller, File, Callee, Usr, Name;
  unsigned Line, Column;
  bool Hit = false;
};
struct ResourcePass : PassInfoMixin<ResourcePass> {
  PreservedAnalyses run(Module &M, ModuleAnalysisManager &) {
    if (sys::path::extension(M.getSourceFileName()) != ".c" || M.debug_compile_units().empty())
      report_fatal_error("otelc: C resource profile needs an original C debug unit");
    for (auto *Unit : M.debug_compile_units()) {
      auto Language = Unit->getSourceLanguage();
      auto Name = Language.getName();
      bool IsC = Language.hasVersionedName() ? Name == dwarf::DW_LNAME_C :
          Name == dwarf::DW_LANG_C || Name == dwarf::DW_LANG_C89 || Name == dwarf::DW_LANG_C99 ||
          Name == dwarf::DW_LANG_C11 || Name == dwarf::DW_LANG_C17;
      if (!IsC) report_fatal_error("otelc: C resource profile cannot hide another source language");
    }
    if (M.getNamedMetadata("otelc.resources.instrumented"))
      report_fatal_error("otelc: C resource boundaries already instrumented");
    const char *Filename = std::getenv("OTELC_RESOURCE_INVENTORY");
    if (!Filename) report_fatal_error("otelc: C resource inventory missing");
    auto Buffer = MemoryBuffer::getFile(Filename);
    if (!Buffer || (*Buffer)->getBufferSize() > 64 * 1024 * 1024)
      report_fatal_error("otelc: C resource inventory unavailable or oversized");
    auto Parsed = json::parse((*Buffer)->getBuffer());
    if (!Parsed) report_fatal_error("otelc: invalid C resource inventory JSON");
    const auto &Inventory = object(*Parsed);
    if (Inventory.getInteger("schema_version") != 1 ||
        Inventory.getString("kind") != "otelc.native.c-resource-inventory" ||
        Inventory.getString("profile") != "clang22-c-allocation-v1")
      report_fatal_error("otelc: unsupported C resource inventory");
    SmallString<256> Canonical;
    if (sys::fs::real_path(M.getSourceFileName(), Canonical) || text(Inventory, "source") != Canonical.str())
      report_fatal_error("otelc: C resource inventory does not bind this source unit");
    const auto *Compile = Inventory.getObject("compile_context");
    if (!Compile) report_fatal_error("otelc: C resource compile context missing");
    auto Directory = text(*Compile, "directory");
    std::vector<Site> Sites;
    for (const auto *Kind : {"allocations", "releases"}) for (const auto &Value : array(Inventory, Kind)) {
      const auto &Row = object(Value);
      const auto *Location = Row.getObject("location");
      if (!Location) report_fatal_error("otelc: C resource source location missing");
      auto Line = Location->getInteger("debug_line"), Column = Location->getInteger("debug_column");
      if (!Line || !Column || *Line <= 0 || *Column <= 0 || *Line > UINT32_MAX || *Column > UINT32_MAX ||
          Row.getBoolean("standard") != true || Row.getBoolean("observer") != false)
        report_fatal_error("otelc: unsupported C resource location or library binding");
      auto Callee = text(Row, "callee");
      bool Allocation = StringRef(Kind) == "allocations";
      if ((Allocation && Callee != "malloc" && Callee != "calloc") || (!Allocation && Callee != "free"))
        report_fatal_error("otelc: unsupported C resource operation");
      Sites.push_back({text(Row, "caller"), path(text(*Location, "debug_file"), Directory), Callee,
          Allocation ? text(Row, "type_usr") : "", Allocation ? text(Row, "type") : "",
          static_cast<unsigned>(*Line), static_cast<unsigned>(*Column)});
    }
    auto &Context = M.getContext();
    auto *Pointer = PointerType::getUnqual(Context);
    auto *Void = Type::getVoidTy(Context);
    auto *Token = Type::getInt64Ty(Context);
    auto Created = probe(M, "otelc_resource_allocated_v1", FunctionType::get(Void, {Pointer, Pointer, Pointer}, false));
    auto Releasing = probe(M, "otelc_resource_releasing_v1", FunctionType::get(Token, {Pointer}, false));
    auto Released = probe(M, "otelc_resource_released_v1", FunctionType::get(Void, {Token}, false));
    std::vector<std::pair<CallInst *, Site *>> Calls;
    for (auto &F : M) {
      if (F.isDeclaration() || F.hasFnAttribute(Attribute::NoProfile)) continue;
      for (auto &Block : F) for (auto &Instruction : Block) {
        auto *Call = dyn_cast<CallBase>(&Instruction);
        if (!Call) continue;
        auto *Target = dyn_cast<Function>(Call->getCalledOperand()->stripPointerCastsAndAliases());
        auto Debug = Call->getDebugLoc();
        if (!Target || !Debug) continue;
        Site *Matched = nullptr;
        for (auto &Site : Sites) {
          if (Site.Caller != F.getName() || Site.Callee != Target->getName() ||
              Site.Line != Debug.getLine() || Site.Column != Debug.getCol() ||
              Site.File != path(Debug->getFilename(), Debug->getDirectory())) continue;
          if (Matched) report_fatal_error("otelc: ambiguous C resource compiler boundary");
          Matched = &Site;
        }
        if (!Matched) continue;
        auto *Plain = dyn_cast<CallInst>(Call);
        bool Free = Matched->Callee == "free";
        if (!Plain || Plain->isMustTailCall() || !Target->isDeclaration() ||
            Target->getFunctionType() != Call->getFunctionType() || Target->getCallingConv() != CallingConv::C ||
            Target->isVarArg() || Call->getCallingConv() != CallingConv::C ||
            Call->arg_size() != (Matched->Callee == "calloc" ? 2u : 1u) ||
            (Free ? !Call->getType()->isVoidTy() : Call->getType() != Pointer))
          report_fatal_error("otelc: unsupported C resource call shape");
        for (auto &Argument : Call->args())
          if (Free ? Argument->getType() != Pointer : !Argument->getType()->isIntegerTy(M.getDataLayout().getPointerSizeInBits()))
            report_fatal_error("otelc: C resource library signature differs from its compiler profile");
        if (Matched->Hit) report_fatal_error("otelc: repeated C resource source boundary is ambiguous");
        Matched->Hit = true;
        Calls.emplace_back(Plain, Matched);
      }
    }
    for (const auto &Site : Sites) {
      const auto *F = M.getFunction(Site.Caller);
      if (F && !F->isDeclaration() && !F->hasFnAttribute(Attribute::NoProfile) && !Site.Hit)
        report_fatal_error("otelc: selected C resource has no matching compiler boundary");
    }
    for (auto [Call, Site] : Calls) {
      Call->setTailCallKind(CallInst::TCK_None);
      if (Site->Callee == "free") {
        IRBuilder<> Before(Call);
        auto *Claim = Before.CreateCall(Releasing, {Call->getArgOperand(0)});
        IRBuilder<>(Call->getNextNode()).CreateCall(Released, {Claim});
      } else {
        auto *Next = Call->getNextNode();
        IRBuilder<> Condition(Next);
        auto *NonNull = Condition.CreateIsNotNull(Call);
        auto *End = SplitBlockAndInsertIfThen(NonNull, Next, false);
        IRBuilder<> Body(End);
        Body.CreateCall(Created, {Call, Body.CreateGlobalStringPtr(Site->Usr), Body.CreateGlobalStringPtr(Site->Name)});
      }
    }
    M.getOrInsertNamedMetadata("otelc.resources.instrumented");
    if (verifyModule(M, &errs())) report_fatal_error("otelc: invalid C resource instrumentation");
    return PreservedAnalyses::none();
  }
};
}  // namespace
void otelcAddResourcePass(ModulePassManager &Passes) { Passes.addPass(ResourcePass()); }
