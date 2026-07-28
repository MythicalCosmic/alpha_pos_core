from datetime import datetime, timedelta, timezone as dt_timezone

from core.shifts.service import _ShiftWindowIndex


def test_shift_window_index_preserves_boundaries_and_overlap_resolution():
    start = datetime(2026, 7, 24, tzinfo=dt_timezone.utc)
    handoff = start + timedelta(hours=1)
    overlapping_start = start + timedelta(minutes=20)
    overlapping_end = start + timedelta(minutes=30)
    windows = [
        (start, handoff, 'first'),
        (overlapping_start, overlapping_end, 'short-later'),
        (handoff, handoff + timedelta(hours=1), 'second'),
    ]
    index = _ShiftWindowIndex(windows)

    # Latest start wins while malformed windows overlap.
    assert index.find(overlapping_start + timedelta(minutes=1)) == 'short-later'
    # Once the short overlap ends, the still-open earlier window owns the row.
    assert index.find(overlapping_end + timedelta(minutes=1)) == 'first'
    # Half-open end makes an exact handoff belong only to the later shift.
    assert index.find(handoff) == 'second'
    assert index.find(start - timedelta(microseconds=1)) is None
    assert index.find(handoff + timedelta(hours=1)) is None


def test_shift_window_lookup_comparisons_grow_logarithmically():
    class Stamp:
        comparisons = 0

        def __init__(self, value):
            self.value = value

        @classmethod
        def reset(cls):
            cls.comparisons = 0

        def _compare(self, other, operation):
            type(self).comparisons += 1
            return operation(self.value, other.value)

        def __lt__(self, other):
            return self._compare(other, lambda left, right: left < right)

        def __le__(self, other):
            return self._compare(other, lambda left, right: left <= right)

        def __gt__(self, other):
            return self._compare(other, lambda left, right: left > right)

    count = 4096
    index = _ShiftWindowIndex([
        (Stamp(number), Stamp(number + 1), number)
        for number in range(count)
    ])
    Stamp.reset()

    assert index.find(Stamp(count - 0.5)) == count - 1
    # A linear scan needs roughly 4,096 comparisons here. Leave ample headroom
    # over the ~25 comparisons made by bisect + the max-end tree.
    assert Stamp.comparisons < 80
