"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

Two things here are carrier-specific: :data:`_STATUS_MAP` and
:func:`normalize_parcel`. Everything else — the history builder, the sort
contract, the delivered filter, the one-shot warnings — is suite-wide
machinery.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-pdn-express/issues/new"
    "?template=unrecognised_status.yml"
)

# Keyed on the event's ``eventCode``. Prefer mapping too little over mapping
# wrongly: an unmapped code is skipped for the current status in favour of the
# newest mappable event, and warned about once.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "D800": ParcelStatus.REGISTERED,
    "D830": ParcelStatus.IN_TRANSIT,
    "D840": ParcelStatus.OUT_FOR_DELIVERY,
    "D850": ParcelStatus.PROBLEM,
    "D814": ParcelStatus.PROBLEM,
    "D845": ParcelStatus.AT_PICKUP_POINT,
    "D860": ParcelStatus.DELIVERED,
    "D890": ParcelStatus.RETURNING,
}

# Codes mapped on the strength of the page's own stage list rather than a
# scan seen on the wire; each warns once so a real one gets confirmed.
_PROVISIONAL_CODES = frozenset({"D845"})

# Customer-service notes: they say nothing about where the parcel is, so they
# never set the status and never warn.
_NOTE_CODES = frozenset({"D810"})

_KNOWN_ENTRY_KEYS = frozenset({"orderNo", "destCountry", "events", "statusCodeConfig"})
_KNOWN_EVENT_KEYS = frozenset(
    {
        "eventCode",
        "eventTime",
        "eventLocation",
        "eventDesc",
        "timezone",
        "utcTimezone",
        "existsPod",
        "statusCodeConfig",
        "pickupSite",
        "pickupSiteId",
    }
)

# Everything we have already warned about, so each unknown is logged only once
# per HA session instead of on every poll.
_unmapped_statuses_logged: set[str] = set()


def warn_once(key: str, message: str, *args: Any) -> None:
    """Log a WARNING the first time ``key`` is seen in this session."""
    if key in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(key)
    _LOGGER.warning(message, *args)


def _warn_unmapped_status(code: str, label: str | None = None) -> None:
    """Log an unmapped carrier status once, with a copy-paste issue link."""
    warn_once(
        f"status:{code}",
        "Unrecognised PDN Express status — help us map it. Open an issue "
        "and paste this line: %s\n  eventCode=%s lanName=%s",
        NEW_ISSUE_URL,
        code,
        label,
    )


def map_event_status(code: str | None, label: str | None = None) -> ParcelStatus | None:
    """Map an event code to a canonical status, or ``None``.

    Notes and unmapped codes give ``None`` (rather than ``unknown``, so a
    consumer can tell "no mapping" from "mapped to unknown"); an unmapped code
    also warns once.
    """
    if not code or code in _NOTE_CODES:
        return None
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        if code in _PROVISIONAL_CODES:
            warn_once(
                f"provisional:{code}",
                "PDN Express status %s (lanName=%s) is mapped to %s without "
                "ever having been seen on the wire; please confirm it: %s",
                code,
                label,
                mapped.value,
                NEW_ISSUE_URL,
            )
        return mapped
    _warn_unmapped_status(code, label)
    return None


def _label(event: dict) -> str | None:
    """Return the stage name of an event (``lanName``), never its free text."""
    config = event.get("statusCodeConfig")
    return config.get("lanName") if isinstance(config, dict) else None


def parse_event_time(event: dict) -> str | None:
    """Return an ISO 8601 timestamp for an event, or ``None``.

    ``eventTime`` is a local wall clock without offset; the offset comes from
    the event's own ``utcTimezone`` (``UTC+2``). That text already settles DST
    and the repeated autumn hour, so the IANA ``timezone`` is not consulted.
    """
    raw_time = event.get("eventTime")
    raw_offset = event.get("utcTimezone")
    match = _OFFSET_RE.fullmatch(str(raw_offset or "").strip())
    try:
        local = datetime.strptime(str(raw_time), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        local = None
    if local is None or match is None:
        warn_once(
            "unparsable-time",
            "PDN Express event with an unparsable eventTime/utcTimezone; "
            "the event is left out of timestamps",
        )
        return None
    sign = -1 if match.group(1) == "-" else 1
    offset = timedelta(hours=int(match.group(2)), minutes=int(match.group(3) or 0))
    return local.replace(tzinfo=timezone(sign * offset)).isoformat()


_OFFSET_RE = re.compile(r"UTC([+-])(\d{1,2})(?::?(\d{2}))?")


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the carrier's event list.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. The carrier lists events newest first, so
    they are reversed to oldest → newest and capped to the most recent
    ``max_events``. Events whose time cannot be read are left out.
    """
    entries: list[dict] = []
    for event in reversed(events or []):
        if not isinstance(event, dict):
            continue
        timestamp = parse_event_time(event)
        if not timestamp:
            continue
        entries.append(
            {
                "timestamp": timestamp,
                "status": map_event_status(event.get("eventCode"), _label(event)),
                "raw_status": event.get("eventDesc") or event.get("eventCode"),
            }
        )
    return entries[-max_events:]


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


def _warn_unknown_shape(raw: dict) -> None:
    """Warn once about keys and pickup hints we have never seen."""
    extra = sorted(set(raw) - _KNOWN_ENTRY_KEYS)
    if extra:
        warn_once("entry-keys:" + ",".join(extra), "PDN Express entry has new keys: %s", extra)
    for event in raw.get("events") or []:
        if not isinstance(event, dict):
            continue
        if "pickupSite" in event:
            warn_once(
                "pickup-site",
                "PDN Express event carries a pickupSite object (eventCode=%s, "
                "lanName=%s); its shape is unconfirmed, please report how it "
                "looks: %s",
                event.get("eventCode"),
                _label(event),
                NEW_ISSUE_URL,
            )
        elif "pickupSiteId" in event:
            warn_once(
                "pickup-site-id",
                "PDN Express event carries only a pickupSiteId (eventCode=%s, "
                "lanName=%s); the point's details are not read: %s",
                event.get("eventCode"),
                _label(event),
                NEW_ISSUE_URL,
            )
        extra = sorted(set(event) - _KNOWN_EVENT_KEYS)
        if extra:
            warn_once(
                "event-keys:" + ",".join(extra),
                "PDN Express event has new keys: %s",
                extra,
            )


def _pickup_site(events: list[dict]) -> tuple[bool, str | None]:
    """Return ``(site_seen, shop_name)`` from the newest inline ``pickupSite``.

    Only the shop name is published; the address, coordinates, photos and
    opening hours stay in ``raw``. A scan with just a ``pickupSiteId`` leaves
    both unset: the point's details sit behind a postcode check we do not use.
    """
    for event in events:
        site = event.get("pickupSite")
        if isinstance(site, dict) and site:
            name = site.get("pudoShopName")
            name = name.strip() if isinstance(name, str) else ""
            return True, name or None
    return False, None


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The status follows the newest event that maps to one, so a customer-service
    note (or a code we do not know) never hides where the parcel really is.
    The **keys of the returned dict are the contract** shared by every carrier
    in the suite; a value the carrier does not expose is ``None``, never
    omitted.
    """
    barcode = raw.get("orderNo")
    events = [e for e in raw.get("events") or [] if isinstance(e, dict)]
    _warn_unknown_shape(raw)

    status = ParcelStatus.UNKNOWN
    raw_status = None
    status_event: dict | None = None
    for event in events:  # newest first
        mapped = map_event_status(event.get("eventCode"), _label(event))
        if mapped is not None:
            status = mapped
            raw_status = event.get("eventDesc") or event.get("eventCode")
            status_event = event
            break
    delivered = status is ParcelStatus.DELIVERED
    site_seen, shop_name = _pickup_site(events)
    at_pickup = status is ParcelStatus.AT_PICKUP_POINT
    pickup = at_pickup or (
        site_seen and status not in (ParcelStatus.DELIVERED, ParcelStatus.RETURNING)
    )

    return {
        "carrier": "PDN Express",
        "barcode": barcode,
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": raw_status,
        "delivered": delivered,
        "delivered_at": parse_event_time(status_event) if delivered else None,
        "planned_from": None,
        "planned_to": None,
        "pickup": pickup,
        "pickup_point": shop_name if pickup else None,
        "url": tracking_url(barcode),
        "weight": None,
        "dimensions": None,
        "history": build_history(events) if include_history else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
