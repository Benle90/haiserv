"""Tests for the haiserv.get_parent_letter service handler."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# homeassistant.exceptions and voluptuous are not part of the mocked package
# set in conftest.py; provide minimal stand-ins when they are not installed.
try:
    import voluptuous  # noqa: F401
except ImportError:
    sys.modules["voluptuous"] = MagicMock()

if not isinstance(sys.modules.get("homeassistant.exceptions"), MagicMock):
    sys.modules["homeassistant.exceptions"] = MagicMock()


class _HomeAssistantError(Exception):
    """Stand-in for homeassistant.exceptions.HomeAssistantError."""


class _ServiceValidationError(_HomeAssistantError):
    """Stand-in for homeassistant.exceptions.ServiceValidationError."""


sys.modules["homeassistant.exceptions"].HomeAssistantError = _HomeAssistantError
sys.modules["homeassistant.exceptions"].ServiceValidationError = _ServiceValidationError

from custom_components.haiserv import async_setup  # noqa: E402
from custom_components.haiserv.api import AuthenticationError, CannotConnect  # noqa: E402
from custom_components.haiserv.parentletter_parser import ParentLetter  # noqa: E402

ENTRY_ID = "01TESTENTRY"
LETTER_UUID = "aabbccdd-1111-2222-3333-444455556666"
CHILD_UUID = "99887766-aaaa-bbbb-cccc-ddddeeeeffff"
URL = f"https://school.example/iserv/parentletter/parent/show/{LETTER_UUID}/{CHILD_UUID}"
DETAIL_HTML = """
<div class="parent-show">
  <h2 class="translatable-content">Wandertag</h2>
  <div class="parent-letter-body"><div><p>Am 19.10. ist Wandertag.</p></div><p>Bitte Rucksack.</p></div>
</div>
"""


def _coordinator(letters: list[ParentLetter] | None = None) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = letters or []
    coordinator.client.fetch_parentletter_detail = AsyncMock(return_value=DETAIL_HTML)
    coordinator.client.parentletter_url = MagicMock(return_value=URL)
    return coordinator


async def _handler(hass_data: dict):
    hass = MagicMock()
    hass.data = hass_data
    assert await async_setup(hass, {}) is True
    args, kwargs = hass.services.async_register.call_args
    return args, kwargs


def _call(**data: str) -> MagicMock:
    call = MagicMock()
    call.data = {
        "config_entry_id": ENTRY_ID,
        "letter_uuid": LETTER_UUID,
        "child_uuid": CHILD_UUID,
        **data,
    }
    return call


class TestGetParentLetterService:
    """Tests for the get_parent_letter service."""

    async def test_registered_as_response_service(self) -> None:
        args, kwargs = await _handler({})
        assert args[0] == "haiserv"
        assert args[1] == "get_parent_letter"
        assert "supports_response" in kwargs
        assert "schema" in kwargs

    async def test_returns_letter_text(self) -> None:
        letter = ParentLetter(
            LETTER_UUID, CHILD_UUID, "Aus der Liste", "Frau Müller", is_unread=True
        )
        coordinator = _coordinator([letter])
        args, _ = await _handler({"haiserv": {f"{ENTRY_ID}_parentletter": coordinator}})
        response = await args[2](_call())

        coordinator.client.fetch_parentletter_detail.assert_awaited_once_with(
            LETTER_UUID, CHILD_UUID
        )
        assert response["subject"] == "Wandertag"
        assert response["sender"] == "Frau Müller"
        assert response["is_unread"] is True
        assert response["url"] == URL
        assert "Am 19.10. ist Wandertag." in response["body_text"]
        assert "Bitte Rucksack." in response["body_text"]

    async def test_letter_missing_from_list_still_fetched(self) -> None:
        coordinator = _coordinator([])
        args, _ = await _handler({"haiserv": {f"{ENTRY_ID}_parentletter": coordinator}})
        response = await args[2](_call())
        assert response["letter_uuid"] == LETTER_UUID
        assert response["subject"] == "Wandertag"
        assert response["is_unread"] is False

    async def test_uuids_lowercased(self) -> None:
        coordinator = _coordinator([])
        args, _ = await _handler({"haiserv": {f"{ENTRY_ID}_parentletter": coordinator}})
        await args[2](
            _call(letter_uuid=LETTER_UUID.upper(), child_uuid=CHILD_UUID.upper())
        )
        coordinator.client.fetch_parentletter_detail.assert_awaited_once_with(
            LETTER_UUID, CHILD_UUID
        )

    async def test_unknown_entry_rejected(self) -> None:
        args, _ = await _handler({"haiserv": {}})
        with pytest.raises(_ServiceValidationError):
            await args[2](_call())

    async def test_connection_error_reported(self) -> None:
        coordinator = _coordinator([])
        coordinator.client.fetch_parentletter_detail = AsyncMock(
            side_effect=CannotConnect("timeout")
        )
        args, _ = await _handler({"haiserv": {f"{ENTRY_ID}_parentletter": coordinator}})
        with pytest.raises(_HomeAssistantError):
            await args[2](_call())

    async def test_authentication_error_reported(self) -> None:
        coordinator = _coordinator([])
        coordinator.client.fetch_parentletter_detail = AsyncMock(
            side_effect=AuthenticationError("expired")
        )
        args, _ = await _handler({"haiserv": {f"{ENTRY_ID}_parentletter": coordinator}})
        with pytest.raises(_HomeAssistantError):
            await args[2](_call())
