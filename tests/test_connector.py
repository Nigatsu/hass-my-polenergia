"""OAuth PKCE connector: login flow, token lifetime and authenticated GETs.

The connector accepts an injected session, so the whole flow can be driven
through a stand-in that records what was sent and replays canned responses —
no HTTP, and no Home Assistant, involved.
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import aiohttp
import pytest

from custom_components.my_polenergia.polenergia.connector import PolEnergiaConnector
from custom_components.my_polenergia.polenergia.errors import (
    PolEnergiaAuthorizationError,
    PolEnergiaConnectionError,
)

LOGIN_HTML = (
    '<form><input name="__RequestVerificationToken" type="hidden" '
    'value="csrf-123" /></form>'
)


class FakeResponse:
    """Minimal stand-in for an aiohttp response used as a context manager."""

    def __init__(self, *, status=200, text="", json=None, url="https://example.invalid/"):
        """Store the canned payload this response will serve."""
        self.status = status
        self._text = text
        self._json = json
        self.url = url

    async def __aenter__(self):
        """Enter the response context."""
        return self

    async def __aexit__(self, *exc):
        """Leave the response context."""
        return False

    async def text(self):
        """Body as text."""
        return self._text

    async def json(self):
        """Body parsed as JSON."""
        return self._json

    def raise_for_status(self):
        """Raise for a 4xx/5xx status, like aiohttp does.

        ClientResponseError needs a real RequestInfo to format itself, so the
        stub raises its base class — the connector only distinguishes
        ClientError from everything else.
        """
        if self.status >= 400:
            raise aiohttp.ClientError(f"HTTP {self.status}")


class FakeSession:
    """Session stub that dispatches on URL and records every call."""

    closed = False

    def __init__(self, *, login_html=LOGIN_HTML, token=None, token_status=200, api=None):
        """Configure the canned login page, token response and API payload."""
        self.login_html = login_html
        self.token = token if token is not None else {"access_token": "tok", "expires_in": 1800}
        self.token_status = token_status
        self.api = api if api is not None else {"ok": True}
        self.calls: list[tuple[str, str, dict]] = []
        self.api_status = 200
        self.login_status = 200

    @property
    def scopes_requested(self) -> list[str]:
        """The `scope` of each login attempt, in order.

        Counted off the login-page GET only; the follow-up POST repeats the same
        ReturnUrl, so counting both would double every attempt.
        """
        return [
            parse_qs(urlparse(kw["params"]["ReturnUrl"]).query)["scope"][0]
            for method, url, kw in self.calls
            if method == "GET" and "Account/Login" in url
        ]

    def grants(self) -> list[str]:
        """The `grant_type` of every token-endpoint call, in order."""
        return [
            c[2]["data"]["grant_type"]
            for c in self.calls
            if c[0] == "POST" and "connect/token" in c[1]
        ]

    def get(self, url, params=None, headers=None, timeout=None):
        """Serve the login page or an API payload."""
        self.calls.append(("GET", url, {"params": params, "headers": headers}))
        if "Account/Login" in url:
            return FakeResponse(status=self.login_status, text=self.login_html)
        return FakeResponse(status=self.api_status, json=self.api)

    def post(self, url, data=None, headers=None, timeout=None, allow_redirects=None):
        """Serve the login POST (redirect) or the token exchange."""
        self.calls.append(("POST", url, {"data": data, "headers": headers}))
        if "connect/token" in url:
            return FakeResponse(status=self.token_status, json=self.token, text="denied")

        # Echo the state the connector generated, as the real redirect does.
        state = parse_qs(urlparse(data["ReturnUrl"]).query)["state"][0]
        return FakeResponse(
            url=f"https://moja.polenergia.pl/authentication/callback?code=abc&state={state}"
        )

    def post_data(self, needle: str) -> dict:
        """The form body of the first POST whose URL contains ``needle``."""
        return next(c[2]["data"] for c in self.calls if c[0] == "POST" and needle in c[1])


@pytest.fixture
def session() -> FakeSession:
    """A default happy-path session stub."""
    return FakeSession()


async def test_authenticate_happy_path(session: FakeSession) -> None:
    """A full PKCE login stores a token and reports authenticated."""
    connector = PolEnergiaConnector(session=session)

    assert await connector.authenticate("user@example.com", "pw") is True
    assert connector.access_token == "tok"
    assert connector.is_authenticated

    login = session.post_data("Account/Login")
    assert login["Email"] == "user@example.com"
    assert login["Password"] == "pw"
    assert login["__RequestVerificationToken"] == "csrf-123"
    assert login["button"] == "login"

    token = session.post_data("connect/token")
    assert token["grant_type"] == "authorization_code"
    assert token["code"] == "abc"
    # PKCE: the verifier sent here must match the challenge in the auth request.
    assert token["code_verifier"]
    assert "client_secret" not in token


async def test_authenticate_without_csrf_token_still_posts() -> None:
    """A login page with no CSRF field does not abort the flow."""
    session = FakeSession(login_html="<form></form>")
    connector = PolEnergiaConnector(session=session)

    assert await connector.authenticate("user@example.com", "pw") is True
    assert "__RequestVerificationToken" not in session.post_data("Account/Login")


async def test_csrf_token_reversed_attribute_order() -> None:
    """The token is found whether value or name comes first."""
    session = FakeSession(
        login_html='<input value="csrf-xyz" name="__RequestVerificationToken">'
    )
    connector = PolEnergiaConnector(session=session)

    await connector.authenticate("user@example.com", "pw")
    assert session.post_data("Account/Login")["__RequestVerificationToken"] == "csrf-xyz"


async def test_login_page_error_raises(session: FakeSession) -> None:
    """A non-200 login page is an authorization failure."""
    session.login_status = 503
    connector = PolEnergiaConnector(session=session)

    with pytest.raises(PolEnergiaAuthorizationError, match="503"):
        await connector.authenticate("user@example.com", "pw")


async def test_state_mismatch_rejected(session: FakeSession) -> None:
    """A redirect carrying someone else's state is refused (CSRF guard)."""
    session.post = lambda url, **kw: FakeResponse(
        url="https://moja.polenergia.pl/authentication/callback?code=abc&state=attacker"
    )
    connector = PolEnergiaConnector(session=session)

    with pytest.raises(PolEnergiaAuthorizationError, match="State parameter mismatch"):
        await connector.authenticate("user@example.com", "pw")


async def test_oauth_error_in_redirect(session: FakeSession) -> None:
    """An OAuth error in the callback is surfaced verbatim."""
    session.post = lambda url, **kw: FakeResponse(
        url="https://moja.polenergia.pl/authentication/callback?error=access_denied"
    )
    connector = PolEnergiaConnector(session=session)

    with pytest.raises(PolEnergiaAuthorizationError, match="access_denied"):
        await connector.authenticate("user@example.com", "pw")


async def test_missing_code_in_redirect(session: FakeSession) -> None:
    """Wrong credentials leave the user on a page with no code."""
    session.post = lambda url, **kw: FakeResponse(
        url="https://logowanie.polenergia.pl/Account/Login"
    )
    connector = PolEnergiaConnector(session=session)

    with pytest.raises(PolEnergiaAuthorizationError, match="Could not obtain"):
        await connector.authenticate("user@example.com", "pw")


async def test_token_exchange_rejected() -> None:
    """A non-200 token response fails authentication."""
    connector = PolEnergiaConnector(session=FakeSession(token_status=400))

    with pytest.raises(PolEnergiaAuthorizationError, match="Token exchange failed"):
        await connector.authenticate("user@example.com", "pw")


async def test_token_response_without_access_token() -> None:
    """A 200 with no token is still a failure, not a silent success."""
    connector = PolEnergiaConnector(session=FakeSession(token={"expires_in": 60}))

    with pytest.raises(PolEnergiaAuthorizationError, match="No access token"):
        await connector.authenticate("user@example.com", "pw")


async def test_connection_error_is_wrapped(session: FakeSession) -> None:
    """A transport failure becomes PolEnergiaConnectionError, not a raw aiohttp one."""

    def _boom(*args, **kwargs):
        raise aiohttp.ClientError("socket down")

    session.get = _boom
    connector = PolEnergiaConnector(session=session)

    with pytest.raises(PolEnergiaConnectionError, match="socket down"):
        await connector.authenticate("user@example.com", "pw")


async def test_unexpected_error_becomes_auth_error(session: FakeSession) -> None:
    """Any other failure is reported as an authentication problem."""

    def _boom(*args, **kwargs):
        raise ValueError("weird")

    session.get = _boom
    connector = PolEnergiaConnector(session=session)

    with pytest.raises(PolEnergiaAuthorizationError, match="weird"):
        await connector.authenticate("user@example.com", "pw")


async def test_token_expiry_is_utc_aware_and_early(session: FakeSession) -> None:
    """The token is treated as expired five minutes before it really is."""
    connector = PolEnergiaConnector(session=session)
    await connector.authenticate("user@example.com", "pw")

    assert connector._token_expiry.tzinfo is not None
    assert not connector._is_token_expired()

    # Four minutes of life left: inside the 5-minute safety margin.
    connector._token_expiry = datetime.now(tz=UTC) + timedelta(minutes=4)
    assert connector._is_token_expired()
    assert not connector.is_authenticated


async def test_token_expiry_unset_counts_as_expired() -> None:
    """Never having authenticated counts as expired."""
    connector = PolEnergiaConnector(session=FakeSession())
    assert connector._is_token_expired()
    assert not connector.is_authenticated


async def test_get_sends_bearer_token(session: FakeSession) -> None:
    """An authenticated GET carries the bearer token and returns parsed JSON."""
    connector = PolEnergiaConnector(session=session)
    await connector.authenticate("user@example.com", "pw")

    assert await connector.get("/accounts", params={"customerNumber": "1"}) == {"ok": True}

    method, url, kwargs = session.calls[-1]
    assert method == "GET"
    assert url == "https://api.polenergia.pl/api/v1/accounts"
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
    assert kwargs["params"] == {"customerNumber": "1"}


async def test_get_without_token_raises(session: FakeSession) -> None:
    """A GET before authenticating is an authorization error, not a 401 round-trip."""
    connector = PolEnergiaConnector(session=session)

    with pytest.raises(PolEnergiaAuthorizationError, match="expired or missing"):
        await connector.get("accounts")


async def test_get_401_raises_authorization_error(session: FakeSession) -> None:
    """A rejected token surfaces as the error that triggers re-login."""
    connector = PolEnergiaConnector(session=session)
    await connector.authenticate("user@example.com", "pw")
    session.api_status = 401

    with pytest.raises(PolEnergiaAuthorizationError, match="401"):
        await connector.get("accounts")


async def test_get_server_error_raises_connection_error(session: FakeSession) -> None:
    """A 500 becomes a connection error, so the coordinator retries."""
    connector = PolEnergiaConnector(session=session)
    await connector.authenticate("user@example.com", "pw")
    session.api_status = 500

    with pytest.raises(PolEnergiaConnectionError):
        await connector.get("accounts")


async def test_injected_session_is_never_closed(session: FakeSession) -> None:
    """Closing the connector must not close a session it does not own."""
    connector = PolEnergiaConnector(session=session)
    await connector.authenticate("user@example.com", "pw")

    await connector.close()

    assert session.closed is False
    assert connector.access_token is None


async def test_owned_session_is_created_and_closed() -> None:
    """With no injected session the connector makes — and closes — its own."""
    async with PolEnergiaConnector() as connector:
        own = connector._session
        assert isinstance(own, aiohttp.ClientSession)
        assert connector._owns_session

    assert own.closed


async def test_only_the_portal_scope_is_requested(session: FakeSession) -> None:
    """No offline_access.

    Verified against the live server on 2026-09-19: adding it makes the
    authorize step return no code at all, so a single plain-scope login is the
    only thing that works. This test is the guard against "helpfully" adding it
    back.
    """
    connector = PolEnergiaConnector(session=session)
    await connector.authenticate("user@example.com", "pw")

    assert session.scopes_requested == ["openid mbok_api"]
    assert "offline_access" not in session.scopes_requested[0]


async def test_expired_token_is_not_refreshed_in_place(session: FakeSession) -> None:
    """An expired token is handed back to the caller, which re-logs in.

    There is no refresh token to fall back on, so the connector must not try a
    refresh grant — the coordinator's re-login path owns this case.
    """
    connector = PolEnergiaConnector(session=session)
    await connector.authenticate("user@example.com", "pw")
    connector._token_expiry = datetime.now(tz=UTC) - timedelta(seconds=1)

    with pytest.raises(PolEnergiaAuthorizationError, match="expired or missing"):
        await connector.get("accounts")

    assert session.grants() == ["authorization_code"]
