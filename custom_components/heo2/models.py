"""Core data types for HEO II. No Home Assistant imports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time, datetime
from typing import Optional
from zoneinfo import ZoneInfo


@dataclass
class RateSlot:
    """A time period with a fixed electricity rate."""
    start: datetime
    end: datetime
    rate_pence: float


@dataclass
class PlannedDispatch:
    """A planned IGO smart-charge dispatch announced by Octopus.

    Used by HEO-8 to pre-position the battery (gc=True + cap=100) on
    slots covering the dispatch window so HEO II is at full when
    Octopus takes control of the EV. Mirrors the BottlecapDave entity's
    `planned_dispatches` attribute shape.
    """

    start: datetime
    end: datetime
    charge_kwh: Optional[float] = None
    source: Optional[str] = None


@dataclass
class SlotConfig:
    """One of 6 inverter timer slots."""
    start_time: time
    end_time: time
    capacity_soc: int   # 0-100
    grid_charge: bool

    def duration_minutes(self) -> int:
        """Duration in minutes, handling midnight crossover."""
        start_mins = self.start_time.hour * 60 + self.start_time.minute
        end_mins = self.end_time.hour * 60 + self.end_time.minute
        if end_mins <= start_mins:
            end_mins += 1440  # add 24 hours
        return end_mins - start_mins

    def contains_time(self, t: time) -> bool:
        """Check if time t falls within this slot (inclusive start, exclusive end)."""
        if self.start_time <= self.end_time:
            # Normal slot: e.g. 05:30-18:30
            return self.start_time <= t < self.end_time
        else:
            # Crosses midnight: e.g. 23:00-05:00
            return t >= self.start_time or t < self.end_time


# Threshold below which a slot is considered a "filler" that can be consumed
# by insert_boundary(). 30 minutes -- short enough to be expendable.
_FILLER_THRESHOLD_MINUTES = 30


@dataclass
class ProgrammeState:
    """The 6-slot programme produced by the rule engine."""
    slots: list[SlotConfig]
    reason_log: list[str] = field(default_factory=list)

    @classmethod
    def default(cls, min_soc: int = 20) -> ProgrammeState:
        """Create a default programme: 5 main slots + 1 short filler, at min_soc, no grid charge.

        The filler slot (23:59-00:00) gives insert_boundary() room to add
        a new time boundary without exceeding the 6-slot limit.
        """
        boundaries = [
            (0, 0), (4, 0), (8, 0), (12, 0), (16, 0), (23, 59), (0, 0),
        ]
        slots = [
            SlotConfig(
                start_time=time(boundaries[i][0], boundaries[i][1]),
                end_time=time(boundaries[i + 1][0], boundaries[i + 1][1]),
                capacity_soc=min_soc,
                grid_charge=False,
            )
            for i in range(6)
        ]
        return cls(slots=slots)

    def find_slot_at(self, t: time) -> int:
        """Return the index of the slot that contains time t."""
        for i, slot in enumerate(self.slots):
            if slot.contains_time(t):
                return i
        raise ValueError(f"No slot contains {t}")

    def insert_boundary(self, at_time: time, reason: str = "") -> bool:
        """Split the slot containing at_time, consuming the shortest filler slot.

        The algorithm:
        1. Split the target slot at at_time, creating 7 slots.
        2. Find the shortest slot (the filler) -- must be < 30 min.
        3. Merge the filler into its neighbour, back to 6 slots.

        Returns False if no slot under the filler threshold exists after splitting.
        """
        # Find the slot containing the target time
        target_idx = self.find_slot_at(at_time)
        target = self.slots[target_idx]

        # Don't split if boundary already exists
        if target.start_time == at_time:
            return True

        # Create two halves from the target slot
        first_half = SlotConfig(
            start_time=target.start_time,
            end_time=at_time,
            capacity_soc=target.capacity_soc,
            grid_charge=target.grid_charge,
        )
        second_half = SlotConfig(
            start_time=at_time,
            end_time=target.end_time,
            capacity_soc=target.capacity_soc,
            grid_charge=target.grid_charge,
        )

        # Build 7-slot list
        new_slots = list(self.slots)
        new_slots[target_idx:target_idx + 1] = [first_half, second_half]

        # Find the shortest slot -- candidate for merging away
        filler_idx = min(range(len(new_slots)), key=lambda i: new_slots[i].duration_minutes())
        if new_slots[filler_idx].duration_minutes() >= _FILLER_THRESHOLD_MINUTES:
            return False  # no expendable filler

        # Merge the filler into its neighbour (prefer the one before it)
        filler = new_slots[filler_idx]
        if filler_idx > 0:
            # Extend the previous slot to cover the filler
            new_slots[filler_idx - 1].end_time = filler.end_time
        elif filler_idx < len(new_slots) - 1:
            # Extend the next slot to cover the filler
            new_slots[filler_idx + 1].start_time = filler.start_time
        del new_slots[filler_idx]

        self.slots = new_slots

        if reason:
            self.reason_log.append(reason)
        return True

    def validate(self) -> list[str]:
        """Validate programme constraints. Returns list of error strings (empty = valid)."""
        errors = []
        if len(self.slots) != 6:
            errors.append(f"Must have exactly 6 slots, got {len(self.slots)}")
            return errors  # can't validate further

        if self.slots[0].start_time != time(0, 0):
            errors.append(f"Slot 1 must start at 00:00, starts at {self.slots[0].start_time}")

        # Check contiguous
        for i in range(len(self.slots) - 1):
            if self.slots[i].end_time != self.slots[i + 1].start_time:
                errors.append(
                    f"Gap between slot {i + 1} (ends {self.slots[i].end_time}) "
                    f"and slot {i + 2} (starts {self.slots[i + 1].start_time})"
                )

        # Last slot must end at 00:00
        if self.slots[-1].end_time != time(0, 0):
            errors.append(f"Last slot must end at 00:00, ends at {self.slots[-1].end_time}")

        # SOC range
        for i, slot in enumerate(self.slots):
            if not (0 <= slot.capacity_soc <= 100):
                errors.append(f"Slot {i + 1} SOC {slot.capacity_soc} outside 0-100 range")

        return errors


@dataclass
class ProgrammeInputs:
    """All inputs gathered by the coordinator for a programme calculation.

    `import_rates` and `export_rates` are the merged "best available"
    schedules: BottlecapDave's published Octopus rates first, with
    AgilePredict (export) and IGO fixed-rate slots (import) filling any
    gaps beyond BD's horizon. Rules and dashboard sensors read these.

    `live_import_rates` and `live_export_rates` are the BottlecapDave-only
    subset, used to enforce SPEC hard rule H4 (Live-prices-only writes).
    Empty when BD is unavailable; the coordinator blocks inverter writes
    in that state. Predictions never reach the inverter.
    """
    now: datetime
    current_soc: float
    battery_capacity_kwh: float
    min_soc: float
    import_rates: list[RateSlot]
    export_rates: list[RateSlot]
    solar_forecast_kwh: list[float]    # 24 hourly buckets, index 0 = 00:00
    load_forecast_kwh: list[float]     # 24 hourly buckets, index 0 = 00:00
    igo_dispatching: bool
    saving_session: bool
    saving_session_start: Optional[time]
    saving_session_end: Optional[time]
    ev_charging: bool
    grid_connected: bool
    active_appliances: list[str]
    appliance_expected_kwh: float
    live_import_rates: list[RateSlot] = field(default_factory=list)
    live_export_rates: list[RateSlot] = field(default_factory=list)
    # 24 hourly buckets for tomorrow's solar forecast. Defaults empty
    # so callers from before HEO-30 step 3 (rank-based pricing) keep
    # working; the rank logic treats an empty list as "no forecast"
    # which biases towards the conservative top-15% sell window.
    solar_forecast_kwh_tomorrow: list[float] = field(default_factory=list)
    # Local timezone for projecting `now` and tz-aware rate slots onto
    # programme-slot time-of-day. Optional for tests that share a single
    # tz between rates and slots; coordinator passes the real local tz
    # in production. Rules that compare `inputs.now.time()` against
    # programme slot times MUST go through `now_local()` so a UTC `now`
    # doesn't alias against local-time slots in DST. See HEO-31 PR2 fix.
    local_tz: Optional[ZoneInfo] = None
    # HEO-8: planned IGO dispatches in the next 24 hours, sourced from
    # `binary_sensor.octopus_energy_..._intelligent_dispatching.attributes.planned_dispatches`.
    # Empty list means no upcoming dispatches; the rule treats it as
    # such and stays a no-op. Defaults preserve backward compatibility
    # with tests built before HEO-8.
    planned_dispatches: list["PlannedDispatch"] = field(default_factory=list)

    def now_local(self) -> datetime:
        """Return `now` projected into the local timezone if known.

        Falls back to whatever tz `now` already carries (UTC in
        production) so existing tests that don't set local_tz continue
        to work.
        """
        if self.local_tz is not None and self.now.tzinfo is not None:
            return self.now.astimezone(self.local_tz)
        return self.now

    def rate_at(self, dt: datetime) -> float | None:
        """Find the import rate at a specific datetime. Returns None if no rate covers it."""
        for rs in self.import_rates:
            if rs.start <= dt < rs.end:
                return rs.rate_pence
        return None

    def export_rate_at(self, dt: datetime) -> float | None:
        """Find the export rate at a specific datetime."""
        for rs in self.export_rates:
            if rs.start <= dt < rs.end:
                return rs.rate_pence
        return None

    def solar_kwh_between(self, start_hour: int, end_hour: int) -> float:
        """Sum solar forecast kWh between two hour indices."""
        if end_hour <= start_hour:
            return 0.0
        return sum(self.solar_forecast_kwh[start_hour:end_hour])

    def load_kwh_between(self, start_hour: int, end_hour: int) -> float:
        """Sum load forecast kWh between two hour indices."""
        if end_hour <= start_hour:
            return 0.0
        return sum(self.load_forecast_kwh[start_hour:end_hour])


@dataclass
class SlotWrite:
    """A pending MQTT write for one slot register."""
    slot_number: int           # 1-6
    capacity_soc: int | None = None   # write only if changed
    time_point: str | None = None     # write only if changed ("HH:MM")
    grid_charge: bool | None = None   # write only if changed
