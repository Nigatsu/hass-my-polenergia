"""Config and options flow tests."""

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest

from custom_components.my_polenergia.const import (
    CONF_CUSTOMER_NUMBER,
    CONF_IMPORT_PRICE,
    DOMAIN,
)
from custom_components.my_polenergia.polenergia.errors import (
    PolEnergiaConnectionError,
)

from .conftest import ACCOUNT_NAME, CUSTOMER_NUMBER, PASSWORD, USERNAME

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
    hass: HomeAssistant, mock_client, setup_mock: str, expected_error: str
) -> None:
    """Authentication problems surface as form errors, not a created entry."""
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


async def test_reauth_wrong_password(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Reauth with a bad password shows an error and keeps the old one."""
    mock_config_entry.add_to_hass(hass)
    mock_client.authenticate.return_value = False

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert mock_config_entry.data[CONF_PASSWORD] == PASSWORD


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
