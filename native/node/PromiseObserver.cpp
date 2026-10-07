// Public V8 APIs only: observing state does not attach a rejection handler.
#include <node.h>
#include <v8.h>

namespace otelc {
using v8::Array;
using v8::FunctionCallbackInfo;
using v8::Local;
using v8::Object;
using v8::String;
using v8::Value;

void State(const FunctionCallbackInfo<Value>& args) {
  if (args.Length() != 1 || !args[0]->IsPromise()) {
    args.GetIsolate()->ThrowException(v8::Exception::TypeError(
        String::NewFromUtf8Literal(args.GetIsolate(), "expected a Promise")));
    return;
  }
  args.GetReturnValue().Set(static_cast<int>(args[0].As<v8::Promise>()->State()));
}

void Frames(const FunctionCallbackInfo<Value>& args) {
  auto* isolate = args.GetIsolate();
  auto context = isolate->GetCurrentContext();
  constexpr int limit = 128;
  auto stack = v8::StackTrace::CurrentStackTrace(isolate, limit,
                                               v8::StackTrace::kDetailed);
  auto output = Array::New(isolate, stack->GetFrameCount());
  for (int i = 0; i < stack->GetFrameCount(); ++i) {
    auto frame = stack->GetFrame(isolate, i);
    auto record = Array::New(isolate, 4);
    auto filename = frame->GetScriptNameOrSourceURL();
    if (filename.IsEmpty()) filename = String::Empty(isolate);
    record->Set(context, 0, filename).Check();
    record->Set(context, 1, v8::Integer::New(isolate, frame->GetScriptId())).Check();
    record->Set(context, 2, v8::Integer::New(isolate, frame->GetLineNumber())).Check();
    record->Set(context, 3, v8::Integer::New(isolate, frame->GetColumn())).Check();
    output->Set(context, i, record).Check();
  }
  args.GetReturnValue().Set(output);
}

void Initialize(Local<Object> exports, Local<Value>, Local<v8::Context>) {
  NODE_SET_METHOD(exports, "state", State);
  NODE_SET_METHOD(exports, "frames", Frames);
}
NODE_MODULE_CONTEXT_AWARE(NODE_GYP_MODULE_NAME, Initialize)
}  // namespace otelc
