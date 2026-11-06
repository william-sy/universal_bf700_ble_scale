"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom_components."""


@pytest.fixture(autouse=True)
def mock_bluetooth(enable_bluetooth):
    """Enable the bluetooth integration."""


@pytest.fixture
def mock_setup_entry() -> Generator[None]:
    """Avoid real setup in config flow tests."""
    with patch("custom_components.universal_ble_scale.async_setup_entry", return_value=True):
        yield
