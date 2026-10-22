"""Tests for the pure parcel-mapping helpers.

These need no Home Assistant instance — the whole point of keeping
``parcels.py`` free of I/O is that the carrier-specific mapping can be tested
as plain functions.
"""

from datetime import UTC, datetime, timedelta
import logging

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pdn_express.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    PENDING_CAPABILITIES,
    ParcelStatus,
)
from custom_components.pdn_express.parcels import (
    apply_delivered_filter,
    build_history,
    map_event_status,
    normalize_parcel,
    parse_event_time,
    parse_iso,
    sort_parcels_by_ts,
    tracking_url,
)

from .payloads import (
    active_sample,
    delivered_sample,
    entry,
    event,
    fixture_entry,
    in_transit_sample,
    pickup_sample,
)

# ---------------------------------------------------------------------------
# status mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("D800", ParcelStatus.REGISTERED),
        ("D830", ParcelStatus.IN_TRANSIT),
        ("D840", ParcelStatus.OUT_FOR_DELIVERY),
        ("D850", ParcelStatus.PROBLEM),
        ("D814", ParcelStatus.PROBLEM),
        ("D860", ParcelStatus.DELIVERED),
        ("D890", ParcelStatus.RETURNING),
    ],
)
def test_map_event_status_known(code, expected):
    assert map_event_status(code) == expected


def test_map_event_status_note_and_missing_are_silent(caplog):
    assert map_event_status("D810") is None
    assert map_event_status(None) is None
    assert caplog.text == ""


def test_unmapped_status_warns_only_once_with_code_and_label(caplog):
    assert map_event_status("D999", "Brand New") is None
    assert map_event_status("D999", "Brand New") is None
    assert caplog.text.count("D999") == 1
    assert "Brand New" in caplog.text
    assert "issues/new" in caplog.text


# ---------------------------------------------------------------------------
# timestamps
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z").tzinfo is not None
    assert parse_iso("2026-04-29T13:12:42").tzinfo == UTC
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        ("UTC+2", "2026-09-19T13:59:10+02:00"),
        ("UTC+0", "2026-09-19T13:59:10+00:00"),
        ("UTC-4", "2026-09-19T13:59:10-04:00"),
        ("UTC+5:30", "2026-09-19T13:59:10+05:30"),
    ],
)
def test_parse_event_time_applies_the_stated_offset(utc, expected):
    item = event("D830", "2026-09-19 13:59:10", "x", utc=utc)
    assert parse_event_time(item) == expected


def test_parse_event_time_ignores_the_iana_zone():
    """The stated offset wins, including for the old Canadian zones."""
    item = event("D860", "2026-01-10 09:00:00", "x", utc="UTC-5", zone="Canada/Eastern")
    assert parse_event_time(item) == "2026-01-10T09:00:00-05:00"


def test_repeated_autumn_hour_is_resolved_by_the_offset():
    first = event("D830", "2026-10-25 02:30:00", "x", utc="UTC+2")
    second = event("D830", "2026-10-25 02:30:00", "x", utc="UTC+1")
    stamps = [parse_iso(parse_event_time(e)) for e in (first, second)]
    assert stamps[1] - stamps[0] == timedelta(hours=1)


@pytest.mark.parametrize(
    "bad",
    [
        event("D830", "garbage", "x"),
        event("D830", "2026-09-19 13:59:10", "x", utc="local"),
        {"eventCode": "D830"},
    ],
)
def test_parse_event_time_unparsable_warns_once(bad, caplog):
    assert parse_event_time(bad) is None
    assert parse_event_time(bad) is None
    assert caplog.text.count("unparsable") == 1


# ---------------------------------------------------------------------------
# build_history
# ---------------------------------------------------------------------------


def test_build_history_reverses_to_oldest_first():
    history = build_history(delivered_sample()["events"])
    assert len(history) == 4
    assert history[0]["raw_status"] == "Package information received."
    assert history[0]["status"] == ParcelStatus.REGISTERED
    assert history[0]["timestamp"] == "2026-09-07T01:44:08+00:00"
    assert history[-1]["status"] == ParcelStatus.DELIVERED


def test_build_history_caps_to_most_recent_events():
    events = [
        event("D830", f"2026-09-{day:02d} 10:00:00", f"step {day}")
        for day in range(25, 0, -1)
    ]
    history = build_history(events, max_events=20)
    assert len(history) == 20
    assert history[-1]["raw_status"] == "step 25"


def test_build_history_handles_missing_and_malformed():
    assert build_history(None) == []
    assert build_history([{"eventCode": "D830"}]) == []
    assert build_history(["not-a-dict"]) == []


def test_build_history_note_has_null_status_and_falls_back_to_code():
    history = build_history([event("D810", "2026-09-20 07:50:13", "", utc="UTC+0")])
    assert history[0]["status"] is None
    assert history[0]["raw_status"] == "D810"


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_capabilities_are_known_values():
    assert CAPABILITIES <= KNOWN_CAPABILITIES
    assert PENDING_CAPABILITIES <= KNOWN_CAPABILITIES


def test_a_capability_is_never_both_populated_and_pending():
    assert not CAPABILITIES & PENDING_CAPABILITIES


def test_capabilities_match_what_normalize_parcel_actually_returns():
    delivered = normalize_parcel(delivered_sample())
    active = normalize_parcel(active_sample())
    pickup = normalize_parcel(pickup_sample())
    with_history = normalize_parcel(delivered_sample(), include_history=True)

    if "weight" in CAPABILITIES:
        assert delivered["weight"] is not None
    if "dimensions" in CAPABILITIES:
        assert delivered["dimensions"] is not None
    if "delivery_window" in CAPABILITIES:
        assert active["planned_from"] is not None or active["planned_to"] is not None
    if "pickup_point" in CAPABILITIES:
        assert pickup["pickup_point"] is not None
    if "url" in CAPABILITIES:
        assert delivered["url"] is not None
    if "history" in CAPABILITIES:
        assert with_history["history"] is not None
    # Everything not declared stays a literal None.
    for field in ("weight", "dimensions"):
        assert delivered[field] is None
    assert active["planned_from"] is None and active["planned_to"] is None
    assert "pickup_point" in PENDING_CAPABILITIES


def test_normalize_delivered_fixture():
    raw = fixture_entry("fixture_delivered", "PDN0000000001")
    parcel = normalize_parcel(raw)
    assert parcel["carrier"] == "PDN Express"
    assert parcel["barcode"] == "PDN0000000001"
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == raw["events"][0]["eventDesc"]
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-09-19T13:59:10+02:00"
    assert parcel["url"] == "https://pdn.express/en/track/PDN0000000001"
    assert parcel["sender"] is parcel["receiver"] is None
    assert parcel["pickup"] is False
    assert parcel["history"] is None  # opt-in, default off
    assert parcel["raw"] is raw


def test_normalize_delivered_fixture_history_runs_oldest_to_newest():
    parcel = normalize_parcel(fixture_entry("fixture_delivered"), include_history=True)
    statuses = [h["status"] for h in parcel["history"]]
    assert statuses[0] == ParcelStatus.REGISTERED
    assert statuses[-1] == ParcelStatus.DELIVERED
    # failed attempt, then back in transit, then out again: status follows time
    assert ParcelStatus.PROBLEM in statuses
    stamps = [parse_iso(h["timestamp"]) for h in parcel["history"]]
    assert stamps == sorted(stamps)


def test_normalize_returned_to_sender_fixture():
    raw = fixture_entry("fixture_returned_to_sender")
    parcel = normalize_parcel(raw, include_history=True)
    assert parcel["status"] == ParcelStatus.RETURNING
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert len(parcel["history"]) == 20  # 21 events, capped
    assert parcel["raw"] is raw and len(raw["events"]) == 21


def test_normalize_skips_a_newest_note_for_the_previous_event():
    """A customer-service note on top must not hide the real position."""
    raw = entry(
        "PDN0000000002",
        [
            event("D810", "2026-09-20 07:50:13", "Address updated.", utc="UTC+0"),
            event("D850", "2026-09-19 10:00:00", "Unable to Deliver."),
            event("D840", "2026-09-19 08:00:00", "Out for delivery."),
        ],
    )
    parcel = normalize_parcel(raw)
    assert parcel["status"] == ParcelStatus.PROBLEM
    assert parcel["raw_status"] == "Unable to Deliver."


def test_problem_then_in_transit_then_out_for_delivery_follows_the_newest():
    base = [
        event("D850", "2026-09-13 18:00:00", "Unable to Deliver."),
        event("D840", "2026-09-12 10:00:00", "Out."),
    ]
    in_transit = entry(
        "P", [event("D830", "2026-09-14 09:00:00", "Back at depot."), *base]
    )
    again = entry(
        "P",
        [event("D840", "2026-09-15 09:00:00", "Out.")] + in_transit["events"],
    )
    assert normalize_parcel(in_transit)["status"] == ParcelStatus.IN_TRANSIT
    assert normalize_parcel(again)["status"] == ParcelStatus.OUT_FOR_DELIVERY


def test_normalize_unmapped_newest_falls_back_to_newest_mappable(caplog):
    raw = entry(
        "PDN0000000002",
        [
            event("D999", "2026-09-20 10:00:00", "Something new.", label="Novel"),
            event("D830", "2026-09-19 10:00:00", "Arrived at our facility."),
        ],
    )
    parcel = normalize_parcel(raw)
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert "D999" in caplog.text and "Novel" in caplog.text
    assert "Something new" not in caplog.text


def test_normalize_only_unmapped_events_is_unknown_with_warning(caplog):
    raw = entry("PDN0000000002", [event("D999", "2026-09-20 10:00:00", "x")])
    parcel = normalize_parcel(raw)
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["raw_status"] is None
    assert "D999" in caplog.text


def test_normalize_only_a_note_is_unknown_without_warning(caplog):
    raw = entry("P", [event("D810", "2026-09-20 07:50:13", "x", utc="UTC+0")])
    assert normalize_parcel(raw)["status"] == ParcelStatus.UNKNOWN
    assert "Unrecognised" not in caplog.text


def test_normalize_pending_placeholder():
    """A tracked-but-unknown code still yields a full parcel dict."""
    parcel = normalize_parcel({"orderNo": "PDN0000000009"})
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["history"] is None


def test_normalize_history_is_opt_in():
    parcel = normalize_parcel(delivered_sample(), include_history=True)
    assert len(parcel["history"]) == 4


def test_canadian_event_uses_its_negative_offset():
    raw = entry(
        "P",
        [
            event(
                "D860",
                "2026-08-14 12:00:00",
                "Delivered.",
                utc="UTC-4",
                zone="America/Toronto",
            )
        ],
    )
    assert normalize_parcel(raw)["delivered_at"] == "2026-08-14T12:00:00-04:00"


def test_inline_pickup_site_fills_pickup_point_and_warns_once(caplog):
    raw = pickup_sample()
    parcel = normalize_parcel(raw)
    normalize_parcel(raw)
    assert caplog.text.count("pickupSite object") == 1
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Corner Shop"
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert "Corner Shop" not in caplog.text and "Example Street" not in caplog.text
    assert parcel["raw"]["events"][0]["pickupSite"]["latitude"] == 52.5


def test_pickup_site_without_a_name_is_pickup_with_no_point():
    raw = in_transit_sample()
    raw["events"][0]["pickupSite"] = {"address": "x"}
    parcel = normalize_parcel(raw)
    assert parcel["pickup"] is True and parcel["pickup_point"] is None


def test_pickup_site_is_ignored_once_delivered_or_returning():
    for code, status in (
        ("D860", ParcelStatus.DELIVERED),
        ("D890", ParcelStatus.RETURNING),
    ):
        raw = pickup_sample()
        raw["events"].insert(0, event(code, "2026-09-20 10:00:00", "x"))
        parcel = normalize_parcel(raw)
        assert parcel["status"] == status
        assert parcel["pickup"] is False and parcel["pickup_point"] is None


def test_pickup_site_id_alone_warns_once_and_leaves_the_point_empty(caplog):
    raw = in_transit_sample()
    raw["events"][0]["pickupSiteId"] = "site-secret"
    parcel = normalize_parcel(raw)
    normalize_parcel(raw)
    assert caplog.text.count("pickupSiteId") == 1
    assert "site-secret" not in caplog.text
    assert parcel["pickup"] is False and parcel["pickup_point"] is None


def test_d845_maps_to_at_pickup_point_with_a_one_shot_warning(caplog):
    raw = entry(
        "P", [event("D845", "2026-09-20 10:00:00", "Ready for pickup.", label="Ready")]
    )
    for _ in range(2):
        parcel = normalize_parcel(raw)
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True and parcel["pickup_point"] is None
    assert caplog.text.count("D845") == 1
    assert "Ready for pickup" not in caplog.text


def test_new_keys_warn_once_with_names_only(caplog):
    raw = in_transit_sample()
    raw["surprise"] = "secret-value"
    raw["events"][0]["twist"] = "other-secret"
    raw["events"].append("not-a-dict")
    normalize_parcel(raw)
    normalize_parcel(raw)
    assert caplog.text.count("surprise") == 1
    assert caplog.text.count("twist") == 1
    assert "secret-value" not in caplog.text and "other-secret" not in caplog.text
    assert caplog.records[0].levelno == logging.WARNING


def test_tracking_url_needs_a_code():
    assert tracking_url(None) is None
    assert tracking_url("PDN0000000001") == "https://pdn.express/en/track/PDN0000000001"


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(UTC)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels
