"""Constants for the Homebox Hub integration."""

from datetime import timedelta

DOMAIN = "homebox_hub"
CONF_AREA = "area"
DEFAULT_NAME = "Homebox Hub"

# Linking
CONF_LINKS = "links"
CONF_HA_DEVICE_TO_HB_ITEM = "ha_device_to_hb_item"
CONF_HB_ITEM_TO_HA_DEVICE = "hb_item_to_ha_device"
CONF_HB_ITEM_ID = "hb_item_id"
CONF_HA_DEVICE_ID = "ha_device_id"
CONF_HB_ITEM_DESCRIPTION = "hb_item_description"
CONF_HB_ITEM_IMAGE_URL = "hb_item_image_url"
CONF_HB_ITEM_MANUFACTURER = "hb_item_manufacturer"
CONF_HB_ITEM_MODEL_NUMBER = "hb_item_model_number"
CONF_HB_ITEM_SERIAL_NUMBER = "hb_item_serial_number"
CONF_HB_ITEM_NAME = "hb_item_name"
CONF_HB_ITEM_PURCHASE_PRICE = "hb_item_purchase_price"

# Tags / backlinks
LINK_TAG_NAME = "HomeAssistant"
LINK_BACKLINK_FIELD_NAME = "Home Assistant Device URL"

# API
API_BASE_PATH = "/api"
DEFAULT_POLL_INTERVAL = timedelta(minutes=5)

# Conversation / LLM
CONF_LLM_BACKEND = "llm_backend"
CONF_LLM_URL = "llm_url"
CONF_LLM_MODEL = "llm_model"
LLM_BACKEND_OLLAMA = "ollama"
LLM_BACKEND_OPENCLAW = "openclaw"
