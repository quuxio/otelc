#pragma once
namespace llvm { class ModulePassManager; }
void otelcAddResourcePass(llvm::ModulePassManager &);
