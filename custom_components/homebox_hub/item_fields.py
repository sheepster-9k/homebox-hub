"""Homebox item field manipulation helpers."""

from __future__ import annotations

from typing import Any

from .const import LINK_BACKLINK_FIELD_NAME


def extract_item_fields(hb_item: dict) -> list[dict[str, Any]]:
    """Extract the custom fields array from a Homebox item response.

    Returns only dict entries; returns an empty list when the key is
    missing or not a list.
    """
    raw = hb_item.get("fields")
    if not isinstance(raw, list):
        return []
    return [f for f in raw if isinstance(f, dict)]


def build_item_update_payload(
    hb_item: dict, fields: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build a full PUT payload for a Homebox item update.

    The Homebox API performs a full replacement on PUT, so every existing
    property must be echoed back to avoid data loss.
    """
    location = hb_item.get("location") or {}
    labels = hb_item.get("labels") or []

    return {
        "id": hb_item.get("id"),
        "name": hb_item.get("name", ""),
        "description": hb_item.get("description", ""),
        "quantity": hb_item.get("quantity", 0),
        "insured": hb_item.get("insured", False),
        "archived": hb_item.get("archived", False),
        "assetId": hb_item.get("assetId", ""),
        "serialNumber": hb_item.get("serialNumber", ""),
        "modelNumber": hb_item.get("modelNumber", ""),
        "manufacturer": hb_item.get("manufacturer", ""),
        "purchasePrice": hb_item.get("purchasePrice", "0"),
        "purchaseFrom": hb_item.get("purchaseFrom", ""),
        "soldTo": hb_item.get("soldTo", ""),
        "soldPrice": hb_item.get("soldPrice", "0"),
        "soldNotes": hb_item.get("soldNotes", ""),
        "notes": hb_item.get("notes", ""),
        "locationId": location.get("id"),
        "labelIds": [lbl["id"] for lbl in labels if isinstance(lbl, dict) and "id" in lbl],
        "fields": fields,
    }


def merge_backlink_field(
    fields: list[dict[str, Any]], ha_device_url: str | None
) -> list[dict[str, Any]]:
    """Add, update, or remove the Home Assistant backlink field.

    All other custom fields are preserved unchanged.
    """
    other_fields = [f for f in fields if f.get("name") != LINK_BACKLINK_FIELD_NAME]

    if ha_device_url is None:
        return other_fields

    backlink = {
        "name": LINK_BACKLINK_FIELD_NAME,
        "type": "text",
        "textValue": ha_device_url,
    }
    return [*other_fields, backlink]
