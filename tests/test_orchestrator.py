from unittest.mock import patch, MagicMock, call
import requests as requests_lib
from spectre.orchestrator import (
    fetch_logs, fetch_recent_failures, _extract_numeric_id, _find_folder_id,
    _get, _find_folder_with_transaction, _TRANSACTION_NOT_FOUND
)


class TestRetry:

    def _make_resp(self, status_code):
        mock = MagicMock()
        mock.status_code = status_code
        mock.raise_for_status = MagicMock()
        mock.json.return_value = {"value": []}
        return mock

    def test_retries_on_500_then_succeeds(self):
        fail = self._make_resp(500)
        ok = self._make_resp(200)
        with patch("spectre.orchestrator.requests.get", side_effect=[fail, ok]) as mock_get, \
             patch("spectre.orchestrator.time.sleep"):
            resp = _get("https://example.com", timeout=10)
        assert resp.status_code == 200
        assert mock_get.call_count == 2

    def test_raises_immediately_on_4xx(self):
        fail = self._make_resp(404)
        with patch("spectre.orchestrator.requests.get", return_value=fail) as mock_get, \
             patch("spectre.orchestrator.time.sleep"):
            resp = _get("https://example.com", timeout=10)
        assert resp.status_code == 404
        assert mock_get.call_count == 1

    def test_retries_on_connection_error_then_succeeds(self):
        ok = self._make_resp(200)
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

    def test_returns_folder_id_when_found(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"value": [{"Id": 3077087, "FullyQualifiedName": "Automation Process/3201 Invoice Processing"}]}
        mock_response.raise_for_status = MagicMock()

        with patch("spectre.orchestrator.requests.get", return_value=mock_response):
            result = _find_folder_id("fake_token", "https://example.com", "3201")

        assert result == "3077087"

    def test_returns_none_when_not_found(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"value": []}
        mock_response.raise_for_status = MagicMock()

        with patch("spectre.orchestrator.requests.get", return_value=mock_response):
            result = _find_folder_id("fake_token", "https://example.com", "9999")

        assert result is None


class TestFindFolderWithTransaction:

    def _make_resp(self, value):
        mock = MagicMock()
        mock.status_code = 200
        mock.json.return_value = {"value": value}
        mock.raise_for_status = MagicMock()
        return mock

    def test_returns_matching_folder(self):
        folders = [
            {"Id": 1, "FullyQualifiedName": "FolderA"},
            {"Id": 2, "FullyQualifiedName": "FolderB"},
        ]
        # FolderA has no match, FolderB has a match
        def fake_folder_has(token, base_url, folder_id, txn_id):
            return folder_id == "2"

        with patch("spectre.orchestrator._folder_has_transaction", side_effect=fake_folder_has):
            result = _find_folder_with_transaction("tok", "https://example.com", "TXN-1", folders)

        assert result["Id"] == 2

    def test_returns_none_when_no_folder_matches(self):
        folders = [{"Id": 1, "FullyQualifiedName": "FolderA"}, {"Id": 2, "FullyQualifiedName": "FolderB"}]
        with patch("spectre.orchestrator._folder_has_transaction", return_value=False):
            result = _find_folder_with_transaction("tok", "https://example.com", "TXN-NOPE", folders)
        assert result is None

    def test_returns_none_for_empty_folder_list(self):
        result = _find_folder_with_transaction("tok", "https://example.com", "TXN-1", [])
        assert result is None

    def test_skips_folders_without_id(self):
        folders = [{"FullyQualifiedName": "NoId"}, {"Id": 5, "FullyQualifiedName": "HasId"}]
        with patch("spectre.orchestrator._folder_has_transaction", side_effect=lambda t, b, fid, txn: fid == "5"):
            result = _find_folder_with_transaction("tok", "https://example.com", "TXN-1", folders)
        assert result["Id"] == 5


class TestFetchLogs:

    def _make_resp(self, value):
        mock = MagicMock()
        mock.status_code = 200
        mock.json.return_value = {"value": value}
        mock.raise_for_status = MagicMock()
        return mock

    def test_layer1_queue_returns_logs(self):
        folder_resp = self._make_resp([{"Id": 3077087}])
        queue_resp = self._make_resp([{"Status": "Successful", "StartProcessing": "2026-06-13T10:00:00Z", "EndProcessing": "2026-06-13T10:05:00Z"}])
        logs_resp = self._make_resp([
            {"TimeStamp": "2026-06-13T10:01:00Z", "Level": "Error", "Message": "Login timeout", "RobotName": "Robot1", "ProcessName": "FinanceBot"}
        ])

        with patch("spectre.orchestrator.requests.get", side_effect=[folder_resp, queue_resp, logs_resp]):
            logs, source = fetch_logs("fake_token", "https://example.com", "3201", "ICSAUTO-3201 Finance Bot")

        assert "Queue transaction window" in source
        assert "Login timeout" in logs

    def test_layer1_queue_item_not_yet_processed(self):
        folder_resp = self._make_resp([])
        queue_resp = self._make_resp([{"Status": "New", "StartProcessing": None, "EndProcessing": None}])

        with patch("spectre.orchestrator.requests.get", side_effect=[folder_resp, queue_resp]):
            logs, source = fetch_logs("fake_token", "https://example.com", "INV-001", "ICSAUTO-3201 Finance Bot")

        assert source == "Queue item status (not yet processed)"
        assert "has not processed this transaction yet" in logs
        assert "New" in logs

    def test_layer1_queue_item_inprogress_falls_through_to_layer2(self):
        """InProgress item falls through — Layer 2 also finds nothing, Layer 3 returns not_found."""
        folder_resp = self._make_resp([])
        queue_resp = self._make_resp([{"Status": "InProgress"}])
        layer2_empty = self._make_resp([])  # Layer 2: no log message match

        all_folders_resp = self._make_resp([])  # Layer 3: no folders

        with patch("spectre.orchestrator.requests.get", side_effect=[folder_resp, queue_resp, layer2_empty, all_folders_resp]):
            logs, source = fetch_logs("fake_token", "https://example.com", "INV-001", "ICSAUTO-3201 Finance Bot")

        assert logs == _TRANSACTION_NOT_FOUND
        assert source == "not_found"

    def test_layer2_performer_logged_transaction(self):
        folder_resp = self._make_resp([])
        queue_resp = self._make_resp([])
        start_log_resp = self._make_resp([{"TimeStamp": "2026-06-13T10:00:00Z", "JobKey": "job-abc-123", "ProcessName": "Finance_Performer"}])
        end_log_resp = self._make_resp([{"TimeStamp": "2026-06-13T10:05:00Z"}])
        window_logs_resp = self._make_resp([
            {"TimeStamp": "2026-06-13T10:02:00Z", "Level": "Error", "Message": "NullRef error", "RobotName": "Robot1", "ProcessName": "Finance_Performer"}
        ])

        with patch("spectre.orchestrator.requests.get", side_effect=[folder_resp, queue_resp, start_log_resp, end_log_resp, window_logs_resp]):
            logs, source = fetch_logs("fake_token", "https://example.com", "TXN-001", "ICSAUTO-3201 Finance Bot")

        assert "Log message window" in source
        assert "NullRef error" in logs

    def test_layer2_dispatcher_falls_through_to_layer3_all_folders(self):
        """Dispatcher logged it — falls to Layer 3 all-folders search, finds nothing → not_found."""
        folder_resp = self._make_resp([])
        queue_resp = self._make_resp([])
        dispatcher_log_resp = self._make_resp([{"TimeStamp": "2026-06-13T09:00:00Z", "JobKey": "job-disp-001", "ProcessName": "Finance_Dispatcher"}])
        all_folders_resp = self._make_resp([])  # Layer 3 returns no folders

        with patch("spectre.orchestrator.requests.get", side_effect=[folder_resp, queue_resp, dispatcher_log_resp, all_folders_resp]):
            logs, source = fetch_logs("fake_token", "https://example.com", "TXN-001", "ICSAUTO-3201 Finance Bot")

        assert logs == _TRANSACTION_NOT_FOUND
        assert source == "not_found"

    def test_layer3_finds_transaction_in_different_folder(self):
        """No folder match from process name — Layer 3 finds transaction in a different folder."""
        folder_resp = self._make_resp([])  # _find_folder_id finds nothing
        queue_resp = self._make_resp([])   # Layer 1 in default headers finds nothing
        layer2_empty = self._make_resp([]) # Layer 2 finds nothing
        # Layer 3: _list_all_folders returns two folders
        all_folders_resp = self._make_resp([
            {"Id": 10, "FullyQualifiedName": "TeamA"},
            {"Id": 20, "FullyQualifiedName": "TeamB"},
        ])
        # _folder_has_transaction: TeamA=False, TeamB=True
        has_txn_false = self._make_resp([])
        has_txn_true = self._make_resp([{"Id": 99}])
        # Layer 1 in TeamB folder: queue item with timestamps
        queue_in_folder = self._make_resp([{"Status": "Successful", "StartProcessing": "2026-06-13T10:00:00Z", "EndProcessing": "2026-06-13T10:05:00Z"}])
        logs_in_folder = self._make_resp([
            {"TimeStamp": "2026-06-13T10:01:00Z", "Level": "Error", "Message": "Found in TeamB", "RobotName": "R1", "ProcessName": "Proc"}
        ])

        with patch("spectre.orchestrator.requests.get", side_effect=[
            folder_resp, queue_resp, layer2_empty,
            all_folders_resp, has_txn_false, has_txn_true,
            queue_in_folder, logs_in_folder
        ]):
            logs, source = fetch_logs("fake_token", "https://example.com", "TXN-001", "ICSAUTO-9999 Unknown Bot")

        assert "Found in TeamB" in logs
        assert "All-folders fallback" in source
        assert "TeamB" in source

    def test_transaction_not_found_in_any_folder(self):
        """All layers fail — returns _TRANSACTION_NOT_FOUND sentinel."""
        folder_resp = self._make_resp([])
        queue_resp = self._make_resp([])
        layer2_empty = self._make_resp([])
        all_folders_resp = self._make_resp([{"Id": 1, "FullyQualifiedName": "FolderA"}])
        has_txn_false = self._make_resp([])

        with patch("spectre.orchestrator.requests.get", side_effect=[
            folder_resp, queue_resp, layer2_empty, all_folders_resp, has_txn_false
        ]):
            logs, source = fetch_logs("fake_token", "https://example.com", "TXN-NOPE", "ICSAUTO-9999 Bot")

        assert logs == _TRANSACTION_NOT_FOUND
        assert source == "not_found"

    def test_folder_found_but_transaction_missing_returns_not_found(self):
        """Folder identified from process name, but transaction not in Layer 1 or 2 → not_found."""
        folder_resp = self._make_resp([{"Id": 3077087}])  # folder found
        queue_resp = self._make_resp([])   # Layer 1 queue: nothing
        layer2_empty = self._make_resp([]) # Layer 2: nothing

        with patch("spectre.orchestrator.requests.get", side_effect=[folder_resp, queue_resp, layer2_empty]):
            logs, source = fetch_logs("fake_token", "https://example.com", "TXN-NOPE", "ICSAUTO-3201 Finance Bot")

        assert logs == _TRANSACTION_NOT_FOUND
        assert source == "not_found"

    def test_retries_on_5xx_before_falling_through(self):
        """A 5xx on Layer 1 queue call should retry, not immediately fall through."""
        folder_resp = self._make_resp([])
        fail_500 = MagicMock()
        fail_500.status_code = 500
        fail_500.raise_for_status = MagicMock()
        ok_empty = self._make_resp([])
        layer2_empty = self._make_resp([])
        all_folders_resp = self._make_resp([{"Id": 1, "FullyQualifiedName": "F"}])
        has_txn_true = self._make_resp([{"Id": 99}])
        queue_in_folder = self._make_resp([{"Status": "Successful", "StartProcessing": "2026-06-13T10:00:00Z", "EndProcessing": "2026-06-13T10:05:00Z"}])
        logs_resp = self._make_resp([
            {"TimeStamp": "t", "Level": "Error", "Message": "err", "RobotName": "r", "ProcessName": "Performer"}
        ])
        with patch("spectre.orchestrator.requests.get", side_effect=[
            folder_resp, fail_500, ok_empty, layer2_empty,
            all_folders_resp, has_txn_true, queue_in_folder, logs_resp
        ]), patch("spectre.orchestrator.time.sleep"):
            logs, source = fetch_logs("tok", "https://example.com", "TXN-1", "ICSAUTO-3201 Bot")
        assert "err" in logs


class TestFetchRecentFailures:

    def _make_resp(self, value):
        mock = MagicMock()
        mock.status_code = 200
        mock.json.return_value = {"value": value}
        mock.raise_for_status = MagicMock()
        return mock

    def test_returns_other_failed_references(self):
        folder_resp = self._make_resp([{"Id": 3077087}])
        queue_resp = self._make_resp([
            {"Reference": "INV-100", "Status": "Failed"},
            {"Reference": "INV-101", "Status": "Failed"},
            {"Reference": "INV-98768", "Status": "Failed"},
        ])

        with patch("spectre.orchestrator.requests.get", side_effect=[folder_resp, queue_resp]):
            result = fetch_recent_failures("fake_token", "https://example.com", "ICSAUTO-3201 Invoice Bot", "INV-98768")

        assert "INV-100" in result
        assert "INV-101" in result
        assert "INV-98768" not in result

    def test_returns_empty_when_no_failures(self):
        folder_resp = self._make_resp([])
        queue_resp = self._make_resp([])

        with patch("spectre.orchestrator.requests.get", side_effect=[folder_resp, queue_resp]):
            result = fetch_recent_failures("fake_token", "https://example.com", "ICSAUTO-3201 Invoice Bot", "INV-001")

        assert result == []

    def test_returns_empty_on_api_error(self):
        with patch("spectre.orchestrator.requests.get", side_effect=Exception("connection failed")):
            result = fetch_recent_failures("fake_token", "https://example.com", "ICSAUTO-3201 Invoice Bot", "INV-001")

        assert result == []
