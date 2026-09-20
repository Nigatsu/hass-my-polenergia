"""Config and options flow tests."""

from types import SimpleNamespace

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.my_polenergia.const import (
    CONF_CUSTOMER_NUMBER,
    CONF_IMPORT_PRICE,
    DOMAIN,
)
from custom_components.my_polenergia.polenergia.errors import (
    PolEnergiaConnectionError,
    PolEnergiaError,
)

from .conftest import (
    ACCOUNT_NAME,
    CUSTOMER_NUMBER,
    PASSWORD,
    USERNAME,
    make_data,
    make_measurement_point,
)

USER_INPUT = {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}


async def test_user_flow_single_customer(
    hass: HomeAssistant, mock_client, bypass_setup
) -> None:
    """Single customer number → entry created directly."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Polenergia ({ACCOUNT_NAME})"
    assert result["data"][CONF_CUSTOMER_NUMBER] == CUSTOMER_NUMBER
    assert result["data"][CONF_USERNAME] == USERNAME
    assert result["result"].unique_id == f"{USERNAME}_{CUSTOMER_NUMBER}"


async def test_user_flow_multiple_customers(
    hass: HomeAssistant, mock_client, bypass_setup
) -> None:
    """Multiple customer numbers → selection step → entry created."""
    mock_client.get_customer_numbers.return_value = ["111", "222"]

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "customer_number"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CUSTOMER_NUMBER: "222"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CUSTOMER_NUMBER] == "222"
    assert result["result"].unique_id == f"{USERNAME}_222"


@pytest.mark.parametrize(
    ("setup_mock", "expected_error"),
    [
        ("auth_false", "invalid_auth"),
        ("connection", "cannot_connect"),
        ("unexpected", "unknown"),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant, mock_client, setup_mock: str, expected_error: str, bypass_setup
) -> None:
    """Authentication problems surface as a form error the user can recover from."""
    if setup_mock == "auth_false":
        mock_client.authenticate.return_value = False
    elif setup_mock == "connection":
        mock_client.authenticate.side_effect = PolEnergiaConnectionError("boom")
    else:
        mock_client.authenticate.side_effect = RuntimeError("boom")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected_error}

    # Recovery: the same flow succeeds once the problem is gone.
    mock_client.authenticate.side_effect = None
    mock_client.authenticate.return_value = True
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CUSTOMER_NUMBER] == CUSTOMER_NUMBER


@pytest.mark.parametrize(
    ("customer_numbers", "expected_error"),
    [
        ([], "no_customer_numbers"),
    ],
)
async def test_user_flow_no_customer_numbers_recovers(
    hass: HomeAssistant,
    mock_client,
    bypass_setup,
    customer_numbers: list[str],
    expected_error: str,
) -> None:
    """An account with no customer numbers errors, then recovers."""
    mock_client.get_customer_numbers.return_value = customer_numbers

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["errors"] == {"base": expected_error}

    mock_client.get_customer_numbers.return_value = [CUSTOMER_NUMBER]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_missing_access_token_recovers(
    hass: HomeAssistant, mock_client, bypass_setup
) -> None:
    """Authentication without a token errors, then recovers."""
    mock_client.connector.access_token = None

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["errors"] == {"base": "no_access_token"}

    mock_client.connector.access_token = "access-token"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_api_error_recovers(
    hass: HomeAssistant, mock_client, bypass_setup
) -> None:
    """A generic client error errors, then recovers."""
    mock_client.authenticate.side_effect = PolEnergiaError("boom")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["errors"] == {"base": "unknown"}

    mock_client.authenticate.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_duplicate_aborts(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """An already-configured account aborts the flow."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_success(
    hass: HomeAssistant, mock_client, mock_config_entry, bypass_setup
) -> None:
    """Reauth with a valid password updates stored credentials."""
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-password"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "new-password"


async def test_reauth_wrong_password_then_recovers(
    hass: HomeAssistant, mock_client, mock_config_entry, bypass_setup
) -> None:
    """A bad password shows an error; the retry updates the stored credentials."""
    mock_config_entry.add_to_hass(hass)
    mock_client.authenticate.return_value = False

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert mock_config_entry.data[CONF_PASSWORD] == PASSWORD

    mock_client.authenticate.return_value = True
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-password"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "new-password"


async def test_reauth_connection_error_recovers(
    hass: HomeAssistant, mock_client, mock_config_entry, bypass_setup
) -> None:
    """A transient connection failure during reauth is recoverable."""
    mock_config_entry.add_to_hass(hass)
    mock_client.authenticate.side_effect = PolEnergiaConnectionError("down")

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-password"}
    )
    assert result["errors"] == {"base": "cannot_connect"}

    mock_client.authenticate.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-password"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"


async def test_reauth_unexpected_error_recovers(
    hass: HomeAssistant, mock_client, mock_config_entry, bypass_setup
) -> None:
    """An unexpected error during reauth is reported and recoverable."""
    mock_config_entry.add_to_hass(hass)
    mock_client.authenticate.side_effect = RuntimeError("boom")

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-password"}
    )
    assert result["errors"] == {"base": "unknown"}

    mock_client.authenticate.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-password"}
    )
    assert result["type"] is FlowResultType.ABORT


async def test_reconfigure_updates_password(
    hass: HomeAssistant, mock_client, mock_config_entry, bypass_setup
) -> None:
    """Reconfigure replaces the stored password without touching options."""
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "rotated"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "rotated"
    assert mock_config_entry.data[CONF_USERNAME] == USERNAME


async def test_reconfigure_wrong_password_recovers(
    hass: HomeAssistant, mock_client, mock_config_entry, bypass_setup
) -> None:
    """A rejected password shows an error, then the retry is accepted."""
    mock_config_entry.add_to_hass(hass)
    mock_client.authenticate.return_value = False

    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    assert mock_config_entry.data[CONF_PASSWORD] == PASSWORD

    mock_client.authenticate.return_value = True
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "rotated"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert mock_config_entry.data[CONF_PASSWORD] == "rotated"


async def test_options_set_price(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """The set-price option round-trips into entry options."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "set_price"}
    )
    assert result["step_id"] == "set_price"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IMPORT_PRICE: 1.23}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options[CONF_IMPORT_PRICE] == 1.23


async def test_options_scan_interval(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """The scan-interval option round-trips into entry options."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "scan_interval"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 3600}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options[CONF_SCAN_INTERVAL] == 3600


async def test_options_set_price_single_zone_form(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A G11 account still sees exactly one price field (plus the zone override)."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "set_price"}
    )

    keys = {str(marker) for marker in result["data_schema"].schema}
    assert CONF_IMPORT_PRICE in keys
    assert "import_price_z2" not in keys


async def test_options_set_price_zoned_form(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """When readings carried zones, one price field per zone is rendered."""
    mock_config_entry.add_to_hass(hass)
    # The options flow reads the zones the coordinator actually saw, so that the
    # form needs no live API call.
    mock_config_entry.runtime_data = SimpleNamespace(
        zones_seen={"mp1": ["z1", "z2"]}, data=None
    )

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "set_price"}
    )

    keys = {str(marker) for marker in result["data_schema"].schema}
    # Zone 1 reuses the existing key so a single-rate setting carries over.
    assert CONF_IMPORT_PRICE in keys
    assert "import_price_z2" in keys

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IMPORT_PRICE: 1.10, "import_price_z2": 0.55}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options["import_price_z2"] == 0.55


async def test_options_set_price_zone_override(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """The manual zone override renders zone fields even with no detected zones."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_config_entry, options={"zone_count": "3"})

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "set_price"}
    )

    keys = {str(marker) for marker in result["data_schema"].schema}
    assert {"import_price_z2", "import_price_z3"} <= keys


async def test_options_reload_history_calls_service(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """The reload branch forwards an explicit start date to the service."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "reload_history"}
    )
    assert result["step_id"] == "reload_history"

    calls = async_mock_service(hass, DOMAIN, "reload_statistics")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"from_date": "2020-01-01"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(calls) == 1
    assert calls[0].data == {"from_date": "2020-01-01"}


async def test_options_reload_history_without_date(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """An empty start date means 'use the agreement start date'."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "reload_history"}
    )
    calls = async_mock_service(hass, DOMAIN, "reload_statistics")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"from_date": ""}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(calls) == 1
    assert calls[0].data == {}


async def test_options_reload_history_invalid_date_recovers(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A malformed date is rejected in the form, then accepted on retry."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "reload_history"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"from_date": "not-a-date"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_date"}

    async_mock_service(hass, DOMAIN, "reload_statistics")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"from_date": "2021-06-01"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_options_clear_stats_confirmed(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Confirming the clear branch calls the service."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "clear_stats"}
    )
    assert result["step_id"] == "clear_stats"

    calls = async_mock_service(hass, DOMAIN, "clear_statistics")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": True}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(calls) == 1


async def test_options_clear_stats_declined(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Declining the confirmation leaves the statistics alone."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "clear_stats"}
    )
    calls = async_mock_service(hass, DOMAIN, "clear_statistics")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": False}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert not calls


async def test_options_price_fields_from_tariff_when_no_zones_seen(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """With no zones in the data, the tariff group decides the price fields."""
    mock_config_entry.add_to_hass(hass)
    mp = make_measurement_point("mp1")
    mp.tariff = "G12w"
    mock_config_entry.runtime_data = SimpleNamespace(
        zones_seen={}, data={"data": make_data([mp])}
    )

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "set_price"}
    )

    keys = {str(marker) for marker in result["data_schema"].schema}
    assert "import_price_z2" in keys
    assert "import_price_z3" not in keys


async def test_options_zone_override_ignores_garbage(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A non-numeric zone override falls through to detection instead of raising."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={"zone_count": "many"}
    )

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "set_price"}
    )

    keys = {str(marker) for marker in result["data_schema"].schema}
    assert CONF_IMPORT_PRICE in keys
    assert "import_price_z2" not in keys


async def test_options_price_fields_from_stored_zone_prices(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """With the entry unloaded, previously stored zone prices drive the form."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={"import_price_z2": 0.55}
    )

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "set_price"}
    )

    keys = {str(marker) for marker in result["data_schema"].schema}
    assert "import_price_z2" in keys


async def test_options_scan_interval_default_from_timedelta(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A stored plain-int interval is shown as-is in the form."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_SCAN_INTERVAL: 1800}
    )

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "scan_interval"}
    )

    marker = next(
        m for m in result["data_schema"].schema if str(m) == CONF_SCAN_INTERVAL
    )
    assert marker.default() == 1800
