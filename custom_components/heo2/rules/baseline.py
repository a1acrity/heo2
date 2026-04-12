"""BaselineRule -- lays down the default 6-slot programme structure."""

from __future__ import annotations

from datetime import time

from ..models import ProgrammeState, ProgrammeInputs, SlotConfig
from ..rule_engine import Rule


class BaselineRule(Rule):
    """Create the baseline programme: overnight charge -> day hold -> evening drain.

    Produces 4 meaningful slots + 2 short fillers for other rules to consume.
    """

    name = "baseline"
    description = "Default programme: cheap-rate overnight charge, solar day, evening drain"

    def __init__(
        self,
        off_peak_start: time = time(23, 30),
        off_peak_end: time = time(5, 30),
        evening_start: time = time(18, 30),
    ):
        self.off_peak_start = off_peak_start
        self.off_peak_end = off_peak_end
        self.evening_start = evening_start

    def apply(self, state: ProgrammeState, inputs: ProgrammeInputs) -> ProgrammeState:
        min_soc = int(inputs.min_soc)

        # Build the 6-slot baseline layout:
        # Slot 1: 00:00 -> off_peak_end       (overnight charge, grid_charge=True)
        # Slot 2: off_peak_end -> evening_start (day -- let solar charge, SOC=100)
        # Slot 3: evening_start -> off_peak_start (evening drain, SOC=min_soc)
        # Slot 4: off_peak_start -> 23:57      (next overnight, grid_charge=True)
        # Slot 5: 23:57 -> 23:58              (filler)
        # Slot 6: 23:58 -> 00:00              (filler)
        state.slots = [
            SlotConfig(time(0, 0), self.off_peak_end, 100, True),
            SlotConfig(self.off_peak_end, self.evening_start, 100, False),
            SlotConfig(self.evening_start, self.off_peak_start, min_soc, False),
            SlotConfig(self.off_peak_start, time(23, 57), 100, True),
            SlotConfig(time(23, 57), time(23, 58), min_soc, False),
            SlotConfig(time(23, 58), time(0, 0), min_soc, False),
        ]

        state.reason_log.append(
            f"Baseline: overnight charge to 100% until {self.off_peak_end}, "
            f"solar day until {self.evening_start}, "
            f"evening drain to {min_soc}%"
        )
        return state
