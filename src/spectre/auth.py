import json
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AUTH_PATH = os.path.join(_PROJECT_ROOT, ".uipath", ".auth.json")
_TOKEN_URL = "https://staging.uipath.com/identity_/connect/token"
_CLIENT_ID = "36dea5b8-e8bb-423d-8e7b-c808df8f1c00"
_DEFAULT_BASE_URL = "https://staging.uipath.com/ad89db7f-af81-463f-865d-6c373f2feb96/ab8ad4cb-8820-42e7-a658-210ffaa23b75"
_LLM_TOKEN_FOLDER = "Shared/Specter"

_llm_token_cache: dict = {}  # keys: access_token, expires_at

# Folder ID for the Shared/Specter folder on the staging tenant.
# Override via SPECTRE_FOLDER_ID env var if deploying to a different tenant.
# On robot, set this as an Orchestrator robot environment variable — .env is not available.
_SPECTRE_FOLDER_ID = os.getenv("SPECTRE_FOLDER_ID", "3087542")


def _get_robot_token() -> str | None:
    return os.getenv("UIPATH_ACCESS_TOKEN") or os.getenv("UIPATH_ROBOT_ACCESS_TOKEN")


def _get_base_url() -> str:
    return os.getenv("UIPATH_URL", _DEFAULT_BASE_URL)


def get_llm_token() -> tuple[str, str]:
    """Return (token, base_url) for LLM calls.
    Locally: reads access_token from .auth.json (has LLMGateway scope), refreshing if expired.
    On robot: fetches SpectreRefreshToken asset via PAT, exchanges for fresh JWT.
    Cached in memory until 60 seconds before expiry.
    """
    base_url = _get_base_url()

    if _llm_token_cache.get("access_token") and time.time() < _llm_token_cache.get("expires_at", 0):
        return _llm_token_cache["access_token"], base_url

    # If running locally (.auth.json present), use the local token directly — it has LLMGateway scope.
    # UIPATH_ACCESS_TOKEN in .env is the same token; .auth.json is the source of truth for expiry.
    if os.path.exists(_AUTH_PATH):
        env_token = _get_robot_token()
        if env_token:
            # Token from .env — trust it (locally it has LLMGateway scope)
            return env_token, base_url
        with open(_AUTH_PATH) as f:
            data = json.load(f)
        issued_at = data.get("issued_at", time.time())
        expires_in = data.get("expires_in", 3600)
        if time.time() < issued_at + expires_in - 60:
            _llm_token_cache["access_token"] = data["access_token"]
            _llm_token_cache["expires_at"] = issued_at + expires_in - 60
            return _llm_token_cache["access_token"], base_url
        # access token expired locally — refresh it
        resp = requests.post(
            _TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": data["refresh_token"], "client_id": _CLIENT_ID},
            timeout=10
        )
        resp.raise_for_status()
        new_data = resp.json()
        new_data["issued_at"] = time.time()
        data.update(new_data)
        with open(_AUTH_PATH, "w") as f:
            json.dump(data, f)
        _llm_token_cache["access_token"] = data["access_token"]
        _llm_token_cache["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
        return _llm_token_cache["access_token"], base_url

    pat, _ = get_pat()

    folder_id = _SPECTRE_FOLDER_ID

    headers = {"Authorization": f"Bearer {pat}", "X-UIPATH-OrganizationUnitId": folder_id}

    # Fetch text asset value
    resp = requests.get(
        f"{base_url}/orchestrator_/odata/Assets?$filter=Name eq 'SpectreRefreshToken'",
        headers=headers,
        timeout=10
    )
    if not resp.ok:
        raise ValueError(f"Asset lookup failed {resp.status_code}: {resp.text}")
    assets = resp.json().get("value", [])
    if not assets:
        raise ValueError("SpectreRefreshToken asset not found")
    refresh_token = assets[0].get("StringValue", "") or assets[0].get("Value", "")
    if not refresh_token:
        raise ValueError("SpectreRefreshToken value is empty")

    # Exchange refresh token for a fresh access token with LLMGateway scope
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

    # Save the new refresh token back to the asset so next run can use it
    new_refresh_token = token_data.get("refresh_token")
    if new_refresh_token:
        asset_id = assets[0].get("Id")
        update_body = {
            "Id": asset_id,
            "Name": "SpectreRefreshToken",
            "ValueType": "Text",
            "StringValue": new_refresh_token,
        }
        requests.put(
            f"{base_url}/orchestrator_/odata/Assets({asset_id})",
            headers=headers,
            json=update_body,
            timeout=10,
        )

    return _llm_token_cache["access_token"], base_url


def get_pat() -> tuple[str, str]:
    """Return (PAT, base_url) for Orchestrator API calls.
    Locally: uses UIPATH_PAT from .env.
    On robot: fetches SpectrePAT asset using the robot token.
    """
    base_url = _get_base_url()

    pat = os.getenv("UIPATH_PAT")
    if pat:
        return pat, base_url

    robot_token = _get_robot_token()
    if not robot_token:
        raise ValueError("Neither UIPATH_PAT nor UIPATH_ACCESS_TOKEN is available")

    # On robot: fetch the PAT stored as an Orchestrator asset
    folder_id = _SPECTRE_FOLDER_ID
    headers = {"Authorization": f"Bearer {robot_token}", "X-UIPATH-OrganizationUnitId": folder_id}
    resp = requests.get(
        f"{base_url}/orchestrator_/odata/Assets?$filter=Name eq 'SpectrePAT'",
        headers=headers,
        timeout=10
    )
    if resp.ok:
        assets = resp.json().get("value", [])
        if assets:
            stored_pat = assets[0].get("StringValue", "") or assets[0].get("Value", "")
            if stored_pat:
                return stored_pat, base_url

    # Fallback: use the robot token itself (limited scope)
    return robot_token, base_url
