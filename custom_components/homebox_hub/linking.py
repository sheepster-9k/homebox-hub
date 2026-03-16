"""Bidirectional linking between HA devices and Homebox items."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .api import HomeBoxApiClient
from .const import (
    CONF_HA_DEVICE_ID,
    CONF_HA_DEVICE_TO_HB_ITEM,
    CONF_HB_ITEM_ID,
    CONF_HB_ITEM_TO_HA_DEVICE,
    CONF_LINKS,
    DOMAIN,
    LINK_BACKLINK_FIELD_NAME,
)
from .item_fields import (
    build_item_update_payload,
    extract_item_fields,
    merge_backlink_field,
)

_LOGGER = logging.getLogger(__name__)

_HA_DEVICE_URL_RE = re.compile(r"/config/devices/device/([^/]+)")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class HomeBoxTaggedItem:
    """A Homebox item that carries the HomeAssistant tag."""

    hb_item_id: str
    name: str
    has_backlink: bool


@dataclass(slots=True)
class HomeBoxLinkScanResult:
    """Result of scanning tagged Homebox items for link state."""

    unlinked_hb_items: list[HomeBoxTaggedItem] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Link map helpers
# ---------------------------------------------------------------------------


def get_link_maps(
    config_entry: ConfigEntry,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return (ha_device_to_hb_item, hb_item_to_ha_device) from entry options."""
    links: dict[str, Any] = config_entry.options.get(CONF_LINKS, {})
    ha_device_to_hb_item: dict[str, str] = dict(
        links.get(CONF_HA_DEVICE_TO_HB_ITEM, {})
    )
    hb_item_to_ha_device: dict[str, str] = dict(
        links.get(CONF_HB_ITEM_TO_HA_DEVICE, {})
    )
    return ha_device_to_hb_item, hb_item_to_ha_device


def build_updated_options(
    config_entry: ConfigEntry,
    ha_device_to_hb_item: dict[str, str],
    hb_item_to_ha_device: dict[str, str],
) -> dict[str, Any]:
    """Return a full replacement options dict with updated link maps."""
    new_options = dict(config_entry.options)
    new_options[CONF_LINKS] = {
        CONF_HA_DEVICE_TO_HB_ITEM: dict(ha_device_to_hb_item),
        CONF_HB_ITEM_TO_HA_DEVICE: dict(hb_item_to_ha_device),
    }
    return new_options


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def get_ha_device_url(hass: HomeAssistant, ha_device_id: str) -> str:
    """Build the full URL to a device page in the HA frontend."""
    try:
        base = get_url(hass)
    except NoURLAvailableError:
        base = ""
    return f"{base}/config/devices/device/{ha_device_id}"


def _extract_backlink_url(hb_item: dict[str, Any]) -> str | None:
    """Return the backlink URL string from a Homebox item, or None."""
    fields = extract_item_fields(hb_item)
    for f in fields:
        if f.get("name") == LINK_BACKLINK_FIELD_NAME:
            value = f.get("textValue")
            if isinstance(value, str) and value:
                return value
    return None


def _extract_ha_device_id_from_url(url: str) -> str | None:
    """Parse an HA device id from a device page URL."""
    match = _HA_DEVICE_URL_RE.search(url)
    if match:
        return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Field / backlink helpers
# ---------------------------------------------------------------------------


def _has_backlink_in_fields(fields: list[dict[str, Any]]) -> bool:
    """Return True if the fields list contains a non-empty backlink entry."""
    for f in fields:
        if f.get("name") == LINK_BACKLINK_FIELD_NAME:
            value = f.get("textValue")
            return isinstance(value, str) and len(value) > 0
    return False


# ---------------------------------------------------------------------------
# Bidirectional map mutation helpers
# ---------------------------------------------------------------------------


def _pop_bidirectional_link(
    ha_device_to_hb_item: dict[str, str],
    hb_item_to_ha_device: dict[str, str],
    ha_device_id: str,
    hb_item_id: str,
) -> None:
    """Remove a link from both maps."""
    ha_device_to_hb_item.pop(ha_device_id, None)
    hb_item_to_ha_device.pop(hb_item_id, None)


def _pop_link_by_hb_item(
    ha_device_to_hb_item: dict[str, str],
    hb_item_to_ha_device: dict[str, str],
    hb_item_id: str,
) -> str | None:
    """Remove a link by Homebox item id. Returns the HA device id or None."""
    ha_device_id = hb_item_to_ha_device.pop(hb_item_id, None)
    if ha_device_id is not None:
        ha_device_to_hb_item.pop(ha_device_id, None)
    return ha_device_id


def _pop_link_by_ha_device(
    ha_device_to_hb_item: dict[str, str],
    hb_item_to_ha_device: dict[str, str],
    ha_device_id: str,
) -> str | None:
    """Remove a link by HA device id. Returns the Homebox item id or None."""
    hb_item_id = ha_device_to_hb_item.pop(ha_device_id, None)
    if hb_item_id is not None:
        hb_item_to_ha_device.pop(hb_item_id, None)
    return hb_item_id


# ---------------------------------------------------------------------------
# HA device / area helpers
# ---------------------------------------------------------------------------


def _get_ha_device_area_name(
    hass: HomeAssistant, ha_device: dr.DeviceEntry
) -> str | None:
    """Resolve the area name for a device, or None if unassigned."""
    if ha_device.area_id is None:
        return None
    area_reg = ar.async_get(hass)
    area = area_reg.async_get_area(ha_device.area_id)
    if area is None:
        return None
    return area.name


async def _async_finalize_ha_device_unlink(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    ha_device_id: str,
) -> None:
    """Clean up the HA side after a link is removed.

    - Clears configuration_url on the device.
    - Removes the linking sensor entity if present.
    - Detaches the config entry from the device if no entities remain.
    """
    dev_reg = dr.async_get(hass)
    ha_device = dev_reg.async_get(ha_device_id)
    if ha_device is None:
        return

    # Clear configuration_url
    dev_reg.async_update_device(ha_device_id, configuration_url=None)

    # Remove linking sensor entity
    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id(
        SENSOR_DOMAIN, DOMAIN, f"{ha_device_id}_homebox_link"
    )
    if entity_id is not None:
        ent_reg.async_remove(entity_id)

    # Detach config entry from device if no entities reference it
    remaining = er.async_entries_for_device(ent_reg, ha_device_id, True)
    our_entities = [
        e for e in remaining if e.config_entry_id == config_entry.entry_id
    ]
    if not our_entities:
        dev_reg.async_update_device(
            ha_device_id,
            remove_config_entry_id=config_entry.entry_id,
        )


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


async def scan_tagged_items_for_links(
    api: HomeBoxApiClient,
    config_entry: ConfigEntry,
) -> HomeBoxLinkScanResult:
    """Scan Homebox for items tagged with HomeAssistant.

    Returns items that are not yet linked and any conflict descriptions.
    """
    ha_device_to_hb_item, hb_item_to_ha_device = get_link_maps(config_entry)
    result = HomeBoxLinkScanResult()

    tagged_items = await api.async_get_items_by_tag()

    for item_summary in tagged_items:
        hb_item_id = item_summary.item_id
        name = item_summary.name
        fields = item_summary.fields or []
        has_backlink = _has_backlink_in_fields(fields)

        # Already linked — skip
        if hb_item_id in hb_item_to_ha_device:
            # Check for backlink pointing to a different device (conflict)
            if has_backlink:
                backlink_url = None
                for f in fields:
                    if f.get("name") == LINK_BACKLINK_FIELD_NAME:
                        backlink_url = f.get("textValue")
                        break
                if backlink_url:
                    parsed_id = _extract_ha_device_id_from_url(backlink_url)
                    expected_id = hb_item_to_ha_device[hb_item_id]
                    if parsed_id and parsed_id != expected_id:
                        result.conflicts.append(
                            f"Item '{name}' ({hb_item_id}) backlink points to "
                            f"device {parsed_id} but is linked to {expected_id}"
                        )
            continue

        # Has a backlink but is not in our link map — possible stale / external link
        if has_backlink:
            backlink_url = None
            for f in fields:
                if f.get("name") == LINK_BACKLINK_FIELD_NAME:
                    backlink_url = f.get("textValue")
                    break
            if backlink_url:
                parsed_id = _extract_ha_device_id_from_url(backlink_url)
                if parsed_id and parsed_id in ha_device_to_hb_item:
                    result.conflicts.append(
                        f"Item '{name}' ({hb_item_id}) has backlink to device "
                        f"{parsed_id} which is already linked to "
                        f"{ha_device_to_hb_item[parsed_id]}"
                    )
                    continue

        result.unlinked_hb_items.append(
            HomeBoxTaggedItem(
                hb_item_id=hb_item_id,
                name=name,
                has_backlink=has_backlink,
            )
        )

    return result


# ---------------------------------------------------------------------------
# Linking / unlinking
# ---------------------------------------------------------------------------


async def apply_link(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    api: HomeBoxApiClient,
    ha_device_id: str,
    hb_item_id: str,
) -> dict[str, Any]:
    """Create a bidirectional link between an HA device and a Homebox item.

    Returns the updated options dict to be persisted.

    Raises ValueError on conflict.
    """
    ha_device_to_hb_item, hb_item_to_ha_device = get_link_maps(config_entry)

    # Validate no conflicts
    if ha_device_id in ha_device_to_hb_item:
        existing = ha_device_to_hb_item[ha_device_id]
        raise ValueError(
            f"HA device {ha_device_id} is already linked to Homebox item {existing}"
        )
    if hb_item_id in hb_item_to_ha_device:
        existing = hb_item_to_ha_device[hb_item_id]
        raise ValueError(
            f"Homebox item {hb_item_id} is already linked to HA device {existing}"
        )

    # Set backlink on Homebox item
    ha_device_url = get_ha_device_url(hass, ha_device_id)
    hb_item = await api.async_get_item(hb_item_id)
    fields = extract_item_fields(hb_item)
    updated_fields = merge_backlink_field(fields, ha_device_url)
    payload = build_item_update_payload(hb_item, updated_fields)
    await api.async_update_item(hb_item_id, payload)

    # Sync location from HA area to Homebox
    dev_reg = dr.async_get(hass)
    ha_device = dev_reg.async_get(ha_device_id)
    if ha_device is not None:
        area_name = _get_ha_device_area_name(hass, ha_device)
        if area_name:
            await _async_sync_location(api, hb_item_id, area_name)

        # Set configuration_url on the HA device
        try:
            hb_item_url = api.get_hb_item_url(hb_item_id)
        except Exception:  # noqa: BLE001
            hb_item_url = None
        if hb_item_url:
            dev_reg.async_update_device(
                ha_device_id, configuration_url=hb_item_url
            )

    # Persist link
    ha_device_to_hb_item[ha_device_id] = hb_item_id
    hb_item_to_ha_device[hb_item_id] = ha_device_id
    return build_updated_options(config_entry, ha_device_to_hb_item, hb_item_to_ha_device)


async def remove_link(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    api: HomeBoxApiClient,
    ha_device_id: str,
    hb_item_id: str,
) -> dict[str, Any]:
    """Remove a bidirectional link. Returns updated options dict."""
    ha_device_to_hb_item, hb_item_to_ha_device = get_link_maps(config_entry)
    _pop_bidirectional_link(
        ha_device_to_hb_item, hb_item_to_ha_device, ha_device_id, hb_item_id
    )

    # Remove backlink from Homebox item
    try:
        hb_item = await api.async_get_item(hb_item_id)
        fields = extract_item_fields(hb_item)
        updated_fields = merge_backlink_field(fields, None)
        payload = build_item_update_payload(hb_item, updated_fields)
        await api.async_update_item(hb_item_id, payload)
    except Exception:  # noqa: BLE001
        _LOGGER.warning(
            "Failed to remove backlink from Homebox item %s", hb_item_id
        )

    # Clean up HA side
    await _async_finalize_ha_device_unlink(hass, config_entry, ha_device_id)

    return build_updated_options(config_entry, ha_device_to_hb_item, hb_item_to_ha_device)


# ---------------------------------------------------------------------------
# Cleanup / sync
# ---------------------------------------------------------------------------


async def async_cleanup_unlinked_hb_backlinks(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    api: HomeBoxApiClient,
) -> tuple[int, dict[str, Any] | None]:
    """Remove backlinks from Homebox items that are no longer linked.

    Returns (cleaned_count, new_options_or_None).
    """
    ha_device_to_hb_item, hb_item_to_ha_device = get_link_maps(config_entry)
    tagged_items = await api.async_get_items_by_tag()
    cleaned = 0
    options_changed = False

    for item_summary in tagged_items:
        hb_item_id = item_summary.item_id
        fields = item_summary.fields or []

        if not _has_backlink_in_fields(fields):
            continue

        # If item is properly linked, skip
        if hb_item_id in hb_item_to_ha_device:
            continue

        # Backlink exists but item is not in our link map — clean it up
        _LOGGER.info(
            "Cleaning orphaned backlink from Homebox item %s (%s)",
            item_summary.name,
            hb_item_id,
        )
        try:
            hb_item = await api.async_get_item(hb_item_id)
            item_fields = extract_item_fields(hb_item)
            updated_fields = merge_backlink_field(item_fields, None)
            payload = build_item_update_payload(hb_item, updated_fields)
            await api.async_update_item(hb_item_id, payload)
            cleaned += 1
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to clean backlink from Homebox item %s", hb_item_id
            )

        # Also remove from link maps if present by some other key
        backlink_url = None
        for f in fields:
            if f.get("name") == LINK_BACKLINK_FIELD_NAME:
                backlink_url = f.get("textValue")
                break
        if backlink_url:
            parsed_id = _extract_ha_device_id_from_url(backlink_url)
            if parsed_id and parsed_id in ha_device_to_hb_item:
                _pop_link_by_ha_device(
                    ha_device_to_hb_item, hb_item_to_ha_device, parsed_id
                )
                options_changed = True

    if options_changed:
        new_options = build_updated_options(
            config_entry, ha_device_to_hb_item, hb_item_to_ha_device
        )
        return cleaned, new_options

    return cleaned, None


async def _async_sync_location(
    api: HomeBoxApiClient,
    hb_item_id: str,
    area_name: str,
) -> None:
    """Set the Homebox item's location to match an HA area name."""
    locations = await api.async_get_locations()
    location_id: str | None = None
    for loc in locations:
        if loc.get("name") == area_name:
            location_id = loc.get("id")
            break

    if location_id is None:
        # Create the location in Homebox
        try:
            new_loc = await api.async_create_location({"name": area_name})
            location_id = new_loc.get("id")
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to create Homebox location for area '%s'", area_name
            )
            return

    if location_id is None:
        return

    hb_item = await api.async_get_item(hb_item_id)
    fields = extract_item_fields(hb_item)
    payload = build_item_update_payload(hb_item, fields)
    payload["locationId"] = location_id
    await api.async_update_item(hb_item_id, payload)


async def async_sync_linked_hb_item_location(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    api: HomeBoxApiClient,
    ha_device_id: str,
) -> None:
    """Sync a single linked HA device's area to its Homebox item's location."""
    ha_device_to_hb_item, _ = get_link_maps(config_entry)
    hb_item_id = ha_device_to_hb_item.get(ha_device_id)
    if hb_item_id is None:
        _LOGGER.debug("Device %s is not linked; skipping location sync", ha_device_id)
        return

    dev_reg = dr.async_get(hass)
    ha_device = dev_reg.async_get(ha_device_id)
    if ha_device is None:
        return

    area_name = _get_ha_device_area_name(hass, ha_device)
    if area_name:
        await _async_sync_location(api, hb_item_id, area_name)
    else:
        _LOGGER.debug(
            "Device %s has no area; skipping location sync for item %s",
            ha_device_id,
            hb_item_id,
        )


async def async_sync_all_linked_hb_item_locations(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    api: HomeBoxApiClient,
) -> None:
    """Sync all linked HA device areas to their Homebox item locations."""
    ha_device_to_hb_item, _ = get_link_maps(config_entry)
    dev_reg = dr.async_get(hass)

    for ha_device_id, hb_item_id in ha_device_to_hb_item.items():
        ha_device = dev_reg.async_get(ha_device_id)
        if ha_device is None:
            _LOGGER.debug(
                "Linked device %s no longer exists; skipping sync", ha_device_id
            )
            continue

        area_name = _get_ha_device_area_name(hass, ha_device)
        if area_name:
            try:
                await _async_sync_location(api, hb_item_id, area_name)
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "Failed to sync location for device %s -> item %s",
                    ha_device_id,
                    hb_item_id,
                )


async def async_cleanup_removed_ha_device_link(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    api: HomeBoxApiClient,
    ha_device_id: str,
) -> dict[str, Any] | None:
    """Clean up a link after an HA device has been removed.

    This is called from a device removal handler.  A race condition exists
    where the config entry may already be unloading when the device-removed
    event fires.  We guard against that by checking the entry state before
    touching the API.

    Returns updated options if the link was removed, None otherwise.
    """
    ha_device_to_hb_item, hb_item_to_ha_device = get_link_maps(config_entry)

    hb_item_id = ha_device_to_hb_item.get(ha_device_id)
    if hb_item_id is None:
        return None

    # Guard: if the entry is no longer loaded, skip API calls to avoid errors
    if config_entry.state is not ConfigEntryState.LOADED:
        _LOGGER.debug(
            "Config entry %s is in state %s; skipping API cleanup for device %s",
            config_entry.entry_id,
            config_entry.state,
            ha_device_id,
        )
        # Still remove from link maps so the options stay consistent
        _pop_bidirectional_link(
            ha_device_to_hb_item, hb_item_to_ha_device, ha_device_id, hb_item_id
        )
        return build_updated_options(
            config_entry, ha_device_to_hb_item, hb_item_to_ha_device
        )

    _LOGGER.info(
        "HA device %s removed; cleaning up link to Homebox item %s",
        ha_device_id,
        hb_item_id,
    )

    # Remove backlink from Homebox item
    try:
        hb_item = await api.async_get_item(hb_item_id)
        fields = extract_item_fields(hb_item)
        updated_fields = merge_backlink_field(fields, None)
        payload = build_item_update_payload(hb_item, updated_fields)
        await api.async_update_item(hb_item_id, payload)
    except Exception:  # noqa: BLE001
        _LOGGER.warning(
            "Failed to remove backlink from Homebox item %s during device cleanup",
            hb_item_id,
        )

    _pop_bidirectional_link(
        ha_device_to_hb_item, hb_item_to_ha_device, ha_device_id, hb_item_id
    )

    return build_updated_options(
        config_entry, ha_device_to_hb_item, hb_item_to_ha_device
    )
