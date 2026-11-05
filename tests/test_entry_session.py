"""Tests: every config entry gets its own HTTP session (own cookie jar)."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# homeassistant.exceptions is not part of the mocked package set in conftest.py.
if "homeassistant.exceptions" not in sys.modules:
    sys.modules["homeassistant.exceptions"] = MagicMock()

from custom_components.haiserv import async_setup_entry, async_unload_entry  # noqa: E402


def _entry(entry_id: str, username: str) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.data = {
        "url": "https://school.example",
        "username": username,
        "password": "secret",
    }
    return entry


def _hass() -> MagicMock:
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    return hass


def _coordinator_class() -> MagicMock:
    cls = MagicMock()
    cls.side_effect = lambda *args, **kwargs: MagicMock(
        async_config_entry_first_refresh=AsyncMock()
    )
    return cls


class TestSessionPerEntry:
    """Two accounts on one server must not share cookies."""

    async def _setup_two(self):
        hass = _hass()
        sessions = [MagicMock(name="session-a"), MagicMock(name="session-b")]
        create = MagicMock(side_effect=sessions)
        with patch(
            "homeassistant.helpers.aiohttp_client.async_create_clientsession", create
        ), patch("custom_components.haiserv.api.IServClient") as client_cls, patch(
            "custom_components.haiserv.coordinator.IServCoordinator",
            _coordinator_class(),
        ), patch(
            "custom_components.haiserv.coordinator.IServParentLetterCoordinator",
            _coordinator_class(),
        ), patch("custom_components.haiserv.cache.ResponseCache"):
            assert await async_setup_entry(hass, _entry("A", "parent-one")) is True
            assert await async_setup_entry(hass, _entry("B", "parent-two")) is True
        return hass, sessions, create, client_cls

    async def test_each_entry_creates_its_own_session(self) -> None:
        hass, sessions, create, client_cls = await self._setup_two()

        assert create.call_count == 2
        used = [call.args[0] for call in client_cls.call_args_list]
        assert used == sessions
        assert used[0] is not used[1]

    async def test_session_stored_per_entry(self) -> None:
        hass, sessions, _, _ = await self._setup_two()
        assert hass.data["haiserv"]["A_session"] is sessions[0]
        assert hass.data["haiserv"]["B_session"] is sessions[1]

    async def test_unload_detaches_only_its_session(self) -> None:
        hass, sessions, _, _ = await self._setup_two()

        assert await async_unload_entry(hass, _entry("A", "parent-one")) is True

        sessions[0].detach.assert_called_once_with()
        sessions[1].detach.assert_not_called()
        assert "A_session" not in hass.data["haiserv"]
        assert hass.data["haiserv"]["B_session"] is sessions[1]
