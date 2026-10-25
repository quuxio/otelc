#pragma once
#include "llvm/IR/PassManager.h"
namespace otelc {
void PlanResourceBoundaries(llvm::Module &, llvm::ModuleAnalysisManager &);
struct ResourceBoundaryPass : llvm::PassInfoMixin<ResourceBoundaryPass> {
  llvm::PreservedAnalyses run(llvm::Module &, llvm::ModuleAnalysisManager &);
};
} // namespace otelc
