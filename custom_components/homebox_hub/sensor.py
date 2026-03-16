"""Sensor platform for the Homebox Hub integration."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HomeBoxConfigEntry, HomeBoxCoordinator, HomeBoxCoordinatorData
from .linking import get_link_maps

_LOGGER = logging.getLogger(__name__)


STATISTICS_SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="total_items",
        translation_key="total_items",
        icon="mdi:package-variant",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="total_locations",
        translation_key="total_locations",
        icon="mdi:map-marker-multiple",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="total_value",
        translation_key="total_value",
        icon="mdi:cash",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: HomeBoxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Homebox Hub sensor entities from a config entry."""
    coordinator: HomeBoxCoordinator = config_entry.runtime_data

    # Determine currency from HA core config
    currency: str = hass.config.currency

    entities: list[SensorEntity] = []

    # Statistics sensors
    for description in STATISTICS_SENSOR_DESCRIPTIONS:
        entities.append(
            HomeBoxStatisticsSensor(
                coordinator=coordinator,
                config_entry=config_entry,
                description=description,
                currency=currency,
            )
        )

    # Linked device diagnostic sensors
    ha_device_to_hb_item, _ = get_link_maps(config_entry)
    for ha_device_id, hb_item_id in ha_device_to_hb_item.items():
        entities.append(
            HomeBoxLinkedDeviceSensor(
                coordinator=coordinator,
                config_entry=config_entry,
                ha_device_id=ha_device_id,
                hb_item_id=hb_item_id,
            )
        )

    async_add_entities(entities)


class HomeBoxStatisticsSensor(
    CoordinatorEntity[HomeBoxCoordinator], SensorEntity
):
    """Sensor for Homebox group statistics."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HomeBoxCoordinator,
        config_entry: HomeBoxConfigEntry,
        description: SensorEntityDescription,
        currency: str,
    ) -> None:
        """Initialize the statistics sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{config_entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, config_entry.entry_id)},
            name=config_entry.title,
            manufacturer="Homebox",
            model="Inventory",
            entry_type=DeviceEntryType.SERVICE,
        )

        # Apply currency unit to the total_value sensor
        if description.key == "total_value":
            self._attr_native_unit_of_measurement = currency

    @property
    def native_value(self) -> int | float | None:
        """Return the sensor value from coordinator data."""
        data: HomeBoxCoordinatorData | None = self.coordinator.data
        if data is None:
            return None

        stats = data.statistics
        key = self.entity_description.key

        if key == "total_items":
            return stats.total_items
        if key == "total_locations":
            return stats.total_locations
        if key == "total_value":
            return round(stats.total_value, 2)

        return None


class HomeBoxLinkedDeviceSensor(
    CoordinatorEntity[HomeBoxCoordinator], SensorEntity
):
    """Diagnostic sensor showing the Homebox item ID linked to an HA device."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:link-variant"

    def __init__(
        self,
        coordinator: HomeBoxCoordinator,
        config_entry: HomeBoxConfigEntry,
        ha_device_id: str,
        hb_item_id: str,
    ) -> None:
        """Initialize the linked device sensor."""
        super().__init__(coordinator)
        self._ha_device_id = ha_device_id
        self._hb_item_id = hb_item_id
        self._attr_unique_id = f"{ha_device_id}_homebox_link"
        self._attr_translation_key = "homebox_link"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, config_entry.entry_id)},
        )

    @property
    def native_value(self) -> str | None:
        """Return the Homebox item ID."""
        return self._hb_item_id
