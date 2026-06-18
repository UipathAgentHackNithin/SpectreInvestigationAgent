import re
import time
import requests
from datetime import datetime, timezone
try:
    from .logger import get_logger
except ImportError:
    from logger import get_logger

log = get_logger("spectre.orchestrator")

_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = 2.0


def _get(url: str, **kwargs) -> requests.Response:
    """requests.get with retry on 5xx/connection errors. 4xx errors are raised immediately."""
    last_exc = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            resp = requests.get(url, **kwargs)
            if resp.status_code < 500:
                return resp
            log.warning(f"Orchestrator API returned {resp.status_code} (attempt {attempt + 1}/{_RETRY_ATTEMPTS}), retrying...")
        except requests.RequestException as e:
            log.warning(f"Orchestrator API request failed (attempt {attempt + 1}/{_RETRY_ATTEMPTS}): {e}")
            last_exc = e
        if attempt < _RETRY_ATTEMPTS - 1:
            time.sleep(_RETRY_BACKOFF ** attempt)
    if last_exc:
        raise last_exc
    resp.raise_for_status()
    return resp


def _extract_numeric_id(process_name: str) -> str | None:
    """Extract the first numeric ID from process name e.g. 'ICSAUTO-3201 Invoice Bot' -> '3201'."""
    match = re.search(r'\d{3,}', process_name)
    return match.group() if match else None


def _find_folder_id(access_token: str, base_url: str, numeric_id: str) -> str | None:
    """Find Orchestrator folder ID whose name contains the numeric process ID."""
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = _get(
            f"{base_url}/orchestrator_/odata/Folders",
            headers=headers,
            params={"$filter": f"contains(FullyQualifiedName, '{numeric_id}')"},
            timeout=10
        )
        resp.raise_for_status()
        folders = resp.json().get("value", [])
        if folders:
            return str(folders[0]["Id"])
    except Exception:
        pass
    return None


def _build_headers(access_token: str, folder_id: str | None) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    if folder_id:
        headers["X-UIPATH-OrganizationUnitId"] = folder_id
    return headers


def _format_logs(logs: list) -> str:
    lines = []
    for log in logs:
        ts = log.get("TimeStamp", "")
        level = log.get("Level", "")
        message = log.get("Message", "")
        robot = log.get("RobotName", "")
        process = log.get("ProcessName", "")
        lines.append(f"[{ts}] [{level}] [{process}/{robot}] {message}")
    return "\n".join(lines)


_NEW_STATUSES = {"New", "Retried"}
_INPROGRESS_STATUSES = {"InProgress"}


def _layer1_queue(access_token: str, base_url: str, headers: dict, transaction_id: str, process_name: str) -> tuple[str | None, str | None]:
    """Layer 1: Find transaction in queue, use start/end timestamps to fetch logs.

    Returns (result, source_label):
      - (message, "Queue item status") — item found but not yet processed; stop fallback
      - (logs, "Queue transaction window") — logs fetched from queue timestamps; stop fallback
      - (None, None) — item not found or in-progress; caller should try next layer
    """
    try:
        resp = _get(
            f"{base_url}/orchestrator_/odata/QueueItems",
            headers=headers,
            params={"$filter": f"contains(Reference, '{transaction_id}')"},
            timeout=10
        )
        resp.raise_for_status()
        items = resp.json().get("value", [])
        if not items:
            return None, None

        item = items[0]
        status = item.get("Status", "")

        if status in _NEW_STATUSES:
            return (
                f"Queue item found with status '{status}' - the bot has not processed this transaction yet. "
                f"No logs available until a robot picks it up.",
                "Queue item status (not yet processed)"
            )

        if status in _INPROGRESS_STATUSES:
            # Still running — fall through so later layers can find partial logs
            return None, None

        start_ts = item.get("StartProcessing")
        end_ts = item.get("EndProcessing")
        if not start_ts:
            return None, None

        # Use timestamps only — ProcessName in logs differs from the process_name input
        # e.g. input 'ICSAUTO-3201 Invoice Performer' vs actual 'InvoiceProcessing_Performer'
        log_filter = f"TimeStamp ge {start_ts}"
        if end_ts:
            log_filter += f" and TimeStamp le {end_ts}"

        resp = _get(
            f"{base_url}/orchestrator_/odata/RobotLogs",
            headers=headers,
            params={"$filter": log_filter, "$orderby": "TimeStamp asc", "$top": 50},
            timeout=10
        )
        resp.raise_for_status()
        logs = resp.json().get("value", [])
        if logs:
            return _format_logs(logs), "Queue transaction window (precise timestamps from queue item)"

    except Exception:
        pass
    return None, None


_PERFORMER_KEYWORDS = {"performer", "runner"}


def _is_performer(process_name: str) -> bool:
    return any(kw in process_name.lower() for kw in _PERFORMER_KEYWORDS)


def _layer2_transaction_in_logs(access_token: str, base_url: str, headers: dict, transaction_id: str, process_name: str) -> tuple[str | None, str | None]:
    """Layer 2: Search log messages for transaction ID without ProcessName filter.

    The dispatcher may log 'Adding INV-98766 to queue' and the performer logs it during processing.
    We find whichever process logged it first, then:
    - If it's a dispatcher: the transaction was queued but performer hasn't run yet — fall through.
    - If it's a performer/runner: fetch the full job window using JobKey + Transaction Ended marker.

    Returns (result, source_label) or (None, None) to fall through.
    """
    try:
        # Search without ProcessName filter — dispatcher or performer may have logged the reference
        resp = _get(
            f"{base_url}/orchestrator_/odata/RobotLogs",
            headers=headers,
            params={
                "$filter": f"contains(Message, '{transaction_id}')",
                "$orderby": "TimeStamp asc",
                "$top": 1
            },
            timeout=10
        )
        resp.raise_for_status()
        entries = resp.json().get("value", [])
        if not entries:
            return None, None

        start_entry = entries[0]
        logged_by = start_entry.get("ProcessName", "")

        if not _is_performer(logged_by):
            # Dispatcher logged it — transaction is queued but performer hasn't touched it yet
            log.info(f"Layer 2: transaction_id found in logs from dispatcher '{logged_by}', not a performer — falling through")
            return None, None

        start_ts = start_entry.get("TimeStamp")
        job_key = start_entry.get("JobKey")
        if not start_ts or not job_key:
            return None, None

        # Find 'Transaction Ended' log for the same job after start to bound the window
        resp = _get(
            f"{base_url}/orchestrator_/odata/RobotLogs",
            headers=headers,
            params={
                "$filter": f"JobKey eq '{job_key}' and TimeStamp ge {start_ts} and contains(Message, 'Transaction Ended')",
                "$orderby": "TimeStamp asc",
                "$top": 1
            },
            timeout=10
        )
        resp.raise_for_status()
        end_entries = resp.json().get("value", [])
        end_ts = end_entries[0].get("TimeStamp") if end_entries else None

        # Fetch all logs in the window
        log_filter = f"JobKey eq '{job_key}' and TimeStamp ge {start_ts}"
        if end_ts:
            log_filter += f" and TimeStamp le {end_ts}"

        resp = _get(
            f"{base_url}/orchestrator_/odata/RobotLogs",
            headers=headers,
            params={"$filter": log_filter, "$orderby": "TimeStamp asc", "$top": 50},
            timeout=10
        )
        resp.raise_for_status()
        logs = resp.json().get("value", [])
        if logs:
            return _format_logs(logs), "Log message window (performer job, bounded by Transaction Ended)"

    except Exception:
        pass
    return None, None


def _layer3_todays_errors(access_token: str, base_url: str, headers: dict, process_name: str) -> str:
    """Layer 3: Fallback — fetch today's error logs for performer/runner process."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        # Match performer or runner suffix — these do the actual transaction processing
        filter_expr = (
            f"(contains(ProcessName, 'Performer') or contains(ProcessName, 'Runner')) "
            f"and Level eq 'Error' "
            f"and TimeStamp ge {today}"
        )
        resp = _get(
            f"{base_url}/orchestrator_/odata/RobotLogs",
            headers=headers,
            params={
                "$filter": filter_expr,
                "$orderby": "TimeStamp desc",
                "$top": 20
            },
            timeout=10
        )
        resp.raise_for_status()
        logs = resp.json().get("value", [])
        if logs:
            return _format_logs(logs)
    except Exception:
        pass
    return f"No logs found for process '{process_name}' today."


def fetch_recent_failures(access_token: str, base_url: str, process_name: str, exclude_transaction_id: str) -> list[str]:
    """Fetch other recent failed transactions in the same process queue (cross-transaction analysis)."""
    numeric_id = _extract_numeric_id(process_name)
    folder_id = _find_folder_id(access_token, base_url, numeric_id) if numeric_id else None
    headers = _build_headers(access_token, folder_id)
    try:
        resp = _get(
            f"{base_url}/orchestrator_/odata/QueueItems",
            headers=headers,
            params={
                "$filter": "Status eq 'Failed'",
                "$orderby": "EndProcessing desc",
                "$top": 10
            },
            timeout=10
        )
        resp.raise_for_status()
        items = resp.json().get("value", [])
        return [
            item.get("Reference", "unknown")
            for item in items
            if item.get("Reference") and item.get("Reference") != exclude_transaction_id
        ]
    except Exception:
        return []


def fetch_logs(access_token: str, base_url: str, transaction_id: str, process_name: str) -> tuple[str, str]:
    """Fetch Orchestrator logs using a 3-layer fallback strategy.

    Returns (logs, source_label) where source_label describes which layer succeeded.
    """
    numeric_id = _extract_numeric_id(process_name)
    log.info(f"Looking up folder for process numeric ID: {numeric_id}")
    folder_id = _find_folder_id(access_token, base_url, numeric_id) if numeric_id else None
    if folder_id:
        log.info(f"Found folder ID: {folder_id}")
    else:
        log.warning(f"No folder found for numeric ID: {numeric_id}, using default folder")
    headers = _build_headers(access_token, folder_id)

    log.info(f"Trying Layer 1: Queue transaction lookup for transaction_id={transaction_id}")
    result, source = _layer1_queue(access_token, base_url, headers, transaction_id, process_name)
    if source:
        log.info(f"Layer 1 resolved: {source}")
        return result, source

    log.info("Layer 1 found nothing, trying Layer 2: Transaction ID in log messages")
    result, source = _layer2_transaction_in_logs(access_token, base_url, headers, transaction_id, process_name)
    if source:
        log.info(f"Layer 2 resolved: {source}")
        return result, source

    log.warning("Layers 1 & 2 found nothing, falling back to Layer 3: Today's error logs")
    fallback = _layer3_todays_errors(access_token, base_url, headers, process_name)
    return fallback, "Today's error logs fallback (broad - not scoped to this transaction)"
