"""BaselineRule -- lays down the default 6-slot programme structure."""

from __future__ import annotations

from datetime import time

from ..models import ProgrammeInputs
from ..rule_engine import PRIO_BASELINE, Rule


class BaselineRule(Rule):
    """Create the baseline programme: overnight charge -> day hold -> evening drain.

    Produces 4 meaningful slots + 2 short fillers for other rules to consume.
    """

    name = "baseline"
    description = "Default programme: cheap-rate overnight charge, solar day, evening drain"
    priority_class = PRIO_BASELINE

    def __init__(
        self,
        off_peak_start: time = time(23, 30),
        off_peak_end: time = time(5, 30),
        evening_start: time = time(18, 30),
    ):
        self.off_peak_start = off_peak_start
        self.off_peak_end = off_peak_end
        self.evening_start = evening_start

    def propose(self, view, inputs: ProgrammeInputs) -> None:
        min_soc = int(inputs.min_soc)

        # 6-slot baseline layout (start, end, capacity_soc, grid_charge):
        # 1: 00:00 -> off_peak_end       (overnight charge, gc=True)
        # 2: off_peak_end -> evening_start (day -- let solar charge, SOC=100)
        # 3: evening_start -> off_peak_start (evening drain, SOC=min_soc)
        # 4: off_peak_start -> 23:57      (next overnight, gc=True)
        # 5: 23:57 -> 23:58              (filler)
        # 6: 23:58 -> 00:00              (filler)
        layout = [
            (time(0, 0), self.off_peak_end, 100, True),
            (self.off_peak_end, self.evening_start, 100, False),
            (self.evening_start, self.off_peak_start, min_soc, False),
            (self.off_peak_start, time(23, 57), 100, True),
            (time(23, 57), time(23, 58), min_soc, False),
            (time(23, 58), time(0, 0), min_soc, False),
        ]
        for i, (start, end, cap, gc) in enumerate(layout):
            view.claim_slot(i, "start_time", start, reason="baseline scaffold")
            view.claim_slot(i, "end_time", end, reason="baseline scaffold")
            view.claim_slot(i, "capacity_soc", cap, reason="baseline scaffold")
            view.claim_slot(i, "grid_charge", gc, reason="baseline scaffold")

        # SPEC §2 globals. SavingSessionRule overrides work_mode to
        # "Selling first" while a session is active; BaselineRule re-
        # runs once the session ends and resets to defaults here.
        # `Load first` energy_pattern means the inverter prioritises
        # supplying load before charging the battery from solar - the
        # right default for a UK house with day load + Octopus IGO.
        # Charge / discharge limits at 100A match the Sunsynk 5kW
        # nominal max (100A * ~51.2V = 5120W) - leaves the inverter
        # free to use its full rate when needed.
        view.claim_global("work_mode", "Zero export to CT", reason="baseline default")
        view.claim_global("energy_pattern", "Load first", reason="baseline default")
        view.claim_global("max_charge_a", 100.0, reason="baseline default")
        view.claim_global("max_discharge_a", 100.0, reason="baseline default")

        view.log(
            f"Baseline: overnight charge to 100% until {self.off_peak_end}, "
            f"solar day until {self.evening_start}, "
            f"evening drain to {min_soc}%; "
            f"work_mode=Zero export to CT, energy_pattern=Load first, "
            f"max_charge=100A, max_discharge=100A"
        )
