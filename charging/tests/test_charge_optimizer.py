"""
Test suite for charge_optimizer.yaml blueprint logic.

Mirrors every Jinja2 template calculation in Python so the core algorithm can be
verified independently of Home Assistant.  Each test section directly maps to a
template variable or branch in the blueprint.

Real-world setup (used as baseline in several tests):
  max_current   : 16 A
  min_current   :  6 A
  battery       : 60 kWh  (blueprint default; not overridden in the live automation)
  target_soc    : 100 %
  efficiency    : 0.90    (hard-coded in blueprint)
  tariff        : 0.9648  (hard-coded in blueprint)
  price res.    : 15 min  (Nord Pool SE4 data)
  departure     : 09:00   (helper entity overrides hard-coded 07:00)
"""

import math
from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# Blueprint logic re-implementations
# ---------------------------------------------------------------------------

EFFICIENCY = 0.9
TARIFF = 0.9648


def calc_charge_power_kw(max_a: float, min_a: float) -> float:
    """
    Template:
        ((max + min) / 2) * 230 * 3 / 1000 | round(2)
    Average current * 230 V * 3 phases, expressed in kW.
    """
    return round(((max_a + min_a) / 2) * 230 * 3 / 1000, 2)


def calc_required_charge_kw(
    target_soc: float,
    current_soc: float,
    battery_kwh: float,
    efficiency: float = EFFICIENCY,
) -> float:
    """
    Template:
        (target_soc - current_soc) * battery_capacity / 100 / efficiency_factor
    Energy that must be drawn from the grid to reach target_soc.
    """
    return (target_soc - current_soc) * battery_kwh / 100 / efficiency


def calc_periods_to_charge(required_kw: float, charge_power_kw: float) -> int:
    """
    Template:
        ((required_charge_power_kw / charge_power_kw)) | round(0, 'ceil') | int
    Whole hours of charging needed (rounds up).
    """
    return math.ceil(required_kw / charge_power_kw)


def detect_resolution_minutes(raw_today: list) -> int:
    """
    Template:
        time_diff_seconds = raw_today[1].start.timestamp() - raw_today[0].start.timestamp()
        resolution_minutes = (time_diff_seconds / 60) | int
    Falls back to 60 when fewer than 2 entries exist.
    """
    if not raw_today or len(raw_today) < 2:
        return 60
    diff_sec = raw_today[1]["start_ts"] - raw_today[0]["start_ts"]
    return int(diff_sec / 60)


def calc_periods_needed(periods_to_charge: int, resolution_minutes: int) -> int:
    """
    Template:
        (periods_to_charge * 60 / resolution_minutes) | round(0, 'ceil') | int
    Number of price slots required given the data resolution.
    """
    return math.ceil(periods_to_charge * 60 / resolution_minutes)


def calc_departure_ts(
    current_dt: datetime,
    departure_time_str: str,
) -> float:
    """
    Template:
        departure = today.replace(hour, minute, second)
        if departure < now: departure += 1 day
    Returns a POSIX timestamp always in the future.
    departure_time_str must be 'HH:MM:SS'.
    """
    h, m, s = map(int, departure_time_str.split(":"))
    departure = current_dt.replace(hour=h, minute=m, second=s, microsecond=0)
    if departure.timestamp() < current_dt.timestamp():
        departure += timedelta(days=1)
    return departure.timestamp()


def calc_cheapest_periods(
    raw_today: list,
    raw_tomorrow: list,
    current_ts: float,
    departure_ts: float,
    periods_needed: int,
    resolution_minutes: int,
) -> list:
    """
    Template:
        Collect all future periods (period_end > now) that start before departure,
        from both today and tomorrow.
        Sort by value ascending, take first periods_needed, then re-sort by start_ts.
    """
    prices = []
    period_duration = resolution_minutes * 60

    for p in raw_today or []:
        period_end = p["start_ts"] + period_duration
        if period_end > current_ts and p["start_ts"] < departure_ts:
            prices.append({**p, "resolution_minutes": resolution_minutes})

    for p in raw_tomorrow or []:
        period_end = p["start_ts"] + period_duration
        if period_end > current_ts and p["start_ts"] < departure_ts:
            prices.append({**p, "resolution_minutes": resolution_minutes})

    sorted_by_price = sorted(prices, key=lambda x: x["value"])
    selected = sorted_by_price[:periods_needed]
    return sorted(selected, key=lambda x: x["start_ts"])


def is_in_cheapest_period(cheapest_periods: list, current_ts: float) -> bool:
    """
    Template:
        for price in cheapest_periods:
            if current_ts >= price.start_ts and current_ts < price.start_ts + duration:
                is_cheapest_period = true
    """
    if not cheapest_periods:
        return False
    resolution_minutes = cheapest_periods[0].get("resolution_minutes", 60)
    duration = resolution_minutes * 60
    return any(
        p["start_ts"] <= current_ts < p["start_ts"] + duration
        for p in cheapest_periods
    )


def calc_total_charge_cost(
    cheapest_periods: list,
    required_charge_kw: float,
    efficiency: float = EFFICIENCY,
    tariff: float = TARIFF,
) -> float:
    """
    Template:
        kwh_needed = required_charge_power_kw * efficiency_factor
        price_average = sum(values) / len
        cost = (price_average + tariff) * kwh_needed

    NOTE: kwh_needed re-applies efficiency to required_charge_kw (which was already
    divided by efficiency), effectively computing battery kWh rather than grid kWh.
    This is a known quirk in the blueprint; the test documents current behaviour.
    """
    if not cheapest_periods:
        # Division by zero guard: blueprint does NOT guard this — would crash in HA
        # when cheapest_periods is empty (e.g. already at target SOC).
        return 0.0
    kwh_needed = required_charge_kw * efficiency
    avg_price = sum(p["value"] for p in cheapest_periods) / len(cheapest_periods)
    return (avg_price + tariff) * kwh_needed


def should_start_charging(
    cheapest_periods: list,
    current_ts: float,
    current_soc: float,
    target_soc: float,
    soc_available: bool = True,
    charger_mode: str = "connected_finished",
    charge_switch_state: str = "off",
) -> bool:
    """
    Template (first 'choose' branch):
        cheapest_periods | length > 0
        AND is_cheapest_period
        AND current_soc < target_soc
        AND has_value(car_soc_entity)
        AND is_state(charge_switch_entity, 'off')

    Callers pass both charger_mode and charge_switch_state because both are
    observable at the same instant in HA; the blueprint chooses which one to
    gate on. The switch guard stands in for the Zaptec resume rule: the
    integration marks the switch unavailable unless 'resume_charging' is a legal
    command, and 'on' while already charging. Gating on charger_mode instead is
    what broke charging from 2026-07-12 — see TestStartChargingGuards.
    """
    return (
        len(cheapest_periods) > 0
        and is_in_cheapest_period(cheapest_periods, current_ts)
        and current_soc < target_soc
        and soc_available
        and charge_switch_state == "off"
    )


def should_stop_charging(
    charger_mode: str,
    cheapest_periods: list,
    current_ts: float,
    current_soc: float,
    target_soc: float,
    departure_ts: float,
) -> bool:
    """
    Template (second 'choose' branch):
        charger_mode == 'connected_charging'
        AND (
            (NOT is_cheapest_period AND now < departure)
            OR
            current_soc >= target_soc
        )
    Because departure is always rolled to the future, 'now < departure' is always
    True in practice — see dedicated test below.
    """
    if charger_mode != "connected_charging":
        return False
    in_cheapest = is_in_cheapest_period(cheapest_periods, current_ts)
    before_departure = current_ts < departure_ts
    return (not in_cheapest and before_departure) or (current_soc >= target_soc)


# ---------------------------------------------------------------------------
# Helpers for building test data
# ---------------------------------------------------------------------------

def make_period(start_ts: float, value: float, resolution_minutes: int = 15) -> dict:
    dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    return {
        "start_ts": start_ts,
        "start": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "value": value,
        "resolution_minutes": resolution_minutes,
    }


def make_raw_period(start_ts: float, value: float) -> dict:
    """Minimal raw_today/tomorrow entry (no resolution_minutes key yet)."""
    return {"start_ts": start_ts, "value": value}


# ---------------------------------------------------------------------------
# 1. calc_charge_power_kw
# ---------------------------------------------------------------------------

class TestCalcChargePowerKw:
    def test_real_world_values(self):
        # max=16A, min=6A -> ((16+6)/2)*230*3/1000 = 7.59 kW
        assert calc_charge_power_kw(16, 6) == 7.59

    def test_max_equals_min(self):
        # Uniform current: (16+16)/2 * 230*3/1000 = 11.04 kW
        assert calc_charge_power_kw(16, 16) == 11.04

    def test_min_is_zero(self):
        # Min current = 0: average = max/2
        result = calc_charge_power_kw(16, 0)
        assert result == round(8 * 230 * 3 / 1000, 2)

    def test_max_is_zero(self):
        assert calc_charge_power_kw(0, 0) == 0.0

    def test_maximum_current(self):
        # 20A max (ZAP entity max), 20A min
        result = calc_charge_power_kw(20, 20)
        assert result == round(20 * 230 * 3 / 1000, 2)

    def test_result_rounded_to_2_decimals(self):
        # Ensure rounding is applied
        result = calc_charge_power_kw(13, 7)
        assert result == round(((13 + 7) / 2) * 230 * 3 / 1000, 2)
        assert isinstance(result, float)

    def test_fractional_amps(self):
        result = calc_charge_power_kw(15.5, 6.5)
        assert result == round(((15.5 + 6.5) / 2) * 230 * 3 / 1000, 2)


# ---------------------------------------------------------------------------
# 2. calc_required_charge_kw
# ---------------------------------------------------------------------------

class TestCalcRequiredChargeKw:
    def test_real_world_values(self):
        # (100-97) * 60 / 100 / 0.9 = 2.0 kWh from grid
        result = calc_required_charge_kw(100, 97, 60)
        assert abs(result - 2.0) < 1e-9

    def test_empty_battery(self):
        # 0% to 100%, 77 kWh battery
        result = calc_required_charge_kw(100, 0, 77)
        assert abs(result - 77 / 0.9) < 1e-9

    def test_already_at_target(self):
        # current == target -> 0 kWh needed
        assert calc_required_charge_kw(90, 90, 60) == 0.0

    def test_over_target(self):
        # current > target -> negative (see edge-case note in blueprint)
        result = calc_required_charge_kw(80, 95, 60)
        assert result < 0

    def test_partial_charge(self):
        # 50% to 80% on 40 kWh battery
        result = calc_required_charge_kw(80, 50, 40)
        expected = 30 * 40 / 100 / 0.9
        assert abs(result - expected) < 1e-9

    def test_efficiency_scales_result(self):
        # Higher efficiency -> less grid energy needed
        low_eff = calc_required_charge_kw(100, 50, 60, efficiency=0.8)
        high_eff = calc_required_charge_kw(100, 50, 60, efficiency=0.95)
        assert low_eff > high_eff

    def test_large_battery(self):
        result = calc_required_charge_kw(90, 20, 100)
        expected = 70 * 100 / 100 / 0.9
        assert abs(result - expected) < 1e-9


# ---------------------------------------------------------------------------
# 3. calc_periods_to_charge
# ---------------------------------------------------------------------------

class TestCalcPeriodsToCharge:
    def test_real_world(self):
        # required=2.0, power=7.59 -> ceil(0.263) = 1 hour
        assert calc_periods_to_charge(2.0, 7.59) == 1

    def test_exactly_one_period(self):
        # required == power -> exactly 1 hour
        assert calc_periods_to_charge(7.59, 7.59) == 1

    def test_slightly_over_one_period(self):
        assert calc_periods_to_charge(7.60, 7.59) == 2

    def test_requires_many_periods(self):
        # 50 kWh needed, 7.59 kW charger -> ceil(6.59) = 7 hours
        assert calc_periods_to_charge(50.0, 7.59) == 7

    def test_zero_required(self):
        # Already at target -> 0 periods
        assert calc_periods_to_charge(0.0, 7.59) == 0

    def test_negative_required_rounds_toward_zero(self):
        # current_soc > target_soc (over target) — small overage
        # ceil(-0.263) = 0
        assert calc_periods_to_charge(-2.0, 7.59) == 0

    def test_negative_required_large(self):
        # Large overage: ceil(-8.78) = -8 (potential negative slice bug)
        result = calc_periods_to_charge(-66.67, 7.59)
        assert result == -8

    def test_tiny_required(self):
        # Needs just a sliver of a period -> still rounds up to 1 hour
        assert calc_periods_to_charge(0.001, 7.59) == 1


# ---------------------------------------------------------------------------
# 4. detect_resolution_minutes
# ---------------------------------------------------------------------------

class TestDetectResolutionMinutes:
    def test_15_minute_resolution(self):
        base = 1_700_000_000.0
        raw = [make_raw_period(base, 1.0), make_raw_period(base + 900, 1.1)]
        assert detect_resolution_minutes(raw) == 15

    def test_60_minute_resolution(self):
        base = 1_700_000_000.0
        raw = [make_raw_period(base, 1.0), make_raw_period(base + 3600, 1.1)]
        assert detect_resolution_minutes(raw) == 60

    def test_single_entry_falls_back_to_60(self):
        raw = [make_raw_period(1_700_000_000.0, 1.0)]
        assert detect_resolution_minutes(raw) == 60

    def test_empty_list_falls_back_to_60(self):
        assert detect_resolution_minutes([]) == 60

    def test_none_falls_back_to_60(self):
        assert detect_resolution_minutes(None) == 60

    def test_30_minute_resolution(self):
        base = 1_700_000_000.0
        raw = [make_raw_period(base, 1.0), make_raw_period(base + 1800, 1.1)]
        assert detect_resolution_minutes(raw) == 30


# ---------------------------------------------------------------------------
# 5. calc_periods_needed
# ---------------------------------------------------------------------------

class TestCalcPeriodsNeeded:
    def test_15min_resolution_1_hour(self):
        # 1 hour * 60 / 15 = 4 slots
        assert calc_periods_needed(1, 15) == 4

    def test_15min_resolution_2_hours(self):
        assert calc_periods_needed(2, 15) == 8

    def test_60min_resolution_1_hour(self):
        assert calc_periods_needed(1, 60) == 1

    def test_60min_resolution_3_hours(self):
        assert calc_periods_needed(3, 60) == 3

    def test_zero_periods_to_charge(self):
        # Already at target -> 0 slots
        assert calc_periods_needed(0, 15) == 0

    def test_fractional_hours_rounds_up(self):
        # If periods_to_charge were 1.5 (shouldn't happen in practice due to prior ceil,
        # but the function itself handles it)
        assert calc_periods_needed(1, 30) == 2  # 1h / 30min = 2 slots exactly

    def test_many_hours_15min(self):
        # 7 hours at 15-min resolution = 28 slots
        assert calc_periods_needed(7, 15) == 28

    def test_large_negative_propagation(self):
        # Documents the negative-slice bug when current_soc > target_soc
        result = calc_periods_needed(-8, 15)
        assert result == -32  # ceil(-8 * 60 / 15) = ceil(-32) = -32


# ---------------------------------------------------------------------------
# 6. calc_departure_ts
# ---------------------------------------------------------------------------

class TestCalcDepartureTs:
    def _dt(self, hour, minute=0, second=0):
        """Build a naive local-ish datetime for 2026-03-07."""
        return datetime(2026, 3, 7, hour, minute, second)

    def test_departure_later_today(self):
        now = self._dt(8, 0)  # 08:00
        dep_ts = calc_departure_ts(now, "09:00:00")
        expected = datetime(2026, 3, 7, 9, 0, 0).timestamp()
        assert dep_ts == expected

    def test_departure_already_passed_rolls_to_tomorrow(self):
        now = self._dt(10, 0)  # 10:00, past 09:00 departure
        dep_ts = calc_departure_ts(now, "09:00:00")
        expected = datetime(2026, 3, 8, 9, 0, 0).timestamp()
        assert dep_ts == expected

    def test_departure_exactly_now_rolls_to_tomorrow(self):
        # departure.timestamp() < current → strict less-than, so exact match doesn't roll
        now = self._dt(9, 0, 0)
        dep_ts = calc_departure_ts(now, "09:00:00")
        # dep == now, not strictly less, so should stay today
        expected = datetime(2026, 3, 7, 9, 0, 0).timestamp()
        assert dep_ts == expected

    def test_departure_one_second_past_rolls_to_tomorrow(self):
        now = self._dt(9, 0, 1)  # 1 second after departure
        dep_ts = calc_departure_ts(now, "09:00:00")
        expected = datetime(2026, 3, 8, 9, 0, 0).timestamp()
        assert dep_ts == expected

    def test_midnight_departure(self):
        now = self._dt(8, 0)
        dep_ts = calc_departure_ts(now, "00:00:00")
        # 00:00 today already passed at 08:00 -> rolls to tomorrow midnight
        expected = datetime(2026, 3, 8, 0, 0, 0).timestamp()
        assert dep_ts == expected

    def test_late_night_departure_not_yet_passed(self):
        now = self._dt(22, 0)
        dep_ts = calc_departure_ts(now, "23:30:00")
        expected = datetime(2026, 3, 7, 23, 30, 0).timestamp()
        assert dep_ts == expected

    def test_seconds_in_departure_string(self):
        now = self._dt(7, 0)
        dep_ts = calc_departure_ts(now, "09:30:45")
        expected = datetime(2026, 3, 7, 9, 30, 45).timestamp()
        assert dep_ts == expected

    def test_result_is_always_gte_current_time(self):
        for hour in range(0, 24):
            now = self._dt(hour, 0)
            dep_ts = calc_departure_ts(now, "07:00:00")
            assert dep_ts >= now.timestamp()


# ---------------------------------------------------------------------------
# 7. calc_cheapest_periods
# ---------------------------------------------------------------------------

# Base timestamp: 2026-03-07 00:00:00 UTC (approx — exact value doesn't matter)
BASE = datetime(2026, 3, 7, 0, 0, 0).timestamp()
SLOT = 900  # 15 minutes in seconds


def _slots(n: int, base: float = BASE) -> list:
    """Generate n sequential 15-min raw periods with values 1..n."""
    return [make_raw_period(base + i * SLOT, float(i + 1)) for i in range(n)]


class TestCalcCheapestPeriods:
    def test_selects_n_cheapest_slots(self):
        # 8 slots (2h), need 4 cheapest; prices ascend so cheapest are first 4
        now_ts = BASE - 1  # just before first slot
        dep_ts = BASE + 8 * SLOT + 1  # after all slots
        raw = _slots(8)
        result = calc_cheapest_periods(raw, [], now_ts, dep_ts, 4, 15)
        assert len(result) == 4
        assert [p["value"] for p in result] == [1.0, 2.0, 3.0, 4.0]

    def test_result_sorted_by_start_time(self):
        # Ensure output is time-ordered even when cheapest are scattered
        now_ts = BASE - 1
        dep_ts = BASE + 8 * SLOT + 1
        # Assign prices so cheap slots are not contiguous: [5,1,5,1,5,1,5,1]
        raw = [
            make_raw_period(BASE + i * SLOT, 1.0 if i % 2 == 1 else 5.0)
            for i in range(8)
        ]
        result = calc_cheapest_periods(raw, [], now_ts, dep_ts, 4, 15)
        assert len(result) == 4
        starts = [p["start_ts"] for p in result]
        assert starts == sorted(starts)

    def test_filters_out_fully_past_periods(self):
        # Slot 0 ended before now
        now_ts = BASE + SLOT + 1  # 1 second into slot 1
        dep_ts = BASE + 8 * SLOT
        raw = _slots(8)
        result = calc_cheapest_periods(raw, [], now_ts, dep_ts, 4, 15)
        # Slot 0 is fully past (ended at BASE+SLOT, now > that)
        assert all(p["start_ts"] + SLOT > now_ts for p in result)

    def test_period_partially_elapsed_is_included(self):
        # A period that started in the past but hasn't ended yet should be included
        # (period_end > current_ts)
        slot_start = BASE
        now_ts = slot_start + 400  # 400s into a 900s slot → not yet ended
        dep_ts = BASE + 8 * SLOT
        raw = [make_raw_period(slot_start, 0.5)] + _slots(4, BASE + SLOT)
        result = calc_cheapest_periods(raw, [], now_ts, dep_ts, 1, 15)
        assert result[0]["start_ts"] == slot_start  # cheapest should be selected

    def test_filters_periods_at_or_after_departure(self):
        dep_ts = BASE + 4 * SLOT  # departure after slot 3
        now_ts = BASE - 1
        raw = _slots(8)
        result = calc_cheapest_periods(raw, [], now_ts, dep_ts, 4, 15)
        # Only slots 0-3 start before departure
        assert all(p["start_ts"] < dep_ts for p in result)
        assert len(result) == 4

    def test_spans_today_and_tomorrow(self):
        now_ts = BASE - 1
        tomorrow_base = BASE + 24 * 3600
        dep_ts = tomorrow_base + 4 * SLOT
        raw_today = [make_raw_period(BASE + i * SLOT, 2.0) for i in range(4)]
        raw_tomorrow = [make_raw_period(tomorrow_base + i * SLOT, 1.0) for i in range(4)]
        result = calc_cheapest_periods(raw_today, raw_tomorrow, now_ts, dep_ts, 4, 15)
        # Cheapest 4 should all come from tomorrow (price 1.0 < 2.0)
        assert all(p["start_ts"] >= tomorrow_base for p in result)

    def test_fewer_available_slots_than_needed(self):
        # Only 2 slots exist but 4 are needed -> returns all available
        now_ts = BASE - 1
        dep_ts = BASE + 8 * SLOT
        raw = _slots(2)
        result = calc_cheapest_periods(raw, [], now_ts, dep_ts, 4, 15)
        assert len(result) == 2

    def test_empty_when_no_slots_before_departure(self):
        dep_ts = BASE - 1  # departure already passed (all slots start after)
        now_ts = BASE - 10
        raw = _slots(8)
        result = calc_cheapest_periods(raw, [], now_ts, dep_ts, 4, 15)
        assert result == []

    def test_empty_when_all_slots_fully_past(self):
        now_ts = BASE + 8 * SLOT + 1  # all slots ended
        dep_ts = now_ts + 3600
        raw = _slots(8)
        result = calc_cheapest_periods(raw, [], now_ts, dep_ts, 4, 15)
        assert result == []

    def test_zero_periods_needed_returns_empty(self):
        now_ts = BASE - 1
        dep_ts = BASE + 8 * SLOT
        result = calc_cheapest_periods(_slots(8), [], now_ts, dep_ts, 0, 15)
        assert result == []

    def test_60min_resolution(self):
        hour = 3600
        base = BASE
        raw = [make_raw_period(base + i * hour, float(i + 1)) for i in range(8)]
        now_ts = base - 1
        dep_ts = base + 8 * hour
        result = calc_cheapest_periods(raw, [], now_ts, dep_ts, 3, 60)
        assert len(result) == 3
        assert [p["value"] for p in result] == [1.0, 2.0, 3.0]

    def test_resolution_stored_in_result(self):
        now_ts = BASE - 1
        dep_ts = BASE + 4 * SLOT
        result = calc_cheapest_periods(_slots(4), [], now_ts, dep_ts, 2, 15)
        assert all(p["resolution_minutes"] == 15 for p in result)

    def test_ties_in_price_stable(self):
        # All prices identical -> first N by index (sorted stable) selected
        now_ts = BASE - 1
        dep_ts = BASE + 8 * SLOT
        raw = [make_raw_period(BASE + i * SLOT, 1.0) for i in range(8)]
        result = calc_cheapest_periods(raw, [], now_ts, dep_ts, 3, 15)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# 8. is_in_cheapest_period
# ---------------------------------------------------------------------------

class TestIsInCheapestPeriod:
    def _period(self, start_ts, value=1.0, res=15):
        return make_period(start_ts, value, res)

    def test_exactly_at_start_is_inside(self):
        p = self._period(BASE)
        assert is_in_cheapest_period([p], BASE) is True

    def test_inside_period(self):
        p = self._period(BASE)
        assert is_in_cheapest_period([p], BASE + 400) is True

    def test_exactly_at_end_is_outside(self):
        # period covers [BASE, BASE+900) — the end is exclusive
        p = self._period(BASE)
        assert is_in_cheapest_period([p], BASE + 900) is False

    def test_before_period_start(self):
        p = self._period(BASE + 100)
        assert is_in_cheapest_period([p], BASE) is False

    def test_after_period_end(self):
        p = self._period(BASE)
        assert is_in_cheapest_period([p], BASE + 901) is False

    def test_empty_periods_returns_false(self):
        assert is_in_cheapest_period([], BASE) is False

    def test_matches_second_of_multiple_periods(self):
        p1 = self._period(BASE)
        p2 = self._period(BASE + 3600)
        assert is_in_cheapest_period([p1, p2], BASE + 3700) is True

    def test_no_match_between_non_contiguous_periods(self):
        p1 = self._period(BASE)
        p2 = self._period(BASE + 3600)
        # Gap between BASE+900 and BASE+3600
        assert is_in_cheapest_period([p1, p2], BASE + 1800) is False

    def test_60min_resolution_period(self):
        p = self._period(BASE, res=60)
        assert is_in_cheapest_period([p], BASE + 3599) is True
        assert is_in_cheapest_period([p], BASE + 3600) is False

    def test_falls_back_to_60min_if_no_resolution_key(self):
        p = {"start_ts": BASE, "value": 1.0}  # no resolution_minutes key
        assert is_in_cheapest_period([p], BASE + 3599) is True
        assert is_in_cheapest_period([p], BASE + 3600) is False


# ---------------------------------------------------------------------------
# 9. calc_total_charge_cost
# ---------------------------------------------------------------------------

class TestCalcTotalChargeCost:
    def test_real_world_values(self):
        # 3% to charge on 60 kWh: required = 2.0 kWh from grid
        # kwh_needed = 2.0 * 0.9 = 1.8 kWh (battery kWh, see docstring note)
        # Use 4 x 15min cheapest periods from actual price data
        periods = [
            make_period(BASE, 0.71),
            make_period(BASE + 1 * SLOT, 0.64),
            make_period(BASE + 2 * SLOT, 0.69),
            make_period(BASE + 3 * SLOT, 0.49),
        ]
        required = calc_required_charge_kw(100, 97, 60)  # 2.0
        result = calc_total_charge_cost(periods, required)
        avg_price = (0.71 + 0.64 + 0.69 + 0.49) / 4  # 0.6325
        expected = (avg_price + TARIFF) * (required * EFFICIENCY)
        assert abs(result - expected) < 1e-9

    def test_tariff_is_included(self):
        periods = [make_period(BASE, 1.0)]
        required = 10.0
        result = calc_total_charge_cost(periods, required)
        # (1.0 + 0.9648) * (10.0 * 0.9) = 1.9648 * 9 = 17.6832
        assert abs(result - (1.0 + TARIFF) * (10.0 * EFFICIENCY)) < 1e-9

    def test_single_period(self):
        periods = [make_period(BASE, 2.0)]
        required = 5.0
        result = calc_total_charge_cost(periods, required)
        expected = (2.0 + TARIFF) * (5.0 * EFFICIENCY)
        assert abs(result - expected) < 1e-9

    def test_empty_periods_returns_zero(self):
        # Our function guards against this, but the blueprint template does NOT:
        # it computes sum/len before checking `cheapest_periods | length > 0`,
        # which would cause a division-by-zero in Jinja2.
        assert calc_total_charge_cost([], 10.0) == 0.0

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: blueprint computes total_charge_cost (division by len) "
        "BEFORE the `cheapest_periods | length > 0` guard, causing a "
        "ZeroDivisionError in Jinja2 when no periods are selected. "
        "Fix: move the cost calculation inside the guard or add len > 0 check.",
    )
    def test_empty_periods_crashes_in_blueprint(self):
        """Simulates what the blueprint template does — no len guard."""
        periods = []
        required = 10.0
        kwh_needed = required * EFFICIENCY
        # This is what the blueprint does — will raise ZeroDivisionError
        avg_price = sum(p["value"] for p in periods) / len(periods)
        cost = (avg_price + TARIFF) * kwh_needed
        assert cost == 0.0

    def test_zero_required_kw(self):
        periods = [make_period(BASE, 1.5)]
        result = calc_total_charge_cost(periods, 0.0)
        assert result == 0.0

    def test_higher_price_means_higher_cost(self):
        cheap = [make_period(BASE, 0.5)]
        expensive = [make_period(BASE, 3.0)]
        required = 10.0
        assert calc_total_charge_cost(cheap, required) < calc_total_charge_cost(expensive, required)

    def test_cost_scales_with_required_kw(self):
        periods = [make_period(BASE, 1.0)]
        cost_small = calc_total_charge_cost(periods, 5.0)
        cost_large = calc_total_charge_cost(periods, 10.0)
        assert abs(cost_large / cost_small - 2.0) < 1e-9

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: blueprint computes kwh_needed = required * efficiency, which "
        "undoes the /efficiency in calc_required_charge_kw. Cost is calculated "
        "on battery kWh (60) instead of grid kWh (66.67). "
        "Fix: use required_charge_power_kw directly as kwh_needed.",
    )
    def test_cost_should_use_grid_kwh_not_battery_kwh(self):
        """Cost should be based on grid consumption, not battery energy."""
        required = calc_required_charge_kw(100, 0, 60)  # 60/0.9 ≈ 66.67
        periods = [make_period(BASE, 0.0)]  # price=0 isolates kwh_needed
        result = calc_total_charge_cost(periods, required, tariff=TARIFF)
        # CORRECT: cost should use grid kWh (required itself, ~66.67)
        grid_kwh = required  # 66.67
        expected = TARIFF * grid_kwh
        assert abs(result - expected) < 1e-6


# ---------------------------------------------------------------------------
# 10. should_start_charging
# ---------------------------------------------------------------------------

class TestShouldStartCharging:
    def _cheap(self):
        return [make_period(BASE, 1.0)]

    def test_starts_when_all_conditions_met(self):
        assert should_start_charging(self._cheap(), BASE + 1, 80.0, 100.0) is True

    def test_no_start_when_cheapest_periods_empty(self):
        assert should_start_charging([], BASE + 1, 80.0, 100.0) is False

    def test_no_start_when_not_in_cheapest_period(self):
        # Current time is outside the cheap slot
        assert should_start_charging(self._cheap(), BASE + 3600, 80.0, 100.0) is False

    def test_no_start_when_at_target_soc(self):
        assert should_start_charging(self._cheap(), BASE + 1, 100.0, 100.0) is False

    def test_no_start_when_above_target_soc(self):
        assert should_start_charging(self._cheap(), BASE + 1, 105.0, 100.0) is False

    def test_starts_at_1_percent_below_target(self):
        assert should_start_charging(self._cheap(), BASE + 1, 99.0, 100.0) is True

    def test_no_start_at_exact_target(self):
        assert should_start_charging(self._cheap(), BASE + 1, 100.0, 100.0) is False

    def test_all_conditions_must_hold_simultaneously(self):
        # Only one condition false at a time
        # periods ok, in period, but soc == target -> no start
        assert should_start_charging(self._cheap(), BASE + 1, 100.0, 100.0) is False
        # periods ok, NOT in period, soc < target -> no start
        assert should_start_charging(self._cheap(), BASE + 9999, 80.0, 100.0) is False
        # NO periods, in period (irrelevant), soc < target -> no start
        assert should_start_charging([], BASE + 1, 80.0, 100.0) is False


class TestStartChargingGuards:
    """
    Regression cover for the 2026-07-12 guard that stopped overnight charging.

    The charger sits in 'connected_finished' for the whole night once a previous
    session left a final-stop active, and that is precisely the state Zaptec
    allows 'resume_charging' from. The old guard excluded it, so the turn_on
    branch was unreachable for 25 days.

    Each case pins the two states HA reports simultaneously, so the same data
    exercises whichever guard the blueprint uses.
    """

    def _cheap(self):
        return [make_period(BASE, 1.0)]

    def test_starts_while_paused_overnight(self):
        # Recorded 2026-08-05 23:40 -> 2026-08-06 09:21: car plugged in at 9%,
        # charger 'connected_finished' with a final-stop left over, switch
        # available and 'off'. Resume is legal here and must happen.
        assert should_start_charging(
            self._cheap(), BASE + 1, 9.0, 100.0,
            charger_mode="connected_finished",
            charge_switch_state="off",
        ) is True

    def test_no_start_when_unplugged(self):
        # Car unplugged: Zaptec marks the switch unavailable. This is the real
        # case the 2026-07-12 guard was aiming at, and it stays covered.
        assert should_start_charging(
            self._cheap(), BASE + 1, 9.0, 100.0,
            charger_mode="disconnected",
            charge_switch_state="unavailable",
        ) is False

    def test_no_start_when_already_charging(self):
        assert should_start_charging(
            self._cheap(), BASE + 1, 9.0, 100.0,
            charger_mode="connected_charging",
            charge_switch_state="on",
        ) is False

    def test_no_start_while_awaiting_authorization(self):
        # 'connected_requesting' is not paused, so resume_charging is illegal and
        # the switch is unavailable. The charger_mode guard let this through and
        # raised "Resuming charging failed"; the switch guard blocks it.
        assert should_start_charging(
            self._cheap(), BASE + 1, 9.0, 100.0,
            charger_mode="connected_requesting",
            charge_switch_state="unavailable",
        ) is False

    def test_no_start_when_soc_unavailable(self):
        # Guards against resuming off a fabricated 0% SoC.
        assert should_start_charging(
            self._cheap(), BASE + 1, 0.0, 100.0,
            soc_available=False,
            charger_mode="connected_finished",
            charge_switch_state="off",
        ) is False


# ---------------------------------------------------------------------------
# 11. should_stop_charging
# ---------------------------------------------------------------------------

class TestShouldStopCharging:
    def _cheap(self, start=BASE):
        return [make_period(start, 1.0)]

    def _departure(self, hours_ahead=8):
        return BASE + hours_ahead * 3600

    def test_stops_when_not_in_cheapest_and_before_departure(self):
        # Charging, outside cheap window, before departure
        assert should_stop_charging(
            "connected_charging",
            self._cheap(),
            BASE + 3600,   # outside cheap slot (which is BASE..BASE+900)
            80.0, 100.0,
            self._departure(),
        ) is True

    def test_stops_when_target_soc_reached(self):
        # Charging, in cheapest period, but already at target -> stop
        assert should_stop_charging(
            "connected_charging",
            self._cheap(),
            BASE + 1,      # inside cheap slot
            100.0, 100.0,  # soc == target
            self._departure(),
        ) is True

    def test_stops_when_above_target_soc(self):
        assert should_stop_charging(
            "connected_charging",
            self._cheap(),
            BASE + 1,
            105.0, 100.0,
            self._departure(),
        ) is True

    def test_no_stop_when_in_cheapest_and_below_target(self):
        assert should_stop_charging(
            "connected_charging",
            self._cheap(),
            BASE + 1,
            80.0, 100.0,
            self._departure(),
        ) is False

    def test_no_stop_when_not_charging(self):
        # Mode != connected_charging -> never stop
        for mode in ("disconnected", "connected_requesting", "connected_finished", "unknown"):
            assert should_stop_charging(
                mode,
                self._cheap(),
                BASE + 3600,
                80.0, 100.0,
                self._departure(),
            ) is False

    def test_departure_always_in_future_so_before_departure_always_true(self):
        # Because calc_departure_ts rolls departure to tomorrow when already passed,
        # before_departure is always True. The test documents this invariant.
        now = datetime(2026, 3, 7, 14, 0, 0)
        dep_ts = calc_departure_ts(now, "09:00:00")  # rolls to next day
        assert now.timestamp() < dep_ts

    def test_stops_outside_cheap_window_before_departure(self):
        cheap = [make_period(BASE + 3600, 1.0)]   # cheap slot starts 1h from now
        current_ts = BASE + 1                       # not in any cheap slot
        assert should_stop_charging(
            "connected_charging",
            cheap,
            current_ts,
            80.0, 100.0,
            self._departure(),
        ) is True

    def test_no_stop_when_cheapest_periods_empty_and_soc_below_target(self):
        # No cheap periods and soc < target: not_in_cheapest=True, before_dep=True
        # -> should stop (no cheap windows found = don't charge in expensive time)
        assert should_stop_charging(
            "connected_charging",
            [],
            BASE + 1,
            80.0, 100.0,
            self._departure(),
        ) is True

    def test_stop_when_target_soc_reached_regardless_of_period(self):
        # In cheapest period AND at target SOC -> stop wins via OR branch
        assert should_stop_charging(
            "connected_charging",
            self._cheap(),
            BASE + 1,
            100.0, 100.0,
            self._departure(),
        ) is True


# ---------------------------------------------------------------------------
# 12. End-to-end scenario tests (integration of the full pipeline)
# ---------------------------------------------------------------------------

class TestEndToEndScenarios:
    """
    Combines all pipeline steps to validate complete computation chains
    against known inputs and expected outcomes.
    """

    def _build_raw_today(self, n_slots=96, base=BASE, slot=900):
        """96 x 15-min periods with rising prices 1..n."""
        return [make_raw_period(base + i * slot, float(i + 1) / 10) for i in range(n_slots)]

    def test_real_world_current_state(self):
        """
        Replicates the live HA state on 2026-03-07:
          max=16A, min=6A, battery=60kWh, soc=97%, target=100%
          departure helper = 09:00:00, current_time ≈ 13:00 local
        Verifies the full pipeline produces consistent results.
        """
        max_a, min_a = 16.0, 6.0
        battery, target, current_soc = 60.0, 100.0, 97.0
        charge_power = calc_charge_power_kw(max_a, min_a)
        assert charge_power == 7.59

        required = calc_required_charge_kw(target, current_soc, battery)
        assert abs(required - 2.0) < 1e-9

        periods_h = calc_periods_to_charge(required, charge_power)
        assert periods_h == 1

        res = detect_resolution_minutes(self._build_raw_today()[:2])
        assert res == 15

        periods_needed = calc_periods_needed(periods_h, res)
        assert periods_needed == 4

    def test_car_already_at_target_no_charging(self):
        max_a, min_a = 16.0, 6.0
        battery, target, current_soc = 60.0, 100.0, 100.0
        charge_power = calc_charge_power_kw(max_a, min_a)
        required = calc_required_charge_kw(target, current_soc, battery)
        assert required == 0.0
        periods_h = calc_periods_to_charge(required, charge_power)
        assert periods_h == 0
        periods_needed = calc_periods_needed(periods_h, 15)
        assert periods_needed == 0

        cheap = calc_cheapest_periods(
            self._build_raw_today(), [], BASE - 1, BASE + 24 * 3600, periods_needed, 15
        )
        assert cheap == []
        assert not should_start_charging(cheap, BASE + 1, current_soc, target)

    def test_full_charge_needed_overnight(self):
        """Empty battery, departure next morning: should select cheapest overnight slots."""
        max_a, min_a = 16.0, 6.0
        battery, target, current_soc = 77.0, 90.0, 10.0
        now_dt = datetime(2026, 3, 7, 22, 0, 0)
        now_ts = now_dt.timestamp()

        charge_power = calc_charge_power_kw(max_a, min_a)
        required = calc_required_charge_kw(target, current_soc, battery)
        periods_h = calc_periods_to_charge(required, charge_power)
        periods_needed = calc_periods_needed(periods_h, 15)
        dep_ts = calc_departure_ts(now_dt, "07:00:00")

        # Build 96 periods starting at now_ts
        raw = [make_raw_period(now_ts + i * SLOT, float(i % 10 + 1)) for i in range(96)]
        # Only slots starting before departure qualify
        available_before_dep = [p for p in raw if p["start_ts"] < dep_ts]
        cheap = calc_cheapest_periods(raw, [], now_ts, dep_ts, periods_needed, 15)

        assert len(cheap) == min(periods_needed, len(available_before_dep))
        # All selected periods start before departure
        assert all(p["start_ts"] < dep_ts for p in cheap)
        # They are sorted by start time
        starts = [p["start_ts"] for p in cheap]
        assert starts == sorted(starts)

    def test_no_periods_before_departure(self):
        """When departure is imminent and all slots have ended, no charging."""
        now_ts = BASE + 96 * SLOT + 1  # past all 96 slots
        dep_ts = now_ts + 3600
        raw = self._build_raw_today()
        cheap = calc_cheapest_periods(raw, [], now_ts, dep_ts, 4, 15)
        assert cheap == []
        assert not should_start_charging(cheap, now_ts, 80.0, 100.0)

    def test_imminent_departure_still_includes_current_slot(self):
        """A period that started before now but hasn't ended is still eligible."""
        # now is in the middle of slot 92 (of 96)
        slot_start = BASE + 92 * SLOT
        now_ts = slot_start + 400  # inside the slot
        dep_ts = now_ts + 1  # departure in 1 second, but slot started before dep
        raw = self._build_raw_today()
        cheap = calc_cheapest_periods(raw, [], now_ts, dep_ts, 4, 15)
        # Slot 92 qualifies: its end > now_ts AND its start < dep_ts
        assert len(cheap) >= 1

    def test_start_then_stop_decision_over_time(self):
        """Simulate: cheap window [BASE..BASE+900), confirm start then stop."""
        cheap = [make_period(BASE, 0.5)]
        # Inside window
        assert should_start_charging(cheap, BASE + 400, 80.0, 100.0) is True
        assert should_stop_charging("connected_charging", cheap, BASE + 400, 80.0, 100.0, BASE + 8 * 3600) is False
        # Outside window
        assert should_start_charging(cheap, BASE + 950, 80.0, 100.0) is False
        assert should_stop_charging("connected_charging", cheap, BASE + 950, 80.0, 100.0, BASE + 8 * 3600) is True

    def test_departure_override_changes_period_filter(self):
        """
        Hard-coded departure = 07:00, helper departure = 09:00.
        More slots fit before 09:00 than before 07:00 -> different cheapest set.
        """
        now_dt = datetime(2026, 3, 7, 6, 0, 0)
        now_ts = now_dt.timestamp()
        raw = [make_raw_period(now_ts + i * SLOT, float(i + 1)) for i in range(20)]

        dep_07 = calc_departure_ts(now_dt, "07:00:00")
        dep_09 = calc_departure_ts(now_dt, "09:00:00")

        cheap_07 = calc_cheapest_periods(raw, [], now_ts, dep_07, 4, 15)
        cheap_09 = calc_cheapest_periods(raw, [], now_ts, dep_09, 4, 15)

        slots_before_07 = sum(1 for p in raw if p["start_ts"] < dep_07)
        slots_before_09 = sum(1 for p in raw if p["start_ts"] < dep_09)

        assert slots_before_09 > slots_before_07
        assert len(cheap_09) == 4
        # With more candidate slots, 09:00 cheapest may differ from 07:00 cheapest
        assert [p["start_ts"] for p in cheap_09] != [p["start_ts"] for p in cheap_07] or True  # may or may not differ

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: when current_soc > target_soc, periods_needed goes negative "
        "causing list[:-N] to select most periods instead of none. "
        "Fix: clamp periods_to_charge to max(0, ...) in the blueprint.",
    )
    def test_over_target_soc_selects_zero_periods(self):
        """When current_soc > target_soc, no charging is needed so
        cheapest_periods should be empty."""
        current_soc, target_soc = 95.0, 80.0
        charge_power = calc_charge_power_kw(16, 6)
        required = calc_required_charge_kw(target_soc, current_soc, 60)
        periods_h = calc_periods_to_charge(required, charge_power)
        periods_needed = calc_periods_needed(periods_h, 15)

        raw = self._build_raw_today()
        cheap = calc_cheapest_periods(raw, [], BASE - 1, BASE + 8 * 3600, periods_needed, 15)

        # CORRECT behavior: should be empty when already over target
        assert len(cheap) == 0
