"""Parcel contract tests: field rules, window helpers, pickup sensors, calendar."""

from datetime import UTC, date, datetime, timedelta, timezone
from unittest.mock import MagicMock

from custom_components.pdn_express.calendar import PDNExpressDeliveriesCalendar
from custom_components.pdn_express.const import ParcelStatus
from custom_components.pdn_express.parcels import (
    is_pickup,
    normalize_parcel,
    planned_for_status,
    planned_window,
)
from custom_components.pdn_express.sensor import (
    PDNExpressEnRouteToPickupPointSensor,
    PDNExpressNextDeliverySensor,
)

from .payloads import entry, event, pickup_sample

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
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
ZONE = timezone(timedelta(hours=2))


def _parcels():
    site = pickup_sample()
    collected = pickup_sample()
    collected["events"].insert(0, event("D860", "2026-09-20 10:00:00", "Collected"))
    returning = pickup_sample()
    returning["events"].insert(0, event("D890", "2026-09-20 10:00:00", "Back"))
    ready = entry("P", [event("D845", "2026-09-20 09:00:00", "Ready")])
    return [site, collected, returning, ready]


def test_contract_rules_hold_for_every_shape():
    for raw in _parcels():
        for history in (True, False):
            parcel = normalize_parcel(raw, include_history=history)
            assert list(parcel) == CANONICAL_KEYS
            assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)
            if not parcel["delivered"]:
                assert parcel["delivered_at"] is None
            assert parcel["planned_from"] is None and parcel["planned_to"] is None
            assert isinstance(parcel["pickup"], bool)
            if not parcel["pickup"]:
                assert parcel["pickup_point"] is None
            assert (parcel["history"] is None) is (not history)


def test_collected_parcel_keeps_pickup_and_point_and_has_delivered_at():
    raw = pickup_sample()
    raw["events"].insert(0, event("D860", "2026-09-20 10:00:00", "Collected"))
    parcel = normalize_parcel(raw)
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["pickup"] is True and parcel["pickup_point"] == "Corner Shop"
    assert parcel["delivered_at"] == "2026-09-20T10:00:00+02:00"


def test_pickup_is_sticky_from_an_arrival_scan_without_a_site():
    raw = entry(
        "P",
        [
            event("D860", "2026-09-21 10:00:00", "Collected"),
            event("D845", "2026-09-20 09:00:00", "Ready"),
        ],
    )
    parcel = normalize_parcel(raw)
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["pickup"] is True and parcel["pickup_point"] is None


def test_home_delivery_is_not_pickup():
    raw = entry("H", [event("D860", "2026-09-21 10:00:00", "Delivered")])
    parcel = normalize_parcel(raw)
    assert parcel["pickup"] is False and parcel["pickup_point"] is None


def test_is_pickup_helper():
    assert is_pickup(True, ParcelStatus.IN_TRANSIT) is True
    assert is_pickup(None, ParcelStatus.IN_TRANSIT) is False
    assert is_pickup(None, ParcelStatus.DELIVERED, [None, ParcelStatus.AT_PICKUP_POINT])


def test_window_shapes():
    assert planned_window("2099-01-01T10:00:00+02:00", None, zone=ZONE) == (
        "2099-01-01T10:00:00+02:00",
        "2099-01-01T23:59:59+02:00",
    )
    assert planned_window("2099-01-01", None, zone=ZONE) == (
        "2099-01-01T00:00:00+02:00",
        "2099-01-01T23:59:59+02:00",
    )
    assert planned_window(date(2099, 1, 1), date(2099, 1, 3), zone=ZONE) == (
        "2099-01-01T00:00:00+02:00",
        "2099-01-03T23:59:59+02:00",
    )
    assert planned_window("2099-01-01T23:59:59+02:00", None, zone=ZONE)[1] is None
    assert planned_window(None, None, zone=ZONE) == (None, None)


def test_window_input_variants():
    aware = datetime(2099, 1, 1, 10, tzinfo=UTC)
    assert planned_window(aware, None, zone=ZONE)[0] == aware.isoformat()
    naive = datetime(2099, 1, 1, 10)
    assert planned_window(naive, None, zone=ZONE)[0] == "2099-01-01T10:00:00+02:00"
    assert planned_window("2099-01-01T10:00:00Z", None, zone=ZONE) == (
        "2099-01-01T10:00:00Z",
        "2099-01-01T23:59:59+02:00",
    )
    assert planned_window("garbage", None, zone=ZONE) == (None, None)
    assert planned_window("2099-13-45", None, zone=ZONE) == (None, None)
    assert planned_window(12345, None, zone=ZONE) == (None, None)
    assert planned_window(None, "2099-01-01", zone=ZONE)[0] == (
        "2099-01-01T00:00:00+02:00"
    )
    assert planned_window("2099-01-02", "2099-01-01", zone=ZONE) == (
        "2099-01-01T00:00:00+02:00",
        "2099-01-02T23:59:59+02:00",
    )
    assert planned_window(
        "2099-01-01T10:00:00+02:00", "2099-01-01T10:00:00+02:00", zone=ZONE
    ) == ("2099-01-01T10:00:00+02:00", "2099-01-01T23:59:59+02:00")
    assert planned_window(
        "2099-01-01T10:00:00+02:00", "2099-01-01T12:00:00+02:00", zone=ZONE
    ) == ("2099-01-01T10:00:00+02:00", "2099-01-01T12:00:00+02:00")


def test_window_emptied_at_destination():
    for status in (
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
        ParcelStatus.DELIVERED,
    ):
        assert planned_for_status(status, "a", "b") == (None, None)
    assert planned_for_status(ParcelStatus.IN_TRANSIT, "a", "b") == ("a", "b")


def _coordinator(data):
    coordinator = MagicMock()
    coordinator.data = data
    return coordinator


def _p(barcode, **fields):
    return {
        "barcode": barcode,
        "sender": None,
        "receiver": None,
        "contents": None,
        "status": ParcelStatus.IN_TRANSIT,
        "planned_from": None,
        "planned_to": None,
        "pickup": False,
        "pickup_point": None,
        "url": None,
        **fields,
    }


def test_next_delivery_skips_an_ended_window():
    stale = _p("OLD", planned_from="2000-01-01T00:00:00+00:00")
    stale_window = _p(
        "OLD2",
        planned_from="2000-01-01T00:00:00+00:00",
        planned_to="2000-01-01T23:59:59+00:00",
    )
    future = _p("NEW", planned_from="2099-01-01T10:00:00+00:00")
    sensor = PDNExpressNextDeliverySensor(
        _coordinator([stale, stale_window, future]), MagicMock(entry_id="e1")
    )
    assert sensor.extra_state_attributes["barcode"] == "NEW"
    assert sensor.native_value == datetime(2099, 1, 1, 10, tzinfo=UTC)


def test_en_route_excludes_returning():
    sensor = PDNExpressEnRouteToPickupPointSensor(
        _coordinator(
            [
                _p("A", pickup=True),
                _p("B", pickup=True, status=ParcelStatus.RETURNING),
            ]
        ),
        MagicMock(entry_id="e1"),
    )
    assert [p["barcode"] for p in sensor.extra_state_attributes["parcels"]] == ["A"]


def test_calendar_whole_day_window_is_all_day():
    parcel = _p(
        "D",
        planned_from="2099-01-01T00:00:00+02:00",
        planned_to="2099-01-02T23:59:59+02:00",
    )
    timed = _p(
        "T",
        planned_from="2099-01-01T10:00:00+02:00",
        planned_to="2099-01-01T23:59:59+02:00",
    )
    cal = PDNExpressDeliveriesCalendar(
        _coordinator([parcel, timed]), MagicMock(entry_id="e1")
    )
    events = {e.uid: e for e in cal._events()}
    assert events["D"].start == date(2099, 1, 1)
    assert events["D"].end == date(2099, 1, 3)
    assert isinstance(events["T"].start, datetime)
