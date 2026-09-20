"""Statistics import logic tests (coordinator.import_statistics)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
import pytest

from custom_components.my_polenergia.const import DOMAIN
from custom_components.my_polenergia.coordinator import (
    PolEnergiaDataUpdateCoordinator,
)

from .conftest import (
    CUSTOMER_NUMBER,
    make_data,
    make_measurement_point,
    make_reading,
)

_ADD_STATS = (
    "custom_components.my_polenergia.coordinator"
    ".async_add_external_statistics"
)


def _make_coordinator(hass, client, entry) -> PolEnergiaDataUpdateCoordinator:
    return PolEnergiaDataUpdateCoordinator(
        hass=hass,
        client=client,
        customer_number=CUSTOMER_NUMBER,
        update_interval=timedelta(hours=24),
        config_entry=entry,
    )


def _streams(add_mock) -> tuple[dict, dict]:
    """Split captured async_add_external_statistics calls into energy/cost maps."""
    energy: dict[str, list] = {}
    cost: dict[str, list] = {}
    for call in add_mock.call_args_list:
        _hass, metadata, stats = call.args
        sid = metadata["statistic_id"]
        if sid.endswith("_energy"):
            energy[sid] = stats
        elif sid.endswith("_cost"):
            cost[sid] = stats
    return energy, cost


async def test_first_import_cumulative_sums(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """First import builds monotonically increasing cumulative sums from zero."""
    data = make_data([make_measurement_point("mp1")])
    mock_client.get_readings.return_value = [
        make_reading(2024, 1, 100.0),
        make_reading(2024, 2, 150.0),
        make_reading(2024, 3, 120.0),
    ]

    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    coord._last_stat_sum = AsyncMock(return_value=None)  # first run

    with patch(_ADD_STATS) as add_mock:
        await coord.import_statistics(data)

    energy, _ = _streams(add_mock)
    sums = [point["sum"] for point in energy[f"{DOMAIN}:mp1_energy"]]
    # 3 readings (100, 250, 370) + a zero-delta current-month anchor (370).
    assert sums == [100.0, 250.0, 370.0, 370.0]
    assert sums == sorted(sums)


async def test_resume_continues_without_duplication(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A resume only appends months newer than the last stored one."""
    data = make_data([make_measurement_point("mp1")])
    mock_client.get_readings.return_value = [
        make_reading(2024, 1, 100.0),
        make_reading(2024, 2, 150.0),
        make_reading(2024, 3, 120.0),
    ]

    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    # Last stored point is February with cumulative sum 250.
    last_ts = datetime(2024, 2, 28, tzinfo=UTC).timestamp()
    coord._last_stat_sum = AsyncMock(return_value=(250.0, last_ts))

    with patch(_ADD_STATS) as add_mock:
        await coord.import_statistics(data)

    energy, _ = _streams(add_mock)
    sums = [point["sum"] for point in energy[f"{DOMAIN}:mp1_energy"]]
    # Only March is new: 250 + 120 = 370, plus the current-month anchor.
    assert sums == [370.0, 370.0]


async def test_cost_is_energy_times_price(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """The cost stream equals energy × the configured price (default 0.95)."""
    data = make_data([make_measurement_point("mp1")])
    mock_client.get_readings.return_value = [make_reading(2024, 1, 100.0)]

    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    coord._last_stat_sum = AsyncMock(return_value=None)

    with patch(_ADD_STATS) as add_mock:
        await coord.import_statistics(data)

    energy, cost = _streams(add_mock)
    energy_sums = [p["sum"] for p in energy[f"{DOMAIN}:mp1_energy"]]
    cost_sums = [p["sum"] for p in cost[f"{DOMAIN}:mp1_cost"]]
    assert energy_sums[0] == pytest.approx(100.0)
    assert cost_sums[0] == pytest.approx(95.0)
    assert cost_sums[0] == pytest.approx(energy_sums[0] * 0.95)


async def test_multi_meter_readings_split_per_point(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Each meter's stream contains only its own readings (Phase 2 regression)."""
    mp1 = make_measurement_point("mp1", ppe="PL0001")
    mp2 = make_measurement_point("mp2", ppe="PL0002")
    data = make_data([mp1, mp2])
    mock_client.get_readings.return_value = [
        make_reading(2024, 1, 100.0, "mp1"),
        make_reading(2024, 1, 500.0, "mp2"),
        make_reading(2024, 2, 150.0, "mp1"),
        make_reading(2024, 2, 600.0, "mp2"),
    ]

    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    coord._last_stat_sum = AsyncMock(return_value=None)

    with patch(_ADD_STATS) as add_mock:
        await coord.import_statistics(data)

    energy, _ = _streams(add_mock)
    mp1_sums = [p["sum"] for p in energy[f"{DOMAIN}:mp1_energy"]]
    mp2_sums = [p["sum"] for p in energy[f"{DOMAIN}:mp2_energy"]]
    # mp1: 100, 250 (not polluted by mp2's 500/600).
    assert mp1_sums[:2] == [100.0, 250.0]
    # mp2: 500, 1100.
    assert mp2_sums[:2] == [500.0, 1100.0]


def _all_streams(add_mock) -> dict[str, list]:
    """Every captured stream, keyed by statistic_id."""
    return {call.args[1]["statistic_id"]: call.args[2] for call in add_mock.call_args_list}


async def test_zoned_readings_write_total_and_per_zone(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A two-zone meter gets per-zone streams *alongside* the unchanged total."""
    data = make_data([make_measurement_point("mp1")])
    mock_client.get_readings.return_value = [
        make_reading(2024, 1, 60.0, zone="z1"),
        make_reading(2024, 1, 40.0, zone="z2"),
        make_reading(2024, 2, 90.0, zone="z1"),
        make_reading(2024, 2, 60.0, zone="z2"),
    ]
    mock_config_entry.add_to_hass(hass)
    coordinator = _make_coordinator(hass, mock_client, mock_config_entry)

    with patch(_ADD_STATS) as add_stats, patch.object(
        coordinator, "_last_stat_sum", AsyncMock(return_value=None)
    ):
        await coordinator.import_statistics(data, full_rebuild=True)

    streams = _all_streams(add_stats)
    assert f"{DOMAIN}:mp1_energy" in streams
    assert f"{DOMAIN}:mp1_energy_z1" in streams
    assert f"{DOMAIN}:mp1_energy_z2" in streams

    # The total is the zones summed back together: 100 then 150 -> sums 100, 250.
    assert [s["sum"] for s in streams[f"{DOMAIN}:mp1_energy"]][:2] == [100.0, 250.0]
    assert [s["sum"] for s in streams[f"{DOMAIN}:mp1_energy_z1"]][:2] == [60.0, 150.0]
    assert [s["sum"] for s in streams[f"{DOMAIN}:mp1_energy_z2"]][:2] == [40.0, 100.0]

    # Zone sums reconcile with the total for every period.
    z1 = streams[f"{DOMAIN}:mp1_energy_z1"]
    z2 = streams[f"{DOMAIN}:mp1_energy_z2"]
    total = streams[f"{DOMAIN}:mp1_energy"]
    assert z1[-1]["sum"] + z2[-1]["sum"] == total[-1]["sum"]

    assert coordinator.zones_seen["mp1"] == ["z1", "z2"]


async def test_per_zone_prices_applied(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Each zone's cost uses its own price; an unset zone falls back to the base rate."""
    data = make_data([make_measurement_point("mp1")])
    mock_client.get_readings.return_value = [
        make_reading(2024, 1, 100.0, zone="z1"),
        make_reading(2024, 1, 50.0, zone="z2"),
    ]
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={"import_price": 0.95, "import_price_z2": 0.55}
    )
    coordinator = _make_coordinator(hass, mock_client, mock_config_entry)

    with patch(_ADD_STATS) as add_stats, patch.object(
        coordinator, "_last_stat_sum", AsyncMock(return_value=None)
    ):
        await coordinator.import_statistics(data, full_rebuild=True)

    streams = _all_streams(add_stats)
    # z1 has no dedicated price -> falls back to the single configured rate.
    assert streams[f"{DOMAIN}:mp1_cost_z1"][0]["state"] == pytest.approx(100.0 * 0.95)
    assert streams[f"{DOMAIN}:mp1_cost_z2"][0]["state"] == pytest.approx(50.0 * 0.55)


async def test_export_readings_go_to_return_stream(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Feed-in never lands in the consumption stream."""
    data = make_data([make_measurement_point("mp1")])
    mock_client.get_readings.return_value = [
        make_reading(2024, 1, 100.0),
        make_reading(2024, 1, 30.0, direction="export"),
    ]
    mock_config_entry.add_to_hass(hass)
    coordinator = _make_coordinator(hass, mock_client, mock_config_entry)

    with patch(_ADD_STATS) as add_stats, patch.object(
        coordinator, "_last_stat_sum", AsyncMock(return_value=None)
    ):
        await coordinator.import_statistics(data, full_rebuild=True)

    streams = _all_streams(add_stats)
    assert streams[f"{DOMAIN}:mp1_energy"][0]["state"] == 100.0
    assert streams[f"{DOMAIN}:mp1_return"][0]["state"] == 30.0
    assert f"{DOMAIN}:mp1_return_z1" not in streams


async def test_no_zone_streams_for_single_zone_account(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """G11 output is byte-identical: two streams, no zone or return suffixes."""
    data = make_data([make_measurement_point("mp1")])
    mock_client.get_readings.return_value = [make_reading(2024, 1, 100.0)]
    mock_config_entry.add_to_hass(hass)
    coordinator = _make_coordinator(hass, mock_client, mock_config_entry)

    with patch(_ADD_STATS) as add_stats, patch.object(
        coordinator, "_last_stat_sum", AsyncMock(return_value=None)
    ):
        await coordinator.import_statistics(data, full_rebuild=True)

    assert set(_all_streams(add_stats)) == {f"{DOMAIN}:mp1_energy", f"{DOMAIN}:mp1_cost"}


async def test_no_measurement_points_is_a_noop(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """An account with no meters writes nothing and does not query readings."""
    coord = _make_coordinator(hass, mock_client, mock_config_entry)

    with patch(_ADD_STATS) as add_mock:
        await coord.import_statistics(make_data([]))

    mock_client.get_readings.assert_not_awaited()
    add_mock.assert_not_called()


async def test_no_readings_writes_nothing(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """An empty readings window is not an error, just nothing to import."""
    mock_client.get_readings.return_value = []
    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    coord._last_stat_sum = AsyncMock(return_value=None)

    with patch(_ADD_STATS) as add_mock:
        await coord.import_statistics(make_data([make_measurement_point("mp1")]))

    add_mock.assert_not_called()


async def test_meter_without_readings_is_skipped(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """On a multi-meter account, a meter with no rows gets no streams at all."""
    mp1 = make_measurement_point("mp1", ppe="PL0001")
    mp2 = make_measurement_point("mp2", ppe="PL0002")
    mock_client.get_readings.return_value = [make_reading(2024, 1, 100.0, "mp1")]

    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    coord._last_stat_sum = AsyncMock(return_value=None)

    with patch(_ADD_STATS) as add_mock:
        await coord.import_statistics(make_data([mp1, mp2]))

    assert set(_all_streams(add_mock)) == {f"{DOMAIN}:mp1_energy", f"{DOMAIN}:mp1_cost"}


async def test_nothing_new_writes_nothing(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A resume with no newer month leaves the recorder untouched."""
    mock_client.get_readings.return_value = [make_reading(2024, 1, 100.0)]
    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    # Last stored point is already past the only reading.
    last_ts = datetime(2024, 6, 1, tzinfo=UTC).timestamp()
    coord._last_stat_sum = AsyncMock(return_value=(100.0, last_ts))

    with patch(_ADD_STATS) as add_mock:
        await coord.import_statistics(make_data([make_measurement_point("mp1")]))

    add_mock.assert_not_called()


async def test_explicit_from_date_is_used_verbatim(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A caller-supplied start date wins over resume and agreement dates."""
    explicit = datetime(2019, 3, 1, tzinfo=UTC)
    mock_client.get_readings.return_value = [make_reading(2024, 1, 100.0)]
    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    coord._last_stat_sum = AsyncMock(return_value=None)

    with patch(_ADD_STATS):
        await coord.import_statistics(
            make_data([make_measurement_point("mp1")]), from_date=explicit
        )

    assert mock_client.get_readings.await_args.kwargs["from_date"] == explicit
    mock_client.get_earliest_agreement_date.assert_not_awaited()


async def test_resume_window_looks_back_from_last_point(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A resume re-fetches ~3 months so a late correction is still picked up."""
    last = datetime(2024, 6, 30, tzinfo=UTC)
    mock_client.get_readings.return_value = [make_reading(2024, 7, 10.0)]
    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    coord._last_stat_sum = AsyncMock(return_value=(100.0, last.timestamp()))

    with patch(_ADD_STATS):
        await coord.import_statistics(make_data([make_measurement_point("mp1")]))

    fetch_from = mock_client.get_readings.await_args.kwargs["from_date"]
    assert last - timedelta(days=96) <= fetch_from <= last - timedelta(days=94)


async def test_fallback_history_without_agreement_date(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """With no agreement date the importer falls back to a two-year window."""
    mock_client.get_earliest_agreement_date.return_value = None
    mock_client.get_readings.return_value = [make_reading(2024, 1, 100.0)]
    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    coord._last_stat_sum = AsyncMock(return_value=None)

    with patch(_ADD_STATS):
        await coord.import_statistics(make_data([make_measurement_point("mp1")]))

    fetch_from = mock_client.get_readings.await_args.kwargs["from_date"]
    assert datetime.now(tz=UTC) - fetch_from >= timedelta(days=729)


async def test_negative_reading_cannot_lower_the_sum(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A negative correction row is clamped, never walking the sum backwards."""
    mock_client.get_readings.return_value = [
        make_reading(2024, 1, 100.0),
        make_reading(2024, 2, -30.0),
    ]
    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    coord._last_stat_sum = AsyncMock(return_value=None)

    with patch(_ADD_STATS) as add_mock:
        await coord.import_statistics(make_data([make_measurement_point("mp1")]))

    sums = [p["sum"] for p in _all_streams(add_mock)[f"{DOMAIN}:mp1_energy"]]
    assert sums == sorted(sums)
    assert sums[:2] == [100.0, 100.0]


async def test_zoned_export_writes_per_zone_return_streams(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A zoned prosumer meter gets per-zone return streams beside the total."""
    mock_client.get_readings.return_value = [
        make_reading(2024, 1, 100.0, zone="z1"),
        make_reading(2024, 1, 30.0, zone="z1", direction="export"),
        make_reading(2024, 1, 20.0, zone="z2", direction="export"),
    ]
    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    coord._last_stat_sum = AsyncMock(return_value=None)

    with patch(_ADD_STATS) as add_mock:
        await coord.import_statistics(make_data([make_measurement_point("mp1")]))

    streams = _all_streams(add_mock)
    assert streams[f"{DOMAIN}:mp1_return"][0]["state"] == 50.0
    assert streams[f"{DOMAIN}:mp1_return_z1"][0]["state"] == 30.0
    assert streams[f"{DOMAIN}:mp1_return_z2"][0]["state"] == 20.0


async def test_return_stream_resumes_without_duplication(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """The return stream skips months it has already written."""
    mock_client.get_readings.return_value = [
        make_reading(2024, 1, 30.0, direction="export"),
    ]
    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    last_ts = datetime(2024, 6, 1, tzinfo=UTC).timestamp()
    coord._last_stat_sum = AsyncMock(return_value=(30.0, last_ts))

    with patch(_ADD_STATS) as add_mock:
        await coord.import_statistics(make_data([make_measurement_point("mp1")]))

    assert f"{DOMAIN}:mp1_return" not in _all_streams(add_mock)


async def test_tariff_zone_count_prefers_observed_zones(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Zones seen in the data beat the count implied by the tariff code."""
    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    mp = make_measurement_point("mp1")

    assert coord.tariff_zone_count([mp]) == 1  # G11
    coord.zones_seen = {"mp1": ["z1", "z2"]}
    assert coord.tariff_zone_count([mp]) == 2
    assert coord.tariff_zone_count([]) == 2


async def test_last_stat_sum_reads_the_recorder(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """The resume helper returns (sum, start) from stored statistics, else None."""
    coord = _make_coordinator(hass, mock_client, mock_config_entry)

    async def _run_in_executor(rows):
        instance = MagicMock()
        instance.async_add_executor_job = AsyncMock(return_value=rows)
        return instance

    with patch(
        "custom_components.my_polenergia.coordinator.get_instance",
        return_value=await _run_in_executor(
            {"my_polenergia:mp1_energy": [{"sum": 12.5, "start": 1700000000.0}]}
        ),
    ):
        assert await coord._last_stat_sum("my_polenergia:mp1_energy") == (
            12.5,
            1700000000.0,
        )

    with patch(
        "custom_components.my_polenergia.coordinator.get_instance",
        return_value=await _run_in_executor({}),
    ):
        assert await coord._last_stat_sum("my_polenergia:mp1_energy") is None
