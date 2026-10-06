"""Optional comments already present in original application source."""


# otelc.instrument
def selected(value):
    return value + 1


# otelc.instrument
# otelc.exclude
def excluded():
    return 20


def configured():
    return 7


print(selected(2) + excluded() + configured())
