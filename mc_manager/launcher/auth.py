"""Browser PKCE login, Xbox/Minecraft token exchange, and renewable player sessions.

No account material goes to the panel. Service errors deliberately omit response
bodies and URLs so an OAuth code or token cannot appear in the launcher log.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Event
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

import httpx

from .credentials import TokenStore
from .installer import InstallCancelled

AUTHORITY = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
SCOPE = "XboxLive.signin offline_access"
MINECRAFT = "https://api.minecraftservices.com"


class AuthenticationError(ValueError):
    pass


class SignInRequired(AuthenticationError):
    pass


class MinecraftAccessDenied(AuthenticationError):
    """Microsoft/Xbox completed but Minecraft rejected the application."""


@dataclass(frozen=True)
class PlayerSession:
    client_id: str
    uuid: str
    name: str
    access_token: str = field(repr=False)
    expires_at: float
    xuid: str = ""

    def require_valid(self):
        if (not re.fullmatch(r"[0-9a-f]{32}", self.uuid)
                or not re.fullmatch(r"[A-Za-z0-9_]{1,16}", self.name)
                or not self.access_token or self.expires_at <= time.time() + 30):
            raise SignInRequired("The Minecraft session expired. Sign in again before playing.")


def _check_cancel(cancel):
    if cancel and cancel.is_set():
        raise InstallCancelled("Sign-in cancelled.")


def browser_authorization(client_id: str, *, cancel: Event | None = None,
                          open_browser=webbrowser.open, timeout=300) -> dict:
    """Receive a single state-verified code on an ephemeral loopback port."""
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(2)
            super().setup()

        def log_message(self, *args):
            pass  # The request contains an authorization code.

        def do_GET(self):
            parsed = urlsplit(self.path)
            try:
                params = parse_qs(parsed.query, max_num_fields=20)
            except ValueError:
                self.send_error(400, "Invalid sign-in callback")
                return
            valid = (parsed.path == "/" and self.headers.get("Host") == f"localhost:{self.server.server_port}"
                     and len(params.get("state", [])) == 1
                     and hmac.compare_digest(params["state"][0].encode("utf-8"), state.encode("ascii")))
            if not valid or result:
                self.send_error(400, "Invalid sign-in callback")
                return
            if "error" in params:
                result["error"] = True
            elif len(params.get("code", [])) == 1 and 0 < len(params["code"][0]) <= 16384:
                result["code"] = params["code"][0]
            else:
                self.send_error(400, "Missing sign-in code")
                return
            body = b"Return to Monkeycraft Launcher to finish signing in. You can close this tab."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = 0.25
        redirect = f"http://localhost:{server.server_port}"
        parameters = {"client_id": client_id, "response_type": "code", "redirect_uri": redirect,
                      "response_mode": "query", "scope": SCOPE, "state": state,
                      "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account"}
        _check_cancel(cancel)
        if not open_browser(AUTHORITY + "/authorize?" + urlencode(parameters)):
            raise AuthenticationError("Could not open the system browser. Set a default browser and try again.")
        deadline = time.monotonic() + timeout
        while not result:
            _check_cancel(cancel)
            if time.monotonic() >= deadline:
                raise AuthenticationError("Microsoft sign-in timed out. Try signing in again.")
            server.handle_request()
    _check_cancel(cancel)
    if "error" in result:
        raise AuthenticationError("Microsoft sign-in was declined or failed. Check the app registration and try again.")
    return {"grant_type": "authorization_code", "code": result["code"],
            "redirect_uri": redirect, "code_verifier": verifier}


class MicrosoftAccounts:
    def __init__(self, store: TokenStore, *, client_factory=httpx.Client, authorize=browser_authorization):
        self.store = store
        self.client_factory = client_factory
        self.authorize = authorize
        self._refresh = None
        self._client_id = None
        self.session: PlayerSession | None = None

    def _json(self, client, method, url, *, stage, cancel=None, **kwargs):
        _check_cancel(cancel)
        try:
            with client.stream(method, url, **kwargs) as response:
                raw = bytearray()
                for chunk in response.iter_bytes():
                    _check_cancel(cancel)
                    raw.extend(chunk)
                    if len(raw) > 1024 * 1024:
                        raise AuthenticationError(f"{stage} returned an oversized response.")
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise AuthenticationError(f"{stage} returned an invalid response.") from None
                if not isinstance(data, dict):
                    raise AuthenticationError(f"{stage} returned an invalid response.")
                if response.status_code >= 300:
                    if data.get("error") in {"invalid_grant", "interaction_required"}:
                        raise SignInRequired("Microsoft needs you to sign in again.")
                    xbox_errors = {2148916233: "Create an Xbox profile at xbox.com, then sign in again.",
                                   2148916235: "Xbox Live is unavailable for this account's region.",
                                   2148916236: "Complete the account's Xbox age verification before signing in.",
                                   2148916237: "Complete the account's Xbox age verification before signing in.",
                                   2148916238: "A parent must configure this child's Microsoft/Xbox family account."}
                    if data.get("XErr") in xbox_errors:
                        raise AuthenticationError(xbox_errors[data["XErr"]])
                    if stage == "Minecraft login" and response.status_code in {401, 403}:
                        raise MinecraftAccessDenied("Microsoft and Xbox sign-in succeeded, but Minecraft rejected this application. Check the client ID and Minecraft API approval, then retry the saved account.")
                    if stage == "Minecraft profile" and response.status_code == 404:
                        raise AuthenticationError("No Minecraft Java profile was found. Own Java Edition and finish profile setup on minecraft.net.")
                    raise AuthenticationError(f"{stage} failed (HTTP {response.status_code}). Try again or check the account/app setup.")
                return data
        except httpx.HTTPError:
            raise AuthenticationError(f"Could not reach {stage}. Check your connection and try again.") from None

    @staticmethod
    def _token(data, key):
        value = data.get(key)
        if not isinstance(value, str) or not 1 <= len(value) <= 32768:
            raise AuthenticationError("The sign-in service returned an incomplete token response.")
        return value

    def _exchange(self, client_id, grant, cancel):
        with self.client_factory(timeout=30, follow_redirects=False) as client:
            ms = self._json(client, "POST", AUTHORITY + "/token", stage="Microsoft sign-in", cancel=cancel,
                            data={"client_id": client_id, "scope": SCOPE, **grant})
            refresh = ms.get("refresh_token") or self._refresh
            if not isinstance(refresh, str) or not 1 <= len(refresh) <= 32768:
                raise AuthenticationError("Microsoft did not return a renewable session. Sign in again.")
            # Persist rotations immediately, even if Xbox or Minecraft is temporarily unavailable.
            self.store.save(client_id, refresh)
            self._refresh, self._client_id = refresh, client_id
            xbl = self._json(client, "POST", "https://user.auth.xboxlive.com/user/authenticate", stage="Xbox sign-in", cancel=cancel,
                json={"Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                                     "RpsTicket": "d=" + self._token(ms, "access_token")},
                      "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"})
            xsts = self._json(client, "POST", "https://xsts.auth.xboxlive.com/xsts/authorize", stage="Xbox authorization", cancel=cancel,
                json={"Properties": {"SandboxId": "RETAIL", "UserTokens": [self._token(xbl, "Token")]},
                      "RelyingParty": "rp://api.minecraftservices.com/", "TokenType": "JWT"})
            try:
                userhash = xsts["DisplayClaims"]["xui"][0]["uhs"]
                if not isinstance(userhash, str) or not userhash.isdigit():
                    raise ValueError()
            except (KeyError, IndexError, TypeError, ValueError):
                raise AuthenticationError("Xbox returned an incomplete account identity.") from None
            mc = self._json(client, "POST", MINECRAFT + "/authentication/login_with_xbox", stage="Minecraft login", cancel=cancel,
                json={"identityToken": f"XBL3.0 x={userhash};{self._token(xsts, 'Token')}"})
            token = self._token(mc, "access_token")
            headers = {"Authorization": "Bearer " + token}
            entitlements = self._json(client, "GET", MINECRAFT + "/entitlements/mcstore", stage="Minecraft ownership check",
                                      cancel=cancel, headers=headers)
            items = entitlements.get("items", [])
            if not isinstance(items, list) or not any(isinstance(item, dict) and item.get("name") in {"game_minecraft", "product_minecraft"} for item in items):
                raise AuthenticationError("This account has no Minecraft Java Edition entitlement. Check your purchase or active subscription.")
            profile = self._json(client, "GET", MINECRAFT + "/minecraft/profile", stage="Minecraft profile", cancel=cancel, headers=headers)
            try:
                lifetime = int(mc["expires_in"])
                if not 30 < lifetime <= 86400:
                    raise ValueError()
                session = PlayerSession(client_id, UUID(profile["id"]).hex, profile["name"], token, time.time() + lifetime)
                xuid = xsts["DisplayClaims"]["xui"][0].get("xid", "")
                if not isinstance(xuid, str) or (xuid and not xuid.isdigit()):
                    raise ValueError()
                session = PlayerSession(client_id, session.uuid, session.name, token, session.expires_at, xuid)
                session.require_valid()
            except (KeyError, TypeError, ValueError, AttributeError):
                raise AuthenticationError("Minecraft returned an invalid player profile or session.") from None
            _check_cancel(cancel)
            self.session = session
            return session

    @staticmethod
    def _validate_id(client_id):
        try:
            return str(UUID(client_id))
        except (ValueError, TypeError, AttributeError):
            raise AuthenticationError("Set your registered Microsoft application client ID in launcher settings first.") from None

    def sign_in(self, client_id: str, *, cancel=None):
        client_id = self._validate_id(client_id)
        self.session = None
        grant = self.authorize(client_id, cancel=cancel)
        self._refresh = None
        return self._exchange(client_id, grant, cancel)

    def refresh(self, client_id: str, *, cancel=None):
        client_id = self._validate_id(client_id)
        self.session = None
        if self._client_id != client_id or not self._refresh:
            self._refresh = self.store.load(client_id)
            self._client_id = client_id
        if not self._refresh:
            raise SignInRequired("Sign in with Microsoft before playing.")
        try:
            return self._exchange(client_id, {"grant_type": "refresh_token", "refresh_token": self._refresh}, cancel)
        except SignInRequired:
            self.sign_out()
            raise

    def has_saved_account(self, client_id: str) -> bool:
        """Presence only: a renewable token does not establish Minecraft access."""
        client_id = self._validate_id(client_id)
        return bool((self._refresh if self._client_id == client_id else None) or self.store.load(client_id))

    def sign_out(self):
        self._refresh, self._client_id, self.session = None, None, None
        self.store.clear()
