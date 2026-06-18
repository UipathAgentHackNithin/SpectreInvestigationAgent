from unittest.mock import patch, AsyncMock, MagicMock
from spectre.agent import investigate, InvestigateIn


def _base_patches(diagnosis_result):
    """Return the common set of patches needed for all tests."""
    return [
        patch("spectre.agent.get_pat", return_value=("fake_pat", "https://example.com")),
        patch("spectre.agent.get_llm_token", return_value=("fake_llm_token", "https://example.com")),
        patch("spectre.agent.UiPath", return_value=MagicMock()),
        patch("spectre.agent._search_kb", return_value=""),
        patch("spectre.agent._ingest_to_kb"),
        patch("spectre.agent.triage", new=AsyncMock(return_value={"issue_type": "credentials", "triage_notes": "login failure"})),
        patch("spectre.agent.fetch_recent_failures", return_value=[]),
        patch("spectre.agent.diagnose", new=AsyncMock(return_value=diagnosis_result)),
    ]


class TestInvestigate:

    def _mock_diagnosis(self):
        return {
            "diagnosis": "Login timeout due to slow network",
            "bot_name": "FinanceBot",
            "confidence": "High",
            "error_found": True,
            "recommended_action": "Reset credentials"
        }

    async def test_returns_correct_output(self):
        with patch("spectre.agent.get_pat", return_value=("fake_pat", "https://example.com")), \
             patch("spectre.agent.get_llm_token", return_value=("fake_llm_token", "https://example.com")), \
             patch("spectre.agent.UiPath", return_value=MagicMock()), \
             patch("spectre.agent._search_kb", return_value=""), \
             patch("spectre.agent._ingest_to_kb"), \
             patch("spectre.agent.fetch_logs", return_value=("[Error] Login timeout", "Queue transaction window")), \
             patch("spectre.agent.triage", new=AsyncMock(return_value={"issue_type": "credentials", "triage_notes": "login failure"})), \
             patch("spectre.agent.fetch_recent_failures", return_value=[]), \
             patch("spectre.agent.diagnose", new=AsyncMock(return_value=self._mock_diagnosis())):

            result = await investigate(InvestigateIn(
                transaction_id="TXN-001",
                description="Bot failing at login",
                team="Finance",
                process_name="ICSAUTO-3201 Invoice Bot",
                channel_id="C123",
                thread_ts="123.456"
            ))

        assert result.diagnosis == "Login timeout due to slow network"
        assert result.bot_name == "FinanceBot"
        assert result.error_found is True

    async def test_defaults_on_missing_llm_fields(self):
        with patch("spectre.agent.get_pat", return_value=("fake_pat", "https://example.com")), \
             patch("spectre.agent.get_llm_token", return_value=("fake_llm_token", "https://example.com")), \
             patch("spectre.agent.UiPath", return_value=MagicMock()), \
             patch("spectre.agent._search_kb", return_value=""), \
             patch("spectre.agent._ingest_to_kb"), \
             patch("spectre.agent.fetch_logs", return_value=("No logs found", "Today's error logs fallback")), \
             patch("spectre.agent.triage", new=AsyncMock(return_value={"issue_type": "unknown", "triage_notes": ""})), \
             patch("spectre.agent.fetch_recent_failures", return_value=[]), \
             patch("spectre.agent.diagnose", new=AsyncMock(return_value={})):

            result = await investigate(InvestigateIn(
                transaction_id="TXN-002",
                description="Unknown issue",
                team="HR",
                process_name="ICSAUTO-3202 HR Bot",
                channel_id="C456",
                thread_ts="789.000"
            ))

        assert result.diagnosis == "Unable to diagnose"
        assert result.bot_name == "Unknown"
        assert result.error_found is False

    async def test_confidence_loop_triggers_targeted_retry(self):
        """When diagnose returns Low confidence and issue_type is known, targeted retry is called."""
        low_result = {"diagnosis": "Unclear", "bot_name": "Bot", "confidence": "Low", "error_found": False, "recommended_action": ""}
        high_result = {"diagnosis": "SAP login failed", "bot_name": "FinanceBot", "confidence": "High", "error_found": True, "recommended_action": "Reset SAP credentials"}

        with patch("spectre.agent.get_pat", return_value=("fake_pat", "https://example.com")), \
             patch("spectre.agent.get_llm_token", return_value=("fake_llm_token", "https://example.com")), \
             patch("spectre.agent.UiPath", return_value=MagicMock()), \
             patch("spectre.agent._search_kb", return_value=""), \
             patch("spectre.agent._ingest_to_kb"), \
             patch("spectre.agent.fetch_logs", return_value=("some logs", "Queue transaction window")), \
             patch("spectre.agent.triage", new=AsyncMock(return_value={"issue_type": "credentials", "triage_notes": "login error"})), \
             patch("spectre.agent.fetch_recent_failures", return_value=[]), \
             patch("spectre.agent.diagnose", new=AsyncMock(return_value=low_result)), \
             patch("spectre.agent.diagnose_targeted", new=AsyncMock(return_value=high_result)) as mock_targeted:

            result = await investigate(InvestigateIn(
                transaction_id="TXN-003",
                description="SAP login failing",
                team="Finance",
                process_name="ICSAUTO-3201 Invoice Bot",
                channel_id="C123",
                thread_ts="123.456"
            ))

        mock_targeted.assert_called_once()
        assert result.confidence == "High"
        assert result.diagnosis == "SAP login failed"

    async def test_confidence_loop_skipped_when_issue_type_unknown(self):
        """Low confidence + unknown issue_type should NOT trigger targeted retry."""
        low_result = {"diagnosis": "No idea", "bot_name": "Bot", "confidence": "Low", "error_found": False, "recommended_action": ""}

        with patch("spectre.agent.get_pat", return_value=("fake_pat", "https://example.com")), \
             patch("spectre.agent.get_llm_token", return_value=("fake_llm_token", "https://example.com")), \
             patch("spectre.agent.UiPath", return_value=MagicMock()), \
             patch("spectre.agent._search_kb", return_value=""), \
             patch("spectre.agent._ingest_to_kb"), \
             patch("spectre.agent.fetch_logs", return_value=("no useful logs", "Today's error logs fallback")), \
             patch("spectre.agent.triage", new=AsyncMock(return_value={"issue_type": "unknown", "triage_notes": ""})), \
             patch("spectre.agent.fetch_recent_failures", return_value=[]), \
             patch("spectre.agent.diagnose", new=AsyncMock(return_value=low_result)), \
             patch("spectre.agent.diagnose_targeted", new=AsyncMock()) as mock_targeted:

            result = await investigate(InvestigateIn(
                transaction_id="TXN-005",
                description="Mystery issue",
                team="Finance",
                process_name="ICSAUTO-3201 Invoice Bot",
                channel_id="C123",
                thread_ts="123.456"
            ))

        mock_targeted.assert_not_called()
        assert result.confidence == "Low"

    async def test_kb_past_resolution_passed_to_diagnose(self):
        """When KB returns a past resolution, it is passed to diagnose."""
        diagnosis_mock = AsyncMock(return_value={
            "diagnosis": "Same SAP issue as before", "bot_name": "FinanceBot",
            "confidence": "High", "error_found": True, "recommended_action": "Reset SAP creds"
        })

        with patch("spectre.agent.get_pat", return_value=("fake_pat", "https://example.com")), \
             patch("spectre.agent.get_llm_token", return_value=("fake_llm_token", "https://example.com")), \
             patch("spectre.agent.UiPath", return_value=MagicMock()), \
             patch("spectre.agent._search_kb", return_value="Similar past incident: SAP creds expired, fixed by rotating asset"), \
             patch("spectre.agent._ingest_to_kb"), \
             patch("spectre.agent.fetch_logs", return_value=("logs", "Queue transaction window")), \
             patch("spectre.agent.triage", new=AsyncMock(return_value={"issue_type": "credentials", "triage_notes": "login error"})), \
             patch("spectre.agent.fetch_recent_failures", return_value=[]), \
             patch("spectre.agent.diagnose", new=diagnosis_mock):

            await investigate(InvestigateIn(
                transaction_id="TXN-006",
                description="SAP login failing again",
                team="Finance",
                process_name="ICSAUTO-3201 Invoice Bot",
                channel_id="C123",
                thread_ts="123.456"
            ))

        call_kwargs = diagnosis_mock.call_args.kwargs
        assert "SAP creds expired" in call_kwargs["past_resolution"]

    async def test_ingest_called_when_error_found(self):
        """When error_found is True, _ingest_to_kb should be called."""
        with patch("spectre.agent.get_pat", return_value=("fake_pat", "https://example.com")), \
             patch("spectre.agent.get_llm_token", return_value=("fake_llm_token", "https://example.com")), \
             patch("spectre.agent.UiPath", return_value=MagicMock()), \
             patch("spectre.agent._search_kb", return_value=""), \
             patch("spectre.agent._ingest_to_kb") as mock_ingest, \
             patch("spectre.agent.fetch_logs", return_value=("logs", "Queue transaction window")), \
             patch("spectre.agent.triage", new=AsyncMock(return_value={"issue_type": "credentials", "triage_notes": ""})), \
             patch("spectre.agent.fetch_recent_failures", return_value=[]), \
             patch("spectre.agent.diagnose", new=AsyncMock(return_value={
                 "diagnosis": "SAP failed", "bot_name": "Bot", "confidence": "High",
                 "error_found": True, "recommended_action": "Fix creds"
             })):

            await investigate(InvestigateIn(
                transaction_id="TXN-007",
                description="SAP issue",
                team="Finance",
                process_name="ICSAUTO-3201 Invoice Bot",
                channel_id="C123",
                thread_ts="123.456"
            ))

        mock_ingest.assert_called_once()

    async def test_ingest_skipped_when_no_error(self):
        """When error_found is False, _ingest_to_kb should NOT be called."""
        with patch("spectre.agent.get_pat", return_value=("fake_pat", "https://example.com")), \
             patch("spectre.agent.get_llm_token", return_value=("fake_llm_token", "https://example.com")), \
             patch("spectre.agent.UiPath", return_value=MagicMock()), \
             patch("spectre.agent._search_kb", return_value=""), \
             patch("spectre.agent._ingest_to_kb") as mock_ingest, \
             patch("spectre.agent.fetch_logs", return_value=("logs", "Queue transaction window")), \
             patch("spectre.agent.triage", new=AsyncMock(return_value={"issue_type": "unknown", "triage_notes": ""})), \
             patch("spectre.agent.fetch_recent_failures", return_value=[]), \
             patch("spectre.agent.diagnose", new=AsyncMock(return_value={
                 "diagnosis": "No error found", "bot_name": "Bot", "confidence": "High",
                 "error_found": False, "recommended_action": "Monitor"
             })):

            await investigate(InvestigateIn(
                transaction_id="TXN-008",
                description="Seems fine",
                team="Finance",
                process_name="ICSAUTO-3201 Invoice Bot",
                channel_id="C123",
                thread_ts="123.456"
            ))

        mock_ingest.assert_not_called()

    async def test_cross_transaction_failures_passed_to_diagnose(self):
        """When other failures exist, cross_transaction_summary is passed to diagnose."""
        diagnosis_mock = AsyncMock(return_value={
            "diagnosis": "Systemic issue", "bot_name": "Bot", "confidence": "High",
            "error_found": True, "recommended_action": "Check infrastructure"
        })

        with patch("spectre.agent.get_pat", return_value=("fake_pat", "https://example.com")), \
             patch("spectre.agent.get_llm_token", return_value=("fake_llm_token", "https://example.com")), \
             patch("spectre.agent.UiPath", return_value=MagicMock()), \
             patch("spectre.agent._search_kb", return_value=""), \
             patch("spectre.agent._ingest_to_kb"), \
             patch("spectre.agent.fetch_logs", return_value=("logs", "Queue transaction window")), \
             patch("spectre.agent.triage", new=AsyncMock(return_value={"issue_type": "system_error", "triage_notes": "crash"})), \
             patch("spectre.agent.fetch_recent_failures", return_value=["TXN-100", "TXN-101", "TXN-102"]), \
             patch("spectre.agent.diagnose", new=diagnosis_mock):

            await investigate(InvestigateIn(
                transaction_id="TXN-004",
                description="System crash",
                team="Finance",
                process_name="ICSAUTO-3201 Invoice Bot",
                channel_id="C123",
                thread_ts="123.456"
            ))

        call_kwargs = diagnosis_mock.call_args.kwargs
        assert "TXN-100" in call_kwargs["cross_transaction_summary"]
