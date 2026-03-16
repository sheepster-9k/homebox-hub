"""The Homebox Hub integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.frontend import (
    async_register_built_in_panel,
    async_remove_panel,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .api import (
    HomeBoxApiClient,
    HomeBoxApiError,
    HomeBoxAuthenticationError,
    HomeBoxConnectionError,
)
from .const import (
    CONF_HA_DEVICE_TO_HB_ITEM,
    CONF_HB_ITEM_TO_HA_DEVICE,
    CONF_LINKS,
    DOMAIN,
)
from .coordinator import HomeBoxConfigEntry, HomeBoxCoordinator
from .linking import (
    async_cleanup_removed_ha_device_link,
    async_sync_all_linked_hb_item_locations,
    async_sync_linked_hb_item_location,
)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.CONVERSATION]
SERVICES: tuple[str, ...] = ("search", "get_item", "list_locations", "move_item", "get_statistics")
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: HomeBoxConfigEntry) -> bool:
    """Set up Homebox Hub from a config entry."""
    # Ensure link maps exist in options
    if CONF_LINKS not in entry.options:
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_LINKS: {
                    CONF_HA_DEVICE_TO_HB_ITEM: {},
                    CONF_HB_ITEM_TO_HA_DEVICE: {},
                },
            },
        )

    # Create and authenticate the API client
    api = HomeBoxApiClient(
        hass=hass,
        host=entry.data[CONF_HOST],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )
    await api.async_authenticate()

    # Create coordinator and do first refresh
    coordinator = HomeBoxCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()

    # Best-effort startup sync of linked item locations
    try:
        await async_sync_all_linked_hb_item_locations(hass, entry, api)
    except (HomeBoxApiError, HomeBoxAuthenticationError, HomeBoxConnectionError):
        _LOGGER.warning(
            "Unable to sync linked Homebox item locations during startup"
        )

    # Listen for device registry changes (area updates, removals)
    async def _async_sync_location_for_device(ha_device_id: str) -> None:
        try:
            await async_sync_linked_hb_item_location(hass, entry, api, ha_device_id)
        except (HomeBoxApiError, HomeBoxAuthenticationError, HomeBoxConnectionError):
            return

    @callback
    def _async_handle_device_registry_updated(
        event: Event[dr.EventDeviceRegistryUpdatedData],
    ) -> None:
        action = event.data["action"]
        if action == "update":
            if "area_id" not in event.data["changes"]:
                return
            hass.async_create_task(
                _async_sync_location_for_device(event.data["device_id"])
            )
            return

        if action != "remove":
            return

        async def _async_cleanup_removed_device() -> None:
            current_entry = hass.config_entries.async_get_entry(entry.entry_id)
            if (
                current_entry is None
                or current_entry.state is not ConfigEntryState.LOADED
            ):
                return

            try:
                new_options = await async_cleanup_removed_ha_device_link(
                    hass, entry, api, event.data["device_id"]
                )
            except (
                HomeBoxApiError,
                HomeBoxAuthenticationError,
                HomeBoxConnectionError,
            ):
                _LOGGER.warning(
                    "Unable to clean up Homebox link after HA device removal"
                )
                return

            if new_options is not None:
                current_entry = hass.config_entries.async_get_entry(entry.entry_id)
                if (
                    current_entry is not None
                    and current_entry.state is ConfigEntryState.LOADED
                ):
                    hass.config_entries.async_update_entry(
                        current_entry, options=new_options
                    )

        hass.async_create_task(_async_cleanup_removed_device())

    entry.async_on_unload(
        hass.bus.async_listen(
            dr.EVENT_DEVICE_REGISTRY_UPDATED,
            _async_handle_device_registry_updated,
        )
    )

    # Store coordinator
    entry.runtime_data = coordinator

    # Forward platform setup
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services
    await _async_register_services(hass, entry)

    # Register sidebar panels
    _async_register_panels(hass, entry)

    # Reload on options change (must be after runtime_data and platform setup)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: HomeBoxConfigEntry
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # Unregister services and panel if this is the last loaded entry
        remaining = [
            e for e in hass.config_entries.async_entries(DOMAIN)
            if e.entry_id != entry.entry_id and e.state is ConfigEntryState.LOADED
        ]
        if not remaining:
            # Remove panel
            try:
                async_remove_panel(hass, "homebox")
            except KeyError:
                pass  # Panel was never registered or already removed
            # Remove services
            for svc in SERVICES:
                if hass.services.has_service(DOMAIN, svc):
                    hass.services.async_remove(DOMAIN, svc)
    return unload_ok


async def _async_reload_entry(
    hass: HomeAssistant, entry: HomeBoxConfigEntry
) -> None:
    """Reload entry after options update."""
    await hass.config_entries.async_reload(entry.entry_id)


def _get_api_for_service(
    hass: HomeAssistant, call: ServiceCall
) -> HomeBoxApiClient:
    """Get the API client from the first loaded config entry."""
    entry_id = call.data.get("config_entry_id")
    entries = hass.config_entries.async_entries(DOMAIN)
    for entry in entries:
        if entry.state is not ConfigEntryState.LOADED:
            continue
        if entry_id and entry.entry_id != entry_id:
            continue
        coordinator: HomeBoxCoordinator = entry.runtime_data
        return coordinator.api
    raise ValueError("No loaded Homebox Hub config entry found")


async def _async_register_services(
    hass: HomeAssistant, entry: HomeBoxConfigEntry
) -> None:
    """Register Homebox Hub services."""
    if hass.services.has_service(DOMAIN, "search"):
        return  # Already registered

    async def handle_search(call: ServiceCall) -> dict[str, Any]:
        api = _get_api_for_service(hass, call)
        query = call.data["query"]
        results = await api.async_search_items(query)
        return {"items": [
            {"id": item.item_id, "name": item.name,
             "location_id": item.location_id, "location_name": item.location_name}
            for item in results
        ]}

    async def handle_get_item(call: ServiceCall) -> dict[str, Any]:
        api = _get_api_for_service(hass, call)
        item_id = call.data["item_id"]
        return await api.async_get_item(item_id)

    async def handle_list_locations(call: ServiceCall) -> dict[str, Any]:
        api = _get_api_for_service(hass, call)
        locations = await api.async_get_locations()
        return {"locations": locations}

    async def handle_move_item(call: ServiceCall) -> None:
        api = _get_api_for_service(hass, call)
        await api.async_set_hb_item_location(
            call.data["item_id"], call.data["location_id"]
        )

    async def handle_get_statistics(call: ServiceCall) -> dict[str, Any]:
        api = _get_api_for_service(hass, call)
        stats = await api.async_get_group_statistics()
        return {
            "total_items": stats.total_items,
            "total_locations": stats.total_locations,
            "total_value": stats.total_value,
        }

    hass.services.async_register(
        DOMAIN,
        "search",
        handle_search,
        schema=vol.Schema(
            {
                vol.Required("query"): cv.string,
                vol.Optional("config_entry_id"): cv.string,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "get_item",
        handle_get_item,
        schema=vol.Schema(
            {
                vol.Required("item_id"): cv.string,
                vol.Optional("config_entry_id"): cv.string,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "list_locations",
        handle_list_locations,
        schema=vol.Schema(
            {vol.Optional("config_entry_id"): cv.string}
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "move_item",
        handle_move_item,
        schema=vol.Schema(
            {
                vol.Required("item_id"): cv.string,
                vol.Required("location_id"): cv.string,
                vol.Optional("config_entry_id"): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "get_statistics",
        handle_get_statistics,
        schema=vol.Schema(
            {vol.Optional("config_entry_id"): cv.string}
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )


@callback
def _async_register_panels(
    hass: HomeAssistant, entry: HomeBoxConfigEntry
) -> None:
    """Register sidebar panels for Homebox."""
    # Guard against duplicate registration from multiple config entries
    if "homebox" in hass.data.get("frontend_panels", {}):
        return
    homebox_url = entry.data.get(CONF_HOST, "")
    if homebox_url:
        async_register_built_in_panel(
            hass,
            "iframe",
            "Homebox",
            "mdi:package-variant",
            "homebox",
            {"url": homebox_url},
            require_admin=False,
        )
