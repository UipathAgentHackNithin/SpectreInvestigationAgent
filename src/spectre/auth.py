import os
import time
import requests

_TOKEN_URL = "https://staging.uipath.com/identity_/connect/token"
_CLIENT_ID = "36dea5b8-e8bb-423d-8e7b-c808df8f1c00"
_DEFAULT_BASE_URL = "https://staging.uipath.com/ad89db7f-af81-463f-865d-6c373f2feb96/ab8ad4cb-8820-42e7-a658-210ffaa23b75"

_llm_token_cache: dict = {}  # keys: access_token, expires_at


def _get_base_url() -> str:
    return os.getenv("UIPATH_URL", _DEFAULT_BASE_URL)


def get_pat() -> tuple[str, str]:
    """Return (PAT, base_url). Reads UIPATH_PAT from env — populated by _load_credentials() in agent.py."""
    base_url = _get_base_url()
    pat = os.getenv("UIPATH_PAT")
    if not pat:
        raise ValueError("UIPATH_PAT not available — ensure SPECTRE_PAT asset is set in Orchestrator")
    return pat, base_url


def get_llm_token() -> tuple[str, str]:
    """Return (token, base_url) for LLM calls.
    Reads UIPATH_REFRESH_TOKEN from env — populated by _load_credentials() in agent.py.
    Exchanges it for a fresh LLMGateway-scoped JWT. Cached in memory until 60s before expiry.
    """
    base_url = _get_base_url()

    if _llm_token_cache.get("access_token") and time.time() < _llm_token_cache.get("expires_at", 0):
        return _llm_token_cache["access_token"], base_url

    refresh_token = os.getenv("UIPATH_REFRESH_TOKEN")
    if not refresh_token:
        raise ValueError("UIPATH_REFRESH_TOKEN not available — ensure SPECTRE_REFRESH_TOKEN asset is set in Orchestrator")

    token_resp = requests.post(
        _TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": _CLIENT_ID,
        },
        timeout=10
    )
    if not token_resp.ok:
        raise ValueError(f"Token refresh failed {token_resp.status_code}: {token_resp.text} | token_prefix={refresh_token[:40]!r}")
    token_resp.raise_for_status()
    token_data = token_resp.json()
    expires_in = token_data.get("expires_in", 3600)
    _llm_token_cache["access_token"] = token_data["access_token"]
    _llm_token_cache["expires_at"] = time.time() + expires_in - 60

    # Update env var with new refresh token so subsequent calls in this session work
    new_refresh_token = token_data.get("refresh_token")
    if new_refresh_token:
        os.environ["UIPATH_REFRESH_TOKEN"] = new_refresh_token

    return _llm_token_cache["access_token"], base_url
