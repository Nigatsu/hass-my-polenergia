"""Constants for My PolEnergia integration."""

from datetime import timedelta

# Integration domain
DOMAIN = "my_polenergia"

# Configuration keys
CONF_CUSTOMER_NUMBER = "customer_number"
CONF_PASSWORD = "password"
CONF_ACCOUNT_NAME = "account_name"
CONF_HISTORICAL_IMPORT_DONE = "historical_import_done"
CONF_IMPORT_PRICE = "import_price"

# Data update interval
DEFAULT_SCAN_INTERVAL = timedelta(hours=24)
MIN_SCAN_INTERVAL = timedelta(minutes=15)

# Pricing
DEFAULT_IMPORT_PRICE = 0.95  # PLN/kWh — placeholder, user must set
CURRENCY_PLN = "PLN"

# Attribute keys
ATTR_LAST_UPDATE = "last_update"
ATTR_PPE = "ppe"
ATTR_TARIFF = "tariff"
ATTR_ADDRESS = "address"
ATTR_CUSTOMER_NUMBER = "customer_number"
ATTR_ACCOUNT_NAME = "account_name"
