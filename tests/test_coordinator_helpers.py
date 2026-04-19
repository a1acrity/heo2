# tests/test_coordinator_helpers.py
"""Tests for the HA recorder state parser.

Originally placed here because the parser lived in coordinator.py, but
importing heo2.coordinator pulls in homeassistant.* which isn't in the
test venv. The parser has since moved to heo2.load_history and is imported
from there. This test file is kept separate because it's semantically
about the HA recorder integration boundary, not the core aggregator
maths (which lives in test_load_history.py).
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from heo2.load_history import states_to_power_samples

UTC = timezone.utc


def _mk_state(ts_iso: str, state: str):
    """Build a minimal HA State-like object for testing."""
    dt = datetime.fromisoformat(ts_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return SimpleNamespace(last_changed=dt, state=state)


class TestStatesToPowerSamples:
    def test_parses_well_formed_states(self):
        states = [
            _mk_state("2026-04-18T12:00:00+00:00", "1500"),
            _mk_state("2026-04-18T12:05:00+00:00", "2000"),
        ]
        out = states_to_power_samples(states)
        assert len(out) == 2
        assert out[0][1] == 1500.0
        assert out[1][1] == 2000.0

    def test_skips_unknown_states(self):
        states = [
            _mk_state("2026-04-18T12:00:00+00:00", "unknown"),
            _mk_state("2026-04-18T12:05:00+00:00", "1500"),
        ]
        out = states_to_power_samples(states)
        assert len(out) == 1
        assert out[0][1] == 1500.0

    def test_skips_unavailable_states(self):
        states = [
            _mk_state("2026-04-18T12:00:00+00:00", "unavailable"),
            _mk_state("2026-04-18T12:05:00+00:00", "1500"),
        ]
        out = states_to_power_samples(states)
        assert len(out) == 1

    def test_skips_non_numeric_states(self):
        states = [
            _mk_state("2026-04-18T12:00:00+00:00", "on"),
            _mk_state("2026-04-18T12:05:00+00:00", "1500"),
            _mk_state("2026-04-18T12:10:00+00:00", "n/a"),
        ]
        out = states_to_power_samples(states)
        assert len(out) == 1
        assert out[0][1] == 1500.0

    def test_handles_negative_and_float_values(self):
        """Negative (export) and decimal values are accepted as floats;
        clamping is the aggregator's job, not the parser's."""
        states = [
            _mk_state("2026-04-18T12:00:00+00:00", "-500"),
            _mk_state("2026-04-18T12:05:00+00:00", "1500.5"),
        ]
        out = states_to_power_samples(states)
        assert len(out) == 2
        assert out[0][1] == -500.0
        assert out[1][1] == 1500.5

    def test_sorts_by_timestamp(self):
        """Recorder usually returns sorted, but we don't trust callers."""
        states = [
            _mk_state("2026-04-18T12:05:00+00:00", "2000"),
            _mk_state("2026-04-18T12:00:00+00:00", "1000"),
            _mk_state("2026-04-18T12:10:00+00:00", "3000"),
        ]
        out = states_to_power_samples(states)
        assert [w for _, w in out] == [1000.0, 2000.0, 3000.0]

    def test_empty_list_returns_empty(self):
        assert states_to_power_samples([]) == []

    def test_skips_states_missing_attributes(self):
        """Malformed state objects without the expected attributes are skipped."""
        states = [
            object(),  # no attributes
            _mk_state("2026-04-18T12:00:00+00:00", "1500"),
        ]
        out = states_to_power_samples(states)
        assert len(out) == 1
        assert out[0][1] == 1500.0

    def test_skips_state_with_none_timestamp(self):
        s = SimpleNamespace(last_changed=None, state="1500")
        out = states_to_power_samples([s])
        assert out == []
