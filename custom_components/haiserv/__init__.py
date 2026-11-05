"""The iServ integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

PLATFORMS = ["sensor"]

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the integration's services.

    Services are registered once per Home Assistant run, independent of the
    config entries; the entry to use is a service field.

    Args:
        hass: The Home Assistant instance.
        config: The YAML configuration (unused; the integration is UI-only).

    Returns:
        True.
    """
    import voluptuous as vol
    from homeassistant.core import SupportsResponse
    from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

    from .api import AuthenticationError, CannotConnect
    from .const import DOMAIN, SERVICE_GET_PARENT_LETTER
    from .parentletter_parser import ParentLetter, parentletter_response

    uuid = vol.All(
        str,
        vol.Match(
            r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
        ),
    )
    schema = vol.Schema(
        {
            vol.Required("config_entry_id"): str,
            vol.Required("letter_uuid"): uuid,
            vol.Required("child_uuid"): uuid,
        }
    )

    async def _get_parent_letter(call):
        """Fetch one Elternbrief and return its text. Reads only."""
        entry_id = call.data["config_entry_id"]
        letter_uuid = call.data["letter_uuid"].lower()
        child_uuid = call.data["child_uuid"].lower()

        coordinator = hass.data.get(DOMAIN, {}).get(f"{entry_id}_parentletter")
        if coordinator is None:
            raise ServiceValidationError(
                f"No loaded HAiServ config entry with id {entry_id}"
            )

        letter = next(
            (
                item
                for item in coordinator.data or []
                if item.letter_uuid.lower() == letter_uuid
                and item.child_uuid.lower() == child_uuid
            ),
            ParentLetter(
                letter_uuid=letter_uuid, child_uuid=child_uuid, subject="", sender=""
            ),
        )
        client = coordinator.client
        try:
            html = await client.fetch_parentletter_detail(letter_uuid, child_uuid)
        except AuthenticationError as err:
            raise HomeAssistantError(f"iServ authentication failed: {err}") from err
        except CannotConnect as err:
            raise HomeAssistantError(f"Cannot fetch the Elternbrief: {err}") from err

        return parentletter_response(
            html, letter, client.parentletter_url(letter_uuid, child_uuid)
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_PARENT_LETTER,
        _get_parent_letter,
        schema=schema,
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up iServ from a config entry.

    Creates the API client, coordinators, triggers first data refreshes,
    stores the coordinators in hass.data, and forwards sensor platform setup.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry being set up.

    Returns:
        True if setup was successful.
    """
    from homeassistant.helpers.aiohttp_client import async_create_clientsession
    from homeassistant.helpers.update_coordinator import UpdateFailed
    from homeassistant.exceptions import ConfigEntryNotReady

    from .api import IServClient
    from .cache import ResponseCache
    from .const import DOMAIN
    from .coordinator import IServCoordinator, IServParentLetterCoordinator

    # Get credentials from config entry data
    url = entry.data["url"]
    username = entry.data["username"]
    password = entry.data["password"]

    # iServ logs in with cookies. Home Assistant's shared session has one
    # cookie jar for all integrations, so two accounts on the same server
    # would overwrite each other's login. Each entry gets its own session.
    session = async_create_clientsession(hass)

    # Instantiate the iServ API client with a local timetable cache so a
    # temporarily disabled module (e.g. HTTP 403) can serve the last result.
    cache = ResponseCache(hass.config.path("haiserv_cache"))
    client = IServClient(session, url, username, password, cache=cache)

    # Create and refresh the current-week coordinator. Some iServ instances have
    # no timetable module at all; keep the entry (and the Elternbrief sensor)
    # usable instead of failing the whole setup.
    coordinator = IServCoordinator(hass, client)
    try:
        await coordinator.async_config_entry_first_refresh()
    except (UpdateFailed, ConfigEntryNotReady) as err:
        _LOGGER.warning("Timetable not available, continuing without it: %s", err)
        coordinator.data = []

    # Fetch the following week for a separate overview entity. Keep the
    # current-week entity usable if this optional request is unavailable.
    next_week_coordinator = IServCoordinator(hass, client, week_offset=1)
    try:
        await next_week_coordinator.async_config_entry_first_refresh()
    except (UpdateFailed, ConfigEntryNotReady):
        next_week_coordinator.data = []

    # Store the coordinators for platform entities
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    hass.data[DOMAIN][f"{entry.entry_id}_next_week"] = next_week_coordinator

    # Set up the Elternbrief coordinator. On failure, the sensor will be
    # unavailable but setup proceeds so timetable entities remain functional.
    parentletter_coordinator = IServParentLetterCoordinator(hass, client)
    try:
        await parentletter_coordinator.async_config_entry_first_refresh()
    except (UpdateFailed, ConfigEntryNotReady):
        parentletter_coordinator.data = []
    hass.data[DOMAIN][f"{entry.entry_id}_parentletter"] = parentletter_coordinator
    hass.data[DOMAIN][f"{entry.entry_id}_session"] = session

    # Forward platform setup to the sensor module
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an iServ config entry.

    Unloads sensor platforms and cleans up stored data.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry being unloaded.

    Returns:
        True if unloading was successful.
    """
    from .const import DOMAIN

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        hass.data[DOMAIN].pop(f"{entry.entry_id}_next_week", None)
        hass.data[DOMAIN].pop(f"{entry.entry_id}_parentletter", None)
        # Drop the entry's own session (and its cookies). detach() rather than
        # close(): the connector belongs to Home Assistant.
        session = hass.data[DOMAIN].pop(f"{entry.entry_id}_session", None)
        if session is not None:
            session.detach()

    return unload_ok
