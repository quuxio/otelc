#include <map>
#include <memory>
#include <string>
#include <vector>
#include "clang/AST/ASTConsumer.h"
#include "clang/AST/Attr.h"
#include "clang/AST/RecursiveASTVisitor.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Index/USRGeneration.h"
#include "clang/Tooling/Tooling.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/raw_ostream.h"

namespace {
using namespace clang;
class Visitor : public RecursiveASTVisitor<Visitor> {
 public:
  Visitor(ASTContext &Context, std::string Root)
      : Sources(Context.getSourceManager()), Root(std::move(Root)) {}

  bool TraverseDecl(Decl *Declaration) {
    const FunctionDecl *PreviousCaller = Caller;
    QualType PreviousHint = Hint;
    if (auto *Function = llvm::dyn_cast_or_null<FunctionDecl>(Declaration)) Caller = Function;
    if (auto *Variable = llvm::dyn_cast_or_null<VarDecl>(Declaration)) Hint = Variable->getType();
    bool Result = RecursiveASTVisitor<Visitor>::TraverseDecl(Declaration);
    Caller = PreviousCaller;
    Hint = PreviousHint;
    return Result;
  }
  bool TraverseStmt(Stmt *Statement) {
    QualType Previous = Hint;
    if (auto *Cast = llvm::dyn_cast_or_null<ExplicitCastExpr>(Statement)) Hint = Cast->getType();
    if (auto *Assignment = llvm::dyn_cast_or_null<BinaryOperator>(Statement))
      if (Assignment->isAssignmentOp()) Hint = Assignment->getLHS()->getType();
    if (llvm::isa_and_nonnull<ReturnStmt>(Statement) && Caller) Hint = Caller->getReturnType();
    if (auto *Call = llvm::dyn_cast_or_null<CallExpr>(Statement)) {
      Hints[Call] = Hint;
      // An argument of another call does not inherit its caller's result type.
      Hint = QualType{};
    }
    bool Result = RecursiveASTVisitor<Visitor>::TraverseStmt(Statement);
    Hint = Previous;
    return Result;
  }
  bool TraverseUnaryExprOrTypeTraitExpr(UnaryExprOrTypeTraitExpr *) { return true; }
  bool VisitVarDecl(VarDecl *Variable) {
    if (llvm::isa<ParmVarDecl>(Variable) || !inside(Variable->getLocation())) return true;
    auto *RecordType = Variable->getType()->getAs<clang::RecordType>();
    if (!RecordType) return true;
    Unsupported.push_back(llvm::json::Object{{"type_usr", type(RecordType->getDecl())},
        {"location", location(Variable->getLocation())}, {"caller", Caller ? Caller->getNameAsString() : ""},
        {"observer", Caller && Caller->hasAttr<NoProfileFunctionAttr>()},
        {"reason", "C record scope boundaries need a separate lifetime profile"}});
    return true;
  }
  bool VisitCallExpr(CallExpr *Call) {
    if (!Caller || !inside(Call->getBeginLoc())) return true;
    const auto *Function = Call->getDirectCallee();
    if (!Function) return true;
    std::string Name = Function->getNameAsString();
    if (llvm::StringRef(Name).starts_with("__builtin_")) Name.erase(0, 10);
    if (Name != "malloc" && Name != "calloc" && Name != "free" && Name != "realloc") return true;
    llvm::json::Object Row{{"callee", Name}, {"caller", Caller->getNameAsString()},
        {"location", location(Call->getBeginLoc())},
        {"standard", Function->getBuiltinID() != 0 && !Function->getDefinition()},
        {"observer", Caller->hasAttr<NoProfileFunctionAttr>()},
        {"arguments", Call->getNumArgs()}, {"signature", Function->getType().getAsString()}};
    if (Name == "free") {
      Releases.push_back(std::move(Row));
    } else if (Name == "realloc") {
      Row["reason"] = "realloc needs a separate allocation-generation profile";
      Unsupported.push_back(std::move(Row));
    } else {
      QualType Pointer = Hints[Call];
      if (Pointer.isNull() || !Pointer->isPointerType()) return true;
      Pointer = Pointer.getCanonicalType();
      auto *RecordType = Pointer->getPointeeType()->getAs<clang::RecordType>();
      if (!RecordType) return true;
      Row["type_usr"] = type(RecordType->getDecl());
      Allocations.push_back(std::move(Row));
    }
    return true;
  }
  llvm::json::Object result() {
    llvm::json::Array Records;
    for (auto &Entry : Types) Records.push_back(std::move(Entry.second));
    return llvm::json::Object{{"schema_version", 1}, {"kind", "otelc.clang.c-resource-semantics"},
        {"types", std::move(Records)}, {"allocations", std::move(Allocations)},
        {"releases", std::move(Releases)}, {"unsupported", std::move(Unsupported)}};
  }
 private:
  std::string type(const RecordDecl *Record) {
    Record = llvm::cast<RecordDecl>(Record->getCanonicalDecl());
    llvm::SmallString<128> Usr;
    if (index::generateUSRForDecl(Record, Usr)) return {};
    const std::string Id = Usr.str().str();
    const auto *Definition = Record->getDefinition();
    std::string Name = Record->getNameAsString();
    if (Name.empty())
      if (auto *Alias = Record->getTypedefNameForAnonDecl()) Name = Alias->getNameAsString();
    if (!Types.count(Id)) Types.emplace(Id, llvm::json::Object{{"usr", Id},
        {"name", Name}, {"location", location((Definition ? Definition : Record)->getLocation())},
        {"complete", Definition != nullptr}});
    return Id;
  }
  llvm::json::Object location(SourceLocation Location) {
    Location = Sources.getExpansionLoc(Location);
    llvm::SmallString<256> Filename(Sources.getFilename(Location));
    if (!Filename.empty()) llvm::sys::fs::real_path(Filename, Filename);
    PresumedLoc Presumed = Sources.getPresumedLoc(Location);
    return llvm::json::Object{{"file", Filename.str().str()}, {"offset", Sources.getFileOffset(Location)},
        {"line", Sources.getSpellingLineNumber(Location)}, {"column", Sources.getSpellingColumnNumber(Location)},
        {"debug_file", Presumed.isValid() ? Presumed.getFilename() : ""},
        {"debug_line", Presumed.isValid() ? Presumed.getLine() : 0},
        {"debug_column", Presumed.isValid() ? Presumed.getColumn() : 0}};
  }
  bool inside(SourceLocation Location) {
    if (Location.isInvalid()) return false;
    return location(Location).getString("file")->starts_with(Root + "/");
  }
  SourceManager &Sources;
  std::string Root;
  const FunctionDecl *Caller = nullptr;
  QualType Hint;
  std::map<const CallExpr *, QualType> Hints;
  std::map<std::string, llvm::json::Object> Types;
  llvm::json::Array Allocations, Releases, Unsupported;
};
class Consumer : public ASTConsumer {
 public:
  explicit Consumer(std::string Root) : Root(std::move(Root)) {}
  void HandleTranslationUnit(ASTContext &Context) override {
    if (Context.getLangOpts().CPlusPlus) {
      auto Id = Context.getDiagnostics().getCustomDiagID(DiagnosticsEngine::Error,
          "C resource profile requires C source");
      Context.getDiagnostics().Report(Id);
      return;
    }
    Visitor Inventory(Context, Root);
    Inventory.TraverseDecl(Context.getTranslationUnitDecl());
    llvm::outs() << llvm::json::Value(Inventory.result()) << '\n';
  }
 private:
  std::string Root;
};
class Action : public ASTFrontendAction {
 public:
  explicit Action(std::string Root) : Root(std::move(Root)) {}
  std::unique_ptr<ASTConsumer> CreateASTConsumer(CompilerInstance &, llvm::StringRef) override {
    return std::make_unique<Consumer>(Root);
  }
 private:
  std::string Root;
};
}  // namespace

int main(int Count, char **Arguments) {
  if (Count < 4) return 2;
  int First = 3;
  std::string Directory = Arguments[1];
  if (llvm::StringRef(Arguments[3]) == "--compile-directory") {
    if (Count < 6) return 2;
    Directory = Arguments[4];
    First = 5;
  }
  std::vector<std::string> Command(Arguments + First, Arguments + Count);
  Command.push_back("-fsyntax-only");
  Command.push_back(Arguments[2]);
  auto Filesystem = llvm::vfs::getRealFileSystem();
  if (Filesystem->setCurrentWorkingDirectory(Directory)) return 2;
  for (int Index = First + 1; Index < Count; ++Index) {
    if (llvm::StringRef(Arguments[Index]) != "-ivfsoverlay") continue;
    if (++Index == Count) return 2;
    auto Buffer = llvm::MemoryBuffer::getFile(Arguments[Index]);
    if (!Buffer) return 2;
    auto Overlay = llvm::vfs::getVFSFromYAML(std::move(*Buffer), nullptr, Arguments[Index], nullptr, Filesystem);
    if (!Overlay) return 2;
    Filesystem = llvm::IntrusiveRefCntPtr<llvm::vfs::FileSystem>(Overlay.release());
  }
  llvm::IntrusiveRefCntPtr<FileManager> Files(new FileManager(FileSystemOptions{}, Filesystem));
  tooling::ToolInvocation Tool(std::move(Command), std::make_unique<Action>(Arguments[1]), Files.get());
  return Tool.run() ? 0 : 1;
}
