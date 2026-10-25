#include <new>
namespace native_object_fixture {
struct Base { Base() {} virtual ~Base() {} };
struct Item : virtual Base {
  int value;
  explicit Item(int input) : value(input) { if (input < 0) throw input; }
  Item() : Item(1) {}
  Item(const Item &other) : value(other.value) {}
  ~Item() override {}
};
Item factory() { return Item(5); }
void throw_cleanup() { throw 13; }
struct ThrowingItem {
  ThrowingItem() {}
  ~ThrowingItem() noexcept(false) { throw_cleanup(); }
};
}  // namespace native_object_fixture
using native_object_fixture::Item;
extern "C" int object_fixture_run() {
  Item first(1);
  Item copy(first);
  Item array[2];
  auto *heap = new Item(4);
  Item returned = native_object_fixture::factory();
  try { Item failed(-1); } catch (int) {}
  delete heap;
  return first.value + copy.value + returned.value + array[0].value;
}
extern "C" int object_fixture_reuse() {
  alignas(Item) unsigned char storage[sizeof(Item)];
  auto *first = new (storage) Item(1);
  int result = first->value;
  first->~Item();
  auto *second = new (storage) Item(2);
  result += second->value;
  second->~Item();
  return result;
}
extern "C" int object_fixture_failed() {
  try { Item failed(-1); } catch (int) { return 7; }
  return 0;
}
extern "C" int object_fixture_unwind() {
  try { Item item(4); throw 7; } catch (int value) { return value; }
}
extern "C" void *object_fixture_make() { return new Item(9); }
extern "C" void object_fixture_delete(void *address) { delete static_cast<Item *>(address); }
extern "C" int object_fixture_throwing_destructor() {
  try { native_object_fixture::ThrowingItem item; } catch (int value) { return value; }
  return 0;
}
