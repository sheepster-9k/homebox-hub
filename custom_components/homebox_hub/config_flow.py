"""Config flow and options flow for the Homebox Hub integration."""

from __future__ import annotations

import logging
from difflib import SequenceMatcher
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithConfigEntry,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, device_registry as dr, selector
from .api import (
    HomeBoxApiClient,
    HomeBoxApiError,
    HomeBoxAuthenticationError,
    HomeBoxConnectionError,
    HomeBoxImageContentTypeError,
    HomeBoxImageDownloadError,
    HomeBoxImageTooLargeError,
    HomeBoxInvalidImageUrlError,
    normalize_homebox_host,
)
from .item_fields import build_item_update_payload, extract_item_fields
from .const import (
    CONF_AREA,
    CONF_HA_DEVICE_ID,
    CONF_HB_ITEM_DESCRIPTION,
    CONF_HB_ITEM_ID,
    CONF_HB_ITEM_IMAGE_URL,
    CONF_HB_ITEM_MANUFACTURER,
    CONF_HB_ITEM_MODEL_NUMBER,
    CONF_HB_ITEM_NAME,
    CONF_HB_ITEM_PURCHASE_PRICE,
    CONF_HB_ITEM_SERIAL_NUMBER,
    CONF_LLM_BACKEND,
    CONF_LLM_MODEL,
    CONF_LLM_URL,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_URL,
    DEFAULT_NAME,
    DOMAIN,
    LLM_BACKEND_OLLAMA,
    LLM_BACKEND_OPENCLAW,
)
from .linking import (
    apply_link,
    async_cleanup_unlinked_hb_backlinks,
    get_link_maps,
    remove_link,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fuzzy matching threshold for device-to-item suggestions
# ---------------------------------------------------------------------------

_FUZZY_MATCH_THRESHOLD = 0.5


# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate invalid auth."""

    def __init__(self, detail: str = "Unknown authentication error") -> None:
        """Initialize with a detail message."""
        super().__init__(detail)
        self.detail = detail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_user_schema(
    defaults: dict[str, Any] | None = None,
) -> vol.Schema:
    """Build the user setup form schema."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_HOST, default=defaults.get(CONF_HOST, "")
            ): str,
            vol.Required(
                CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")
            ): str,
            vol.Required(
                CONF_PASSWORD, default=defaults.get(CONF_PASSWORD, "")
            ): str,
            vol.Optional(
                CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME)
            ): str,
            vol.Optional(CONF_AREA): selector.AreaSelector(),
        }
    )


def _get_ha_devices(
    hass: Any,
    config_entry: ConfigEntry,
) -> dict[str, str]:
    """Return a dict of {device_id: display_name} for all HA devices.

    Excludes devices that are already linked via the integration.
    """
    dev_reg = dr.async_get(hass)
    ha_device_to_hb_item, _ = get_link_maps(config_entry)
    devices: dict[str, str] = {}

    for device in dev_reg.devices.values():
        if device.id in ha_device_to_hb_item:
            continue
        name = device.name_by_user or device.name or device.id
        devices[device.id] = name

    return devices


def _get_linked_ha_devices(
    hass: Any,
    config_entry: ConfigEntry,
) -> dict[str, str]:
    """Return a dict of {device_id: display_name} for linked HA devices."""
    dev_reg = dr.async_get(hass)
    ha_device_to_hb_item, _ = get_link_maps(config_entry)
    devices: dict[str, str] = {}

    for ha_device_id in ha_device_to_hb_item:
        device = dev_reg.async_get(ha_device_id)
        if device is not None:
            name = device.name_by_user or device.name or device.id
            devices[ha_device_id] = name
        else:
            devices[ha_device_id] = f"(removed) {ha_device_id}"

    return devices


def _fuzzy_best_match(
    query: str,
    candidates: dict[str, str],
) -> str | None:
    """Return the key of the best fuzzy match above the threshold, or None."""
    best_key: str | None = None
    best_ratio = 0.0
    query_lower = query.lower()

    for key, name in candidates.items():
        ratio = SequenceMatcher(None, query_lower, name.lower()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_key = key

    if best_ratio >= _FUZZY_MATCH_THRESHOLD:
        return best_key
    return None


def _get_api(
    hass: Any,
    data: dict[str, Any],
) -> HomeBoxApiClient:
    """Create an API client from config data."""
    return HomeBoxApiClient(
        hass=hass,
        host=data[CONF_HOST],
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
    )


def _get_api_from_entry(
    hass: Any,
    config_entry: ConfigEntry,
) -> HomeBoxApiClient:
    """Create an API client from a config entry."""
    return HomeBoxApiClient(
        hass=hass,
        host=config_entry.data[CONF_HOST],
        username=config_entry.data[CONF_USERNAME],
        password=config_entry.data[CONF_PASSWORD],
    )


# ---------------------------------------------------------------------------
# Setup flow
# ---------------------------------------------------------------------------


class HomeBoxHubConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Homebox Hub."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the initial setup step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                host = normalize_homebox_host(user_input[CONF_HOST])
                user_input[CONF_HOST] = host

                api = _get_api(self.hass, user_input)
                await api.async_authenticate()
                stats = await api.async_get_group_statistics()
                _LOGGER.debug(
                    "Connected to Homebox at %s — %d total items",
                    host,
                    stats.total_items,
                )

            except HomeBoxAuthenticationError as err:
                _LOGGER.warning("Authentication failed: %s", err)
                errors["base"] = "invalid_auth"
            except HomeBoxConnectionError as err:
                _LOGGER.warning("Connection failed: %s", err)
                errors["base"] = "cannot_connect"
            except HomeBoxApiError as err:
                _LOGGER.warning("API error during setup: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during setup")
                errors["base"] = "unknown"
            else:
                # Set unique ID to the normalized host URL
                await self.async_set_unique_id(host)
                self._abort_if_unique_id_configured()

                title = user_input.get(CONF_NAME, DEFAULT_NAME)

                data: dict[str, Any] = {
                    CONF_HOST: host,
                    CONF_USERNAME: user_input[CONF_USERNAME],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_NAME: title,
                }
                if user_input.get(CONF_AREA):
                    data[CONF_AREA] = user_input[CONF_AREA]

                return self.async_create_entry(title=title, data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=_build_user_schema(user_input),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> HomeBoxOptionsFlow:
        """Return the options flow handler."""
        return HomeBoxOptionsFlow(config_entry)


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


class HomeBoxOptionsFlow(OptionsFlowWithConfigEntry):
    """Handle options for Homebox Hub."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        super().__init__(config_entry)
        # Transient state for the create-item wizard
        self._created_hb_item_id: str | None = None
        self._selected_ha_device_id: str | None = None

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show the options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "configure_llm",
                "create_hb_item_from_ha_device",
                "link_ha_device",
                "unlink_ha_device",
                "resync",
            ],
        )

    # ------------------------------------------------------------------
    # Configure LLM
    # ------------------------------------------------------------------

    async def async_step_configure_llm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Configure the LLM backend for conversation agent."""
        if user_input is not None:
            new_options = dict(self.options)
            new_options[CONF_LLM_BACKEND] = user_input[CONF_LLM_BACKEND]
            new_options[CONF_LLM_URL] = user_input[CONF_LLM_URL]
            new_options[CONF_LLM_MODEL] = user_input[CONF_LLM_MODEL]
            return self.async_create_entry(title="", data=new_options)

        current_backend = self.options.get(CONF_LLM_BACKEND, LLM_BACKEND_OLLAMA)
        current_url = self.options.get(CONF_LLM_URL, DEFAULT_LLM_URL)
        current_model = self.options.get(CONF_LLM_MODEL, DEFAULT_LLM_MODEL)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_LLM_BACKEND, default=current_backend
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=LLM_BACKEND_OLLAMA, label="Ollama"
                            ),
                            selector.SelectOptionDict(
                                value=LLM_BACKEND_OPENCLAW, label="OpenClaw"
                            ),
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_LLM_URL, default=current_url
                ): str,
                vol.Required(
                    CONF_LLM_MODEL, default=current_model
                ): str,
            }
        )

        return self.async_show_form(
            step_id="configure_llm",
            data_schema=schema,
        )

    # ------------------------------------------------------------------
    # Link HA device to existing Homebox item
    # ------------------------------------------------------------------

    async def async_step_link_ha_device(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Link an existing Homebox item to an HA device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            ha_device_id = user_input[CONF_HA_DEVICE_ID]
            hb_item_id = user_input[CONF_HB_ITEM_ID]

            try:
                api = _get_api_from_entry(self.hass, self.config_entry)
                await api.async_authenticate()
                new_options = await apply_link(
                    self.hass,
                    self.config_entry,
                    api,
                    ha_device_id,
                    hb_item_id,
                )
                return self.async_create_entry(title="", data=new_options)
            except ValueError as err:
                _LOGGER.warning("Link conflict: %s", err)
                errors["base"] = "already_linked"
            except HomeBoxApiError as err:
                _LOGGER.warning("API error during linking: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during linking")
                errors["base"] = "unknown"

        # Build device and item selectors
        ha_devices = _get_ha_devices(self.hass, self.config_entry)
        if not ha_devices:
            return self.async_abort(reason="no_devices_available")

        # Fetch Homebox items tagged with HomeAssistant
        try:
            api = _get_api_from_entry(self.hass, self.config_entry)
            await api.async_authenticate()
            tagged_items = await api.async_get_items_by_tag()
        except HomeBoxApiError:
            return self.async_abort(reason="cannot_connect")

        _, hb_item_to_ha_device = get_link_maps(self.config_entry)
        unlinked_items = {
            item.item_id: item.name
            for item in tagged_items
            if item.item_id not in hb_item_to_ha_device
        }

        if not unlinked_items:
            return self.async_abort(reason="no_unlinked_items")

        schema = vol.Schema(
            {
                vol.Required(CONF_HA_DEVICE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=did, label=dname)
                            for did, dname in sorted(
                                ha_devices.items(), key=lambda x: x[1]
                            )
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_HB_ITEM_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=iid, label=iname)
                            for iid, iname in sorted(
                                unlinked_items.items(), key=lambda x: x[1]
                            )
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="link_ha_device",
            data_schema=schema,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Unlink HA device from Homebox item
    # ------------------------------------------------------------------

    async def async_step_unlink_ha_device(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Remove a link between an HA device and a Homebox item."""
        errors: dict[str, str] = {}

        if user_input is not None:
            ha_device_id = user_input[CONF_HA_DEVICE_ID]
            ha_device_to_hb_item, _ = get_link_maps(self.config_entry)
            hb_item_id = ha_device_to_hb_item.get(ha_device_id)

            if hb_item_id is None:
                errors["base"] = "not_linked"
            else:
                try:
                    api = _get_api_from_entry(self.hass, self.config_entry)
                    await api.async_authenticate()
                    new_options = await remove_link(
                        self.hass,
                        self.config_entry,
                        api,
                        ha_device_id,
                        hb_item_id,
                    )
                    return self.async_create_entry(title="", data=new_options)
                except HomeBoxApiError as err:
                    _LOGGER.warning("API error during unlinking: %s", err)
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error during unlinking")
                    errors["base"] = "unknown"

        linked_devices = _get_linked_ha_devices(self.hass, self.config_entry)
        if not linked_devices:
            return self.async_abort(reason="no_linked_devices")

        schema = vol.Schema(
            {
                vol.Required(CONF_HA_DEVICE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=did, label=dname)
                            for did, dname in sorted(
                                linked_devices.items(), key=lambda x: x[1]
                            )
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="unlink_ha_device",
            data_schema=schema,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Resync
    # ------------------------------------------------------------------

    async def async_step_resync(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Refresh tagged items and clean stale backlinks."""
        try:
            api = _get_api_from_entry(self.hass, self.config_entry)
            await api.async_authenticate()
            cleaned, new_options = await async_cleanup_unlinked_hb_backlinks(
                self.hass,
                self.config_entry,
                api,
            )
            _LOGGER.info("Resync complete — cleaned %d stale backlinks", cleaned)

            if new_options is not None:
                return self.async_create_entry(title="", data=new_options)

            # No changes needed — just return to the options menu
            return self.async_create_entry(title="", data=dict(self.options))

        except HomeBoxApiError as err:
            _LOGGER.warning("API error during resync: %s", err)
            return self.async_abort(reason="cannot_connect")
        except Exception:
            _LOGGER.exception("Unexpected error during resync")
            return self.async_abort(reason="unknown")

    # ------------------------------------------------------------------
    # Create Homebox item from HA device — Step 1: select device
    # ------------------------------------------------------------------

    async def async_step_create_hb_item_from_ha_device(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Wizard step 1: select an HA device to create a Homebox item for."""
        if user_input is not None:
            self._selected_ha_device_id = user_input[CONF_HA_DEVICE_ID]
            return await self.async_step_create_hb_item_details()

        ha_devices = _get_ha_devices(self.hass, self.config_entry)
        if not ha_devices:
            return self.async_abort(reason="no_devices_available")

        schema = vol.Schema(
            {
                vol.Required(CONF_HA_DEVICE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=did, label=dname)
                            for did, dname in sorted(
                                ha_devices.items(), key=lambda x: x[1]
                            )
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="create_hb_item_from_ha_device",
            data_schema=schema,
        )

    # ------------------------------------------------------------------
    # Create Homebox item from HA device — Step 2: item details
    # ------------------------------------------------------------------

    async def async_step_create_hb_item_details(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Wizard step 2: fill in Homebox item details and create it."""
        errors: dict[str, str] = {}
        ha_device_id = self._selected_ha_device_id

        if ha_device_id is None:
            return self.async_abort(reason="no_device_selected")

        if user_input is not None:
            return await self._async_create_hb_item(ha_device_id, user_input)

        # Pre-populate from the HA device
        device = dr.async_get(self.hass).async_get(ha_device_id)

        defaults: dict[str, Any] = {}
        if device is not None:
            defaults[CONF_HB_ITEM_NAME] = (
                device.name_by_user or device.name or ""
            )
            defaults[CONF_HB_ITEM_MANUFACTURER] = device.manufacturer or ""
            defaults[CONF_HB_ITEM_MODEL_NUMBER] = device.model or ""
            defaults[CONF_HB_ITEM_SERIAL_NUMBER] = device.serial_number or ""

        # Try fuzzy matching against existing Homebox items for suggestions
        try:
            api = _get_api_from_entry(self.hass, self.config_entry)
            await api.async_authenticate()
            all_items = await api.async_get_all_items()
            item_names = {item.item_id: item.name for item in all_items}
            suggested_id = _fuzzy_best_match(
                defaults.get(CONF_HB_ITEM_NAME, ""), item_names
            )
            if suggested_id is not None:
                _LOGGER.debug(
                    "Fuzzy match suggestion: device '%s' -> item '%s' (%s)",
                    defaults.get(CONF_HB_ITEM_NAME),
                    item_names.get(suggested_id),
                    suggested_id,
                )
        except Exception:
            _LOGGER.debug("Could not fetch items for fuzzy matching")

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_HB_ITEM_NAME,
                    default=defaults.get(CONF_HB_ITEM_NAME, ""),
                ): str,
                vol.Optional(
                    CONF_HB_ITEM_MANUFACTURER,
                    default=defaults.get(CONF_HB_ITEM_MANUFACTURER, ""),
                ): str,
                vol.Optional(
                    CONF_HB_ITEM_MODEL_NUMBER,
                    default=defaults.get(CONF_HB_ITEM_MODEL_NUMBER, ""),
                ): str,
                vol.Optional(
                    CONF_HB_ITEM_SERIAL_NUMBER,
                    default=defaults.get(CONF_HB_ITEM_SERIAL_NUMBER, ""),
                ): str,
                vol.Optional(
                    CONF_HB_ITEM_DESCRIPTION, default=""
                ): str,
                vol.Optional(
                    CONF_HB_ITEM_PURCHASE_PRICE, default=""
                ): str,
                vol.Optional(
                    CONF_HB_ITEM_IMAGE_URL, default=""
                ): str,
            }
        )

        return self.async_show_form(
            step_id="create_hb_item_details",
            data_schema=schema,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Internal: create the Homebox item, tag, update details, link
    # ------------------------------------------------------------------

    async def _async_create_hb_item(
        self,
        ha_device_id: str,
        user_input: dict[str, Any],
    ) -> ConfigFlowResult:
        """Create a Homebox item, set details, upload image, and link."""
        errors: dict[str, str] = {}
        created_item_id: str | None = None

        try:
            api = _get_api_from_entry(self.hass, self.config_entry)
            await api.async_authenticate()

            # 1. Create the item with basic name
            item_name = user_input[CONF_HB_ITEM_NAME]
            created = await api.async_create_item({"name": item_name})
            created_item_id = created["id"]
            _LOGGER.debug(
                "Created Homebox item '%s' with id %s", item_name, created_item_id
            )

            # 2. Ensure the HomeAssistant tag exists and add it
            tag = await api.async_ensure_tag("HomeAssistant")
            tag_id = tag["id"]

            # Fetch the full item so we can build a proper update payload
            full_item = await api.async_get_item(created_item_id)
            fields = extract_item_fields(full_item)
            update_payload = build_item_update_payload(full_item, fields)

            # Set user-provided details
            update_payload["name"] = item_name

            manufacturer = user_input.get(CONF_HB_ITEM_MANUFACTURER, "")
            if manufacturer:
                update_payload["manufacturer"] = manufacturer

            model_number = user_input.get(CONF_HB_ITEM_MODEL_NUMBER, "")
            if model_number:
                update_payload["modelNumber"] = model_number

            serial_number = user_input.get(CONF_HB_ITEM_SERIAL_NUMBER, "")
            if serial_number:
                update_payload["serialNumber"] = serial_number

            description = user_input.get(CONF_HB_ITEM_DESCRIPTION, "")
            if description:
                update_payload["description"] = description

            purchase_price = user_input.get(CONF_HB_ITEM_PURCHASE_PRICE, "")
            if purchase_price:
                try:
                    update_payload["purchasePrice"] = float(purchase_price)
                except (ValueError, TypeError):
                    _LOGGER.warning(
                        "Invalid purchase price '%s', skipping", purchase_price
                    )

            # Ensure the HomeAssistant label is in labelIds
            existing_label_ids: list[str] = update_payload.get("labelIds", [])
            if tag_id not in existing_label_ids:
                existing_label_ids.append(tag_id)
                update_payload["labelIds"] = existing_label_ids

            await api.async_update_item(created_item_id, update_payload)

            # 3. Upload image if provided
            image_url = user_input.get(CONF_HB_ITEM_IMAGE_URL, "")
            if image_url:
                try:
                    await api.async_upload_image_from_url(
                        created_item_id, image_url
                    )
                    _LOGGER.debug(
                        "Uploaded image for item %s from %s",
                        created_item_id,
                        image_url,
                    )
                except (
                    HomeBoxInvalidImageUrlError,
                    HomeBoxImageDownloadError,
                    HomeBoxImageTooLargeError,
                    HomeBoxImageContentTypeError,
                ) as img_err:
                    _LOGGER.warning(
                        "Image upload failed for item %s: %s",
                        created_item_id,
                        img_err,
                    )
                    # Non-fatal: continue with the link

            # 4. Apply the bidirectional link
            new_options = await apply_link(
                self.hass,
                self.config_entry,
                api,
                ha_device_id,
                created_item_id,
            )
            return self.async_create_entry(title="", data=new_options)

        except HomeBoxAuthenticationError as err:
            _LOGGER.warning("Auth error creating item: %s", err)
            errors["base"] = "invalid_auth"
        except HomeBoxConnectionError as err:
            _LOGGER.warning("Connection error creating item: %s", err)
            errors["base"] = "cannot_connect"
        except HomeBoxApiError as err:
            _LOGGER.warning("API error creating item: %s", err)
            errors["base"] = "cannot_connect"
        except ValueError as err:
            _LOGGER.warning("Link conflict creating item: %s", err)
            errors["base"] = "already_linked"
        except Exception:
            _LOGGER.exception("Unexpected error creating item")
            errors["base"] = "unknown"

        # Rollback: delete the partially created item if we got an error
        if created_item_id is not None:
            _LOGGER.info(
                "Rolling back: deleting partially created Homebox item %s",
                created_item_id,
            )
            try:
                await api.async_delete_item(created_item_id)
            except Exception:
                _LOGGER.warning(
                    "Failed to rollback Homebox item %s", created_item_id
                )

        # Show the details form again with errors
        return self.async_show_form(
            step_id="create_hb_item_details",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HB_ITEM_NAME,
                        default=user_input.get(CONF_HB_ITEM_NAME, ""),
                    ): str,
                    vol.Optional(
                        CONF_HB_ITEM_MANUFACTURER,
                        default=user_input.get(CONF_HB_ITEM_MANUFACTURER, ""),
                    ): str,
                    vol.Optional(
                        CONF_HB_ITEM_MODEL_NUMBER,
                        default=user_input.get(CONF_HB_ITEM_MODEL_NUMBER, ""),
                    ): str,
                    vol.Optional(
                        CONF_HB_ITEM_SERIAL_NUMBER,
                        default=user_input.get(CONF_HB_ITEM_SERIAL_NUMBER, ""),
                    ): str,
                    vol.Optional(
                        CONF_HB_ITEM_DESCRIPTION,
                        default=user_input.get(CONF_HB_ITEM_DESCRIPTION, ""),
                    ): str,
                    vol.Optional(
                        CONF_HB_ITEM_PURCHASE_PRICE,
                        default=user_input.get(CONF_HB_ITEM_PURCHASE_PRICE, ""),
                    ): str,
                    vol.Optional(
                        CONF_HB_ITEM_IMAGE_URL,
                        default=user_input.get(CONF_HB_ITEM_IMAGE_URL, ""),
                    ): str,
                }
            ),
            errors=errors,
        )
