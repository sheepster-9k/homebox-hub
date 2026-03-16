"""DataUpdateCoordinator for the Homebox Hub integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import HomeBoxApiClient, HomeBoxApiError, HomeBoxAuthenticationError, HomeBoxConnectionError
from .const import DEFAULT_POLL_INTERVAL, DOMAIN
from .linking import HomeBoxLinkScanResult, HomeBoxTaggedItem, scan_tagged_items_for_links
from .models import HomeBoxGroupStatistics

_LOGGER = logging.getLogger(__name__)

type HomeBoxConfigEntry = ConfigEntry[HomeBoxCoordinator]


@dataclass
class HomeBoxCoordinatorData:
    """Data returned by the Homebox coordinator."""

    statistics: HomeBoxGroupStatistics
    unlinked_hb_items: list[HomeBoxTaggedItem]


class HomeBoxCoordinator(DataUpdateCoordinator[HomeBoxCoordinatorData]):
    """Coordinator that polls Homebox for statistics and link state."""

    config_entry: HomeBoxConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: HomeBoxConfigEntry,
        api: HomeBoxApiClient,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: Home Assistant instance.
            config_entry: The config entry for this integration.
            api: Authenticated Homebox API client.

        """
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_POLL_INTERVAL,
            config_entry=config_entry,
        )
        self.api = api

    async def _async_update_data(self) -> HomeBoxCoordinatorData:
        """Fetch statistics and link scan data from Homebox."""
        try:
            statistics = await self.api.async_get_group_statistics()
            scan_result: HomeBoxLinkScanResult = await scan_tagged_items_for_links(
                self.api, self.config_entry
            )
        except HomeBoxAuthenticationError:
            # Re-authenticate and retry once before failing
            try:
                await self.api.async_authenticate()
                statistics = await self.api.async_get_group_statistics()
                scan_result = await scan_tagged_items_for_links(
                    self.api, self.config_entry
                )
            except HomeBoxAuthenticationError as err:
                raise UpdateFailed(
                    f"Authentication failed after re-authentication: {err}"
                ) from err
            except HomeBoxConnectionError as err:
                raise UpdateFailed(
                    f"Cannot connect to Homebox: {err}"
                ) from err
            except HomeBoxApiError as err:
                raise UpdateFailed(
                    f"Error communicating with Homebox: {err}"
                ) from err
        except HomeBoxConnectionError as err:
            raise UpdateFailed(
                f"Cannot connect to Homebox: {err}"
            ) from err
        except HomeBoxApiError as err:
            raise UpdateFailed(
                f"Error communicating with Homebox: {err}"
            ) from err

        return HomeBoxCoordinatorData(
            statistics=statistics,
            unlinked_hb_items=scan_result.unlinked_hb_items,
        )
