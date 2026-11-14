"""Undo and redo for the database editors: a history of editor states, recorded one edit step at a time.

An editor captures its state before and after each step (`edit_step()`); undo restores the state before the step,
redo the one after it. What a state is belongs to the editor: the CAN editor keeps references to its (never again
modified) message objects, so a step costs a copy of the messages it changes, not of the whole database; the LIN
editor keeps a copy of its (small) LDF data.
"""

from contextlib import contextmanager
from typing import Any, Iterator, List, Optional, Tuple

UNDO_LIMIT = 200  # Steps kept; older ones are dropped


class EditHistory:
    def __init__(self, limit: int = UNDO_LIMIT):
        self.limit = limit
        self._undo: List[Tuple[Any, Any]] = []  # (state before, state after) per step, oldest first
        self._redo: List[Tuple[Any, Any]] = []

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def push(self, before: Any, after: Any) -> None:
        """Record a step; a new step makes the undone ones unreachable."""
        self._undo.append((before, after))
        del self._undo[: -self.limit]
        self._redo.clear()

    def undo(self) -> Optional[Tuple[Any, Any]]:
        """The (before, after) states of the step to undo, or None."""
        if not self._undo:
            return None
        step = self._undo.pop()
        self._redo.append(step)
        return step

    def redo(self) -> Optional[Tuple[Any, Any]]:
        if not self._redo:
            return None
        step = self._redo.pop()
        self._undo.append(step)
        return step

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()


class UndoableEditor:
    """Undo, redo and unsaved-change tracking for an editor that implements _capture(), _restore() and _same().

    Edits run inside `edit_step()`: an edit that raises is rolled back (so it leaves no partial change), one that
    changes nothing is not recorded, and nested steps form one step. `dirty` compares the state with the saved one,
    so undoing back to the saved state clears it.
    """

    def _init_history(self) -> None:
        self.history = EditHistory()
        self._step_depth = 0
        self._saved_state = self._capture()

    # Implemented by the editors
    def _capture(self) -> Any:
        raise NotImplementedError

    def _restore(self, state: Any) -> None:
        raise NotImplementedError

    def _same(self, first: Any, second: Any) -> bool:
        raise NotImplementedError

    def _begin_step(self) -> None:
        """Called when an outermost step starts."""

    def state(self) -> Any:
        """The current state, e.g. to compare it after an undo (see changed_message of the editors)."""
        return self._capture()

    @property
    def dirty(self) -> bool:
        return not self._same(self._capture(), self._saved_state)

    def _mark_saved(self) -> None:
        self._saved_state = self._capture()

    @contextmanager
    def edit_step(self) -> Iterator[None]:
        if self._step_depth:
            yield  # Part of the enclosing step
            return
        before = self._capture()
        self._begin_step()
        self._step_depth = 1
        try:
            yield
        except BaseException:
            self._restore(before)
            raise
        finally:
            self._step_depth = 0
        after = self._capture()
        if self._same(before, after):
            self._restore(before)  # Nothing changed (e.g. a field set to its value): no step, no stray copies
        else:
            self.history.push(before, after)

    def undo(self) -> bool:
        step = self.history.undo()
        if step is None:
            return False
        self._restore(step[0])
        return True

    def redo(self) -> bool:
        step = self.history.redo()
        if step is None:
            return False
        self._restore(step[1])
        return True
