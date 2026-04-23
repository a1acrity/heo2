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
) -> tuple[bool, str]:
    """Determine if writes are currently blocked and why.

    Returns (blocked, reason). When blocked=False, reason is empty string.
    When blocked=True, reason is a short human-readable explanation for
    the dashboard.

    Order of checks matters: dry_run takes precedence over transport
    state because dry_run is an intentional user choice - reporting
    "transport disconnected" when the user has explicitly disabled
    writes would be misleading.
    """
    if dry_run:
        return True, "dry_run enabled"
    if not writer_constructed or not transport_exists:
        return True, "MQTT writer not yet initialised"
    if not transport_connected:
        return True, f"MQTT transport disconnected from {host}"
    return False, ""
