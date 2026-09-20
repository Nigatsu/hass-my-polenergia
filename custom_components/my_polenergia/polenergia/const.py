"""Constants for PolEnergia API client."""

# OAuth2/OpenID Connect endpoints
AUTH_BASE_URL = "https://logowanie.polenergia.pl"
AUTH_LOGIN_URL = f"{AUTH_BASE_URL}/Account/Login"
AUTH_TOKEN_URL = f"{AUTH_BASE_URL}/connect/token"
AUTH_AUTHORIZE_URL = f"{AUTH_BASE_URL}/connect/authorize/callback"

# API endpoints
API_BASE_URL = "https://api.polenergia.pl/api/v1"

# OAuth2 configuration
CLIENT_ID = "mBok_web"
REDIRECT_URI = "https://moja.polenergia.pl/authentication/callback"
# Adding "offline_access" here does not work: tested against the live server on
# 2026-09-19, the authorize step then returns no code at all. There is no
# refresh token to be had. See CLAUDE.md section 4.
SCOPE = "openid mbok_api"
RESPONSE_TYPE = "code"

# HTTP headers
USER_AGENT = "HomeAssistant-Polenergia/1.0"
