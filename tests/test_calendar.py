"""Tests for the PDN Express deliveries calendar."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock

from homeassistant.components.calendar import CalendarEvent

from custom_components.pdn_express.calendar import PDNExpressDeliveriesCalendar


def _entry(entry_id: str = "e1") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _coordinator(data: list[dict]) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = data
    return coordinator


def _parcel(
    barcode, planned_from=None, planned_to=None, pickup=False, pickup_point=None
):
    return {
        "barcode": barcode,
        "sender": "Example Sender",
        "status": "out_for_delivery",
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": pickup,
        "pickup_point": pickup_point,
        "url": "https://track/1",
    }


def _cal(data):
    return PDNExpressDeliveriesCalendar(_coordinator(data), _entry())


def test_event_returns_earliest_upcoming():
    cal = _cal(
        [
            _parcel("LATE", planned_from="2099-01-02T10:00:00Z"),
            _parcel("SOON", planned_from="2099-01-01T10:00:00Z"),
        ]
    )
    event = cal.event
    assert event.uid == "SOON"
    assert event.summary == "Example Sender"


def test_event_none_when_no_planned():
    assert _cal([_parcel("A")]).event is None


def test_moment_gets_one_hour_duration():
    cal = _cal([_parcel("A", planned_from="2099-01-01T10:00:00Z")])
    events = cal._events()
    assert events[0].end == datetime(2099, 1, 1, 11, 0, tzinfo=UTC)


def test_interval_uses_window():
    cal = _cal(
        [
            _parcel(
                "A",
                planned_from="2099-01-01T10:00:00Z",
                planned_to="2099-01-01T12:00:00Z",
            )
        ]
    )
    assert cal._events()[0].end == datetime(2099, 1, 1, 12, 0, tzinfo=UTC)


def test_pickup_sets_location():
    cal = _cal(
        [
            _parcel(
                "A",
                planned_from="2099-01-01T10:00:00Z",
                pickup=True,
                pickup_point="PDN Express Shop",
            )
        ]
    )
    assert cal._events()[0].location == "PDN Express Shop"


def test_naive_planned_from_treated_as_utc():
    cal = _cal([_parcel("A", planned_from="2099-01-01T10:00:00")])  # no tz
    assert cal._events()[0].start == datetime(2099, 1, 1, 10, 0, tzinfo=UTC)


def test_unparseable_planned_from_skipped():
    assert _cal([_parcel("A", planned_from="not-a-date")]).event is None


async def test_get_events_filters_by_range():
    cal = _cal(
        [
            _parcel("PAST", planned_from="2000-01-01T10:00:00Z"),
            _parcel("FUTURE", planned_from="2099-01-01T10:00:00Z"),
        ]
    )
    start = datetime(2098, 1, 1, tzinfo=UTC)
    end = datetime(2100, 1, 1, tzinfo=UTC)
    events = await cal.async_get_events(MagicMock(), start, end)
    assert {e.uid for e in events} == {"FUTURE"}


def _mixed_events():
    return [
        CalendarEvent(
            start=date(2099, 3, 1), end=date(2099, 3, 2), summary="ALLDAY", uid="ALLDAY"
        ),
        CalendarEvent(
            start=datetime(2099, 3, 5, 10, 0, tzinfo=UTC),
            end=datetime(2099, 3, 5, 11, 0, tzinfo=UTC),
            summary="TIMED",
            uid="TIMED",
        ),
    ]


def test_event_handles_all_day_and_timed():
    cal = _cal([])
    cal._events = _mixed_events
    assert cal.event.uid == "ALLDAY"
    cal._events = lambda: _mixed_events()[1:]
    assert cal.event.uid == "TIMED"


async def test_get_events_handles_all_day_and_timed():
    cal = _cal([])
    cal._events = _mixed_events
    start = datetime(2099, 1, 1, tzinfo=UTC)
    both = await cal.async_get_events(
        MagicMock(), start, datetime(2099, 12, 1, tzinfo=UTC)
    )
    assert {e.uid for e in both} == {"ALLDAY", "TIMED"}
    only_timed = await cal.async_get_events(
        MagicMock(), datetime(2099, 3, 4, tzinfo=UTC), datetime(2099, 12, 1, tzinfo=UTC)
    )
    assert {e.uid for e in only_timed} == {"TIMED"}
