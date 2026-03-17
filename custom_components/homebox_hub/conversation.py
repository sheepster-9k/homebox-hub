"""Conversation agent for the Homebox Hub integration.

Provides a Home Assistant conversation entity that answers natural language
questions about the user's Homebox inventory.  When a configured LLM backend
(Ollama / OpenClaw) is reachable the query is enriched with inventory context
and forwarded to the model.  If the LLM is unavailable, a simple keyword-based
fallback produces a direct answer from the inventory data.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import aiohttp

from homeassistant.components import conversation
from homeassistant.components.conversation import ChatLog
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import intent

from .const import (
    DOMAIN,
    CONF_LLM_BACKEND,
    CONF_LLM_MODEL,
    CONF_LLM_URL,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_URL,
    LLM_BACKEND_OPENCLAW,
    MAX_QUERY_LENGTH,
)
from .coordinator import HomeBoxCoordinator

_LOGGER = logging.getLogger(__name__)

_THINKING_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_LLM_TIMEOUT = aiohttp.ClientTimeout(total=120)
_RATE_LIMIT_SECONDS = 2.0  # minimum seconds between queries

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
        self._last_query_time: float = 0.0
        self._attr_unique_id = f"{config_entry.entry_id}_conversation"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, config_entry.entry_id)},
            name=config_entry.title,
            manufacturer="Homebox",
            model="Inventory",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def _llm_url(self) -> str:
        """Return the configured LLM base URL."""
        return self._config_entry.options.get(CONF_LLM_URL, DEFAULT_LLM_URL)

    @property
    def _llm_model(self) -> str:
        """Return the configured LLM model name."""
        return self._config_entry.options.get(CONF_LLM_MODEL, DEFAULT_LLM_MODEL)

    # ------------------------------------------------------------------
    # Conversation handling (modern HA 2025.12+ API)
    # ------------------------------------------------------------------

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: ChatLog,
    ) -> conversation.ConversationResult:
        """Handle the user's natural language query."""
        query = user_input.text

        # Rate limit to prevent flooding the LLM / Homebox backend
        now = time.monotonic()
        if now - self._last_query_time < _RATE_LIMIT_SECONDS:
            intent_response = intent.IntentResponse(
                language=user_input.language
            )
            intent_response.async_set_speech(
                "Please wait a moment before sending another query."
            )
            return conversation.ConversationResult(
                response=intent_response,
                conversation_id=chat_log.conversation_id,
            )
        self._last_query_time = now

        # Truncate excessively long queries
        if len(query) > MAX_QUERY_LENGTH:
            query = query[:MAX_QUERY_LENGTH]

        api = self._coordinator.api

        # Use coordinator's cached data for totals; only do a targeted search
        try:
            all_items, locations = await asyncio.gather(
                api.async_get_all_items(),
                api.async_get_locations(),
            )
        except Exception:
            _LOGGER.exception("Failed to fetch inventory data from Homebox")
            all_items = []
            locations = []

        matched = _search_items(all_items, query)
        context = _build_inventory_context(matched, all_items, locations)

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
            # Reuse already-fetched data instead of re-fetching
            response_text = _keyword_response(matched, len(all_items), len(locations))

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(response_text)

        return conversation.ConversationResult(
            response=intent_response,
            conversation_id=chat_log.conversation_id,
        )

    # ------------------------------------------------------------------
    # LLM query (unified Ollama / OpenClaw)
    # ------------------------------------------------------------------

    async def _query_llm(self, system_prompt: str, user_message: str) -> str:
        """Send a chat completion request to the configured LLM endpoint."""
        llm_url = self._llm_url
        if not llm_url.startswith(("http://", "https://")):
            raise ValueError("LLM URL must use http:// or https:// scheme")

        session = async_get_clientsession(self.hass)
        backend = self._config_entry.options.get(CONF_LLM_BACKEND)
        is_openclaw = backend == LLM_BACKEND_OPENCLAW

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        if is_openclaw:
            url = f"{llm_url.rstrip('/')}/v1/chat/completions"
            payload: dict[str, Any] = {"model": self._llm_model, "messages": messages}
        else:
            url = f"{llm_url.rstrip('/')}/api/chat"
            payload = {"model": self._llm_model, "messages": messages, "stream": False}

        async with session.post(url, json=payload, timeout=_LLM_TIMEOUT) as resp:
            resp.raise_for_status()
            data = await resp.json()

        if is_openclaw:
            content: str = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        else:
            content = data.get("message", {}).get("content", "")

        if not content:
            raise ValueError("Empty response from LLM")

        # Strip <think>...</think> blocks emitted by qwen3 models.
        return _THINKING_RE.sub("", content).strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_inventory_context(
    matched: list[tuple[str, str]],
    all_items: list[Any],
    locations: list[dict[str, Any]],
) -> str:
    """Build a concise inventory context string for the LLM."""
    parts: list[str] = []

    location_names = [loc.get("name", "Unknown") for loc in locations]
    if location_names:
        parts.append(f"Locations: {', '.join(location_names)}")

    if matched:
        formatted = [f"{name} ({loc})" for name, loc in matched]
        parts.append(f"Found items: {', '.join(formatted)}")

    parts.append(
        f"Total: {len(all_items)} items across {len(locations)} locations"
    )
    return "\n".join(parts)


def _search_items(
    items: list[Any],
    query: str,
) -> list[tuple[str, str]]:
    """Return (name, location_name) pairs for items matching the query."""
    keywords = query.lower().split()
    results: list[tuple[str, str]] = []

    for item in items:
        if any(kw in item.name.lower() for kw in keywords):
            results.append((item.name, item.location_name or "Unknown"))

    return results


def _keyword_response(
    matched: list[tuple[str, str]],
    total_items: int,
    total_locations: int,
) -> str:
    """Format a plain-text response from keyword-matched items."""
    if matched:
        lines = [f"- {name} ({loc})" for name, loc in matched]
        return f"I found {len(matched)} matching item(s):\n" + "\n".join(lines)

    return (
        f"I couldn't find any items matching your query. "
        f"There are {total_items} items across {total_locations} locations."
    )
