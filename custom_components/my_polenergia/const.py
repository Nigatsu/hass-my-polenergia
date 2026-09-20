"""Constants for My PolEnergia integration."""

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

# Integration domain
DOMAIN = "my_polenergia"

# Configuration keys
CONF_CUSTOMER_NUMBER = "customer_number"
CONF_PASSWORD = "password"
CONF_ACCOUNT_NAME = "account_name"
CONF_IMPORT_PRICE = "import_price"
# Per-zone prices are stored as "import_price_z2" / "import_price_z3"; zone 1
# reuses CONF_IMPORT_PRICE so single-zone users keep their existing setting.
CONF_ZONE_COUNT = "zone_count"
ZONE_COUNT_AUTO = "auto"


def import_price_key(zone: str | None) -> str:
    """Option key holding the price for a tariff zone."""
    return f"{CONF_IMPORT_PRICE}_{zone}" if zone else CONF_IMPORT_PRICE


# Data update interval
DEFAULT_SCAN_INTERVAL = timedelta(hours=24)
MIN_SCAN_INTERVAL = timedelta(minutes=15)

# Pricing
DEFAULT_IMPORT_PRICE = 0.95  # PLN/kWh — placeholder, user must set
CURRENCY_PLN = "PLN"


def price_options(options: Mapping[str, Any]) -> dict[str, float]:
    """The effective import prices, keyed by option name.

    Used to tell a rate change apart from any other options edit, so only a
    price change triggers a cost rebuild. The base rate is always present at
    its default, so first-time configuring of the placeholder value does not
    read as a change.
    """
    prices = {CONF_IMPORT_PRICE: DEFAULT_IMPORT_PRICE}
    prices.update({
        key: float(value)
        for key, value in options.items()
        if key.startswith(CONF_IMPORT_PRICE) and value is not None
    })
    return prices


# Repair issue ids (also used as translation keys under "issues" in strings.json)
ISSUE_IMPORT_PRICE_UNSET = "import_price_unset"
ISSUE_NO_READINGS = "no_readings"

# Attribute keys
ATTR_LAST_UPDATE = "last_update"
ATTR_PPE = "ppe"
ATTR_TARIFF = "tariff"
ATTR_ZONE_COUNT = "zone_count"
ATTR_ADDRESS = "address"
ATTR_CUSTOMER_NUMBER = "customer_number"
ATTR_ACCOUNT_NAME = "account_name"
