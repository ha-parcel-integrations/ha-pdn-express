"""Sample PDN Express API payloads shared by the test modules.

Keep them in one module rather than inline in each test — when the payload
shape turns out to be different from what was assumed, there is then exactly
one place to fix. The JSON files in ``fixtures/`` are scrubbed real replies.
"""
from __future__ import annotations

import json
from pathlib import Path

ACTIVE_CODE = "PDN0000000002"
DELIVERED_CODE = "PDN0000000001"

_FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    """Return the full reply envelope stored in ``fixtures/<name>.json``."""
    return json.loads((_FIXTURES / f"{name}.json").read_text())


def fixture_entry(name: str, code: str | None = None) -> dict:
    """Return the single ``data`` entry of a fixture, optionally renumbered."""
    entry = load_fixture(name)["data"][0]
    if code is not None:
        entry["orderNo"] = code
    return entry


def event(
    code: str,
    local_time: str,
    description: str,
    *,
    utc: str = "UTC+2",
    zone: str = "Europe/Amsterdam",
    label: str = "Stage",
    location: str | None = "NL - Zwolle",
) -> dict:
    """One entry of the carrier's own event timeline."""
    item = {
        "eventCode": code,
        "eventTime": local_time,
        "eventDesc": description,
        "timezone": zone,
        "utcTimezone": utc,
        "statusCodeConfig": {"lanName": label},
    }
    if location is not None:
        item["eventLocation"] = location
    return item


def entry(code: str, events: list[dict]) -> dict:
    """A tracking entry; ``events`` must be newest first, as the API sends them."""
    return {"orderNo": code, "destCountry": "Netherlands", "events": events}


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A representative entry for a delivered parcel."""
    return entry(
        code,
        [
            event("D860", "2026-09-19 13:59:10", "Your package has been delivered to the Mailbox."),
            event("D840", "2026-09-18 01:49:24", "Your package is out for delivery."),
            event("D830", "2026-09-17 23:37:58", "Arrived at our facility."),
            event("D800", "2026-09-07 01:44:08", "Package information received.", utc="UTC+0", location=None),
        ],
    )


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """An out-for-delivery parcel."""
    sample = delivered_sample(code)
    sample["events"] = sample["events"][1:]
    return sample


def in_transit_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel that is in the network."""
    sample = delivered_sample(code)
    sample["events"] = sample["events"][2:]
    return sample


def pickup_sample(code: str = ACTIVE_CODE) -> dict:
    """An in-transit scan carrying an inline ``pickupSite`` (shape unconfirmed)."""
    sample = in_transit_sample(code)
    sample["events"][0]["pickupSite"] = {
        "pudoShopName": "Corner Shop",
        "address": "1 Example Street, Zwolle",
        "latitude": 52.5,
        "longitude": 6.1,
        "siteBanners": [],
        "businessInfoList": [
            {"dayOfWeek": "MONDAY", "startTime": "09:00", "endTime": "17:00"}
        ],
    }
    return sample
