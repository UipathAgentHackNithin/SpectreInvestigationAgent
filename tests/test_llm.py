import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from spectre.llm import diagnose, triage, diagnose_targeted


def _make_mock_service(content: str):
    mock_message = MagicMock()
    mock_message.content = content
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_service = MagicMock()
    mock_service.chat_completions = AsyncMock(return_value=mock_response)
    return mock_service


class TestDiagnose:

    async def test_returns_structured_result(self):
        mock_service = _make_mock_service('{"diagnosis": "Timeout at login", "bot_name": "FinanceBot", "confidence": "High", "error_found": true, "recommended_action": "Reset credentials"}')
        with patch("spectre.llm.UiPathOpenAIService", return_value=mock_service), \
             patch("spectre.llm.UiPathApiConfig"), patch("spectre.llm.UiPathExecutionContext"):
            result = await diagnose(
                access_token="fake", base_url="https://example.com",
                team="Finance", transaction_id="TXN-001",
                description="Bot failing at login", logs="[Error] Login timeout"
            )
        assert result["diagnosis"] == "Timeout at login"
        assert result["bot_name"] == "FinanceBot"
        assert result["error_found"] is True

    async def test_fallback_source_injects_low_confidence_note(self):
        mock_service = _make_mock_service('{"diagnosis": "Unknown", "bot_name": "Bot", "confidence": "Low", "error_found": false, "recommended_action": "Monitor"}')
        with patch("spectre.llm.UiPathOpenAIService", return_value=mock_service), \
             patch("spectre.llm.UiPathApiConfig"), patch("spectre.llm.UiPathExecutionContext"):
            await diagnose(
                access_token="fake", base_url="https://example.com",
                team="Finance", transaction_id="TXN-001", description="desc", logs="some logs",
                log_source="Today's error logs fallback (broad - not scoped to this transaction)"
            )
        call_args = mock_service.chat_completions.call_args
        messages = call_args[0][0]
        user_prompt = next(m["content"] for m in messages if m["role"] == "user")
        assert "NOTE" in user_prompt
        assert "Low" in user_prompt

    async def test_non_fallback_source_has_no_note(self):
        mock_service = _make_mock_service('{"diagnosis": "T", "bot_name": "B", "confidence": "High", "error_found": true, "recommended_action": "None"}')
        with patch("spectre.llm.UiPathOpenAIService", return_value=mock_service), \
             patch("spectre.llm.UiPathApiConfig"), patch("spectre.llm.UiPathExecutionContext"):
            await diagnose(
                access_token="fake", base_url="https://example.com",
                team="Finance", transaction_id="TXN-001", description="desc", logs="some logs",
                log_source="Queue transaction window (precise timestamps from queue item)"
            )
        call_args = mock_service.chat_completions.call_args
        messages = call_args[0][0]
        user_prompt = next(m["content"] for m in messages if m["role"] == "user")
        assert "NOTE" not in user_prompt

    async def test_strips_markdown_fences(self):
        mock_service = _make_mock_service('```json\n{"diagnosis": "Test", "bot_name": "Bot", "confidence": "Low", "error_found": false, "recommended_action": "Monitor"}\n```')
        with patch("spectre.llm.UiPathOpenAIService", return_value=mock_service), \
             patch("spectre.llm.UiPathApiConfig"), patch("spectre.llm.UiPathExecutionContext"):
            result = await diagnose(access_token="fake", base_url="https://example.com", team="Team", transaction_id="TXN-001", description="desc", logs="logs")
        assert result["diagnosis"] == "Test"


class TestParse:

    async def test_raises_on_malformed_json(self):
        mock_service = _make_mock_service("not valid json at all {")
        with patch("spectre.llm.UiPathOpenAIService", return_value=mock_service), \
             patch("spectre.llm.UiPathApiConfig"), patch("spectre.llm.UiPathExecutionContext"):
            with pytest.raises(ValueError, match="LLM returned invalid JSON"):
                await diagnose(access_token="fake", base_url="https://example.com", team="T", transaction_id="TXN-001", description="d", logs="l")

    async def test_raises_on_missing_required_keys(self):
        mock_service = _make_mock_service('{"diagnosis": "x", "bot_name": "y"}')
        with patch("spectre.llm.UiPathOpenAIService", return_value=mock_service), \
             patch("spectre.llm.UiPathApiConfig"), patch("spectre.llm.UiPathExecutionContext"):
            with pytest.raises(ValueError, match="missing required keys"):
                await diagnose(access_token="fake", base_url="https://example.com", team="T", transaction_id="TXN-001", description="d", logs="l")

    async def test_raises_on_non_object_response(self):
        mock_service = _make_mock_service('["a", "b"]')
        with patch("spectre.llm.UiPathOpenAIService", return_value=mock_service), \
             patch("spectre.llm.UiPathApiConfig"), patch("spectre.llm.UiPathExecutionContext"):
            with pytest.raises(ValueError, match="not a JSON object"):
                await diagnose(access_token="fake", base_url="https://example.com", team="T", transaction_id="TXN-001", description="d", logs="l")


class TestTriage:

    async def test_returns_issue_type_and_notes(self):
        mock_service = _make_mock_service('{"issue_type": "credentials", "triage_notes": "Login failure detected"}')
        with patch("spectre.llm.UiPathOpenAIService", return_value=mock_service), \
             patch("spectre.llm.UiPathApiConfig"), patch("spectre.llm.UiPathExecutionContext"):
            result = await triage(
                access_token="fake", base_url="https://example.com",
                description="SAP login failed", logs="[Error] AuthenticationException"
            )
        assert result["issue_type"] == "credentials"
        assert result["triage_notes"] == "Login failure detected"

    async def test_strips_markdown_fences(self):
        mock_service = _make_mock_service('```json\n{"issue_type": "timeout", "triage_notes": "Network timeout"}\n```')
        with patch("spectre.llm.UiPathOpenAIService", return_value=mock_service), \
             patch("spectre.llm.UiPathApiConfig"), patch("spectre.llm.UiPathExecutionContext"):
            result = await triage(
                access_token="fake", base_url="https://example.com",
                description="Bot timed out", logs="[Error] TimeoutException"
            )
        assert result["issue_type"] == "timeout"


class TestDiagnoseTargeted:

    async def test_includes_issue_type_in_prompt(self):
        mock_service = _make_mock_service('{"diagnosis": "SAP creds expired", "bot_name": "FinanceBot", "confidence": "Medium", "error_found": true, "recommended_action": "Rotate SAP password"}')
        with patch("spectre.llm.UiPathOpenAIService", return_value=mock_service), \
             patch("spectre.llm.UiPathApiConfig"), patch("spectre.llm.UiPathExecutionContext"):
            result = await diagnose_targeted(
                access_token="fake", base_url="https://example.com",
                team="Finance", transaction_id="TXN-001",
                description="SAP login failing", logs="[Error] AuthenticationException",
                process_name="ICSAUTO-3201", log_source="Queue transaction window",
                issue_type="credentials",
                first_diagnosis={"diagnosis": "Unclear", "confidence": "Low", "error_found": False}
            )
        assert result["confidence"] == "Medium"
        # Verify credentials-specific instruction was in the prompt
        call_args = mock_service.chat_completions.call_args
        messages = call_args[0][0]
        user_prompt = next(m["content"] for m in messages if m["role"] == "user")
        assert "credential" in user_prompt.lower() or "login" in user_prompt.lower()

    async def test_returns_structured_result(self):
        mock_service = _make_mock_service('{"diagnosis": "Null ref", "bot_name": "Bot", "confidence": "High", "error_found": true, "recommended_action": "Fix null check"}')
        with patch("spectre.llm.UiPathOpenAIService", return_value=mock_service), \
             patch("spectre.llm.UiPathApiConfig"), patch("spectre.llm.UiPathExecutionContext"):
            result = await diagnose_targeted(
                access_token="fake", base_url="https://example.com",
                team="Ops", transaction_id="TXN-002",
                description="Crash", logs="[Error] NullReferenceException",
                process_name="ICSAUTO-3202", log_source="Queue transaction window",
                issue_type="system_error",
                first_diagnosis={"diagnosis": "Unknown", "confidence": "Low"}
            )
        assert result["error_found"] is True
        assert result["recommended_action"] == "Fix null check"

    async def test_critical_rule_present_in_prompt(self):
        """Regression guard: diagnose_targeted must forbid hallucination via CRITICAL RULE."""
        mock_service = _make_mock_service('{"diagnosis": "No error found", "bot_name": "Bot", "confidence": "Low", "error_found": false, "recommended_action": "Monitor"}')
        with patch("spectre.llm.UiPathOpenAIService", return_value=mock_service), \
             patch("spectre.llm.UiPathApiConfig"), patch("spectre.llm.UiPathExecutionContext"):
            await diagnose_targeted(
                access_token="fake", base_url="https://example.com",
                team="Finance", transaction_id="TXN-003",
                description="Possible login issue", logs="Bot started. Transaction processed. Bot ended.",
                process_name="ICSAUTO-3201", log_source="Queue transaction window",
                issue_type="credentials",
                first_diagnosis={"diagnosis": "Unclear", "confidence": "Low", "error_found": False}
            )
        call_args = mock_service.chat_completions.call_args
        messages = call_args[0][0]
        user_prompt = next(m["content"] for m in messages if m["role"] == "user")
        assert "CRITICAL RULE" in user_prompt
        assert "FORBIDDEN" in user_prompt
        assert "error_found to false" in user_prompt

    async def test_no_upgrade_instruction_without_log_evidence(self):
        """Regression guard: prompt must NOT instruct LLM to upgrade confidence on 'ANY relevant signals'."""
        mock_service = _make_mock_service('{"diagnosis": "x", "bot_name": "x", "confidence": "Low", "error_found": false, "recommended_action": "x"}')
        with patch("spectre.llm.UiPathOpenAIService", return_value=mock_service), \
             patch("spectre.llm.UiPathApiConfig"), patch("spectre.llm.UiPathExecutionContext"):
            await diagnose_targeted(
                access_token="fake", base_url="https://example.com",
                team="Finance", transaction_id="TXN-004",
                description="Possible timeout", logs="Bot started. Bot ended.",
                process_name="ICSAUTO-3201", log_source="Queue transaction window",
                issue_type="timeout",
                first_diagnosis={"diagnosis": "Unclear", "confidence": "Low", "error_found": False}
            )
        call_args = mock_service.chat_completions.call_args
        messages = call_args[0][0]
        user_prompt = next(m["content"] for m in messages if m["role"] == "user")
        assert "ANY relevant signals" not in user_prompt
