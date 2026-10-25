namespace native_object_storage {
struct Item {
  int value;
  explicit Item(int input) : value(input) {}
  ~Item() {}
};
Item global_item(5);
thread_local Item tls_item(6);
struct Holder {
  Item member{7};
  Holder() {}
  explicit Holder(int) : member(8) {}
  explicit Holder(char) {}
};
}  // namespace native_object_storage

extern "C" int object_fixture_storage(int input) {
  using namespace native_object_storage;
  Holder first;
  Holder second(1);
  Holder third('x');
  static Item shared(9);
  return global_item.value + tls_item.value + first.member.value +
      second.member.value + third.member.value + shared.value + input;
}

extern "C" __attribute__((no_profile_instrument_function)) int observed_storage() {
  native_object_storage::Item observer_only(11);
  return observer_only.value;
}
