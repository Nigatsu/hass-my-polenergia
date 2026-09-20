"""API client: tariff detection and the readings parse/aggregate path.

The Agreements payload below is the real captured shape, including the
server-side misspelling of ``agreeementGroup``, with identifiers replaced.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from custom_components.my_polenergia.polenergia.client import PolEnergiaClient
from custom_components.my_polenergia.polenergia.errors import (
    PolEnergiaAPIError,
    PolEnergiaNoDataError,
)

AGREEMENTS_RESPONSE = {
    "start": 0,
    "size": 1,
    "total": 1,
    "results": [
        {
            "documentNumber": "0000/00/2020",
            "dateFrom": "2020-04-25T00:00:00",
            "dateTo": None,
            "agreementType": "SelfComprehensive",
            "agreeementGroup": "Tariff",
            "parameters": [
                {"type": "Tariff", "value": "G11"},
                {"type": "SettlementType", "value": "Miesięczny"},
            ],
            "agreementId": "62714",
            "status": "Active",
        }
    ],
}

MEASUREMENT_POINTS_RESPONSE = {
    "start": 0,
    "size": 1,
    "total": 1,
    "results": [
        {
            "measurementPointId": "140017222",
            "number": "590000000000000000",
            "addressLine1": "Main St 1",
            "addressLine2": "00-000 City",
            "agreementId": "62714",
            "lastConsumptionValue": 142.0,
            "consumptionUnit": "kWh",
            "isProsumer": False,
        }
    ],
}


@pytest.fixture
def client() -> PolEnergiaClient:
    """A client whose connector is mocked at the HTTP boundary."""
    api = PolEnergiaClient()
    api._connector = AsyncMock()
    return api


async def test_get_tariff_by_agreement(client: PolEnergiaClient) -> None:
    """The tariff code is read from Agreements.parameters, keyed by agreement id."""
    client._connector.get.return_value = AGREEMENTS_RESPONSE

    assert await client.get_tariff_by_agreement("300") == {"62714": "G11"}


async def test_agreements_fetched_once_per_refresh(client: PolEnergiaClient) -> None:
    """Tariff lookup and earliest-date lookup share a single round-trip."""
    client._connector.get.return_value = AGREEMENTS_RESPONSE

    await client.get_tariff_by_agreement("300")
    earliest = await client.get_earliest_agreement_date("300")

    # Naive API date, stamped Europe/Warsaw and normalised to UTC.
    assert earliest == datetime(2020, 4, 24, 22, 0, tzinfo=UTC)
    assert client._connector.get.call_count == 1


async def test_get_tariff_tolerates_missing_parameters(client: PolEnergiaClient) -> None:
    """A payload without a parameters list must not break the refresh."""
    client._connector.get.return_value = {"results": [{"agreementId": "1"}]}

    assert await client.get_tariff_by_agreement("300") == {}


async def test_measurement_points_get_tariff_stamped(client: PolEnergiaClient) -> None:
    """get_all_data joins the tariff onto each measurement point by agreement id."""

    async def fake_get(endpoint: str, params=None):
        if endpoint == "MeasurementPoints":
            return MEASUREMENT_POINTS_RESPONSE
        if endpoint == "Agreements":
            return AGREEMENTS_RESPONSE
        if endpoint == "MeasurementPoints/readings":
            return []
        if endpoint == "accounts":
            return {"name": "Someone"}
        return {}

    client._connector.get.side_effect = fake_get

    data = await client.get_all_data("300")

    assert data.tariffs == {"62714": "G11"}
    assert data.measurement_points[0].agreement_id == "62714"
    # The MeasurementPoints payload carries no tariff of its own — this value can
    # only have come from the Agreements join.
    assert data.measurement_points[0].tariff == "G11"


async def test_get_readings_aggregates_duplicate_periods(client: PolEnergiaClient) -> None:
    """Rows sharing a period collapse before they can become duplicate statistics."""
    client._connector.get.return_value = [
        {"date": "2025-01-31T00:00:00", "amount": 100.0, "measurementPointId": "mp1"},
        {"date": "2025-01-31T00:00:00", "amount": 50.0, "measurementPointId": "mp1"},
    ]

    readings = await client.get_readings(
        from_date=datetime(2025, 1, 1, tzinfo=UTC), to_date=datetime(2025, 2, 1, tzinfo=UTC)
    )

    assert len(readings) == 1
    assert readings[0].value == 150.0


async def test_get_readings_captures_evidence(client: PolEnergiaClient) -> None:
    """Unknown fields and a sample row are kept for diagnostics."""
    client._connector.get.return_value = {
        "results": [{"date": "2025-01-31T00:00:00", "amount": 1.0, "mysteryKey": 5}],
        "zones": [{"index": 0, "label": "Strefa całodobowa:"}],
    }

    await client.get_readings(
        from_date=datetime(2025, 1, 1, tzinfo=UTC), to_date=datetime(2025, 2, 1, tzinfo=UTC)
    )

    assert client.last_unknown_reading_fields == {"mysteryKey"}
    assert client.last_envelope_keys == ["results", "zones"]
    assert len(client.last_reading_sample) == 1


def _two_meter_responses(readings: list[dict]):
    """Route the connector by endpoint for a two-measurement-point account."""
    two_points = {
        "results": [
            {**MEASUREMENT_POINTS_RESPONSE["results"][0], "measurementPointId": "mp1"},
            {
                **MEASUREMENT_POINTS_RESPONSE["results"][0],
                "measurementPointId": "mp2",
                "number": "590000000000000001",
            },
        ]
    }

    async def _get(path: str, params=None):
        if path == "MeasurementPoints":
            return two_points
        if path == "MeasurementPoints/readings":
            return readings
        if path == "Agreements":
            return AGREEMENTS_RESPONSE
        return {}

    return _get


async def test_unmatched_readings_dropped_on_multi_meter_account(
    client: PolEnergiaClient,
) -> None:
    """A row with no measurement point id is not copied onto every meter."""
    client._connector.get.side_effect = _two_meter_responses(
        [
            {"date": "2025-01-31T00:00:00", "amount": 100.0, "measurementPointId": "mp1"},
            {"date": "2025-01-31T00:00:00", "amount": 999.0},
        ]
    )

    data = await client.get_all_data("30000000")

    assert [r.value for r in data.readings["mp1"]] == [100.0]
    assert data.readings["mp2"] == []


async def test_unmatched_readings_kept_on_single_meter_account(
    client: PolEnergiaClient,
) -> None:
    """On a one-meter account the API omits the id, so the row still counts."""

    async def _get(path: str, params=None):
        if path == "MeasurementPoints":
            return MEASUREMENT_POINTS_RESPONSE
        if path == "MeasurementPoints/readings":
            return [{"date": "2025-01-31T00:00:00", "amount": 100.0}]
        if path == "Agreements":
            return AGREEMENTS_RESPONSE
        return {}

    client._connector.get.side_effect = _get

    data = await client.get_all_data("30000000")

    assert [r.value for r in data.readings["140017222"]] == [100.0]


async def test_delegating_methods_reach_the_connector(client: PolEnergiaClient) -> None:
    """authenticate/close/is_authenticated are thin pass-throughs."""
    client._connector.authenticate.return_value = True
    assert await client.authenticate("u", "p") is True
    client._connector.authenticate.assert_awaited_once_with("u", "p")

    await client.close()
    client._connector.close.assert_awaited_once()

    assert client.connector is client._connector
    client._connector.is_authenticated = True
    assert client.is_authenticated is True


async def test_context_manager_delegates(client: PolEnergiaClient) -> None:
    """The client's async context manager drives the connector's."""
    async with client as entered:
        assert entered is client
        client._connector.__aenter__.assert_awaited_once()
    client._connector.__aexit__.assert_awaited_once()


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (["30000000", 30000001], ["30000000", "30000001"]),
        ({"customerNumbers": ["30000000"]}, ["30000000"]),
        ({"data": ["30000000"]}, ["30000000"]),
    ],
)
async def test_get_customer_numbers_shapes(
    client: PolEnergiaClient, response, expected: list[str]
) -> None:
    """Bare array, and both envelope spellings, are all accepted."""
    client._connector.get.return_value = response
    assert await client.get_customer_numbers() == expected


async def test_get_customer_numbers_rejects_garbage(client: PolEnergiaClient) -> None:
    """An unexpected payload type is an API error, not a crash later on."""
    client._connector.get.return_value = "nope"
    with pytest.raises(PolEnergiaAPIError):
        await client.get_customer_numbers()


async def test_get_account_name_variants(client: PolEnergiaClient) -> None:
    """The name is trimmed, falls back, and never breaks the refresh."""
    client._connector.get.return_value = {"name": "  ADA LOVELACE  "}
    assert await client.get_account_name("30000000") == "ADA LOVELACE"

    client._connector.get.return_value = {"correspondenceAddressName": "ADA"}
    assert await client.get_account_name("30000000") == "ADA"

    client._connector.get.return_value = {"name": None}
    assert await client.get_account_name("30000000") is None

    client._connector.get.side_effect = RuntimeError("boom")
    assert await client.get_account_name("30000000") is None


@pytest.mark.parametrize(
    ("response", "expected_len"),
    [([{"agreementId": "1"}], 1), ({"data": [{"agreementId": "1"}]}, 1), ("junk", 0)],
)
async def test_get_agreements_shapes(
    client: PolEnergiaClient, response, expected_len: int
) -> None:
    """Bare array, ``data`` envelope and an unusable payload are all handled."""
    client._connector.get.return_value = response
    assert len(await client.get_agreements("30000000")) == expected_len


async def test_get_tariff_ignores_malformed_entries(client: PolEnergiaClient) -> None:
    """Non-dict agreements and parameters are skipped rather than raising."""
    client._connector.get.return_value = [
        "not-a-dict",
        {"agreementId": None, "parameters": [{"type": "Tariff", "value": "G12"}]},
        {"agreementId": "1", "parameters": "not-a-list"},
        {"agreementId": "2", "parameters": ["nope", {"type": "Tariff", "value": "G13"}]},
    ]
    assert await client.get_tariff_by_agreement("30000000") == {"2": "G13"}


async def test_get_tariff_survives_agreements_failure(client: PolEnergiaClient) -> None:
    """Tariff is optional metadata — a failed lookup yields an empty map."""
    client._connector.get.side_effect = RuntimeError("boom")
    assert await client.get_tariff_by_agreement("30000000") == {}


async def test_earliest_agreement_date_skips_bad_dates(client: PolEnergiaClient) -> None:
    """Unparseable dates are ignored; the oldest valid one wins."""
    client._connector.get.return_value = [
        {"dateFrom": "not-a-date"},
        {"dateFrom": "2021-05-01T00:00:00"},
        {"dateFrom": "2019-01-01T00:00:00Z"},
        {},
    ]
    earliest = await client.get_earliest_agreement_date("30000000")
    # The Z-suffixed row is already UTC; the naive one is stamped Europe/Warsaw.
    assert earliest == datetime(2019, 1, 1, tzinfo=UTC)
    assert earliest.tzinfo is not None


async def test_earliest_agreement_date_is_utc_aware(client: PolEnergiaClient) -> None:
    """A naive API date becomes UTC-aware, stamped as Warsaw local time."""
    client._connector.get.return_value = [{"dateFrom": "2020-04-25T00:00:00"}]

    earliest = await client.get_earliest_agreement_date("30000000")

    assert earliest.tzinfo is not None
    # CEST in April: 00:00 Warsaw is 22:00 UTC the previous day.
    assert earliest == datetime(2020, 4, 24, 22, 0, tzinfo=UTC)


async def test_earliest_agreement_date_failure_returns_none(
    client: PolEnergiaClient,
) -> None:
    """A failed agreements call falls back to the two-year window upstream."""
    client._connector.get.side_effect = RuntimeError("boom")
    assert await client.get_earliest_agreement_date("30000000") is None


async def test_measurement_points_bare_array(client: PolEnergiaClient) -> None:
    """A bare array of measurement points is accepted."""
    client._connector.get.return_value = MEASUREMENT_POINTS_RESPONSE["results"]
    points = await client.get_measurement_points("30000000")
    assert [p.id for p in points] == ["140017222"]


async def test_measurement_points_empty_raises(client: PolEnergiaClient) -> None:
    """An account with no meters is a no-data error, not an empty success."""
    client._connector.get.return_value = {"results": []}
    with pytest.raises(PolEnergiaNoDataError):
        await client.get_measurement_points("30000000")


async def test_measurement_points_rejects_garbage(client: PolEnergiaClient) -> None:
    """An unexpected payload type is reported as an API error."""
    client._connector.get.return_value = "junk"
    with pytest.raises(PolEnergiaAPIError):
        await client.get_measurement_points("30000000")


async def test_get_readings_rejects_garbage(client: PolEnergiaClient) -> None:
    """An unexpected readings payload is an API error."""
    client._connector.get.return_value = "junk"
    with pytest.raises(PolEnergiaAPIError):
        await client.get_readings()


async def test_get_readings_defaults_to_last_year(client: PolEnergiaClient) -> None:
    """With no bounds the client asks for the trailing 365 days."""
    client._connector.get.return_value = []
    await client.get_readings()

    params = client._connector.get.await_args.kwargs["params"]
    start = datetime.strptime(params["from"], "%Y-%m-%dT%H:%M:%S.000Z")
    end = datetime.strptime(params["to"], "%Y-%m-%dT%H:%M:%S.000Z")
    assert (end - start).days == 365


async def test_export_rows_logged_once(client: PolEnergiaClient, caplog) -> None:
    """The 'export detected' notice is informational and not repeated."""
    client._connector.get.return_value = [
        {"date": "2025-01-31T00:00:00", "amount": -30.0, "measurementPointId": "mp1"},
    ]

    await client.get_readings()
    assert client._logged_export is True

    caplog.clear()
    await client.get_readings()
    assert "Export (feed-in) readings detected" not in caplog.text
