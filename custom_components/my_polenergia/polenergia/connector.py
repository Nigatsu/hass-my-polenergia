"""OAuth2 connector for PolEnergia API."""

import base64
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

from .const import (
    AUTH_LOGIN_URL,
    AUTH_TOKEN_URL,
    AUTH_AUTHORIZE_URL,
    API_BASE_URL,
    CLIENT_ID,
    REDIRECT_URI,
    RESPONSE_TYPE,
    SCOPE,
    USER_AGENT,
)
from .errors import (
    PolEnergiaAuthorizationError,
    PolEnergiaConnectionError,
)

_LOGGER = logging.getLogger(__name__)

# Per-request timeout. Applied to individual requests rather than the session,
# because an injected HA-managed session is not ours to configure.
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class PolEnergiaConnector:
    """Handles OAuth2 authentication and HTTP requests to PolEnergia API."""

    def __init__(self, session: aiohttp.ClientSession | None = None):
        """Initialize the connector.

        If ``session`` is provided (e.g. an HA-managed session), it is used as-is
        and never closed by this connector — its lifecycle belongs to the owner.
        If omitted, the connector creates and owns its own session (used by the
        standalone scripts in ``dev/``).
        """
        self._session: aiohttp.ClientSession | None = session
        self._owns_session: bool = session is None
        self._access_token: str | None = None
        self._token_expiry: datetime | None = None
        self._code_verifier: str | None = None
        self._code_challenge: str | None = None

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
            self._owns_session = True

    async def close(self):
        """Close the session and clear token state.

        Only an owned (self-created) session is actually closed; an injected
        HA-managed session is left open for its owner to dispose of.
        """
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        self._access_token = None
        self._token_expiry = None

    def _generate_pkce_pair(self) -> tuple[str, str]:
        code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("utf-8").rstrip("=")
        challenge_bytes = hashlib.sha256(code_verifier.encode("utf-8")).digest()
        code_challenge = base64.urlsafe_b64encode(challenge_bytes).decode("utf-8").rstrip("=")
        return code_verifier, code_challenge

    def _extract_csrf_token(self, html: str) -> str | None:
        import re
        patterns = [
            r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',
            r'value="([^"]+)"[^>]*name="__RequestVerificationToken"',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    async def authenticate(self, username: str, password: str) -> bool:
        """Authenticate with PolEnergia using OAuth2 PKCE flow."""
        await self._ensure_session()

        try:
            self._code_verifier, self._code_challenge = self._generate_pkce_pair()
            state = secrets.token_urlsafe(16)

            auth_params = {
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "response_type": RESPONSE_TYPE,
                "scope": SCOPE,
                "state": state,
                "code_challenge": self._code_challenge,
                "code_challenge_method": "S256",
                "response_mode": "query",
            }
            return_url = f"/connect/authorize/callback?{urlencode(auth_params)}"

            # Load login page to get CSRF token
            async with self._session.get(
                AUTH_LOGIN_URL,
                params={"ReturnUrl": return_url},
                headers={"User-Agent": USER_AGENT},
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                if response.status != 200:
                    raise PolEnergiaAuthorizationError(f"Login page returned {response.status}")
                html = await response.text()
                csrf_token = self._extract_csrf_token(html)

            # Submit credentials
            login_data = {
                "ReturnUrl": return_url,
                "ClientId": CLIENT_ID,
                "Email": username,
                "Password": password,
                "button": "login",
            }
            if csrf_token:
                login_data["__RequestVerificationToken"] = csrf_token

            async with self._session.post(
                AUTH_LOGIN_URL,
                data=login_data,
                allow_redirects=True,
                headers={"User-Agent": USER_AGENT},
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                final_url = str(response.url)
                parsed_url = urlparse(final_url)
                query_params = parse_qs(parsed_url.query)

                if "code" in query_params:
                    auth_code = query_params["code"][0]
                    returned_state = query_params.get("state", [None])[0]
                    if returned_state != state:
                        raise PolEnergiaAuthorizationError("State parameter mismatch (CSRF protection)")
                    return await self._exchange_code_for_token(auth_code)

                error = query_params.get("error", [None])[0]
                if error:
                    raise PolEnergiaAuthorizationError(f"OAuth error: {error}")
                raise PolEnergiaAuthorizationError("Could not obtain authorization code")

        except aiohttp.ClientError as err:
            raise PolEnergiaConnectionError(f"Connection failed: {err}") from err
        except PolEnergiaAuthorizationError:
            raise
        except Exception as err:
            raise PolEnergiaAuthorizationError(f"Authentication failed: {err}") from err

    async def _exchange_code_for_token(self, auth_code: str) -> bool:
        token_data = {
            "client_id": CLIENT_ID,
            "code": auth_code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": self._code_verifier,
            "grant_type": "authorization_code",
        }

        try:
            async with self._session.post(
                AUTH_TOKEN_URL,
                data=token_data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": USER_AGENT,
                },
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                if response.status != 200:
                    response_text = await response.text()
                    _LOGGER.error("Token exchange failed: %s - %s", response.status, response_text[:200])
                    raise PolEnergiaAuthorizationError(f"Token exchange failed with status {response.status}")

                token_response = await response.json()

                self._access_token = token_response.get("access_token")
                if not self._access_token:
                    raise PolEnergiaAuthorizationError("No access token received")

                expires_in = token_response.get("expires_in", 1800)
                self._token_expiry = datetime.now() + timedelta(seconds=expires_in)

                _LOGGER.info("Authenticated successfully (token expires in %ds)", expires_in)
                return True

        except aiohttp.ClientError as err:
            raise PolEnergiaConnectionError(f"Token exchange connection failed: {err}") from err

    def _is_token_expired(self) -> bool:
        if not self._token_expiry:
            return True
        return datetime.now() >= self._token_expiry - timedelta(minutes=5)

    async def _ensure_valid_token(self):
        if not self._access_token or self._is_token_expired():
            raise PolEnergiaAuthorizationError("Access token expired or missing")

    async def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make authenticated GET request to API."""
        await self._ensure_session()
        await self._ensure_valid_token()

        url = f"{API_BASE_URL}/{endpoint.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "User-Agent": USER_AGENT,
        }

        try:
            async with self._session.get(
                url, params=params, headers=headers, timeout=_REQUEST_TIMEOUT
            ) as response:
                if response.status == 401:
                    raise PolEnergiaAuthorizationError("Access token rejected (401)")
                response.raise_for_status()
                return await response.json()

        except aiohttp.ClientError as err:
            raise PolEnergiaConnectionError(f"API request failed: {err}") from err

    @property
    def access_token(self) -> str | None:
        """Current access token, if any (read-only)."""
        return self._access_token

    @property
    def is_authenticated(self) -> bool:
        return self._access_token is not None and not self._is_token_expired()
