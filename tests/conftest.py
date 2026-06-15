"""Shared fixtures for My Polenergia tests."""

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.my_polenergia.const import (
    CONF_ACCOUNT_NAME,
    CONF_CUSTOMER_NUMBER,
    DOMAIN,
)
from custom_components.my_polenergia.polenergia.data import (
    EnergyReading,
    MeasurementPoint,
    PolEnergiaData,
)

pytest_plugins = "pytest_homeassistant_custom_component"

USERNAME = "user@example.com"
PASSWORD = "s3cret"
CUSTOMER_NUMBER = "140017222"
ACCOUNT_NAME = "MARCIN PYTERAF"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of the custom integration in every test."""
    yield


def make_measurement_point(
    mp_id: str = "mp1", ppe: str = "PL0001", address: str = "Main St 1"
) -> MeasurementPoint:
    """Build a measurement point."""
    return MeasurementPoint(
        id=mp_id,
        customer_number=CUSTOMER_NUMBER,
        ppe=ppe,
        address=address,
        tariff="G11",
        agreement_status="Active",
        raw_data={},
    )


def make_reading(
    year: int, month: int, value: float, mp_id: str | None = None
) -> EnergyReading:
    """Build a monthly reading anchored at the last instant of the month (UTC)."""
    # Anchor near end of month; exact day is irrelevant to the import logic.
    anchor = datetime(year, month, 28, 0, 0, tzinfo=UTC)
    return EnergyReading(
        timestamp=anchor,
        value=value,
        unit="kWh",
        measurement_point_id=mp_id,
    )


def make_data(
    measurement_points: list[MeasurementPoint] | None = None,
    readings: dict[str, list[EnergyReading]] | None = None,
) -> PolEnergiaData:
    """Build a PolEnergiaData container."""
    mps = measurement_points or [make_measurement_point()]
    return PolEnergiaData(
        customer_number=CUSTOMER_NUMBER,
        measurement_points=mps,
        readings=readings or {mp.id: [] for mp in mps},
        account_name=ACCOUNT_NAME,
        last_update=datetime.now(tz=UTC),
    )


@pytest.fixture
def mock_client() -> Iterator[AsyncMock]:
    """A mocked PolEnergiaClient, patched everywhere it is constructed.

    Patches the client class in both modules that build it, and the HA session
    helper so no real aiohttp session is created.
    """
    client = AsyncMock()
    client.authenticate.return_value = True
    client.get_customer_numbers.return_value = [CUSTOMER_NUMBER]
    client.get_account_name.return_value = ACCOUNT_NAME
    client.get_all_data.return_value = make_data()
    client.get_readings.return_value = []
    client.get_earliest_agreement_date.return_value = datetime(2020, 1, 1, tzinfo=UTC)
    client.close.return_value = None
    client.is_authenticated = True
    # connector.access_token is read by the config flow; keep it a plain truthy value.
    client.connector = MagicMock()
    client.connector.access_token = "access-token"

    with (
        patch(
            "custom_components.my_polenergia.PolEnergiaClient", return_value=client
        ),
        patch(
            "custom_components.my_polenergia.config_flow.PolEnergiaClient",
            return_value=client,
        ),
        patch("custom_components.my_polenergia.async_create_clientsession"),
        patch("custom_components.my_polenergia.config_flow.async_create_clientsession"),
    ):
        yield client


@pytest.fixture
def bypass_setup() -> Iterator[None]:
    """Skip entry setup so config-flow tests don't run the coordinator."""
    with patch(
        "custom_components.my_polenergia.async_setup_entry", return_value=True
    ):
        yield


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A config entry for the integration."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Polenergia ({ACCOUNT_NAME})",
        unique_id=f"{USERNAME}_{CUSTOMER_NUMBER}",
        data={
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: PASSWORD,
            CONF_CUSTOMER_NUMBER: CUSTOMER_NUMBER,
            CONF_ACCOUNT_NAME: ACCOUNT_NAME,
        },
        options={},
    )
