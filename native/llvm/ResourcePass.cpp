// C allocation/free observation; original calls and application storage stay
// owned by C.
#include "resource_pass.h"
#include "llvm/BinaryFormat/Dwarf.h"
#include "llvm/IR/DebugInfoMetadata.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/InstIterator.h"
#include "llvm/IR/Instructions.h"
#include "llvm/IR/Module.h"
#include "llvm/IR/PassManager.h"
#include "llvm/IR/Verifier.h"
#include "llvm/Support/ErrorHandling.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Transforms/Utils/BasicBlockUtils.h"
#include <cstdint>
#include <cstdlib>
#include <string>
#include <vector>

using namespace llvm;
namespace {
const json::Object &object(const json::Value &Value) {
  auto *Row = Value.getAsObject();
  if (!Row)
    report_fatal_error("otelc: invalid C resource record");
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
  if (!Value)
    report_fatal_error("otelc: incomplete C resource array");
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
        F->getCallingConv() != CallingConv::C ||
        F->getAttributes().hasAttributesAtIndex(AttributeList::ReturnIndex))
      report_fatal_error(
          "otelc: application conflicts with the C resource ABI");
    for (unsigned Index = 0; Index < F->arg_size(); ++Index)
      if (F->getAttributes().hasParamAttrs(Index))
        report_fatal_error("otelc: application conflicts with C resource "
                           "parameter attributes");
    for (auto Attribute : F->getAttributes().getFnAttrs())
      if (!Attribute.isEnumAttribute() ||
          (Attribute.getKindAsEnum() != Attribute::NoUnwind &&
           Attribute.getKindAsEnum() != Attribute::WillReturn))
        report_fatal_error(
            "otelc: application conflicts with C resource function attributes");
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
  unsigned Hits = 0;
  std::vector<std::string> EmittedCallees;
};
void validateUnit(Module &M) {
  if (sys::path::extension(M.getSourceFileName()) != ".c" ||
      M.debug_compile_units().empty())
    report_fatal_error(
        "otelc: C resource profile needs an original C debug unit");
  for (auto *Unit : M.debug_compile_units()) {
    auto Language = Unit->getSourceLanguage();
    auto Name = Language.getName();
    bool IsC = Language.hasVersionedName()
                   ? Name == dwarf::DW_LNAME_C
                   : Name == dwarf::DW_LANG_C || Name == dwarf::DW_LANG_C89 ||
                         Name == dwarf::DW_LANG_C99 ||
                         Name == dwarf::DW_LANG_C11 ||
                         Name == dwarf::DW_LANG_C17;
    if (!IsC)
      report_fatal_error(
          "otelc: C resource profile cannot hide another source language");
  }
  if (M.getNamedMetadata("otelc.resources.instrumented"))
    report_fatal_error("otelc: C resource boundaries already instrumented");
}
std::string encodedPlan(Module &M, bool Late) {
  std::string Encoded;
  auto *Plan = M.getNamedMetadata("otelc.resources.plan");
  if (Late) {
    if (!Plan || Plan->getNumOperands() != 1 ||
        Plan->getOperand(0)->getNumOperands() != 1)
      report_fatal_error(
          "otelc: C resource plan missing at the optimiser boundary");
    auto *Value = dyn_cast<MDString>(Plan->getOperand(0)->getOperand(0));
    if (!Value)
      report_fatal_error("otelc: invalid C resource plan metadata");
    Encoded = Value->getString().str();
  } else {
    if (Plan)
      report_fatal_error("otelc: C resource source plan already present");
    const char *Filename = std::getenv("OTELC_RESOURCE_INVENTORY");
    if (!Filename)
      report_fatal_error("otelc: C resource inventory missing");
    auto Buffer = MemoryBuffer::getFile(Filename);
    if (!Buffer || (*Buffer)->getBufferSize() > 64 * 1024 * 1024)
      report_fatal_error(
          "otelc: C resource inventory unavailable or oversized");
    Encoded = (*Buffer)->getBuffer().str();
  }
  return Encoded;
}
std::vector<Site> sites(const json::Object &Inventory) {
  const auto *Compile = Inventory.getObject("compile_context");
  if (!Compile)
    report_fatal_error("otelc: C resource compile context missing");
  auto Directory = text(*Compile, "directory");
  std::vector<Site> Sites;
  for (const auto *Kind : {"allocations", "releases"})
    for (const auto &Value : array(Inventory, Kind)) {
      const auto &Row = object(Value);
      const auto *Location = Row.getObject("location");
      if (!Location)
        report_fatal_error("otelc: C resource source location missing");
      auto Line = Location->getInteger("debug_line"),
           Column = Location->getInteger("debug_column");
      if (!Line || !Column || *Line <= 0 || *Column <= 0 ||
          *Line > UINT32_MAX || *Column > UINT32_MAX ||
          Row.getBoolean("standard") != true ||
          Row.getBoolean("observer") != false)
        report_fatal_error(
            "otelc: unsupported C resource location or library binding");
      auto Callee = text(Row, "callee");
      bool Allocation = StringRef(Kind) == "allocations";
      if ((Allocation && Callee != "malloc" && Callee != "calloc") ||
          (!Allocation && Callee != "free"))
        report_fatal_error("otelc: unsupported C resource operation");
      Sites.push_back(
          {text(Row, "caller"), path(text(*Location, "debug_file"), Directory),
           Callee, Allocation ? text(Row, "type_usr") : "",
           Allocation ? text(Row, "type") : "", static_cast<unsigned>(*Line),
           static_cast<unsigned>(*Column)});
    }
  return Sites;
}
Site *matchSite(const Function &F, const Function &Target,
                const DebugLoc &Debug, std::vector<Site> &Sites, bool Late) {
  Site *Matched = nullptr;
  for (auto &Site : Sites) {
    const auto *Scope = Debug->getScope()->getSubprogram();
    auto Caller = Scope ? Scope->getName() : F.getName();
    // Optimisation may inline a selected caller or turn malloc into calloc.
    bool SameOperation =
        Site.Callee == Target.getName() ||
        (Late && Site.Callee != "free" &&
         (Target.getName() == "malloc" || Target.getName() == "calloc"));
    if (Site.Caller != Caller || !SameOperation ||
        Site.Line != Debug.getLine() || Site.Column != Debug.getCol() ||
        Site.File != path(Debug->getFilename(), Debug->getDirectory()))
      continue;
    if (Matched)
      report_fatal_error("otelc: ambiguous C resource compiler boundary");
    Matched = &Site;
  }
  return Matched;
}
CallInst *checkedCall(Module &M, CallBase &Call, Function &Target,
                      const Site &Matched) {
  auto *Pointer = PointerType::getUnqual(M.getContext());
  auto *Plain = dyn_cast<CallInst>(&Call);
  bool Free = Matched.Callee == "free";
  if (!Plain || Plain->isMustTailCall() || !Target.isDeclaration() ||
      Target.getFunctionType() != Call.getFunctionType() ||
      Target.getCallingConv() != CallingConv::C || Target.isVarArg() ||
      Call.getCallingConv() != CallingConv::C ||
      Call.arg_size() != (Target.getName() == "calloc" ? 2u : 1u) ||
      (Free ? !Call.getType()->isVoidTy() : Call.getType() != Pointer))
    report_fatal_error("otelc: unsupported C resource call shape");
  for (auto &Argument : Call.args())
    if (Free ? Argument->getType() != Pointer
             : !Argument->getType()->isIntegerTy(
                   M.getDataLayout().getPointerSizeInBits()))
      report_fatal_error("otelc: C resource library signature differs from its "
                         "compiler profile");
  return Plain;
}
std::vector<std::pair<CallInst *, Site *>>
matchedCalls(Module &M, std::vector<Site> &Sites, bool Late) {
  std::vector<std::pair<CallInst *, Site *>> Calls;
  for (auto &F : M) {
    if (F.isDeclaration() || (!Late && F.hasFnAttribute(Attribute::NoProfile)))
      continue;
    for (auto &Instruction : instructions(F)) {
      auto *Call = dyn_cast<CallBase>(&Instruction);
      if (!Call)
        continue;
      auto *Target = dyn_cast<Function>(
          Call->getCalledOperand()->stripPointerCastsAndAliases());
      auto Debug = Call->getDebugLoc();
      if (!Target || !Debug)
        continue;
      Site *Matched = matchSite(F, *Target, Debug, Sites, Late);
      if (!Matched)
        continue;
      auto *Plain = checkedCall(M, *Call, *Target, *Matched);
      if (!Late && Matched->Hits)
        report_fatal_error(
            "otelc: repeated C resource source boundary is ambiguous");
      ++Matched->Hits;
      if (Late)
        Matched->EmittedCallees.push_back(Target->getName().str());
      Calls.emplace_back(Plain, Matched);
    }
  }
  for (const auto &Site : Sites) {
    const auto *F = M.getFunction(Site.Caller);
    if (!Late && F && !F->isDeclaration() &&
        !F->hasFnAttribute(Attribute::NoProfile) && !Site.Hits)
      report_fatal_error(
          "otelc: selected C resource has no matching compiler boundary");
  }
  return Calls;
}
void writeResolution(StringRef Canonical, const std::vector<Site> &Sites) {
  const char *Resolution = std::getenv("OTELC_RESOURCE_RESOLUTION");
  if (!Resolution)
    report_fatal_error("otelc: C resource resolution output missing");
  int Descriptor = -1;
  if (sys::fs::openFileForWrite(Resolution, Descriptor, sys::fs::CD_CreateNew))
    report_fatal_error("otelc: cannot create C resource resolution output");
  json::Array Rows;
  for (const auto &Site : Sites) {
    json::Array Callees;
    for (const auto &Callee : Site.EmittedCallees)
      Callees.push_back(Callee);
    Rows.push_back(json::Object{{"caller", Site.Caller},
                                {"file", Site.File},
                                {"line", Site.Line},
                                {"column", Site.Column},
                                {"callee", Site.Callee},
                                {"type_usr", Site.Usr},
                                {"emitted_calls", Site.Hits},
                                {"emitted_callees", std::move(Callees)}});
  }
  raw_fd_ostream Output(Descriptor, true);
  Output << json::Value(
                json::Object{{"schema_version", 1},
                             {"kind", "otelc.native.c-resource-resolution"},
                             {"source", Canonical},
                             {"sites", std::move(Rows)}})
         << '\n';
  Output.close();
  if (Output.has_error())
    report_fatal_error("otelc: C resource resolution write failed");
}
struct ResourcePass : PassInfoMixin<ResourcePass> {
  bool Late;
  explicit ResourcePass(bool Late) : Late(Late) {}
  PreservedAnalyses run(Module &M, ModuleAnalysisManager &) {
    validateUnit(M);
    auto Encoded = encodedPlan(M, Late);
    auto Parsed = json::parse(Encoded);
    if (!Parsed)
      report_fatal_error("otelc: invalid C resource inventory JSON");
    const auto &Inventory = object(*Parsed);
    if (Inventory.getInteger("schema_version") != 1 ||
        Inventory.getString("kind") != "otelc.native.c-resource-inventory" ||
        Inventory.getString("profile") != "clang22-c-allocation-v1")
      report_fatal_error("otelc: unsupported C resource inventory");
    SmallString<256> Canonical;
    if (sys::fs::real_path(M.getSourceFileName(), Canonical) ||
        text(Inventory, "source") != Canonical.str())
      report_fatal_error(
          "otelc: C resource inventory does not bind this source unit");
    auto Sites = sites(Inventory);
    auto &Context = M.getContext();
    auto *Pointer = PointerType::getUnqual(Context);
    auto *Void = Type::getVoidTy(Context);
    auto *Token = Type::getInt64Ty(Context);
    auto Created =
        probe(M, "otelc_resource_allocated_v1",
              FunctionType::get(Void, {Pointer, Pointer, Pointer}, false));
    auto Releasing = probe(M, "otelc_resource_releasing_v1",
                           FunctionType::get(Token, {Pointer}, false));
    auto Released = probe(M, "otelc_resource_released_v1",
                          FunctionType::get(Void, {Token}, false));
    auto Calls = matchedCalls(M, Sites, Late);
    if (!Late) {
      // String metadata validates the source plan without keeping calls or
      // objects alive.
      M.getOrInsertNamedMetadata("otelc.resources.plan")
          ->addOperand(MDNode::get(Context, MDString::get(Context, Encoded)));
      return PreservedAnalyses::none();
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
        Body.CreateCall(Created, {Call, Body.CreateGlobalString(Site->Usr),
                                  Body.CreateGlobalString(Site->Name)});
      }
    }
    writeResolution(Canonical, Sites);
    M.getOrInsertNamedMetadata("otelc.resources.instrumented");
    if (verifyModule(M, &errs()))
      report_fatal_error("otelc: invalid C resource instrumentation");
    return PreservedAnalyses::none();
  }
};
} // namespace
void otelc::PlanResourceBoundaries(Module &M, ModuleAnalysisManager &Manager) {
  ResourcePass(false).run(M, Manager);
}
PreservedAnalyses
otelc::ResourceBoundaryPass::run(Module &M, ModuleAnalysisManager &Manager) {
  return ResourcePass(true).run(M, Manager);
}
