"""Attempt independent benchmark finalisers without replacing the first failure."""

from collections.abc import Callable


class Cleanup:
    """Retain the body error, or fail with the first unsuccessful cleanup step."""

    def __init__(self, failure: BaseException | None = None) -> None:
        self.failure = failure
        self._original = failure

    def run(self, operation: Callable[[], object], description: str) -> bool:
        """Attempt one step; later failures become notes on the primary error."""
        try:
            operation()
            return True
        except BaseException as error:
            if self.failure is None:
                self.failure = error
            elif error is not self.failure:
                self.failure.add_note(description + ' also failed: ' + type(error).__name__)
                for note in tuple(getattr(error, '__notes__', ())):
                    self.failure.add_note(note)
            return False

    def finish(self) -> None:
        """Raise a new cleanup failure; an existing body error stays in flight."""
        if self.failure is not None and self.failure is not self._original:
            raise self.failure
