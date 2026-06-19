import json
import os
import time
import pytest
from unittest.mock import patch, MagicMock, mock_open
from spectre.auth import get_pat, get_llm_token


def _make_resp(ok=True, status_code=200, json_data=None):
    mock = MagicMock()
    mock.ok = ok
    mock.status_code = status_code
    mock.json.return_value = json_data or {}
    mock.raise_for_status = MagicMock()
    return mock


class TestGetPat:

    def test_returns_pat_from_env(self):
        with patch.dict(os.environ, {"UIPATH_PAT": "my-pat", "UIPATH_URL": "https://example.com"}, clear=False):
            pat, base_url = get_pat()
        assert pat == "my-pat"
        assert base_url == "https://example.com"

    def test_raises_when_no_pat_and_no_robot_token(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="Neither UIPATH_PAT nor UIPATH_ACCESS_TOKEN"):
                get_pat()

    def test_fetches_pat_asset_on_robot(self):
        asset_resp = _make_resp(json_data={"value": [{"StringValue": "asset-pat"}]})
        with patch.dict(os.environ, {"UIPATH_ACCESS_TOKEN": "robot-tok", "UIPATH_URL": "https://example.com"}, clear=False), \
             patch("spectre.auth.os.getenv", side_effect=lambda k, *a: {
                 "UIPATH_PAT": None, "UIPATH_ACCESS_TOKEN": "robot-tok", "UIPATH_URL": "https://example.com",
                 "SPECTRE_FOLDER_ID": "3087542"
             }.get(k, a[0] if a else None)), \
             patch("spectre.auth.requests.get", return_value=asset_resp):
            pat, _ = get_pat()
        assert pat == "asset-pat"

    def test_falls_back_to_robot_token_when_asset_empty(self):
        asset_resp = _make_resp(json_data={"value": []})
        with patch("spectre.auth.os.getenv", side_effect=lambda k, *a: {
                "UIPATH_PAT": None, "UIPATH_ACCESS_TOKEN": "robot-tok", "UIPATH_URL": "https://example.com",
                "SPECTRE_FOLDER_ID": "3087542"
             }.get(k, a[0] if a else None)), \
             patch("spectre.auth.requests.get", return_value=asset_resp):
            pat, _ = get_pat()
        assert pat == "robot-tok"


class TestGetLlmToken:

    def test_returns_env_token_when_auth_json_present_and_robot_token_set(self):
        with patch("spectre.auth.os.path.exists", return_value=True), \
             patch("spectre.auth.os.getenv", side_effect=lambda k, *a: {
                 "UIPATH_ACCESS_TOKEN": "env-llm-token", "UIPATH_URL": "https://example.com"
             }.get(k, a[0] if a else None)):
            token, url = get_llm_token()
        assert token == "env-llm-token"

    def test_reads_valid_token_from_auth_json(self):
        auth_data = {"access_token": "file-token", "issued_at": time.time(), "expires_in": 3600}
        with patch("spectre.auth.os.path.exists", return_value=True), \
             patch("spectre.auth.os.getenv", side_effect=lambda k, *a: {
                 "UIPATH_ACCESS_TOKEN": None, "UIPATH_URL": "https://example.com"
             }.get(k, a[0] if a else None)), \
             patch("builtins.open", mock_open(read_data=json.dumps(auth_data))):
            import spectre.auth as auth_mod
            auth_mod._llm_token_cache.clear()
            token, _ = get_llm_token()
        assert token == "file-token"

    def test_raises_when_asset_not_found_on_robot(self):
        asset_resp = _make_resp(json_data={"value": []})
        with patch("spectre.auth.os.path.exists", return_value=False), \
             patch("spectre.auth.get_pat", return_value=("pat", "https://example.com")), \
             patch("spectre.auth.requests.get", return_value=asset_resp):
            with pytest.raises(ValueError, match="SpectreRefreshToken asset not found"):
                import spectre.auth as auth_mod
                auth_mod._llm_token_cache.clear()
                get_llm_token()

    def test_raises_when_asset_value_empty(self):
        asset_resp = _make_resp(json_data={"value": [{"StringValue": ""}]})
        with patch("spectre.auth.os.path.exists", return_value=False), \
             patch("spectre.auth.get_pat", return_value=("pat", "https://example.com")), \
             patch("spectre.auth.requests.get", return_value=asset_resp):
            with pytest.raises(ValueError, match="SpectreRefreshToken value is empty"):
                import spectre.auth as auth_mod
                auth_mod._llm_token_cache.clear()
                get_llm_token()

    def test_raises_when_token_exchange_fails(self):
        asset_resp = _make_resp(json_data={"value": [{"StringValue": "refresh-tok"}]})
        fail_resp = _make_resp(ok=False, status_code=401, json_data={})
        fail_resp.text = "Unauthorized"
        with patch("spectre.auth.os.path.exists", return_value=False), \
             patch("spectre.auth.get_pat", return_value=("pat", "https://example.com")), \
             patch("spectre.auth.requests.get", return_value=asset_resp), \
             patch("spectre.auth.requests.post", return_value=fail_resp):
            with pytest.raises(ValueError, match="Token refresh failed"):
                import spectre.auth as auth_mod
                auth_mod._llm_token_cache.clear()
                get_llm_token()

    def test_returns_cached_token_when_valid(self):
        import spectre.auth as auth_mod
        auth_mod._llm_token_cache["access_token"] = "cached-token"
        auth_mod._llm_token_cache["expires_at"] = time.time() + 3600
        with patch("spectre.auth.os.getenv", side_effect=lambda k, *a: {
                "UIPATH_URL": "https://example.com"
             }.get(k, a[0] if a else None)):
            token, _ = get_llm_token()
        assert token == "cached-token"
        auth_mod._llm_token_cache.clear()
