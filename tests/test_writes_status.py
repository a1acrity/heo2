# tests/test_writes_status.py
"""Tests for the pure _compute_writes_blocked helper."""

from __future__ import annotations

import pytest

from heo2.writes_status import _compute_writes_blocked


class TestComputeWritesBlocked:
    def test_dry_run_blocks_writes(self):
        """dry_run=True always blocks, regardless of transport state."""
        blocked, reason = _compute_writes_blocked(
            dry_run=True,
            writer_constructed=True,
            transport_exists=True,
            transport_connected=True,
            host="192.168.4.7",
        )
        assert blocked is True
        assert "dry_run" in reason

    def test_dry_run_takes_precedence_over_disconnect(self):
        """When dry_run=True AND transport disconnected, reason cites
        dry_run (user's intentional choice), not the disconnect."""
        blocked, reason = _compute_writes_blocked(
            dry_run=True,
            writer_constructed=False,
            transport_exists=False,
            transport_connected=False,
            host="192.168.4.7",
        )
        assert blocked is True
        assert "dry_run" in reason
        assert "disconnected" not in reason
        assert "initialised" not in reason

    def test_writer_not_constructed_blocks(self):
        """Early startup state - writer is None - blocks writes."""
        blocked, reason = _compute_writes_blocked(
            dry_run=False,
            writer_constructed=False,
            transport_exists=False,
            transport_connected=False,
            host="192.168.4.7",
        )
        assert blocked is True
        assert "not yet initialised" in reason

    def test_transport_exists_but_disconnected_blocks(self):
        """Post-startup but broker link is down - blocks with host info."""
        blocked, reason = _compute_writes_blocked(
            dry_run=False,
            writer_constructed=True,
            transport_exists=True,
            transport_connected=False,
            host="192.168.4.7",
        )
        assert blocked is True
        assert "disconnected" in reason
        assert "192.168.4.7" in reason

    def test_happy_path_not_blocked(self):
        """Everything up and running - writes are permitted."""
        blocked, reason = _compute_writes_blocked(
            dry_run=False,
            writer_constructed=True,
            transport_exists=True,
            transport_connected=True,
            host="192.168.4.7",
        )
        assert blocked is False
        assert reason == ""

    def test_host_appears_in_disconnect_reason(self):
        """Custom host should appear in the reason so multi-install
        users can tell which SA broker has disconnected."""
        blocked, reason = _compute_writes_blocked(
            dry_run=False,
            writer_constructed=True,
            transport_exists=True,
            transport_connected=False,
            host="10.0.5.42",
        )
        assert blocked is True
        assert "10.0.5.42" in reason
