"""API client: tariff detection and the readings parse/aggregate path.

The Agreements payload below is the real captured shape, including the
server-side misspelling of ``agreeementGroup``, with identifiers replaced.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from custom_components.my_polenergia.polenergia.client import PolEnergiaClient

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

    assert earliest == datetime(2020, 4, 25)
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
