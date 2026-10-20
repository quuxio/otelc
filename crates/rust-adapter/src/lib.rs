mod artifact;
pub mod compiler;
pub mod compiler_frontend;
mod compiler_inputs;
pub mod compiler_inventory;
mod compiler_llvm;
// Qualify the descriptor contract before wiring the public compiler path.
#[cfg(test)]
mod compiler_mir_profile;
mod compiler_profile;
pub mod transform;
