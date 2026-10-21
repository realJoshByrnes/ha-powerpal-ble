import importlib.util
import pathlib
import sys

import pytest

_path = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "powerpal_ble"
    / "factor_timeline.py"
)
_spec = importlib.util.spec_from_file_location("factor_timeline", _path)
ft = importlib.util.module_from_spec(_spec)
sys.modules["factor_timeline"] = ft  # dataclasses look the module up here
_spec.loader.exec_module(ft)

FOREVER = ft.FOREVER
DAY = 86400
JAN1 = 1_767_225_600  # 2026-01-01 00:00 UTC
FEB2 = JAN1 + 32 * DAY + 9 * 3600 + 30 * 60  # 2026-02-02 09:30
MAR3 = JAN1 + 61 * DAY + 14 * 3600 + 15 * 60  # 2026-03-03 14:15


def example():
    """The timeline from the discussion: 1000, then 3000 from 02/02, then 2000 from 03/03."""
    return ft.FactorTimeline([(0, 1000), (FEB2, 3000), (MAR3, 2000)])


def values(timeline):
    return [f for _, f in timeline.segments]


# ------------------------------------------------------------- mapping a minute to a factor

def test_each_minute_uses_the_entry_in_force_at_its_timestamp():
    t = example()
    assert t.factor_at(JAN1 + 10 * DAY) == 1000
    assert t.factor_at(FEB2 - 60) == 1000
    assert t.factor_at(FEB2) == 3000  # the boundary instant belongs to the new entry
    assert t.factor_at(MAR3 - 60) == 3000
    assert t.factor_at(MAR3) == 2000
    assert t.factor_at(MAR3 + 400 * DAY) == 2000
    assert t.factor_at(0) == 1000 and t.current == 2000


def test_the_same_pulses_are_worth_different_energy_under_each_meter():
    t = example()
    wh = lambda ts: 50 * 1000 / t.factor_at(ts)  # 50 pulses
    assert wh(JAN1 + DAY) == pytest.approx(50.0)
    assert wh(FEB2 + DAY) == pytest.approx(16.6667, rel=1e-4)
    assert wh(MAR3 + DAY) == pytest.approx(25.0)


# ------------------------------------------------------------- the editing rules

def test_correcting_the_first_period_leaves_the_real_meter_changes_alone():
    """'Change it from 01/01' with the default 'until the next change'."""
    new = example().with_value(1500, JAN1, origin=JAN1)
    assert new.segments == ((0, 1500), (FEB2, 3000), (MAR3, 2000))


def test_replacing_everything_from_a_date_on_is_only_done_when_asked_for():
    new = example().with_value(1500, JAN1, until=FOREVER, origin=JAN1)
    assert new.segments == ((0, 1500),)
    assert example().removed_boundaries(new) == [(FEB2, 3000), (MAR3, 2000)]


def test_the_default_does_not_remove_boundaries():
    old = example()
    assert old.removed_boundaries(old.with_value(1500, JAN1, origin=JAN1)) == []


def test_a_change_from_now_on_is_a_new_entry_and_history_is_untouched():
    old = example()
    now = MAR3 + 30 * DAY
    new = old.with_value(2500, now)
    assert new.segments == old.segments + ((now, 2500),)
    assert old.changed_from(new) == now  # nothing before `now` changes
    assert new.factor_at(MAR3 + DAY) == 2000


def test_a_start_inside_a_period_splits_it_and_keeps_the_later_boundary():
    mid = FEB2 + 5 * DAY
    new = example().with_value(4000, mid)
    assert new.segments == ((0, 1000), (FEB2, 3000), (mid, 4000), (MAR3, 2000))


def test_an_explicit_until_inside_a_period_restores_the_old_factor_after_it():
    a, b = FEB2 + 5 * DAY, FEB2 + 10 * DAY
    new = example().with_value(4000, a, until=b)
    assert new.segments == ((0, 1000), (FEB2, 3000), (a, 4000), (b, 3000), (MAR3, 2000))


def test_an_until_spanning_a_boundary_overwrites_the_boundary_it_covers():
    new = example().with_value(1234, JAN1 + DAY, until=FEB2 + DAY)
    assert new.factor_at(JAN1 + 2 * DAY) == 1234
    assert new.factor_at(FEB2 + 60) == 1234  # the old 02/02 boundary was inside the span
    assert new.factor_at(FEB2 + 2 * DAY) == 3000  # and what was in force after it resumes
    assert new.factor_at(JAN1 + 1) == 1000 and new.factor_at(MAR3) == 2000


def test_setting_the_value_already_in_force_is_a_no_op():
    old = example()
    new = old.with_value(3000, FEB2 + DAY)
    assert new.segments == old.segments
    assert old.changed_from(new) is None


def test_neighbouring_periods_with_equal_factors_are_merged():
    new = example().with_value(1000, FEB2)  # period 2 becomes 1000 like period 1
    assert new.segments == ((0, 1000), (MAR3, 2000))


def test_times_are_rounded_down_to_the_minute_so_no_minute_is_split():
    odd = FEB2 + 5 * DAY + 37
    new = example().with_value(4000, odd)
    start = [s for s, f in new.segments if f == 4000][0]
    assert start % 60 == 0 and start == odd - 37


def test_invalid_edits_are_refused():
    t = example()
    with pytest.raises(ValueError):
        t.with_value(0, JAN1)
    with pytest.raises(ValueError):
        t.with_value(1500, JAN1 + DAY, until=JAN1 + DAY)
    with pytest.raises(ValueError):
        t.with_value(1500, JAN1 + 2 * DAY, until=JAN1 + DAY)


def test_editing_never_modifies_the_original():
    old = example()
    before = old.segments
    old.with_value(1500, JAN1, until=FOREVER)
    assert old.segments == before


# ------------------------------------------------------------- comparing

def test_changes_to_reports_exactly_the_spans_that_differ():
    old = example()
    new = old.with_value(1500, JAN1, origin=JAN1)
    assert old.changes_to(new) == [ft.Change(0, FEB2, 1000, 1500)]
    flattened = old.with_value(1500, JAN1, until=FOREVER, origin=JAN1)
    assert old.changes_to(flattened) == [
        ft.Change(0, FEB2, 1000, 1500),
        ft.Change(FEB2, MAR3, 3000, 1500),
        ft.Change(MAR3, FOREVER, 2000, 1500),
    ]


def test_changed_from_is_the_earliest_affected_instant():
    old = example()
    assert old.changed_from(old.with_value(4000, FEB2 + 5 * DAY)) == FEB2 + 5 * DAY


# ------------------------------------------------------------- display and storage

def test_describe_lists_every_period():
    lines = example().describe(lambda t: f"t{t}")
    assert lines == [
        f"the beginning to t{FEB2}: 1000",
        f"t{FEB2} to t{MAR3}: 3000",
        f"t{MAR3} onwards: 2000",
    ]


def test_storage_roundtrip():
    t = example()
    assert ft.FactorTimeline.from_dict(t.to_dict()).segments == t.segments


def test_initial_value_covers_all_time():
    t = ft.FactorTimeline(initial=3200)
    assert t.segments == ((0, 3200),) and t.factor_at(10**10) == 3200


def test_a_start_at_or_before_the_first_reading_edits_the_whole_first_period():
    # nothing exists before JAN1, so starting there (or earlier) must not split the first period
    for start in (JAN1, JAN1 - 5 * DAY, 0):
        new = example().with_value(1500, start, origin=JAN1)
        assert new.segments[0] == (0, 1500) and len(new.segments) == 3


def test_a_start_just_after_the_first_reading_does_split_the_first_period():
    new = example().with_value(1500, JAN1 + 60, origin=JAN1)
    assert new.segments == ((0, 1000), (JAN1 + 60, 1500), (FEB2, 3000), (MAR3, 2000))


def test_describe_labels_the_first_period_with_the_oldest_reading():
    lines = example().describe(lambda t: f"t{t}", origin=JAN1)
    assert lines[0] == f"t{JAN1} to t{FEB2}: 1000"


# ------------------------------------------------- mapping real minutes through a timeline

_energy_path = _path.parent / "energy.py"
_espec = importlib.util.spec_from_file_location("energy_for_timeline_tests", _energy_path)
energy = importlib.util.module_from_spec(_espec)
sys.modules["energy_for_timeline_tests"] = energy
_espec.loader.exec_module(energy)

HOUR = 3600


def wh_for(timeline):
    return lambda ts, pulses: pulses * 1000 / timeline.factor_at(ts)


def test_an_hour_containing_a_meter_change_uses_each_factor_for_its_own_minutes():
    hour = JAN1 + 100 * DAY
    timeline = ft.FactorTimeline([(0, 1000), (hour + 30 * 60, 2000)])
    log = energy.MinuteLog()
    for i in range(60):
        log.add(hour + i * 60, 10)  # 10 pulses a minute throughout
    # 30 minutes at 1000/kWh (10 Wh each) and 30 at 2000/kWh (5 Wh each)
    assert log.hourly_wh(wh_for(timeline))[hour] == pytest.approx(30 * 10 + 30 * 5)


def test_a_change_from_now_on_leaves_all_earlier_energy_exactly_as_it_was():
    log = energy.MinuteLog()
    start = JAN1 + 10 * DAY
    for i in range(3 * 24 * 60):  # three days
        log.add(start + i * 60, 7)
    old = ft.FactorTimeline(initial=1000)
    now = start + 3 * DAY
    new = old.with_value(3200, now)
    before = log.hourly_wh(wh_for(old))
    after = log.hourly_wh(wh_for(new))
    assert after == before  # no minute is at or after `now`, so no hour changes at all


def test_a_backdated_change_alters_only_the_hours_from_that_instant_on():
    log = energy.MinuteLog()
    start = JAN1 + 10 * DAY
    for i in range(48 * 60):  # two days
        log.add(start + i * 60, 10)
    old = ft.FactorTimeline(initial=1000)
    cut = start + DAY + 6 * HOUR  # day 2, 06:00
    new = old.with_value(2000, cut, origin=start)
    before, after = log.hourly_wh(wh_for(old)), log.hourly_wh(wh_for(new))
    changed = sorted(h for h in before if before[h] != after[h])
    assert changed[0] == cut and all(h >= cut for h in changed)  # nothing earlier was touched
    assert old.changed_from(new) == cut
    for h in before:  # and every hour from the cut on is exactly half
        if h >= cut:
            assert after[h] == pytest.approx(before[h] / 2)


def test_pruning_old_minutes_keeps_the_energy_they_had_under_their_own_factor():
    log = energy.MinuteLog()
    start = JAN1
    for i in range(10 * 24 * 60):  # ten days at 10 pulses a minute
        log.add(start + i * 60, 10)
    timeline = ft.FactorTimeline([(0, 1000), (start + 5 * DAY, 2000)])
    wh = wh_for(timeline)
    total = log.total_wh(wh)
    log.prune(start + 10 * DAY, 3 * DAY, wh)  # fold everything older than 3 days
    assert log.total_wh(wh) == pytest.approx(total)
    assert log.base_wh > 0  # the folded energy keeps the factor it was counted under
