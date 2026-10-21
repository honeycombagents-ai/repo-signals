"""Hard caps on provider calls.

Every provider call goes through a CallBudget (see
``providers.base.find_people_within``). No configuration can raise a budget
above HARD_MAX_CALLS.
"""

HARD_MAX_CALLS = 500
DEFAULT_MAX_CALLS = 25


class BudgetExceeded(Exception):
    """Raised when a call would pass the cap. Nothing is consumed."""


def _positive_int(value, what):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{what} must be a positive whole number")
    return value


class CallBudget:
    def __init__(self, max_calls: int):
        _positive_int(max_calls, "max_calls")
        if max_calls > HARD_MAX_CALLS:
            raise ValueError(f"max_calls cannot exceed the hard bound of {HARD_MAX_CALLS}")
        self._max = max_calls
        self._used = 0

    @property
    def max_calls(self) -> int:
        return self._max

    @property
    def used(self) -> int:
        return self._used

    @property
    def remaining(self) -> int:
        return self._max - self._used

    def consume(self, n: int = 1) -> None:
        _positive_int(n, "n")
        if self._used + n > self._max:
            raise BudgetExceeded("call budget exhausted")
        self._used += n

    def __repr__(self):
        return f"CallBudget(max_calls={self._max}, used={self._used})"
