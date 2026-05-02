# custom_components/heo2/writes_status.py
"""Pure logic for determining whether HEO II is currently able to write
to the inverter.

Separated out so it can be unit-tested without HA imports. The coordinator
calls `_compute_writes_blocked` to drive both the binary_sensor and any
internal branching.
"""

from __future__ import annotations


def _compute_writes_blocked(
    *,
    dry_run: bool,
    writer_constructed: bool,
    transport_exists: bool,
    transport_connected: bool,
    host: str,
    live_rates_present: bool = True,
) -> tuple[bool, str]:
    """Determine if writes are currently blocked and why.

    Returns (blocked, reason). When blocked=False, reason is empty string.
    When blocked=True, reason is a short human-readable explanation for
    the dashboard.

    Order of checks matters:
      1. dry_run takes precedence over everything else - it's an
         intentional user choice; reporting any other reason would be
         misleading.
      2. Transport readiness comes next so early-startup state is
         described accurately.
      3. SPEC H4 (live-prices-only writes) is checked last: by this
         point the transport is up, so the only remaining reason to
         block is missing live BottlecapDave rates. The default True
         keeps backward compatibility for callers that pre-date HEO-14.
    """
    if dry_run:
        return True, "dry_run enabled"
    if not writer_constructed or not transport_exists:
        return True, "MQTT writer not yet initialised"
    if not transport_connected:
        return True, f"MQTT transport disconnected from {host}"
    if not live_rates_present:
        return True, "HEO-14: no live BottlecapDave rates (SPEC H4)"
    return False, ""
