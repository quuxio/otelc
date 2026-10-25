#include <map>
#include <memory>
#include <set>
#include <string>
#include <vector>

#include "clang/AST/ASTConsumer.h"
#include "clang/AST/DeclCXX.h"
#include "clang/AST/ExprCXX.h"
#include "clang/AST/Mangle.h"
#include "clang/AST/RecursiveASTVisitor.h"
#include "clang/Basic/SourceManager.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Index/USRGeneration.h"
#include "clang/Tooling/CompilationDatabase.h"
#include "clang/Tooling/Tooling.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/raw_ostream.h"

namespace {
using namespace clang;

std::string usr(const Decl *Declaration) {
  llvm::SmallString<128> Value;
  if (index::generateUSRForDecl(Declaration->getCanonicalDecl(), Value)) return {};
  return Value.str().str();
}

class Visitor : public RecursiveASTVisitor<Visitor> {
 public:
  Visitor(ASTContext &Context, std::string Root)
      : Context(Context), Sources(Context.getSourceManager()), Root(std::move(Root)),
        Mangler(Context.createMangleContext()) {}

  bool shouldVisitTemplateInstantiations() const { return true; }
  bool shouldVisitImplicitCode() const { return true; }

  bool TraverseDecl(Decl *Declaration) {
    const FunctionDecl *Previous = Caller;
    if (auto *Function = llvm::dyn_cast_or_null<FunctionDecl>(Declaration)) Caller = Function;
    bool Result = RecursiveASTVisitor<Visitor>::TraverseDecl(Declaration);
    Caller = Previous;
    return Result;
  }

  bool VisitCXXRecordDecl(CXXRecordDecl *Declaration) {
    if (Declaration->isCompleteDefinition() && inside(Declaration->getLocation())) type(Declaration);
    return true;
  }

  bool VisitCXXConstructExpr(CXXConstructExpr *Expression) {
    if (!inside(Expression->getBeginLoc()) || !Statements.insert(Expression).second) return true;
    const CXXConstructorDecl *Constructor = Expression->getConstructor();
    llvm::json::Object Row = site(Expression->getBeginLoc(), Constructor->getParent());
    Row["kind"] = llvm::isa<CXXTemporaryObjectExpr>(Expression) ? "CXXTemporaryObjectExpr" : "CXXConstructExpr";
    switch (Expression->getConstructionKind()) {
      case CXXConstructionKind::Complete: Row["construction_kind"] = "complete"; break;
      case CXXConstructionKind::Delegating: Row["construction_kind"] = "delegating"; break;
      case CXXConstructionKind::NonVirtualBase: Row["construction_kind"] = "non-virtual base"; break;
      case CXXConstructionKind::VirtualBase: Row["construction_kind"] = "virtual base"; break;
    }
    Row["constructor_symbol"] = symbol(Constructor);
    Row["constructor_signature"] = Constructor->getType().getAsString();
    Row["trivial"] = Constructor->isTrivial();
    Row["elidable"] = Expression->isElidable();
    Row["array_type"] = Context.getAsArrayType(Expression->getType())
        ? llvm::json::Value(Expression->getType().getAsString()) : llvm::json::Value(nullptr);
    Constructions.push_back(std::move(Row));
    return true;
  }

  bool VisitInitListExpr(InitListExpr *Expression) {
    auto *Record = record(Expression->getType());
    if (!Record || !inside(Expression->getBeginLoc()) || !Statements.insert(Expression).second) return true;
    llvm::json::Object Row = site(Expression->getBeginLoc(), Record);
    Row["kind"] = "InitListExpr";
    Row["construction_kind"] = "aggregate";
    Row["constructor_symbol"] = nullptr;
    Row["constructor_signature"] = nullptr;
    Row["trivial"] = Record->hasTrivialDefaultConstructor();
    Row["array_type"] = Context.getAsArrayType(Expression->getType())
        ? llvm::json::Value(Expression->getType().getAsString()) : llvm::json::Value(nullptr);
    Constructions.push_back(std::move(Row));
    return true;
  }

  bool VisitVarDecl(VarDecl *Declaration) {
    auto *Record = record(Declaration->getType());
    if (!Record || !inside(Declaration->getLocation())) return true;
    llvm::json::Object Row = site(Declaration->getLocation(), Record);
    Row["kind"] = "VarDecl";
    Row["variable"] = Declaration->getNameAsString();
    Row["nrvo"] = Declaration->isNRVOVariable();
    Row["initializer_kind"] = Declaration->hasInit()
        ? llvm::json::Value(Declaration->getInit()->getStmtClassName()) : llvm::json::Value(nullptr);
    Row["storage_class"] = static_cast<int>(Declaration->getStorageClass());
    Row["parameter"] = llvm::isa<ParmVarDecl>(Declaration);
    Row["local"] = Declaration->isLocalVarDecl();
    Row["global_storage"] = Declaration->hasGlobalStorage();
    Row["static_local"] = Declaration->isStaticLocal();
    Row["thread_local"] = Declaration->getTLSKind() != VarDecl::TLS_None;
    Variables.push_back(std::move(Row));
    return true;
  }

  llvm::json::Object result() {
    llvm::json::Array Records;
    for (auto &Entry : Types) Records.push_back(std::move(Entry.second));
    return llvm::json::Object{{"schema_version", 1}, {"kind", "otelc.clang.lifetime-semantics"},
            {"types", std::move(Records)}, {"constructions", std::move(Constructions)},
            {"variables", std::move(Variables)}};
  }

 private:
  CXXRecordDecl *record(QualType Type) {
    return Context.getBaseElementType(Type).getUnqualifiedType()->getAsCXXRecordDecl();
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
    auto Value = location(Location);
    auto Filename = Value.getString("file");
    return Filename && Filename->starts_with(Root + "/");
  }

  std::string symbol(const FunctionDecl *Declaration) {
    if (Declaration->isDependentContext()) return {};
    if (!Mangler->shouldMangleDeclName(Declaration)) return Declaration->getNameAsString();
    std::string Value;
    llvm::raw_string_ostream Stream(Value);
    if (auto *Constructor = llvm::dyn_cast<CXXConstructorDecl>(Declaration))
      Mangler->mangleName(GlobalDecl(Constructor, Ctor_Complete), Stream);
    else if (auto *Destructor = llvm::dyn_cast<CXXDestructorDecl>(Declaration))
      Mangler->mangleName(GlobalDecl(Destructor, Dtor_Complete), Stream);
    else
      Mangler->mangleName(Declaration, Stream);
    return Value;
  }

  llvm::json::Object method(const CXXMethodDecl *Declaration) {
    if (auto *Definition = Declaration->getDefinition()) Declaration = llvm::cast<CXXMethodDecl>(Definition);
    auto *Constructor = llvm::dyn_cast<CXXConstructorDecl>(Declaration);
    return llvm::json::Object{{"kind", Constructor ? "CXXConstructorDecl" : llvm::isa<CXXDestructorDecl>(Declaration)
                ? "CXXDestructorDecl" : "CXXMethodDecl"},
            {"name", Declaration->getNameAsString()}, {"symbol", symbol(Declaration)},
            {"signature", Declaration->getType().getAsString()}, {"implicit", Declaration->isImplicit()},
            {"delegating", Constructor && Constructor->isDelegatingConstructor()},
            {"copy", Constructor && Constructor->isCopyConstructor()},
            {"move", Constructor && Constructor->isMoveConstructor()}, {"trivial", Declaration->isTrivial()}};
  }

  std::string type(const CXXRecordDecl *Declaration) {
    if (auto *Definition = Declaration->getDefinition()) Declaration = Definition;
    std::string Id = usr(Declaration);
    if (Types.count(Id)) return Id;
    PrintingPolicy Policy(Context.getLangOpts());
    Policy.SuppressTagKeyword = true;
    Policy.FullyQualifiedName = true;
    QualType RecordType = Context.getCanonicalTagType(Declaration);
    std::string Name = RecordType.getAsString(Policy);
    if (auto *Alias = Declaration->getTypedefNameForAnonDecl()) Name = Alias->getQualifiedNameAsString();
    llvm::json::Array Methods, Bases;
    for (auto *Method : Declaration->methods()) Methods.push_back(method(Method));
    for (const auto &Base : Declaration->bases())
      Bases.push_back(llvm::json::Object{{"name", Base.getType().getAsString(Policy)}, {"virtual", Base.isVirtual()}});
    Types.emplace(Id, llvm::json::Object{{"usr", Id}, {"name", Name}, {"location", location(Declaration->getLocation())},
        {"dependent", Declaration->isDependentContext()}, {"polymorphic", Declaration->isPolymorphic()},
        {"trivial_default_constructor", Declaration->hasTrivialDefaultConstructor()},
        {"trivial_destructor", Declaration->hasTrivialDestructor()},
        {"methods", std::move(Methods)}, {"bases", std::move(Bases)}});
    return Id;
  }

  llvm::json::Object site(SourceLocation Location, const CXXRecordDecl *Declaration) {
    return llvm::json::Object{{"type_usr", type(Declaration)}, {"location", location(Location)},
            {"caller", Caller ? symbol(Caller) : ""}};
  }

  ASTContext &Context;
  SourceManager &Sources;
  std::string Root;
  std::unique_ptr<MangleContext> Mangler;
  const FunctionDecl *Caller = nullptr;
  std::map<std::string, llvm::json::Object> Types;
  std::set<const Stmt *> Statements;
  llvm::json::Array Constructions, Variables;
};

class Consumer : public ASTConsumer {
 public:
  explicit Consumer(std::string Root) : Root(std::move(Root)) {}
  void HandleTranslationUnit(ASTContext &Context) override {
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

class Factory : public tooling::FrontendActionFactory {
 public:
  explicit Factory(std::string Root) : Root(std::move(Root)) {}
  std::unique_ptr<FrontendAction> create() override { return std::make_unique<Action>(Root); }
 private:
  std::string Root;
};

class Database : public tooling::CompilationDatabase {
 public:
  explicit Database(tooling::CompileCommand Command) : Command(std::move(Command)) {}
  std::vector<tooling::CompileCommand> getCompileCommands(llvm::StringRef) const override { return {Command}; }
  std::vector<std::string> getAllFiles() const override { return {Command.Filename}; }
 private:
  tooling::CompileCommand Command;
};
}  // namespace

int main(int Count, char **Arguments) {
  if (Count < 4) return 2;
  std::vector<std::string> Command(Arguments + 3, Arguments + Count);
  Command.push_back(Arguments[2]);
  Database Compilation({Arguments[1], Arguments[2], Command, ""});
  tooling::ClangTool Tool(Compilation, {Arguments[2]});
  Factory Inventory(Arguments[1]);
  return Tool.run(&Inventory);
}
