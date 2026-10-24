#include <stdexcept>

extern "C" int selected_c_transform(int value);

int selected_cpp(int value) {
  if (value == 0) return 0;
  return value + selected_cpp(value - 1);
}

int selected_cpp_outer(int value) { return selected_c_transform(value); }

void selected_throw() { throw std::runtime_error("original exception"); }
