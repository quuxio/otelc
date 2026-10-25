; Ordinary LLVM IR. No telemetry declarations or calls.
@format = private constant [4 x i8] c"%d\0A\00"
declare i32 @printf(ptr, ...)

define i32 @workload(i32 %input) {
entry:
  %result = add i32 %input, 1
  ret i32 %result
}

define i32 @main() {
entry:
  %result = call i32 @workload(i32 41)
  %printed = call i32 (ptr, ...) @printf(ptr @format, i32 %result)
  ret i32 0
}
