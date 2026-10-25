#ifndef OTELC_OBJECT_PASS_H
#define OTELC_OBJECT_PASS_H
#include "llvm/IR/PassManager.h"
namespace otelc {
void InstrumentObjectBoundaries(llvm::Module &module, llvm::ModuleAnalysisManager &manager);
}
#endif
