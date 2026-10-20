"""Ordinary resources: explicit close, a failed close and unchanged object state."""
import gc
import weakref


class Resource:
    def __init__(self, value):
        self.value = value
        self.closed = False

    def close(self, fail=False):
        if fail:
            raise ValueError('close failed')
        self.closed = True
        return self.value


def create():
    return [Resource(3), Resource(4)]


values = create()
assert all(value.__dict__ == {'value': value.value, 'closed': False} for value in values)
try:
    values[0].close(fail=True)
except ValueError:
    pass
assert not values[0].closed
total = sum(value.close() for value in values)
assert values[0].close() == 3
assert all(value.__dict__ == {'value': value.value, 'closed': True} for value in values)
references = [weakref.ref(value) for value in values]
control = [values[0]]
values.clear()
gc.collect()
if references[0]() is not control[0]:
    raise AssertionError('retention control disappeared while strongly held')
control.clear()
gc.collect()
retained = sum(reference() is not None for reference in references)
if retained:
    raise AssertionError('closed resource retained after application owners were cleared')
print(f'resources=2; value={total}; failed_close=1; layouts=true; retained={retained}')
