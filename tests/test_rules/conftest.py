"""Shared fixtures for rule tests."""

import pytest
from datetime import time

from heo2.models import ProgrammeState


@pytest.fixture
def baseline_programme() -> ProgrammeState:
    """A fresh default programme for rules to work with."""
    return ProgrammeState.default(min_soc=20)
