from unittest.mock import patch, MagicMock
from spectre.orchestrator import fetch_logs, fetch_recent_failures, _extract_numeric_id, _find_folder_id


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
        mock_response.json.return_value = {"value": [{"Id": 3077087, "FullyQualifiedName": "Automation Process/3201 Invoice Processing"}]}
        mock_response.raise_for_status = MagicMock()

        with patch("spectre.orchestrator.requests.get", return_value=mock_response):
            result = _find_folder_id("fake_token", "https://example.com", "3201")

        assert result == "3077087"

    def test_returns_none_when_not_found(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"value": []}
        mock_response.raise_for_status = MagicMock()

        with patch("spectre.orchestrator.requests.get", return_value=mock_response):
            result = _find_folder_id("fake_token", "https://example.com", "9999")

        assert result is None


class TestFetchLogs:

    def _make_resp(self, value):
        mock = MagicMock()
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

    def test_layer1_queue_item_inprogress_falls_through(self):
        folder_resp = self._make_resp([])
        queue_resp = self._make_resp([{"Status": "InProgress", "StartProcessing": None, "EndProcessing": None}])
        # Layer 2 also finds nothing, so falls to layer 3
        empty = self._make_resp([])
        todays_errors_resp = self._make_resp([
            {"TimeStamp": "2026-06-14T09:00:00Z", "Level": "Error", "Message": "Still running error", "RobotName": "R1", "ProcessName": "FinancePerformer"}
        ])

        with patch("spectre.orchestrator.requests.get", side_effect=[folder_resp, queue_resp, empty, todays_errors_resp]):
            logs, source = fetch_logs("fake_token", "https://example.com", "INV-001", "ICSAUTO-3201 Finance Bot")

        assert "fallback" in source
        assert "Still running error" in logs

    def test_layer2_performer_logged_transaction(self):
        folder_resp = self._make_resp([])
        queue_resp = self._make_resp([])
        # ProcessName contains 'Performer' — Layer 2 should proceed to fetch the job window
        start_log_resp = self._make_resp([{"TimeStamp": "2026-06-13T10:00:00Z", "JobKey": "job-abc-123", "ProcessName": "Finance_Performer"}])
        end_log_resp = self._make_resp([{"TimeStamp": "2026-06-13T10:05:00Z"}])
        window_logs_resp = self._make_resp([
            {"TimeStamp": "2026-06-13T10:02:00Z", "Level": "Error", "Message": "NullRef error", "RobotName": "Robot1", "ProcessName": "Finance_Performer"}
        ])

        with patch("spectre.orchestrator.requests.get", side_effect=[folder_resp, queue_resp, start_log_resp, end_log_resp, window_logs_resp]):
            logs, source = fetch_logs("fake_token", "https://example.com", "TXN-001", "ICSAUTO-3201 Finance Bot")

        assert "Log message window" in source
        assert "NullRef error" in logs

    def test_layer2_dispatcher_logged_transaction_falls_through(self):
        folder_resp = self._make_resp([])
        queue_resp = self._make_resp([])
        # ProcessName is a dispatcher — Layer 2 should fall through to Layer 3
        dispatcher_log_resp = self._make_resp([{"TimeStamp": "2026-06-13T09:00:00Z", "JobKey": "job-disp-001", "ProcessName": "Finance_Dispatcher"}])
        todays_errors_resp = self._make_resp([
            {"TimeStamp": "2026-06-13T09:30:00Z", "Level": "Error", "Message": "Downstream error", "RobotName": "Robot1", "ProcessName": "Finance_Performer"}
        ])

        with patch("spectre.orchestrator.requests.get", side_effect=[folder_resp, queue_resp, dispatcher_log_resp, todays_errors_resp]):
            logs, source = fetch_logs("fake_token", "https://example.com", "TXN-001", "ICSAUTO-3201 Finance Bot")

        assert "fallback" in source
        assert "Downstream error" in logs

    def test_layer3_fallback_todays_errors(self):
        folder_resp = self._make_resp([])
        queue_resp = self._make_resp([])
        start_log_resp = self._make_resp([])
        todays_errors_resp = self._make_resp([
            {"TimeStamp": "2026-06-13T09:00:00Z", "Level": "Error", "Message": "Timeout error", "RobotName": "Robot1", "ProcessName": "FinanceBot"}
        ])

        with patch("spectre.orchestrator.requests.get", side_effect=[folder_resp, queue_resp, start_log_resp, todays_errors_resp]) as mock_get:
            logs, source = fetch_logs("fake_token", "https://example.com", "TXN-001", "ICSAUTO-3201 Finance Bot")

        assert "fallback" in source
        assert "Timeout error" in logs

        # Verify Layer 3 filters for Performer or Runner processes
        layer3_call = mock_get.call_args_list[-1]
        layer3_url = layer3_call[0][0] if layer3_call[0] else layer3_call[1].get("url", "")
        layer3_params = layer3_call[1].get("params", {}) if layer3_call[1] else {}
        filter_str = layer3_params.get("$filter", "") or layer3_url
        assert "Performer" in filter_str or "Runner" in filter_str, \
            f"Layer 3 filter should target Performer/Runner processes, got: {filter_str}"

    def test_layer3_no_logs_found(self):
        empty = self._make_resp([])

        with patch("spectre.orchestrator.requests.get", side_effect=[empty, empty, empty, empty]):
            logs, source = fetch_logs("fake_token", "https://example.com", "TXN-999", "ICSAUTO-9999 Unknown Bot")

        assert "No logs found" in logs
        assert "fallback" in source


class TestFetchRecentFailures:

    def _make_resp(self, value):
        mock = MagicMock()
        mock.json.return_value = {"value": value}
        mock.raise_for_status = MagicMock()
        return mock

    def test_returns_other_failed_references(self):
        folder_resp = self._make_resp([{"Id": 3077087}])
        queue_resp = self._make_resp([
            {"Reference": "INV-100", "Status": "Failed"},
            {"Reference": "INV-101", "Status": "Failed"},
            {"Reference": "INV-98768", "Status": "Failed"},  # current — should be excluded
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
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("API error")

        with patch("spectre.orchestrator.requests.get", side_effect=Exception("connection failed")):
            result = fetch_recent_failures("fake_token", "https://example.com", "ICSAUTO-3201 Invoice Bot", "INV-001")

        assert result == []
