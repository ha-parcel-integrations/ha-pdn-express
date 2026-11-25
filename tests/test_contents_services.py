"""Tests for the contents action, the track_parcel field and the store lifecycle."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.pdn_express.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
)
from custom_components.pdn_express.contents import CONTENTS_STORE_MAX_CODES
from custom_components.pdn_express.coordinator import PDNExpressCoordinator

from .payloads import ACTIVE_CODE, DELIVERED_CODE, active_sample, delivered_sample

CLIENT = "custom_components.pdn_express.api.PDNExpressApiClient"
COORDINATOR = f"{PDNExpressCoordinator.__module__}.PDNExpressCoordinator"
UNTRACKED = "PDN0000000555"
STORE_KEY = f"{DOMAIN}.contents.%s"


def _config_entry(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, options={CONF_PARCELS: parcels}
    )


async def _setup(hass, codes: list[str] | None = None) -> MockConfigEntry:
    entry = _config_entry([{CONF_TRACKING_CODE: code} for code in codes or []])
    entry.add_to_hass(hass)
    with patch(
        f"{CLIENT}.async_get_parcel", new=AsyncMock(return_value=active_sample())
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _call(hass, service: str, data: dict) -> None:
    with patch(
        f"{CLIENT}.async_get_parcel",
        new=AsyncMock(side_effect=lambda code: active_sample(code)),
    ):
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
        await hass.async_block_till_done()
        # A refresh requested inside the debouncer's cooldown runs after it.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()


def _contents(entry: MockConfigEntry, code: str) -> str | None:
    parcels = {p["barcode"]: p for p in entry.runtime_data.coordinator.data}
    return parcels[code]["contents"]


async def _call_no_refresh(hass, entry, service: str, data: dict):
    """Call a service with the carrier fetch and the refresh both forbidden."""
    coordinator = entry.runtime_data.coordinator
    listeners = MagicMock(wraps=coordinator.async_update_listeners)
    with (
        patch(f"{CLIENT}.async_get_parcel", new=AsyncMock()) as fetch,
        patch(f"{COORDINATOR}.async_request_refresh", new=AsyncMock()) as refresh,
        patch.object(coordinator, "async_update_listeners", listeners),
    ):
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
        await hass.async_block_till_done()
    assert fetch.await_count == 0
    assert refresh.await_count == 0
    return listeners


async def test_update_contents_sets_replaces_and_clears(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    base = {CONF_TRACKING_CODE: ACTIVE_CODE}

    await _call(hass, "update_contents", {**base, "contents": "Running shoes"})
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"

    await _call(hass, "update_contents", {**base, "contents": "Winter socks"})
    assert _contents(entry, ACTIVE_CODE) == "Winter socks"

    await _call(hass, "update_contents", {**base, "contents": ""})
    assert _contents(entry, ACTIVE_CODE) is None


async def test_update_contents_without_the_field_clears(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )
    await _call(hass, "update_contents", {CONF_TRACKING_CODE: ACTIVE_CODE})
    assert _contents(entry, ACTIVE_CODE) is None


async def test_update_contents_trims_whitespace(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "  Running \n shoes  "},
    )
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"


async def test_update_contents_normalizes_the_code(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: "pdn-0000 000002", "contents": "Running shoes"},
    )
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"


async def test_update_contents_works_for_a_code_that_is_not_tracked(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
    )

    assert entry.runtime_data.coordinator.contents_store.get(UNTRACKED) == (
        "Running shoes"
    )
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: ACTIVE_CODE}]


async def test_a_contents_change_never_fetches_or_refreshes(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    data = {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"}

    listeners = await _call_no_refresh(hass, entry, "update_contents", data)

    assert listeners.call_count == 1
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"


async def test_an_unchanged_contents_value_does_nothing(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    data = {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"}
    await _call_no_refresh(hass, entry, "update_contents", data)

    listeners = await _call_no_refresh(hass, entry, "update_contents", data)
    assert listeners.call_count == 0

    listeners = await _call_no_refresh(
        hass, entry, "update_contents", {CONF_TRACKING_CODE: UNTRACKED}
    )
    assert listeners.call_count == 0


async def test_clearing_fires_the_listeners_once(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    base = {CONF_TRACKING_CODE: ACTIVE_CODE}
    await _call_no_refresh(
        hass, entry, "update_contents", {**base, "contents": "Running shoes"}
    )

    listeners = await _call_no_refresh(hass, entry, "update_contents", base)

    assert listeners.call_count == 1
    assert _contents(entry, ACTIVE_CODE) is None


async def test_a_contents_change_reaches_the_delivered_list(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={
            CONF_PARCELS: [{CONF_TRACKING_CODE: DELIVERED_CODE}],
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
    )
    entry.add_to_hass(hass)
    with patch(
        f"{CLIENT}.async_get_parcel",
        new=AsyncMock(return_value=delivered_sample()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    assert coordinator.delivered[0]["contents"] is None

    await _call_no_refresh(
        hass,
        entry,
        "update_contents",
        {CONF_TRACKING_CODE: DELIVERED_CODE, "contents": "Running shoes"},
    )

    assert coordinator.delivered[0]["contents"] == "Running shoes"


async def test_update_contents_rejects_an_empty_code(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, "update_contents", {CONF_TRACKING_CODE: ""})


async def test_update_contents_needs_a_loaded_entry(hass):
    entry = await _setup(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
        )


async def test_track_parcel_stores_contents_for_a_new_code(hass):
    entry = await _setup(hass)
    await _call(
        hass,
        "track_parcel",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )

    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: ACTIVE_CODE}]
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"


async def test_a_newly_tracked_code_still_refreshes_through_the_listener(hass):
    entry = await _setup(hass)
    with (
        patch(
            f"{CLIENT}.async_get_parcel", new=AsyncMock(return_value=active_sample())
        ),
        patch(f"{COORDINATOR}.async_request_refresh", new=AsyncMock()) as refresh,
    ):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert refresh.await_count == 1
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: ACTIVE_CODE}]


async def test_track_parcel_without_contents_is_unchanged(hass):
    entry = await _setup(hass)
    await _call(hass, "track_parcel", {CONF_TRACKING_CODE: ACTIVE_CODE})

    assert entry.runtime_data.coordinator.contents_store.get(ACTIVE_CODE) is None
    assert _contents(entry, ACTIVE_CODE) is None


async def test_track_parcel_replaces_and_clears_contents_of_a_tracked_code(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    track = {CONF_TRACKING_CODE: ACTIVE_CODE}

    await _call(hass, "track_parcel", {**track, "contents": "Running shoes"})
    assert _contents(entry, ACTIVE_CODE) == "Running shoes"

    await _call(hass, "track_parcel", {**track, "contents": "Winter socks"})
    assert _contents(entry, ACTIVE_CODE) == "Winter socks"

    await _call(hass, "track_parcel", {**track, "contents": ""})
    assert _contents(entry, ACTIVE_CODE) is None
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: ACTIVE_CODE}]


async def test_track_parcel_on_a_tracked_code_never_fetches_or_refreshes(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    data = {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"}

    listeners = await _call_no_refresh(hass, entry, "track_parcel", data)
    assert listeners.call_count == 1

    listeners = await _call_no_refresh(hass, entry, "track_parcel", data)
    assert listeners.call_count == 0


async def test_untrack_parcel_drops_the_contents(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )

    await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: ACTIVE_CODE})

    assert entry.runtime_data.coordinator.contents_store.get(ACTIVE_CODE) is None
    assert entry.options[CONF_PARCELS] == []


async def test_the_store_caps_at_200_codes_dropping_the_oldest(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    store = entry.runtime_data.coordinator.contents_store
    for number in range(CONTENTS_STORE_MAX_CODES + 1):
        await hass.services.async_call(
            DOMAIN,
            "update_contents",
            {CONF_TRACKING_CODE: f"PDN{number:010d}", "contents": f"Item {number}"},
            blocking=True,
        )

    assert store.get("PDN0000000000") is None
    assert store.get("PDN0000000001") == "Item 1"
    assert store.get(f"PDN{CONTENTS_STORE_MAX_CODES:010d}") is not None


async def test_an_entry_without_a_stored_file_works(hass, hass_storage):
    assert not hass_storage
    entry = await _setup(hass, [ACTIVE_CODE])

    assert _contents(entry, ACTIVE_CODE) is None


async def test_a_stored_value_survives_a_restart_of_the_entry(hass, hass_storage):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )
    store = entry.runtime_data.coordinator.contents_store
    await store._store.async_save(store._data_to_save())

    assert await hass.config_entries.async_unload(entry.entry_id)
    with patch(
        f"{CLIENT}.async_get_parcel", new=AsyncMock(return_value=active_sample())
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert _contents(entry, ACTIVE_CODE) == "Running shoes"


async def test_removing_the_entry_removes_its_store(hass, hass_storage):
    entry = await _setup(hass, [ACTIVE_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )
    store = entry.runtime_data.coordinator.contents_store
    await store._store.async_save(store._data_to_save())
    key = STORE_KEY % entry.entry_id
    assert key in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


async def test_a_contents_change_alone_fires_no_event(hass):
    entry = await _setup(hass, [ACTIVE_CODE])
    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", events.append)
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", events.append)
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", events.append)

    await _call_no_refresh(
        hass,
        entry,
        "update_contents",
        {CONF_TRACKING_CODE: ACTIVE_CODE, "contents": "Running shoes"},
    )

    assert events == []
