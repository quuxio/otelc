// Public V8 observation only: no Promise reactions, handlers or result access.
#include <node.h>
#include <v8.h>

#include <cstdint>
#include <utility>

namespace otelc {
using v8::Array;
using v8::FunctionCallbackInfo;
using v8::Local;
using v8::Object;
using v8::String;
using v8::Value;

struct ObserverState {
  explicit ObserverState(v8::Isolate* owner) : isolate(owner) {}
  v8::Isolate* isolate;
  v8::Global<v8::Function> callback;
  uint64_t generation = 0;
  uint64_t failures = 0;
  bool installed = false;
  bool running = false;
};

ObserverState* Owner(const FunctionCallbackInfo<Value>& args) {
  return static_cast<ObserverState*>(args.Data().As<v8::External>()->Value());
}

void Completed(v8::Isolate* isolate, void* data);

void Uninstall(ObserverState* state) {
  if (state->installed) {
    state->isolate->RemoveMicrotasksCompletedCallback(Completed, state);
    state->installed = false;
  }
  state->callback.Reset();
}

void Completed(v8::Isolate* isolate, void* data) {
  auto* state = static_cast<ObserverState*>(data);
  if (!state->installed || state->running) return;
  v8::HandleScope handles(isolate);
  const auto callback = state->callback.Get(isolate);
  const auto context = callback->GetCreationContextChecked();
  v8::Context::Scope scope(context);
  v8::TryCatch caught(isolate);
  const auto generation = state->generation;
  state->running = true;
  const bool failed = callback->Call(context, v8::Undefined(isolate), 0, nullptr).IsEmpty();
  state->running = false;
  if (failed) {
    // Never inspect or format a thrown application object, or cancel termination.
    state->failures++;
    if (state->generation == generation) Uninstall(state);
  }
}

void Cleanup(void* data) {
  auto* state = static_cast<ObserverState*>(data);
  Uninstall(state);
  delete state;
}

void Checkpoint(const FunctionCallbackInfo<Value>& args) {
  auto* isolate = args.GetIsolate();
  if (args.Length() != 1 || !args[0]->IsFunction()) {
    isolate->ThrowException(v8::Exception::TypeError(String::NewFromUtf8Literal(isolate, "expected own callback")));
    return;
  }
  auto* state = Owner(args);
  // One state/callback per addon instance, not one allocation or cleanup hook
  // per subscription. A superseded generation can never stop its replacement.
  constexpr uint64_t max_generation = 9007199254740991ULL;
  if (state->generation == max_generation) {
    isolate->ThrowException(v8::Exception::RangeError(String::NewFromUtf8Literal(isolate, "checkpoint generation exhausted")));
    return;
  }
  Uninstall(state);
  state->generation++;
  state->callback.Reset(isolate, args[0].As<v8::Function>());
  isolate->AddMicrotasksCompletedCallback(Completed, state);
  state->installed = true;
  args.GetReturnValue().Set(static_cast<double>(state->generation));
}

void StopCheckpoint(const FunctionCallbackInfo<Value>& args) {
  auto* state = Owner(args);
  if (args.Length() != 1 || !args[0]->IsNumber()
      || args[0].As<v8::Number>()->Value() != static_cast<double>(state->generation)) {
    args.GetReturnValue().Set(false);
    return;
  }
  const bool installed = state->installed;
  Uninstall(state);
  args.GetReturnValue().Set(installed);
}

void CheckpointStats(const FunctionCallbackInfo<Value>& args) {
  auto* isolate = args.GetIsolate();
  const auto context = isolate->GetCurrentContext();
  const auto value = Object::New(isolate);
  const auto* state = Owner(args);
  value->CreateDataProperty(context, String::NewFromUtf8Literal(isolate, "failures"),
      v8::Number::New(isolate, static_cast<double>(state->failures))).Check();
  value->CreateDataProperty(context, String::NewFromUtf8Literal(isolate, "active"),
      v8::Boolean::New(isolate, state->installed)).Check();
  value->CreateDataProperty(context, String::NewFromUtf8Literal(isolate, "generation"),
      v8::Number::New(isolate, static_cast<double>(state->generation))).Check();
  args.GetReturnValue().Set(value);
}

void State(const FunctionCallbackInfo<Value>& args) {
  if (args.Length() != 1 || !args[0]->IsPromise()) {
    args.GetIsolate()->ThrowException(v8::Exception::TypeError(
        String::NewFromUtf8Literal(args.GetIsolate(), "expected a Promise")));
    return;
  }
  args.GetReturnValue().Set(static_cast<int>(args[0].As<v8::Promise>()->State()));
}

void TypeName(const FunctionCallbackInfo<Value>& args) {
  auto* isolate = args.GetIsolate();
  if (args.Length() != 1) {
    isolate->ThrowException(v8::Exception::TypeError(String::NewFromUtf8Literal(isolate, "expected one value")));
    return;
  }
  const auto value = args[0];
  // V8 constructor metadata skips accessors/interceptors and proxy traversal.
  // Never read application name/message/stack/constructor properties in JS.
  if (value->IsObject() && !value->IsProxy()) {
    const auto name = value.As<Object>()->GetConstructorName();
    args.GetReturnValue().Set(name->Length() <= 1024 ? name : String::NewFromUtf8Literal(isolate, "object"));
    return;
  }
  args.GetReturnValue().Set(value->IsNull() ? String::NewFromUtf8Literal(isolate, "null") : value->TypeOf(isolate));
}

void Frames(const FunctionCallbackInfo<Value>& args) {
  auto* isolate = args.GetIsolate();
  const auto context = isolate->GetCurrentContext();
  constexpr int limit = 128;
  const auto stack = v8::StackTrace::CurrentStackTrace(isolate, limit, v8::StackTrace::kDetailed);
  const auto output = Array::New(isolate, stack->GetFrameCount());
  for (int i = 0; i < stack->GetFrameCount(); ++i) {
    const auto frame = stack->GetFrame(isolate, i);
    const auto record = Array::New(isolate, 4);
    auto filename = frame->GetScriptName();
    if (filename.IsEmpty()) filename = String::Empty(isolate);
    // CreateDataProperty bypasses inherited setters on application prototypes.
    record->CreateDataProperty(context, 0, filename).Check();
    record->CreateDataProperty(context, 1, v8::Integer::New(isolate, frame->GetScriptId())).Check();
    record->CreateDataProperty(context, 2, v8::Integer::New(isolate, frame->GetLineNumber())).Check();
    record->CreateDataProperty(context, 3, v8::Integer::New(isolate, frame->GetColumn())).Check();
    output->CreateDataProperty(context, i, record).Check();
  }
  args.GetReturnValue().Set(output);
}

void Initialize(Local<Object> exports, Local<Value>, Local<v8::Context> context, void*) {
  auto* isolate = v8::Isolate::GetCurrent();
  auto* state = new ObserverState(isolate);
  node::AddEnvironmentCleanupHook(isolate, Cleanup, state);
  const auto data = v8::External::New(isolate, state);
  const std::pair<const char*, v8::FunctionCallback> methods[] = {
      {"checkpoint", Checkpoint}, {"stopCheckpoint", StopCheckpoint},
      {"checkpointStats", CheckpointStats}, {"state", State},
      {"typeName", TypeName}, {"frames", Frames}};
  for (const auto& method : methods) {
    exports->CreateDataProperty(context, String::NewFromUtf8(isolate, method.first).ToLocalChecked(),
        v8::Function::New(context, method.second, data).ToLocalChecked()).Check();
  }
}
NODE_MODULE_CONTEXT_AWARE(NODE_GYP_MODULE_NAME, Initialize)
}  // namespace otelc
