import base64
import hashlib
import json
import os
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from mc_manager.launcher.auth import (
    AUTHORITY, AuthenticationError, MinecraftAccessDenied, MicrosoftAccounts, SignInRequired, browser_authorization,
)
from mc_manager.launcher.credentials import TokenStore
from mc_manager.launcher.installer import InstallCancelled
from mc_manager.launcher.settings import DesktopSettings

CLIENT = "12345678-1234-1234-1234-123456789abc"
UUID = "abcd1234abcd1234abcd1234abcd1234"


class MemoryStore:
    def __init__(self):
        self.saved = None
    def save(self, client_id, token):
        self.saved = client_id, token
    def load(self, client_id):
        return self.saved[1] if self.saved and self.saved[0] == client_id else None
    def clear(self):
        self.saved = None


def account_service(overrides=None):
    calls = []
    answers = {
        "/consumers/oauth2/v2.0/token": {"access_token": "MS-SECRET", "refresh_token": "REFRESH-SECRET"},
        "/user/authenticate": {"Token": "XBL-SECRET"},
        "/xsts/authorize": {"Token": "XSTS-SECRET", "DisplayClaims": {"xui": [{"uhs": "123", "xid": "456"}]}},
        "/authentication/login_with_xbox": {"access_token": "GAME-SECRET", "expires_in": 86400},
        "/entitlements/mcstore": {"items": [{"name": "game_minecraft"}]},
        "/minecraft/profile": {"id": UUID, "name": "TestPlayer"},
    }
    answers.update(overrides or {})
    def handler(request):
        calls.append(request)
        answer = answers[request.url.path]
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)
    store = MemoryStore()
    accounts = MicrosoftAccounts(store, client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw),
                                 authorize=lambda *_args, **_kwargs: {"grant_type": "authorization_code", "code": "CODE-SECRET", "code_verifier": "VERIFIER"})
    return accounts, store, calls, answers


def test_full_login_refresh_rotation_and_signout():
    accounts, store, calls, answers = account_service()
    session = accounts.sign_in(CLIENT)
    assert session.name == "TestPlayer" and session.uuid == UUID and session.xuid == "456"
    assert "GAME-SECRET" not in repr(session)
    assert store.saved == (CLIENT, "REFRESH-SECRET")
    assert parse_qs(calls[0].content.decode())["code_verifier"] == ["VERIFIER"]
    assert json.loads(calls[1].content)["Properties"]["RpsTicket"] == "d=MS-SECRET"
    assert json.loads(calls[3].content)["identityToken"] == "XBL3.0 x=123;XSTS-SECRET"
    assert calls[4].headers["Authorization"] == "Bearer GAME-SECRET"
    answers["/consumers/oauth2/v2.0/token"]["refresh_token"] = "ROTATED-SECRET"
    accounts.refresh(CLIENT)
    assert store.saved == (CLIENT, "ROTATED-SECRET")
    assert parse_qs(calls[6].content.decode())["refresh_token"] == ["REFRESH-SECRET"]
    accounts.sign_out()
    assert store.saved is None and accounts.session is None
    with pytest.raises(SignInRequired):
        accounts.refresh(CLIENT)


@pytest.mark.parametrize("path,response,message", [
    ("/entitlements/mcstore", {"items": []}, "entitlement"),
    ("/minecraft/profile", httpx.Response(404, json={"error": "NOT_FOUND"}), "No Minecraft Java profile"),
    ("/authentication/login_with_xbox", httpx.Response(403, json={"error": "SECRET"}), "application"),
    ("/xsts/authorize", httpx.Response(401, json={"XErr": 2148916233}), "Xbox profile"),
    ("/xsts/authorize", httpx.Response(401, json={"XErr": 2148916238}), "parent"),
    ("/minecraft/profile", {"id": UUID, "name": "../../bad"}, "invalid player"),
    ("/consumers/oauth2/v2.0/token", httpx.Response(500, json={"error_description": "MS-SECRET"}), "HTTP 500"),
    ("/consumers/oauth2/v2.0/token", httpx.Response(302, json={}, headers={"Location": "https://evil.test"}), "HTTP 302"),
    ("/consumers/oauth2/v2.0/token", httpx.Response(200, content=b"x" * (1024 * 1024 + 1)), "oversized"),
    ("/consumers/oauth2/v2.0/token", httpx.Response(200, content=b"MS-SECRET"), "invalid response"),
    ("/xsts/authorize", {"Token": "secret", "DisplayClaims": {}}, "incomplete account"),
])
def test_signin_failures_do_not_create_launch_session_or_expose_tokens(path, response, message):
    accounts, _, calls, _ = account_service({path: response})
    with pytest.raises(AuthenticationError, match=message) as error:
        accounts.sign_in(CLIENT)
    assert "SECRET" not in str(error.value)
    assert accounts.session is None
    assert not any(request.url.host == "evil.test" for request in calls)


def test_revoked_refresh_removed_and_other_client_cannot_reuse_it():
    accounts, store, calls, answers = account_service()
    accounts.sign_in(CLIENT)
    with pytest.raises(SignInRequired):
        accounts.refresh("00000000-0000-0000-0000-000000000001")
    assert len(calls) == 6
    answers["/consumers/oauth2/v2.0/token"] = httpx.Response(400, json={"error": "invalid_grant"})
    with pytest.raises(SignInRequired):
        accounts.refresh(CLIENT)
    assert store.saved is None


def test_saved_account_can_retry_after_minecraft_api_approval_without_browser():
    accounts, store, calls, answers = account_service({
        "/authentication/login_with_xbox": httpx.Response(403, json={"error": "SENSITIVE-response"})})
    assert not accounts.has_saved_account(CLIENT)
    with pytest.raises(MinecraftAccessDenied) as error:
        accounts.sign_in(CLIENT)
    assert "SENSITIVE" not in str(error.value)
    assert accounts.session is None and accounts.has_saved_account(CLIENT)
    before = len(calls)
    assert not accounts.has_saved_account("00000000-0000-0000-0000-000000000001")
    assert len(calls) == before  # Presence checks never contact any auth service.
    accounts.authorize = lambda *a, **kw: pytest.fail("Retry opened a browser")
    answers["/authentication/login_with_xbox"] = {"access_token": "NEW-GAME-SECRET", "expires_in": 3600}
    assert accounts.refresh(CLIENT).name == "TestPlayer"
    assert parse_qs(calls[before].content.decode())["grant_type"] == ["refresh_token"]
    accounts.sign_out()
    assert not accounts.has_saved_account(CLIENT)


def test_cancelled_exchange_never_calls_network():
    accounts, _, calls, _ = account_service()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InstallCancelled):
        accounts.sign_in(CLIENT, cancel=cancel)
    assert calls == []


def test_missing_client_id_is_actionable():
    accounts, _, calls, _ = account_service()
    with pytest.raises(AuthenticationError, match="client ID"):
        accounts.sign_in("")
    assert calls == []


def test_browser_pkce_state_and_callback_host():
    observed = {}
    responses = []
    thread = None
    def open_browser(url):
        nonlocal thread
        parsed = urlsplit(url)
        observed.update(parse_qs(parsed.query))
        assert parsed.scheme == "https" and parsed.hostname == "login.microsoftonline.com"
        callback = observed["redirect_uri"][0]
        local_callback = callback.replace("localhost", "127.0.0.1")
        host = urlsplit(callback).netloc
        def send():
            with httpx.Client(trust_env=False) as client:
                responses.append(client.get(local_callback, params={"state": "wrong", "code": "attacker"}, headers={"Host": host}).status_code)
                responses.append(client.get(local_callback, params={"state": observed["state"][0], "code": "REAL-CODE"},
                                            headers={"Host": "evil.test"}).status_code)
                responses.append(client.get(local_callback, params={"state": observed["state"][0], "code": "REAL-CODE"}, headers={"Host": host}).status_code)
        thread = threading.Thread(target=send)
        thread.start()
        return True
    grant = browser_authorization(CLIENT, open_browser=open_browser, timeout=5)
    thread.join(timeout=5)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(grant["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert observed["code_challenge"] == [challenge]
    assert observed["code_challenge_method"] == ["S256"]
    assert grant["code"] == "REAL-CODE" and responses == [400, 400, 200]
    assert "client_secret" not in observed


def test_browser_unavailable_timeout_and_cancel():
    with pytest.raises(AuthenticationError, match="default browser"):
        browser_authorization(CLIENT, open_browser=lambda _: False)
    with pytest.raises(AuthenticationError, match="timed out"):
        browser_authorization(CLIENT, open_browser=lambda _: True, timeout=0)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InstallCancelled):
        browser_authorization(CLIENT, open_browser=lambda _: pytest.fail("Browser opened"), cancel=cancel)


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI integration")
def test_windows_tokens_are_encrypted_bound_to_client_and_removable(tmp_path):
    store = TokenStore(tmp_path)
    store.save(CLIENT, "SENSITIVE-REFRESH")
    assert b"SENSITIVE-REFRESH" not in (tmp_path / "microsoft-account.json").read_bytes()
    assert store.load(CLIENT) == "SENSITIVE-REFRESH"
    assert store.load("another-client") is None
    store.clear()
    assert store.load(CLIENT) is None


def test_corrupt_credentials_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(TokenStore, "persistent", property(lambda _: True))
    (tmp_path / "microsoft-account.json").write_text('{"refresh_token":"plaintext"}')
    with pytest.raises(ValueError, match="could not be unlocked"):
        TokenStore(tmp_path).load(CLIENT)


def test_settings_accept_public_client_id_and_reject_relative_java(tmp_path):
    settings = DesktopSettings(library_directory=str(tmp_path), microsoft_client_id=CLIENT)
    assert settings.microsoft_client_id == CLIENT
    for value in ["not-a-guid", "secret=value"]:
        with pytest.raises(ValueError):
            DesktopSettings(library_directory=str(tmp_path), microsoft_client_id=value)
    with pytest.raises(ValueError):
        DesktopSettings(library_directory=str(tmp_path), java_executable="java.exe")
