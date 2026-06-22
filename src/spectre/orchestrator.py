import re
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def _extract_keyword(process_name: str) -> str | None:
    """Extract a meaningful keyword from process name for folder search fallback.

    e.g. 'ICSAUTO-3201 Invoice Processing Performer' -> 'Invoice'
    Strips numeric tokens, short tokens, and common UiPath suffixes.
    """
    _STOP_WORDS = {"performer", "runner", "dispatcher", "bot", "process", "processing", "automation"}
    parts = re.split(r'[\s\-_]+', process_name)
    for part in parts:
        clean = re.sub(r'\d+', '', part).strip()
        if len(clean) >= 4 and clean.lower() not in _STOP_WORDS:
            return clean
    return None


def _find_folder_id(access_token: str, base_url: str, numeric_id: str | None, process_name: str = "") -> str | None:
    """Find Orchestrator folder ID whose name contains the numeric process ID.

    Fallback 1: search by keyword extracted from process name.
    Fallback 2: return the first folder from all-folders list whose name best matches process name.
    """
    headers = {"Authorization": f"Bearer {access_token}"}

    # Primary: numeric ID search
    if numeric_id:
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

    # Fallback 1: keyword search
    keyword = _extract_keyword(process_name) if process_name else None
    if keyword:
        try:
            resp = _get(
                f"{base_url}/orchestrator_/odata/Folders",
                headers=headers,
                params={"$filter": f"contains(FullyQualifiedName, '{keyword}')"},
                timeout=10
            )
            resp.raise_for_status()
            folders = resp.json().get("value", [])
            if folders:
                log.info(f"Folder found via keyword fallback '{keyword}': {folders[0].get('FullyQualifiedName')}")
                return str(folders[0]["Id"])
        except Exception:
            pass

    # Fallback 2: all folders, pick closest name match
    if process_name:
        all_folders = _list_all_folders(access_token, base_url)
        if all_folders:
            process_lower = process_name.lower()
            best = max(
                all_folders,
                key=lambda f: sum(
                    1 for word in re.split(r'\W+', process_lower)
                    if len(word) >= 3 and word in f.get("FullyQualifiedName", "").lower()
                )
            )
            score = sum(
                1 for word in re.split(r'\W+', process_lower)
                if len(word) >= 3 and word in best.get("FullyQualifiedName", "").lower()
            )
            if score > 0:
                log.info(f"Folder found via best-match fallback (score={score}): {best.get('FullyQualifiedName')}")
                return str(best["Id"])

    return None


def _list_all_folders(access_token: str, base_url: str) -> list[dict]:
    """Return all Orchestrator folders as a list of {Id, FullyQualifiedName} dicts."""
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = _get(
            f"{base_url}/orchestrator_/odata/Folders",
            headers=headers,
            timeout=10
        )
        resp.raise_for_status()
        return resp.json().get("value", [])
    except Exception:
        return []


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
_TRANSACTION_NOT_FOUND = "__TRANSACTION_NOT_FOUND__"


def _layer1_queue(access_token: str, base_url: str, headers: dict, transaction_id: str, process_name: str) -> tuple[str | None, str | None]:
    """Layer 1: Find transaction in queue by Reference, use start/end timestamps to fetch logs.

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


def _search_specific_content_by_status(
    access_token: str,
    base_url: str,
    headers: dict,
    transaction_id: str,
    status: str,
) -> dict | None:
    """Fetch queue items with the given status and search SpecificContent for transaction_id in Python.

    OData does not support filtering on SpecificContent (large data field) — filter client-side.
    Returns the first matching queue item dict, or None.
    """
    try:
        resp = _get(
            f"{base_url}/orchestrator_/odata/QueueItems",
            headers=headers,
            params={
                "$filter": f"Status eq '{status}'",
                "$orderby": "EndProcessing desc",
                "$top": 20,
            },
            timeout=10
        )
        resp.raise_for_status()
        items = resp.json().get("value", [])
        for item in items:
            specific = item.get("SpecificContent") or item.get("SpecificData") or ""
            if isinstance(specific, dict):
                specific = str(specific)
            if transaction_id in specific:
                return item
    except Exception:
        pass
    return None


def _layer3_specific_content(
    access_token: str,
    base_url: str,
    headers: dict,
    transaction_id: str,
    process_name: str,
) -> tuple[str | None, str | None]:
    """Layer 3: Search queue items across all statuses in parallel via SpecificContent.

    OData filtering on SpecificContent is not supported — fetch by status and filter in Python.
    Spins up one thread per status (Failed, New, InProgress) for parallelism.
    If found, attempts to fetch logs via Layer 1 timestamps (Failed items only).

    Returns (result, source_label) or (None, None).
    """
    statuses = ["Failed", "New", "InProgress"]

    found_item: dict | None = None
    found_status: str | None = None

    with ThreadPoolExecutor(max_workers=len(statuses)) as executor:
        futures = {
            executor.submit(
                _search_specific_content_by_status,
                access_token, base_url, headers, transaction_id, status
            ): status
            for status in statuses
        }
        for future in as_completed(futures):
            item = future.result()
            if item and found_item is None:
                found_item = item
                found_status = futures[future]

    if found_item is None:
        return None, None

    status = found_item.get("Status", found_status or "")

    if status in _NEW_STATUSES:
        return (
            f"Transaction found in queue SpecificContent with status '{status}' — "
            f"the bot has not processed this transaction yet. No logs available until a robot picks it up.",
            "SpecificContent search (not yet processed)"
        )

    if status in _INPROGRESS_STATUSES:
        return (
            f"Transaction found in queue SpecificContent with status 'InProgress' — "
            f"currently being processed. No complete logs available yet.",
            "SpecificContent search (in progress)"
        )

    # Failed — attempt to get logs via timestamps
    start_ts = found_item.get("StartProcessing")
    end_ts = found_item.get("EndProcessing")
    if start_ts:
        log_filter = f"TimeStamp ge {start_ts}"
        if end_ts:
            log_filter += f" and TimeStamp le {end_ts}"
        try:
            resp = _get(
                f"{base_url}/orchestrator_/odata/RobotLogs",
                headers=headers,
                params={"$filter": log_filter, "$orderby": "TimeStamp asc", "$top": 50},
                timeout=10
            )
            resp.raise_for_status()
            logs = resp.json().get("value", [])
            if logs:
                return _format_logs(logs), "SpecificContent search → log window from timestamps"
        except Exception:
            pass

    return (
        f"Transaction found in queue SpecificContent with status '{status}' "
        f"but no log window could be extracted.",
        "SpecificContent search (no log window)"
    )


def fetch_recent_failures(access_token: str, base_url: str, process_name: str, exclude_transaction_id: str) -> list[str]:
    """Fetch other recent failed transactions in the same process queue (cross-transaction analysis)."""
    numeric_id = _extract_numeric_id(process_name)
    folder_id = _find_folder_id(access_token, base_url, numeric_id, process_name)
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

    Layer 1: Queue item by Reference — uses start/end timestamps for a precise log window.
    Layer 2: Transaction ID in log messages — bounded by JobKey + Transaction Ended marker.
    Layer 3: SpecificContent parallel search — fetches items by status, filters in Python.

    Returns (logs, source_label) where source_label describes which layer succeeded.
    If not found, returns (_TRANSACTION_NOT_FOUND, "not_found").
    """
    numeric_id = _extract_numeric_id(process_name)
    log.info(f"Looking up folder for process: '{process_name}' (numeric_id={numeric_id})")
    folder_id = _find_folder_id(access_token, base_url, numeric_id, process_name)
    if folder_id:
        log.info(f"Found folder ID: {folder_id}")
    else:
        log.warning(f"No folder found for process '{process_name}' — proceeding without folder scope")
    headers = _build_headers(access_token, folder_id)

    log.info(f"Trying Layer 1: Queue Reference lookup for transaction_id={transaction_id}")
    result, source = _layer1_queue(access_token, base_url, headers, transaction_id, process_name)
    if source:
        log.info(f"Layer 1 resolved: {source}")
        return result, source

    log.info("Layer 1 found nothing, trying Layer 2: Transaction ID in log messages")
    result, source = _layer2_transaction_in_logs(access_token, base_url, headers, transaction_id, process_name)
    if source:
        log.info(f"Layer 2 resolved: {source}")
        return result, source

    log.info("Layer 2 found nothing, trying Layer 3: SpecificContent parallel search")
    result, source = _layer3_specific_content(access_token, base_url, headers, transaction_id, process_name)
    if source:
        log.info(f"Layer 3 resolved: {source}")
        return result, source

    log.warning(f"Transaction '{transaction_id}' not found in any layer")
    return _TRANSACTION_NOT_FOUND, "not_found"
