from unittest.mock import patch, MagicMock, call
import requests as requests_lib
from spectre.orchestrator import (
    fetch_logs, fetch_recent_failures, _extract_numeric_id, _find_folder_id,
    _get, _TRANSACTION_NOT_FOUND
)


def _resp(value, status_code=200):
    """Helper: mock response with given value list."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = {"value": value}
    mock.raise_for_status = MagicMock()
    return mock


def _folder_found():
    """Single folder-lookup response that returns a match — consumes 1 API call."""
    return _resp([{"Id": 3077087, "FullyQualifiedName": "Automation Process/3201 Invoice Processing"}])


def _folder_miss():
    """Single folder-lookup response that returns nothing — one attempt in _find_folder_id."""
    return _resp([])


def _folder_not_found_seq(process_name="ICSAUTO-3201 Finance Bot"):
    """Return a sequence of mock responses that drives _find_folder_id to return None.

    _find_folder_id makes up to 3 API calls when no folder is found:
      1. numeric-ID filter  → empty
      2. keyword filter     → empty  (only if keyword extracted)
      3. all-folders list   → empty

    'ICSAUTO-3201 Finance Bot': keyword 'Finance' is extracted → 3 calls.
    'ICSAUTO-3201 Bot':         no keyword (all tokens short/stop) → 2 calls (numeric + all-folders).
    """
    import re
    _STOP_WORDS = {"performer", "runner", "dispatcher", "bot", "process", "processing", "automation"}
    parts = re.split(r'[\s\-_]+', process_name)
    has_keyword = any(
        len(re.sub(r'\d+', '', p).strip()) >= 4 and re.sub(r'\d+', '', p).strip().lower() not in _STOP_WORDS
        for p in parts
    )
    calls = 3 if has_keyword else 2
    return [_resp([]) for _ in range(calls)]


class TestRetry:

    def test_retries_on_500_then_succeeds(self):
        fail = _resp([], 500)
        ok = _resp([])
        with patch("spectre.orchestrator.requests.get", side_effect=[fail, ok]) as mock_get, \
             patch("spectre.orchestrator.time.sleep"):
            resp = _get("https://example.com", timeout=10)
        assert resp.status_code == 200
        assert mock_get.call_count == 2

    def test_raises_immediately_on_4xx(self):
        fail = _resp([], 404)
        with patch("spectre.orchestrator.requests.get", return_value=fail) as mock_get, \
             patch("spectre.orchestrator.time.sleep"):
            resp = _get("https://example.com", timeout=10)
        assert resp.status_code == 404
        assert mock_get.call_count == 1

    def test_retries_on_connection_error_then_succeeds(self):
        ok = _resp([])
        with patch("spectre.orchestrator.requests.get", side_effect=[requests_lib.ConnectionError("down"), ok]) as mock_get, \
             patch("spectre.orchestrator.time.sleep"):
            resp = _get("https://example.com", timeout=10)
        assert resp.status_code == 200
        assert mock_get.call_count == 2

    def test_raises_after_all_attempts_exhausted(self):
        import pytest
        with patch("spectre.orchestrator.requests.get", side_effect=requests_lib.ConnectionError("down")), \
             patch("spectre.orchestrator.time.sleep"):
            with pytest.raises(requests_lib.ConnectionError):
                _get("https://example.com", timeout=10)


class TestExtractNumericId:

    def test_extracts_id_from_icsauto_format(self):
        assert _extract_numeric_id("ICSAUTO-3201 Invoice Processing") == "3201"

    def test_extracts_plain_numeric_id(self):
        assert _extract_numeric_id("3202 GL Reconciliation") == "3202"

    def test_returns_none_when_no_number(self):
        assert _extract_numeric_id("Invoice Processing") is None


class TestFindFolderId:

    def test_returns_folder_id_when_found_by_numeric_id(self):
        with patch("spectre.orchestrator.requests.get", return_value=_folder_found()):
            result = _find_folder_id("fake_token", "https://example.com", "3201")
        assert result == "3077087"

    def test_returns_none_when_numeric_id_not_found_and_no_process_name(self):
        # No process_name → no keyword, no all-folders → just 1 call (numeric)
        with patch("spectre.orchestrator.requests.get", return_value=_resp([])):
            result = _find_folder_id("fake_token", "https://example.com", "9999")
        assert result is None

    def test_keyword_fallback_when_numeric_id_fails(self):
        # "ICSAUTO-9999 Invoice Performer": numeric→empty, keyword "Invoice"→match
        keyword_match = _resp([{"Id": 5555, "FullyQualifiedName": "AP/Invoice Processing"}])
        with patch("spectre.orchestrator.requests.get", side_effect=[_resp([]), keyword_match]):
            result = _find_folder_id("fake_token", "https://example.com", "9999", "ICSAUTO-9999 Invoice Performer")
        assert result == "5555"

    def test_best_match_fallback_when_keyword_also_fails(self):
        # "Invoice Processing Performer": "Invoice"→keyword, numeric→empty, keyword→empty, all-folders→match
        # process_name has no numeric_id so primary search is skipped; keyword "Invoice" extracted
        all_folders = _resp([
            {"Id": 11, "FullyQualifiedName": "Random/Folder"},
            {"Id": 22, "FullyQualifiedName": "AP/Invoice Processing Performer"},
        ])
        with patch("spectre.orchestrator.requests.get", side_effect=[_resp([]), all_folders]):
            # numeric_id=None → skip primary; keyword "Invoice" extracted → 1 keyword call → empty → all-folders
            result = _find_folder_id("fake_token", "https://example.com", None, "Invoice Processing Performer")
        assert result == "22"


class TestFetchLogs:

    def test_layer1_queue_returns_logs(self):
        queue_resp = _resp([{"Status": "Successful", "StartProcessing": "2026-06-13T10:00:00Z", "EndProcessing": "2026-06-13T10:05:00Z"}])
        logs_resp = _resp([
            {"TimeStamp": "2026-06-13T10:01:00Z", "Level": "Error", "Message": "Login timeout", "RobotName": "Robot1", "ProcessName": "FinanceBot"}
        ])
        with patch("spectre.orchestrator.requests.get", side_effect=[_folder_found(), queue_resp, logs_resp]):
            logs, source = fetch_logs("fake_token", "https://example.com", "3201", "ICSAUTO-3201 Finance Bot")
        assert "Queue transaction window" in source
        assert "Login timeout" in logs

    def test_layer1_queue_item_not_yet_processed(self):
        queue_resp = _resp([{"Status": "New", "StartProcessing": None, "EndProcessing": None}])
        # folder not found: "ICSAUTO-3201 Finance Bot" → keyword "Finance" → 3 folder calls
        side_effects = _folder_not_found_seq("ICSAUTO-3201 Finance Bot") + [queue_resp]
        with patch("spectre.orchestrator.requests.get", side_effect=side_effects):
            logs, source = fetch_logs("fake_token", "https://example.com", "INV-001", "ICSAUTO-3201 Finance Bot")
        assert source == "Queue item status (not yet processed)"
        assert "has not processed this transaction yet" in logs
        assert "New" in logs

    def test_layer1_queue_item_inprogress_falls_through(self):
        """InProgress item falls through Layer 1 and Layer 2; Layer 3 SpecificContent also empty → not_found."""
        queue_resp = _resp([{"Status": "InProgress"}])
        layer2_empty = _resp([])
        side_effects = _folder_not_found_seq("ICSAUTO-3201 Finance Bot") + [queue_resp, layer2_empty]
        with patch("spectre.orchestrator.requests.get", side_effect=side_effects), \
             patch("spectre.orchestrator._search_specific_content_by_status", return_value=None):
            logs, source = fetch_logs("fake_token", "https://example.com", "INV-001", "ICSAUTO-3201 Finance Bot")
        assert logs == _TRANSACTION_NOT_FOUND
        assert source == "not_found"

    def test_layer2_performer_logged_transaction(self):
        start_log_resp = _resp([{"TimeStamp": "2026-06-13T10:00:00Z", "JobKey": "job-abc-123", "ProcessName": "Finance_Performer"}])
        end_log_resp = _resp([{"TimeStamp": "2026-06-13T10:05:00Z"}])
        window_logs_resp = _resp([
            {"TimeStamp": "2026-06-13T10:02:00Z", "Level": "Error", "Message": "NullRef error", "RobotName": "Robot1", "ProcessName": "Finance_Performer"}
        ])
        side_effects = _folder_not_found_seq("ICSAUTO-3201 Finance Bot") + [
            _resp([]),        # Layer 1 queue: nothing
            start_log_resp, end_log_resp, window_logs_resp,
        ]
        with patch("spectre.orchestrator.requests.get", side_effect=side_effects):
            logs, source = fetch_logs("fake_token", "https://example.com", "TXN-001", "ICSAUTO-3201 Finance Bot")
        assert "Log message window" in source
        assert "NullRef error" in logs

    def test_layer2_dispatcher_falls_through_to_layer3(self):
        """Dispatcher logged it — falls to Layer 3 SpecificContent search, all empty → not_found."""
        dispatcher_log_resp = _resp([{"TimeStamp": "2026-06-13T09:00:00Z", "JobKey": "job-disp-001", "ProcessName": "Finance_Dispatcher"}])
        side_effects = _folder_not_found_seq("ICSAUTO-3201 Finance Bot") + [
            _resp([]),            # Layer 1: nothing
            dispatcher_log_resp,  # Layer 2: dispatcher
        ]
        with patch("spectre.orchestrator.requests.get", side_effect=side_effects), \
             patch("spectre.orchestrator._search_specific_content_by_status", return_value=None):
            logs, source = fetch_logs("fake_token", "https://example.com", "TXN-001", "ICSAUTO-3201 Finance Bot")
        assert logs == _TRANSACTION_NOT_FOUND
        assert source == "not_found"

    def test_layer3_finds_transaction_in_specific_content_failed(self):
        """Layer 1 and 2 find nothing; Layer 3 SpecificContent finds a Failed item and fetches logs."""
        failed_item = {
            "Status": "Failed", "Reference": "TXN-SC-001",
            "SpecificContent": '{"TransactionId": "TXN-SC-001", "Amount": 100}',
            "StartProcessing": "2026-06-13T10:00:00Z",
            "EndProcessing": "2026-06-13T10:05:00Z",
        }
        logs_resp = _resp([
            {"TimeStamp": "2026-06-13T10:01:00Z", "Level": "Error", "Message": "SC error found", "RobotName": "R1", "ProcessName": "P"}
        ])

        def fake_sc(token, base_url, headers, txn_id, status):
            return failed_item if status == "Failed" else None

        side_effects = [_folder_found(),
            _resp([]),   # Layer 1: nothing
            _resp([]),   # Layer 2: nothing
            logs_resp,   # log window fetch after Layer 3 finds Failed item
        ]
        with patch("spectre.orchestrator.requests.get", side_effect=side_effects), \
             patch("spectre.orchestrator._search_specific_content_by_status", side_effect=fake_sc):
            logs, source = fetch_logs("fake_token", "https://example.com", "TXN-SC-001", "ICSAUTO-3201 Finance Bot")
        assert "SC error found" in logs
        assert "SpecificContent" in source

    def test_layer3_finds_transaction_new_status(self):
        """Layer 3 finds item with New status — returns status message, no logs."""
        new_item = {"Status": "New", "Reference": "TXN-NEW-001", "SpecificContent": '{"TransactionId": "TXN-NEW-001"}'}

        def fake_sc(token, base_url, headers, txn_id, status):
            return new_item if status == "New" else None

        side_effects = _folder_not_found_seq("ICSAUTO-3201 Finance Bot") + [
            _resp([]),  # Layer 1
            _resp([]),  # Layer 2
        ]
        with patch("spectre.orchestrator.requests.get", side_effect=side_effects), \
             patch("spectre.orchestrator._search_specific_content_by_status", side_effect=fake_sc):
            logs, source = fetch_logs("fake_token", "https://example.com", "TXN-NEW-001", "ICSAUTO-3201 Finance Bot")
        assert "SpecificContent search" in source
        assert "has not processed this transaction yet" in logs

    def test_layer3_finds_transaction_inprogress_status(self):
        """Layer 3 finds item with InProgress status — returns status message."""
        wip_item = {"Status": "InProgress", "Reference": "TXN-WIP-001", "SpecificContent": '{"TransactionId": "TXN-WIP-001"}'}

        def fake_sc(token, base_url, headers, txn_id, status):
            return wip_item if status == "InProgress" else None

        side_effects = _folder_not_found_seq("ICSAUTO-3201 Finance Bot") + [
            _resp([]),  # Layer 1
            _resp([]),  # Layer 2
        ]
        with patch("spectre.orchestrator.requests.get", side_effect=side_effects), \
             patch("spectre.orchestrator._search_specific_content_by_status", side_effect=fake_sc):
            logs, source = fetch_logs("fake_token", "https://example.com", "TXN-WIP-001", "ICSAUTO-3201 Finance Bot")
        assert "SpecificContent search" in source
        assert "currently being processed" in logs

    def test_transaction_not_found_in_any_layer(self):
        """All layers fail — returns _TRANSACTION_NOT_FOUND sentinel."""
        side_effects = [_folder_found(),
            _resp([]),  # Layer 1
            _resp([]),  # Layer 2
        ]
        with patch("spectre.orchestrator.requests.get", side_effect=side_effects), \
             patch("spectre.orchestrator._search_specific_content_by_status", return_value=None):
            logs, source = fetch_logs("fake_token", "https://example.com", "TXN-NOPE", "ICSAUTO-3201 Bot")
        assert logs == _TRANSACTION_NOT_FOUND
        assert source == "not_found"

    def test_retries_on_5xx_before_falling_through(self):
        """A 5xx on Layer 1 queue call should retry, not immediately fall through."""
        fail_500 = MagicMock()
        fail_500.status_code = 500
        fail_500.raise_for_status = MagicMock()
        failed_item = {
            "Status": "Failed", "Reference": "TXN-1", "SpecificContent": "TXN-1",
            "StartProcessing": "2026-06-13T10:00:00Z", "EndProcessing": "2026-06-13T10:05:00Z",
        }
        logs_resp = _resp([
            {"TimeStamp": "t", "Level": "Error", "Message": "err", "RobotName": "r", "ProcessName": "Performer"}
        ])

        def fake_sc(token, base_url, headers, txn_id, status):
            return failed_item if status == "Failed" else None

        side_effects = [_folder_found(),
            fail_500, _resp([]),  # Layer 1: 500 retry → empty
            _resp([]),            # Layer 2: nothing
            logs_resp,            # Layer 3 log window fetch
        ]
        with patch("spectre.orchestrator.requests.get", side_effect=side_effects), \
             patch("spectre.orchestrator.time.sleep"), \
             patch("spectre.orchestrator._search_specific_content_by_status", side_effect=fake_sc):
            logs, source = fetch_logs("tok", "https://example.com", "TXN-1", "ICSAUTO-3201 Bot")
        assert "err" in logs


class TestFetchRecentFailures:

    def test_returns_other_failed_references(self):
        queue_resp = _resp([
            {"Reference": "INV-100", "Status": "Failed"},
            {"Reference": "INV-101", "Status": "Failed"},
            {"Reference": "INV-98768", "Status": "Failed"},
        ])
        with patch("spectre.orchestrator.requests.get", side_effect=[_folder_found(), queue_resp]):
            result = fetch_recent_failures("fake_token", "https://example.com", "ICSAUTO-3201 Invoice Bot", "INV-98768")
        assert "INV-100" in result
        assert "INV-101" in result
        assert "INV-98768" not in result

    def test_returns_empty_when_no_failures(self):
        side_effects = _folder_not_found_seq("ICSAUTO-3201 Invoice Bot") + [_resp([])]
        with patch("spectre.orchestrator.requests.get", side_effect=side_effects):
            result = fetch_recent_failures("fake_token", "https://example.com", "ICSAUTO-3201 Invoice Bot", "INV-001")
        assert result == []

    def test_returns_empty_on_api_error(self):
        with patch("spectre.orchestrator.requests.get", side_effect=Exception("connection failed")):
            result = fetch_recent_failures("fake_token", "https://example.com", "ICSAUTO-3201 Invoice Bot", "INV-001")
        assert result == []
