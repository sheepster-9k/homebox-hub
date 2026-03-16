"""Conversation agent for the Homebox Hub integration.

Provides a Home Assistant conversation entity that answers natural language
questions about the user's Homebox inventory.  When a configured LLM backend
(Ollama / OpenClaw) is reachable the query is enriched with inventory context
and forwarded to the model.  If the LLM is unavailable, a simple keyword-based
fallback produces a direct answer from the inventory data.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_LLM_BACKEND, CONF_LLM_MODEL, CONF_LLM_URL, DOMAIN, LLM_BACKEND_OLLAMA
from .coordinator import HomeBoxCoordinator

_LOGGER = logging.getLogger(__name__)

_THINKING_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

_DEFAULT_LLM_URL = "http://192.168.1.146:11434"
_DEFAULT_LLM_MODEL = "qwen3-vl:30b"
_LLM_TIMEOUT = aiohttp.ClientTimeout(total=120)

_SYSTEM_PROMPT = (
    "You are a home inventory assistant connected to Homebox. "
    "Answer questions about item locations, quantities, and details "
    "based on the inventory data provided. Be concise and helpful. "
    "If you can't find something, say so clearly."
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Homebox conversation entity from a config entry."""
    coordinator: HomeBoxCoordinator = config_entry.runtime_data
    async_add_entities([HomeBoxConversationEntity(coordinator, config_entry)])


class HomeBoxConversationEntity(conversation.ConversationEntity):
    """Conversation agent that answers questions about Homebox inventory."""

    _attr_has_entity_name = True
    _attr_name = "Homebox Inventory Assistant"
    _attr_supported_languages = conversation.MATCH_ALL

    def __init__(
        self,
        coordinator: HomeBoxCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the conversation entity."""
        self._coordinator = coordinator
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_conversation"

    @property
    def _llm_url(self) -> str:
        """Return the configured LLM base URL."""
        return self._config_entry.options.get(CONF_LLM_URL, _DEFAULT_LLM_URL)

    @property
    def _llm_model(self) -> str:
        """Return the configured LLM model name."""
        return self._config_entry.options.get(CONF_LLM_MODEL, _DEFAULT_LLM_MODEL)

    # ------------------------------------------------------------------
    # Conversation handling
    # ------------------------------------------------------------------

    async def async_process(
        self,
        user_input: conversation.ConversationInput,
    ) -> conversation.ConversationResult:
        """Process the user's natural language query."""
        query = user_input.text

        try:
            context = await self._build_inventory_context(query)
        except Exception:
            _LOGGER.exception("Failed to fetch inventory context from Homebox")
            context = "Inventory data is temporarily unavailable."

        # Try LLM first; fall back to keyword search if unavailable.
        try:
            user_message = (
                f"Inventory context:\n{context}\n\nUser question: {query}"
            )
            response_text = await self._query_llm(_SYSTEM_PROMPT, user_message)
        except Exception:
            _LOGGER.debug(
                "LLM unavailable, falling back to keyword search", exc_info=True
            )
            response_text = await self._keyword_fallback(query)

        intent_response = conversation.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(response_text)

        return conversation.ConversationResult(
            response=intent_response,
            conversation_id=user_input.conversation_id,
        )

    # ------------------------------------------------------------------
    # Inventory context builder
    # ------------------------------------------------------------------

    async def _build_inventory_context(self, query: str) -> str:
        """Build a concise inventory context string for the LLM."""
        api = self._coordinator.api
        parts: list[str] = []

        # Locations
        locations = await api.async_get_locations()
        location_names = [loc.get("name", "Unknown") for loc in locations]
        location_map: dict[str, str] = {
            loc.get("id", ""): loc.get("name", "Unknown") for loc in locations
        }
        if location_names:
            parts.append(f"Locations: {', '.join(location_names)}")

        # All items (used for search and stats)
        all_items = await api.async_get_all_items()

        # Search for items matching the query
        matched = _search_items(all_items, query, location_map)
        if matched:
            formatted = [
                f"{name} ({loc})" for name, loc in matched
            ]
            parts.append(f"Found items: {', '.join(formatted)}")

        # Basic statistics
        parts.append(
            f"Total: {len(all_items)} items across {len(locations)} locations"
        )

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # LLM query
    # ------------------------------------------------------------------

    async def _query_llm(self, system_prompt: str, user_message: str) -> str:
        """Send a chat completion request to the configured LLM endpoint."""
        session = async_get_clientsession(self.hass)
        url = f"{self._llm_url.rstrip('/')}/api/chat"
        payload: dict[str, Any] = {
            "model": self._llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
        }

        async with session.post(url, json=payload, timeout=_LLM_TIMEOUT) as resp:
            resp.raise_for_status()
            data = await resp.json()

        content: str = data.get("message", {}).get("content", "")
        if not content:
            raise ValueError("Empty response from LLM")

        # Strip <think>...</think> blocks emitted by qwen3 models.
        content = _THINKING_RE.sub("", content).strip()
        return content

    # ------------------------------------------------------------------
    # Keyword fallback (no LLM)
    # ------------------------------------------------------------------

    async def _keyword_fallback(self, query: str) -> str:
        """Return a simple keyword-matched answer without an LLM."""
        api = self._coordinator.api

        try:
            locations = await api.async_get_locations()
            location_map: dict[str, str] = {
                loc.get("id", ""): loc.get("name", "Unknown") for loc in locations
            }
            all_items = await api.async_get_all_items()
        except Exception:
            _LOGGER.exception("Failed to query Homebox for fallback")
            return "Sorry, I couldn't reach the inventory system right now."

        matched = _search_items(all_items, query, location_map)
        if matched:
            lines = [f"- {name} ({loc})" for name, loc in matched]
            return f"I found {len(matched)} matching item(s):\n" + "\n".join(lines)

        return (
            f"I couldn't find any items matching your query. "
            f"There are {len(all_items)} items across {len(locations)} locations."
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _search_items(
    items: list[Any],
    query: str,
    location_map: dict[str, str],
) -> list[tuple[str, str]]:
    """Return (name, location_name) pairs for items matching the query."""
    keywords = query.lower().split()
    results: list[tuple[str, str]] = []

    for item in items:
        name: str = item.name if hasattr(item, "name") else str(item)
        name_lower = name.lower()

        if any(kw in name_lower for kw in keywords):
            # Resolve location from the full item data if available
            loc_name = _resolve_location(item, location_map)
            results.append((name, loc_name))

    return results


def _resolve_location(item: Any, location_map: dict[str, str]) -> str:
    """Best-effort extraction of location name from an item."""
    # HomeBoxItemSummary doesn't carry location directly — check raw fields
    # or fall back to "Unknown".
    if hasattr(item, "location"):
        loc = item.location
        if isinstance(loc, dict):
            return loc.get("name", "Unknown")
        if isinstance(loc, str) and loc in location_map:
            return location_map[loc]
    return "Unknown"
