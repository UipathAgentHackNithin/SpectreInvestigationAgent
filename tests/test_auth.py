import os
import time
import pytest
from unittest.mock import patch, MagicMock
from spectre.auth import get_pat, get_llm_token


def _make_resp(ok=True, status_code=200, json_data=None, text=""):
    mock = MagicMock()
    mock.ok = ok
    mock.status_code = status_code
    mock.json.return_value = json_data or {}
    mock.text = text
    mock.raise_for_status = MagicMock()
    return mock


class TestGetPat:

    def test_returns_pat_from_env(self):
        with patch.dict(os.environ, {"UIPATH_PAT": "my-pat", "UIPATH_URL": "https://example.com"}, clear=False):
            pat, base_url = get_pat()
        assert pat == "my-pat"
        assert base_url == "https://example.com"

    def test_raises_when_no_pat(self):
        env = {k: v for k, v in os.environ.items() if k != "UIPATH_PAT"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValueError, match="UIPATH_PAT not available"):
                get_pat()

    def test_uses_default_base_url_when_uipath_url_not_set(self):
        env = {k: v for k, v in os.environ.items() if k not in ("UIPATH_PAT", "UIPATH_URL")}
        env["UIPATH_PAT"] = "tok"
        with patch.dict(os.environ, env, clear=True):
            pat, base_url = get_pat()
        assert pat == "tok"
        assert "staging.uipath.com" in base_url


class TestGetLlmToken:

    def setup_method(self):
        import spectre.auth as auth_mod
        auth_mod._llm_token_cache.clear()

    def test_returns_cached_token_when_valid(self):
        import spectre.auth as auth_mod
        auth_mod._llm_token_cache["access_token"] = "cached-token"
        auth_mod._llm_token_cache["expires_at"] = time.time() + 3600
        with patch.dict(os.environ, {"UIPATH_URL": "https://example.com"}, clear=False):
            token, _ = get_llm_token()
        assert token == "cached-token"

    def test_raises_when_no_refresh_token(self):
        env = {k: v for k, v in os.environ.items() if k != "UIPATH_REFRESH_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValueError, match="UIPATH_REFRESH_TOKEN not available"):
                get_llm_token()

    def test_exchanges_refresh_token_and_returns_access_token(self):
        resp = _make_resp(json_data={"access_token": "new-access", "expires_in": 3600})
        with patch.dict(os.environ, {"UIPATH_REFRESH_TOKEN": "ref-tok", "UIPATH_URL": "https://example.com"}, clear=False), \
             patch("spectre.auth.requests.post", return_value=resp) as mock_post:
            token, url = get_llm_token()
        assert token == "new-access"
        assert url == "https://example.com"
        mock_post.assert_called_once()

    def test_rotated_refresh_token_stored_in_env(self):
        resp = _make_resp(json_data={"access_token": "access", "refresh_token": "new-ref", "expires_in": 3600})
        with patch.dict(os.environ, {"UIPATH_REFRESH_TOKEN": "old-ref", "UIPATH_URL": "https://example.com"}, clear=False), \
             patch("spectre.auth.requests.post", return_value=resp):
            get_llm_token()
            assert os.environ.get("UIPATH_REFRESH_TOKEN") == "new-ref"

    def test_raises_when_token_exchange_fails(self):
        resp = _make_resp(ok=False, status_code=401, text="Unauthorized")
        with patch.dict(os.environ, {"UIPATH_REFRESH_TOKEN": "bad-ref", "UIPATH_URL": "https://example.com"}, clear=False), \
             patch("spectre.auth.requests.post", return_value=resp):
            with pytest.raises(ValueError, match="Token refresh failed"):
                get_llm_token()
