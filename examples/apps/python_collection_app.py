"""Ordinary application classes: no probes, decorators, fields or import edits."""
import gc
import weakref


class Item:
    def __init__(self, value, cycle=False):
        self.value = value
        if cycle:
            self.cycle = self


def create():
    return [Item(1), Item(2), Item(3, cycle=True)]


items = create()
total = sum(item.value for item in items)
layouts = all(set(vars(item)) == ({'value', 'cycle'} if item.value == 3 else {'value'}) for item in items)
references = [weakref.ref(item) for item in items]
del items
gc.collect()  # This application already requests collection; the observer does not.
collected = sum(reference() is None for reference in references)
assert collected == 3 and total == 6 and layouts
print(f'collections={collected}; value={total}; layouts={str(layouts).lower()}')
