"""Homebox API client for the Homebox Hub integration.

Based on JeffreyDissmann/ha-homebox with fixes for token refresh,
retry with backoff, and pagination support.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Any
from urllib.parse import urlparse

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.network import normalize_url

from .const import API_BASE_PATH, LINK_TAG_NAME
from .item_fields import (
    build_item_update_payload,
    extract_item_fields,
    merge_backlink_field,
)
from .models import HomeBoxGroupStatistics, HomeBoxItemSummary

_LOGGER = logging.getLogger(__name__)

MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_IMAGE_CONTENT_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)
DEFAULT_PAGE_SIZE = 50
MAX_PAGES = 200  # Safety cap: 200 pages * 50 = 10,000 items max
MAX_RETRIES = 1
RETRY_DELAY = 1.0  # seconds
MAX_ERROR_BODY_LENGTH = 200


def _sanitize_for_log(text: str) -> str:
    """Remove newlines, control chars, and ANSI escapes to prevent log injection."""
    import re
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
    return text.replace("\n", " ").replace("\r", " ")[:500]


def _is_private_or_loopback(hostname: str) -> bool:
    """Return True if hostname resolves to a private, loopback, or link-local IP."""
    try:
        addr = ipaddress.ip_address(hostname)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        pass
    # Check common private hostnames
    lower = hostname.lower()
    if lower in ("localhost", "localhost.localdomain"):
        return True
    # Hostnames ending in .local or .internal are considered private
    if lower.endswith((".local", ".internal")):
        return True
    return False


def _validate_image_url(image_url: str) -> None:
    """Validate that an image URL does not target private/internal networks.

    Raises:
        HomeBoxInvalidImageUrlError: If the URL targets a private network.

    """
    if not image_url or not image_url.startswith(("http://", "https://")):
        raise HomeBoxInvalidImageUrlError("Invalid image URL scheme")

    parsed = urlparse(image_url)
    hostname = parsed.hostname
    if not hostname:
        raise HomeBoxInvalidImageUrlError("Image URL has no hostname")

    if _is_private_or_loopback(hostname):
        raise HomeBoxInvalidImageUrlError(
            "Image URL must not target private or internal networks"
        )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class HomeBoxApiError(Exception):
    """Generic Homebox API error."""


# Alias for backward compatibility
HomeBoxApiClientError = HomeBoxApiError


class HomeBoxAuthenticationError(HomeBoxApiError):
    """Authentication failed (bad credentials or expired token)."""


class HomeBoxConnectionError(HomeBoxApiError):
    """Could not reach the Homebox server."""


class HomeBoxInvalidImageUrlError(HomeBoxApiError):
    """The provided image URL is invalid."""


class HomeBoxImageDownloadError(HomeBoxApiError):
    """Failed to download the image from the provided URL."""


class HomeBoxImageTooLargeError(HomeBoxApiError):
    """The image exceeds the maximum allowed size."""


class HomeBoxImageContentTypeError(HomeBoxApiError):
    """The image has an unsupported content type."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class HomeBoxApiClient:
    """Async client for the Homebox REST API.

    Supports two auth modes:
    - **Login mode** (default): username + password, auto-refreshes token on 401.
    - **Token mode**: static bearer token, no refresh.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        username: str = "",
        password: str = "",
        token: str = "",
    ) -> None:
        """Initialize the API client.

        Args:
            hass: Home Assistant instance.
            host: Homebox server URL (e.g. "http://homebox:7745").
            username: Homebox username (login mode).
            password: Homebox password (login mode).
            token: Static bearer token (token mode).

        """
        self._hass = hass
        self._host = normalize_url(host).rstrip("/")
        self._username = username
        self._password = password
        self._session: aiohttp.ClientSession = async_get_clientsession(hass)
        self._token: str | None = token or None
        self._uses_static_token: bool = bool(token)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def host(self) -> str:
        """Return the normalized host URL."""
        return self._host

    def get_hb_item_url(self, item_id: str) -> str:
        """Return the Homebox web UI URL for an item.

        Args:
            item_id: The Homebox item UUID.

        Returns:
            URL string pointing to the item in the Homebox UI.

        """
        return f"{self._host}/item/{item_id}"

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def async_authenticate(self) -> None:
        """Authenticate with the Homebox server and store the bearer token.

        Raises:
            HomeBoxAuthenticationError: If credentials are invalid.
            HomeBoxConnectionError: If the server is unreachable.

        """
        url = f"{self._host}{API_BASE_PATH}/v1/users/login"
        payload = {"username": self._username, "password": self._password}

        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status == 401:
                    raise HomeBoxAuthenticationError(
                        "Invalid Homebox credentials"
                    )
                if resp.status != 200:
                    raise HomeBoxAuthenticationError(
                        f"Authentication failed with status {resp.status}"
                    )
                data = await resp.json()
                token = data.get("token")
                if not token:
                    raise HomeBoxAuthenticationError(
                        "No token returned from Homebox login"
                    )
                self._token = token
                _LOGGER.debug("Homebox authentication successful")
        except aiohttp.ClientError as err:
            raise HomeBoxConnectionError(
                "Cannot connect to Homebox server"
            ) from err

    # ------------------------------------------------------------------
    # Internal request helpers
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Return authorization headers."""
        if self._token is None:
            raise HomeBoxAuthenticationError("Not authenticated")
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        data: aiohttp.FormData | None = None,
        _is_retry: bool = False,
    ) -> dict[str, Any] | list[Any]:
        """Send an authenticated request with token-refresh and retry logic.

        On a 401 response the client re-authenticates and retries once.
        On transient failures (server error / connection error) the client
        retries once after a 1-second delay.
        """
        url = f"{self._host}{API_BASE_PATH}/{path.lstrip('/')}"
        headers = self._auth_headers()

        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                json=json,
                params=params,
                data=data,
            ) as resp:
                # --- Token refresh on 401 ---
                if resp.status == 401 and not _is_retry:
                    _LOGGER.debug(
                        "Received 401, re-authenticating and retrying"
                    )
                    await self.async_authenticate()
                    return await self._request(
                        method,
                        path,
                        json=json,
                        params=params,
                        data=data,
                        _is_retry=True,
                    )

                # --- Transient failure retry ---
                if resp.status >= 500 and not _is_retry:
                    _LOGGER.debug(
                        "Received %s, retrying after %.1fs",
                        resp.status,
                        RETRY_DELAY,
                    )
                    await asyncio.sleep(RETRY_DELAY)
                    return await self._request(
                        method,
                        path,
                        json=json,
                        params=params,
                        data=data,
                        _is_retry=True,
                    )

                if resp.status == 401:
                    raise HomeBoxAuthenticationError(
                        "Authentication failed after token refresh"
                    )

                if resp.status == 404:
                    raise HomeBoxApiError("Resource not found")

                if not 200 <= resp.status < 300:
                    body = await resp.text()
                    # Truncate and sanitize to avoid leaking internal details
                    safe_body = _sanitize_for_log(body[:MAX_ERROR_BODY_LENGTH])
                    raise HomeBoxApiError(
                        f"Homebox API error {resp.status}: {safe_body}"
                    )

                # Some endpoints return 204 No Content
                if resp.status == 204:
                    return {}

                return await resp.json()  # type: ignore[no-any-return]

        except aiohttp.ClientError as err:
            if not _is_retry:
                _LOGGER.debug(
                    "Connection error, retrying after %.1fs: %s",
                    RETRY_DELAY,
                    err,
                )
                await asyncio.sleep(RETRY_DELAY)
                return await self._request(
                    method,
                    path,
                    json=json,
                    params=params,
                    data=data,
                    _is_retry=True,
                )
            raise HomeBoxConnectionError(
                "Cannot connect to Homebox server"
            ) from err

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    async def async_get_group_statistics(self) -> HomeBoxGroupStatistics:
        """Fetch group-level statistics.

        Returns:
            HomeBoxGroupStatistics with totals for items, locations, and value.

        """
        data = await self._request("GET", "v1/groups/statistics")
        if not isinstance(data, dict):
            raise HomeBoxApiError("Invalid statistics response")
        return HomeBoxGroupStatistics(
            total_items=int(data.get("totalItems", 0)),
            total_locations=int(data.get("totalLocations", 0)),
            total_value=float(data.get("totalItemPrice", 0.0)),
        )

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------

    async def async_get_items(
        self,
        *,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> list[HomeBoxItemSummary]:
        """Fetch a single page of items.

        Args:
            page: 1-based page number.
            page_size: Number of items per page.

        Returns:
            List of HomeBoxItemSummary for the requested page.

        """
        data = await self._request(
            "GET",
            "v1/items",
            params={"page": page, "pageSize": page_size},
        )
        return _parse_items_response(data)

    async def _async_paginate_items(
        self,
        extra_params: dict[str, Any] | None = None,
    ) -> list[HomeBoxItemSummary]:
        """Fetch all items matching extra_params, handling pagination.

        Args:
            extra_params: Additional query parameters (e.g. tags, q).

        Returns:
            Complete list of HomeBoxItemSummary from all pages.

        """
        all_items: list[HomeBoxItemSummary] = []
        page = 1

        while True:
            params: dict[str, Any] = {
                "page": page,
                "pageSize": DEFAULT_PAGE_SIZE,
            }
            if extra_params:
                params.update(extra_params)

            data = await self._request("GET", "v1/items", params=params)
            if not isinstance(data, dict):
                raise HomeBoxApiError("Invalid items response")

            items = _parse_items_response(data)
            all_items.extend(items)

            total = int(data.get("total", 0))
            if len(all_items) >= total or not items:
                break
            if page >= MAX_PAGES:
                _LOGGER.warning(
                    "Pagination cap reached (%d pages); truncating", MAX_PAGES
                )
                break
            page += 1

        return all_items

    async def async_get_all_items(self) -> list[HomeBoxItemSummary]:
        """Fetch all items, handling pagination automatically.

        Returns:
            Complete list of HomeBoxItemSummary from all pages.

        """
        return await self._async_paginate_items()

    async def async_get_item(self, item_id: str) -> dict[str, Any]:
        """Fetch a single item by ID.

        Args:
            item_id: The Homebox item UUID.

        Returns:
            Full item dict from the API.

        """
        data = await self._request("GET", f"v1/items/{item_id}")
        if not isinstance(data, dict):
            raise HomeBoxApiError(f"Invalid item response for {item_id}")
        return data

    async def async_create_item(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Create a new item.

        Args:
            payload: Item creation payload.

        Returns:
            Created item dict.

        """
        data = await self._request("POST", "v1/items", json=payload)
        if not isinstance(data, dict):
            raise HomeBoxApiError("Invalid create-item response")
        return data

    async def async_update_item(
        self, item_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Update an existing item.

        Args:
            item_id: The Homebox item UUID.
            payload: Full item update payload.

        Returns:
            Updated item dict.

        """
        data = await self._request(
            "PUT", f"v1/items/{item_id}", json=payload
        )
        if not isinstance(data, dict):
            raise HomeBoxApiError(f"Invalid update-item response for {item_id}")
        return data

    async def async_delete_item(self, item_id: str) -> None:
        """Delete an item by ID.

        Args:
            item_id: The Homebox item UUID.

        """
        await self._request("DELETE", f"v1/items/{item_id}")

    async def async_search_items(
        self, query: str
    ) -> list[HomeBoxItemSummary]:
        """Search for items matching a query string using server-side search.

        Args:
            query: Search string to match against items.

        Returns:
            List of matching HomeBoxItemSummary.

        """
        return await self._async_paginate_items(extra_params={"q": query})

    async def async_set_hb_item_location(
        self, item_id: str, location_id: str
    ) -> dict[str, Any]:
        """Set the location of a Homebox item.

        Args:
            item_id: The Homebox item UUID.
            location_id: The Homebox location UUID to assign.

        Returns:
            Updated item dict.

        """
        item = await self.async_get_item(item_id)
        fields = extract_item_fields(item)
        payload = build_item_update_payload(item, fields)
        payload["locationId"] = location_id
        return await self.async_update_item(item_id, payload)

    # ------------------------------------------------------------------
    # Item backlink management
    # ------------------------------------------------------------------

    async def async_set_item_backlink(
        self, item_id: str, url: str | None
    ) -> dict[str, Any]:
        """Set or remove the Home Assistant backlink field on an item.

        Args:
            item_id: The Homebox item UUID.
            url: The HA device URL to set, or None to remove the backlink.

        Returns:
            Updated item dict.

        """
        item = await self.async_get_item(item_id)
        fields = extract_item_fields(item)
        updated_fields = merge_backlink_field(fields, url)
        payload = build_item_update_payload(item, updated_fields)
        return await self.async_update_item(item_id, payload)

    # ------------------------------------------------------------------
    # Items by tag (paginated)
    # ------------------------------------------------------------------

    async def async_get_items_by_tag(
        self, tag_name: str = LINK_TAG_NAME
    ) -> list[HomeBoxItemSummary]:
        """Fetch all items with a specific tag, handling pagination.

        Args:
            tag_name: Tag name to filter by (default: LINK_TAG_NAME).

        Returns:
            List of HomeBoxItemSummary with the given tag.

        """
        # First, resolve the tag name to its ID
        tags = await self.async_get_tags()
        tag_id: str | None = None
        for tag in tags:
            if tag.get("name") == tag_name:
                tag_id = tag.get("id")
                break

        if tag_id is None:
            return []

        return await self._async_paginate_items(extra_params={"tags": tag_id})

    # ------------------------------------------------------------------
    # Locations
    # ------------------------------------------------------------------

    async def async_get_locations(self) -> list[dict[str, Any]]:
        """Fetch all locations.

        Returns:
            List of location dicts.

        """
        data = await self._request("GET", "v1/locations")
        if not isinstance(data, list):
            raise HomeBoxApiError("Invalid locations response")
        return data

    async def async_get_location(
        self, location_id: str
    ) -> dict[str, Any]:
        """Fetch a single location by ID.

        Args:
            location_id: The Homebox location UUID.

        Returns:
            Location dict.

        """
        data = await self._request(
            "GET", f"v1/locations/{location_id}"
        )
        if not isinstance(data, dict):
            raise HomeBoxApiError(
                f"Invalid location response for {location_id}"
            )
        return data

    async def async_create_location(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Create a new location.

        Args:
            payload: Location creation payload.

        Returns:
            Created location dict.

        """
        data = await self._request("POST", "v1/locations", json=payload)
        if not isinstance(data, dict):
            raise HomeBoxApiError("Invalid create-location response")
        return data

    async def async_update_location(
        self, location_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Update an existing location.

        Args:
            location_id: The Homebox location UUID.
            payload: Location update payload.

        Returns:
            Updated location dict.

        """
        data = await self._request(
            "PUT", f"v1/locations/{location_id}", json=payload
        )
        if not isinstance(data, dict):
            raise HomeBoxApiError(
                f"Invalid update-location response for {location_id}"
            )
        return data

    async def async_delete_location(self, location_id: str) -> None:
        """Delete a location by ID.

        Args:
            location_id: The Homebox location UUID.

        """
        await self._request("DELETE", f"v1/locations/{location_id}")

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------

    async def async_get_tags(self) -> list[dict[str, Any]]:
        """Fetch all tags.

        Returns:
            List of tag dicts.

        """
        data = await self._request("GET", "v1/labels")
        if not isinstance(data, list):
            raise HomeBoxApiError("Invalid tags response")
        return data

    async def async_get_tag(self, tag_id: str) -> dict[str, Any]:
        """Fetch a single tag by ID.

        Args:
            tag_id: The Homebox tag UUID.

        Returns:
            Tag dict.

        """
        data = await self._request("GET", f"v1/labels/{tag_id}")
        if not isinstance(data, dict):
            raise HomeBoxApiError(f"Invalid tag response for {tag_id}")
        return data

    async def async_create_tag(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Create a new tag.

        Args:
            payload: Tag creation payload (e.g. {"name": "MyTag"}).

        Returns:
            Created tag dict.

        """
        data = await self._request("POST", "v1/labels", json=payload)
        if not isinstance(data, dict):
            raise HomeBoxApiError("Invalid create-tag response")
        return data

    async def async_delete_tag(self, tag_id: str) -> None:
        """Delete a tag by ID.

        Args:
            tag_id: The Homebox tag UUID.

        """
        await self._request("DELETE", f"v1/labels/{tag_id}")

    async def async_ensure_tag(self, tag_name: str) -> dict[str, Any]:
        """Return an existing tag by name, creating it if necessary.

        Args:
            tag_name: The desired tag name.

        Returns:
            Tag dict (existing or newly created).

        """
        tags = await self.async_get_tags()
        for tag in tags:
            if tag.get("name") == tag_name:
                return tag
        return await self.async_create_tag({"name": tag_name})

    # ------------------------------------------------------------------
    # Image upload
    # ------------------------------------------------------------------

    async def async_upload_image_from_url(
        self,
        item_id: str,
        image_url: str,
    ) -> dict[str, Any]:
        """Download an image from a URL and upload it to a Homebox item.

        Args:
            item_id: The Homebox item UUID.
            image_url: Public URL of the image to upload.

        Returns:
            Upload response dict.

        Raises:
            HomeBoxInvalidImageUrlError: If the URL is malformed.
            HomeBoxImageDownloadError: If the download fails.
            HomeBoxImageTooLargeError: If the image exceeds 10 MB.
            HomeBoxImageContentTypeError: If the content type is not allowed.

        """
        _validate_image_url(image_url)

        try:
            # Disable redirects to prevent SSRF via open redirect
            async with self._session.get(
                image_url, allow_redirects=False
            ) as resp:
                if resp.status != 200:
                    raise HomeBoxImageDownloadError(
                        f"Failed to download image: HTTP {resp.status}"
                    )

                content_type = resp.content_type or ""
                if content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
                    raise HomeBoxImageContentTypeError(
                        f"Unsupported image content type: {content_type}. "
                        f"Allowed: {', '.join(sorted(ALLOWED_IMAGE_CONTENT_TYPES))}"
                    )

                # Check Content-Length header first if available
                content_length = resp.content_length
                if content_length is not None and content_length > MAX_IMAGE_SIZE:
                    raise HomeBoxImageTooLargeError(
                        f"Image too large: {content_length} bytes "
                        f"(max {MAX_IMAGE_SIZE} bytes)"
                    )

                # Read with size cap to prevent memory exhaustion
                # when Content-Length is absent or spoofed
                image_data = await resp.content.read(MAX_IMAGE_SIZE + 1)
                if len(image_data) > MAX_IMAGE_SIZE:
                    raise HomeBoxImageTooLargeError(
                        f"Image too large (max {MAX_IMAGE_SIZE} bytes)"
                    )

        except aiohttp.ClientError as err:
            raise HomeBoxImageDownloadError(
                "Failed to download image from provided URL"
            ) from err

        # Determine filename from URL
        filename = image_url.rsplit("/", maxsplit=1)[-1].split("?", maxsplit=1)[0]
        if not filename:
            filename = "image"

        form_data = aiohttp.FormData()
        form_data.add_field(
            "file",
            image_data,
            filename=filename,
            content_type=content_type,
        )

        data = await self._request(
            "POST",
            f"v1/items/{item_id}/attachments",
            data=form_data,
        )
        if not isinstance(data, dict):
            raise HomeBoxApiError("Invalid image upload response")
        return data


# ---------------------------------------------------------------------------
# Response parsing helpers
# ---------------------------------------------------------------------------


def _parse_items_response(
    data: dict[str, Any] | list[Any],
) -> list[HomeBoxItemSummary]:
    """Parse an items API response into a list of HomeBoxItemSummary.

    The Homebox API returns either:
    - A dict with "items" list and "total" count (paginated).
    - A plain list of items (legacy / non-paginated).

    Args:
        data: Raw response from the items endpoint.

    Returns:
        List of HomeBoxItemSummary.

    Raises:
        HomeBoxApiError: If the response structure is unexpected.

    """
    if isinstance(data, dict):
        raw_items = data.get("items", [])
    elif isinstance(data, list):
        raw_items = data
    else:
        raise HomeBoxApiError("Unexpected items response format")

    if not isinstance(raw_items, list):
        raise HomeBoxApiError("Invalid items list in response")

    results: list[HomeBoxItemSummary] = []
    for item in raw_items:
        if not isinstance(item, dict):
            _LOGGER.warning("Skipping non-dict item in response: %s", item)
            continue
        item_id = item.get("id")
        name = item.get("name")
        if not item_id or not name:
            _LOGGER.warning("Skipping item missing id or name: %s", item)
            continue
        location = item.get("location")
        loc_id = None
        loc_name = None
        if isinstance(location, dict):
            loc_id = location.get("id")
            loc_name = location.get("name")
        results.append(
            HomeBoxItemSummary(
                item_id=str(item_id),
                name=str(name),
                fields=item.get("fields"),
                location_id=str(loc_id) if loc_id else None,
                location_name=str(loc_name) if loc_name else None,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Host normalization helper
# ---------------------------------------------------------------------------


def normalize_homebox_host(host: str) -> str:
    """Normalize a Homebox host URL.

    Ensures the URL has a scheme and strips trailing slashes.

    Args:
        host: Raw host string from user input.

    Returns:
        Normalized URL string.

    """
    host = host.strip()
    if not host:
        return host
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    return normalize_url(host).rstrip("/")
