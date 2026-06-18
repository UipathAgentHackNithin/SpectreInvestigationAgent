import json
from uipath.platform.common._config import UiPathApiConfig
from uipath.platform.common._execution_context import UiPathExecutionContext
from uipath.platform.chat._llm_gateway_service import UiPathOpenAIService, ChatModels

_TRIAGE_REQUIRED = {"issue_type", "triage_notes"}
_DIAGNOSE_REQUIRED = {"diagnosis", "bot_name", "confidence", "error_found", "recommended_action"}


def _make_service(access_token: str, base_url: str) -> UiPathOpenAIService:
    execution_context = UiPathExecutionContext()
    config = UiPathApiConfig(base_url=base_url, secret=access_token, execution_context=execution_context)
    return UiPathOpenAIService(config=config, execution_context=execution_context)


def _parse(raw: str, required_keys: set | None = None) -> dict:
    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM returned invalid JSON: {e}\nRaw response: {raw[:500]}") from e
    if not isinstance(data, dict):
        raise ValueError(f"LLM response is not a JSON object: {type(data)}")
    if required_keys:
        missing = required_keys - data.keys()
        if missing:
            raise ValueError(f"LLM response missing required keys {missing}. Got: {list(data.keys())}")
    return data


async def triage(access_token: str, base_url: str, description: str, logs: str) -> dict:
    """Classify the issue type before deep investigation."""
    service = _make_service(access_token, base_url)
    prompt = f"""Classify this RPA bot incident into one of these categories:
- credentials: login failures, authentication errors, password/credential issues
- timeout: network timeouts, application timeouts, wait failures
- business_exception: data validation errors, business rule violations, missing data
- system_error: application crashes, unexpected exceptions, infrastructure issues
- unknown: cannot determine from available information

Issue Description: {description}

Log excerpt:
{logs[:2000]}

Respond ONLY in valid JSON with:
- issue_type (string: one of credentials/timeout/business_exception/system_error/unknown)
- triage_notes (string: one sentence explaining the classification)"""

    messages = [
        {"role": "system", "content": "You are an RPA triage specialist. Always respond with valid JSON only."},
        {"role": "user", "content": prompt},
    ]
    response = await service.chat_completions(messages, model=ChatModels.gpt_4_1_mini_2025_04_14, max_tokens=256, temperature=0)
    raw = response.choices[0].message.content
    return _parse(raw, _TRIAGE_REQUIRED)


async def diagnose(access_token: str, base_url: str, team: str, transaction_id: str, description: str,
                   logs: str, process_name: str = "", log_source: str = "Orchestrator logs",
                   issue_type: str = "unknown", past_resolution: str = "",
                   cross_transaction_summary: str = "") -> dict:
    """Send ticket details + logs to LLM and return structured diagnosis."""
    service = _make_service(access_token, base_url)

    fallback_note = (
        "\nNOTE: These logs are a broad fallback and may not be scoped to this specific transaction. "
        "Set confidence to Low unless the evidence directly matches the reported issue."
        if "fallback" in log_source.lower() else ""
    )

    past_resolution_section = (
        f"\n--- Similar Past Incident ---\n{past_resolution}\n"
        if past_resolution else ""
    )

    cross_tx_section = (
        f"\n--- Cross-Transaction Analysis ---\n{cross_transaction_summary}\n"
        if cross_transaction_summary else ""
    )

    issue_type_hint = (
        f"\nPre-triage classification: {issue_type} — focus your analysis on this category first."
        if issue_type != "unknown" else ""
    )

    prompt = f"""You are a senior RPA support engineer analysing a bot incident. Be thorough and specific.

CRITICAL RULE: Base your diagnosis ONLY on what is explicitly visible in the logs. Do NOT infer, guess, or speculate about errors that are not directly evidenced in the log entries. If the logs do not show a clear error, say so plainly and set error_found to false. "Likely", "may indicate", "absence suggests", "probably" are FORBIDDEN — only state what the logs actually show.

--- Ticket Details ---
Team: {team}
Process: {process_name}
Transaction ID: {transaction_id}
Issue Description: {description}{issue_type_hint}

--- Orchestrator Logs ({log_source}) ---{fallback_note}
{logs}{past_resolution_section}{cross_tx_section}

--- Instructions ---
1. Scan the logs carefully for: exceptions, stack traces, error messages, warning signs, unexpected status changes, timeouts, or business rule violations.
2. If an error IS found in the logs: identify the exact error message, the step/activity where it occurred, likely root cause, and a recommended fix or next action.
3. If NO error is found in logs: explicitly state that no error was found in the available logs. Do NOT infer or guess an error from the issue description, past resolutions, or cross-transaction data. Set error_found to false.
4. Cross-reference the user's issue description with what the logs show — highlight any mismatch.
5. If a past resolution is provided, use it ONLY to supplement a diagnosis already supported by the logs — do NOT use it to invent a diagnosis when logs show nothing.
6. If cross-transaction data shows multiple failures, note this as context only — do NOT use it as evidence of an error if the logs for this transaction show nothing.
7. Set confidence based on evidence quality:
   - High = clear error found in logs, OR logs clearly show successful completion with no errors
   - Medium = partial evidence, ambiguous logs, or error implied but not explicit
   - Low = fallback logs not scoped to this transaction, or logs are empty/missing

Respond ONLY in valid JSON with these exact keys:
- diagnosis (string: detailed root cause analysis — be specific, reference actual log messages where possible)
- bot_name (string: process/bot name from logs, or "Unknown")
- confidence (string: one of High / Medium / Low)
- error_found (boolean: true if a clear error is visible in logs, false otherwise)
- recommended_action (string: concrete next step for the support team)"""

    messages = [
        {"role": "system", "content": "You are an RPA support specialist. Always respond with valid JSON only."},
        {"role": "user", "content": prompt},
    ]
    response = await service.chat_completions(messages, model=ChatModels.gpt_4_1_mini_2025_04_14, max_tokens=1024, temperature=0.2)
    raw = response.choices[0].message.content
    return _parse(raw, _DIAGNOSE_REQUIRED)


async def diagnose_targeted(access_token: str, base_url: str, team: str, transaction_id: str,
                             description: str, logs: str, process_name: str,
                             log_source: str, issue_type: str, first_diagnosis: dict) -> dict:
    """Second-pass diagnosis when confidence is Low — more targeted prompt."""
    service = _make_service(access_token, base_url)

    prompt = f"""You are a senior RPA support engineer. A previous analysis of this incident returned LOW confidence.
Re-examine the logs more carefully with a targeted focus on the issue type: {issue_type}

CRITICAL RULE: Base your diagnosis ONLY on what is explicitly visible in the logs. Do NOT infer, guess, or speculate about errors that are not directly evidenced in the log entries. If the logs do not show a clear error, say so plainly and set error_found to false. "Likely", "may indicate", "absence suggests", "probably" are FORBIDDEN — only state what the logs actually show.

--- Ticket Details ---
Team: {team}
Process: {process_name}
Transaction ID: {transaction_id}
Issue Description: {description}

--- Previous Analysis (Low Confidence) ---
{json.dumps(first_diagnosis, indent=2)}

--- Orchestrator Logs ({log_source}) ---
{logs}

--- Targeted Instructions for issue type '{issue_type}' ---
{'Scan logs ONLY for: AuthenticationException, login-related error messages, credential asset access failures, explicit login step failures.' if issue_type == 'credentials' else ''}
{'Scan logs ONLY for: TimeoutException, WaitForReady failures, element not found within timeout, explicit timeout error messages.' if issue_type == 'timeout' else ''}
{'Scan logs ONLY for: BusinessRuleException, explicit validation failure messages, data format error messages.' if issue_type == 'business_exception' else ''}
{'Scan logs ONLY for: System.Exception, NullReferenceException, application crash messages, explicit infrastructure error messages.' if issue_type == 'system_error' else ''}

If you find explicit log evidence of such an error: describe it precisely and upgrade confidence to Medium.
If the logs do NOT contain explicit evidence of the above: keep error_found as false, keep confidence as Low, and state clearly that the logs contain no evidence of a {issue_type} error.
Do NOT upgrade confidence based on the issue description, KB past resolutions, or cross-transaction data alone.

Respond ONLY in valid JSON with keys: diagnosis, bot_name, confidence, error_found, recommended_action"""

    messages = [
        {"role": "system", "content": "You are an RPA support specialist. Always respond with valid JSON only."},
        {"role": "user", "content": prompt},
    ]
    response = await service.chat_completions(messages, model=ChatModels.gpt_4_1_mini_2025_04_14, max_tokens=1024, temperature=0.1)
    raw = response.choices[0].message.content
    return _parse(raw, _DIAGNOSE_REQUIRED)
