# SpectreInvestigationAgent

**SpectreInvestigationAgent** is a Python-based UiPath coded agent and the *diagnostic engine* of the **SpectreAI** autonomous RPA bot self-healing system. When a UiPath bot fails, this agent automatically retrieves the failure logs from Orchestrator, triages the issue type, searches a knowledge base for past resolutions, and returns a structured diagnosis with confidence scoring ready for the downstream coding agent to act on.

## Role in the SpectreAI System

SpectreAI is a two-agent pipeline for autonomous RPA bot repair:

| Agent | Responsibility |
|---|---|
| **SpectreInvestigationAgent** *(this repo)* | Pulls Orchestrator logs, triages issue type, diagnoses root cause, produces a structured fix recommendation |
| **SpectreCodingAgent** | Consumes the diagnosis, fetches XAML source from GitHub, applies the LLM-generated patch, opens a draft PR |

```
Bot Failure (Orchestrator Job)
    |
    v
SpectreInvestigationAgent
    +-- Orchestrator API --> Job Logs & Exception Details
    +-- Triage LLM      --> Issue Type Classification
    +-- KB Search       --> Past Resolution Lookup
    +-- Diagnose LLM    --> Root Cause & Recommendation
    +-- Output          --> Structured Diagnosis JSON
                                    |
                                    v
                        SpectreCodingAgent (patch & PR)
```

## How It Works

1. **Fetch logs** — 3-layer fallback strategy:
   - Layer 1: Queue item timestamps — finds the transaction in its folder, uses start/end timestamps for a precise log window
   - Layer 2: Transaction ID in log messages — searches logs for the reference, bounded by the job window if a Performer logged it
   - Layer 3: All-folders parallel search — when the folder can't be identified from the process name, all Orchestrator folders are searched concurrently for the transaction ID
   - If the transaction is not found in any folder, the agent returns an early exit with a user-friendly message — no LLM call is made
2. **Triage** — LLM classifies issue type: `credentials`, `timeout`, `business_exception`, `system_error`, or `unknown`
3. **KB search** — searches SpectreKB context grounding index for similar past incidents
4. **Cross-transaction analysis** — checks if other transactions in the same process also failed (systemic issue detection)
5. **Diagnose** — LLM produces structured diagnosis grounded strictly in log evidence
6. **Confidence retry** — if confidence is Low and issue type is known, a second targeted LLM call re-examines the logs
7. **KB ingest** — successful diagnoses are uploaded to SpectreKB for future reference

### Non-happy path handling

| Scenario | Behaviour |
|---|---|
| Orchestrator auth fails | Returns user-facing message + `@rpa-support` tag, no crash |
| LLM token unavailable | Returns user-facing message + `@rpa-support` tag, no crash |
| Transaction not found in any folder | Early exit with clear message, no LLM call |
| Queue item status `New`/`Retried` (not yet processed) | Early exit — "check back once the bot picks it up", no LLM call |
| Logs fetched but empty | LLM is still called but confidence is forced to `Low` |
| LLM returns lowercase confidence (`"high"`) | Normalised to `"High"` before any comparisons |

## Output

```json
{
  "diagnosis": "AuthenticationException at SAP login step — credential asset may have expired",
  "bot_name": "InvoiceProcessing_Performer",
  "confidence": "High",
  "error_found": true,
  "recommended_action": "Rotate the SAP credential asset in Orchestrator and retry"
}
```

## Local Setup

```bash
uv sync
cp .env.example .env   # fill in UIPATH_PAT and UIPATH_URL
uv run uipath run main '{"transaction_id": "INV-001", "description": "...", "team": "Finance", "process_name": "ICSAUTO-3201 Invoice Processing Performer", "channel_id": "C123", "thread_ts": "123.456"}'
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `UIPATH_PAT` | Yes (local) | Personal Access Token for Orchestrator API calls |
| `UIPATH_URL` | No | Orchestrator base URL (defaults to staging tenant) |
| `UIPATH_ACCESS_TOKEN` | Yes (robot) | Injected by UiPath at runtime |
| `SPECTRE_FOLDER_ID` | No | Orchestrator folder ID for Spectre assets (defaults to `3087542`) — override when deploying to a different tenant |

## Orchestrator Assets

| Asset | Folder | Description |
|---|---|---|
| `SpectrePAT` | `Shared/Specter` | Personal Access Token used for Orchestrator API calls on robot |
| `SpectreRefreshToken` | `Shared/Specter` | Refresh token exchanged for an LLM-scoped JWT |
| `SPECTRE_SUPPORT_HANDLE` | `Shared/Specter` | Slack user group tag for support contact (e.g. `<!subteam^S0BBTE9DA0N>`) — rendered as `@rpa-support` in failure messages |

## Running Tests

```bash
uv run pytest tests/ -v
```

## Running Evaluations

```bash
uv run uipath eval main evaluations/eval-sets/spectre-investigation.json --output-file eval-results.json
```

## Project Structure

```
src/spectre/
  agent.py          — main orchestration (7-step pipeline)
  orchestrator.py   — Orchestrator API calls with 3-layer log fetch and retry
  llm.py            — LLM calls (triage, diagnose, diagnose_targeted) with response validation
  auth.py           — token management (local .auth.json + robot asset fallback)
  logger.py         — structured logging

evaluations/
  eval-sets/        — evaluation test cases
  evaluators/       — evaluator configs

tests/
  test_agent.py
  test_auth.py
  test_llm.py
  test_orchestrator.py
```

## Related Repositories

- [SpectreCodingAgent](../SpectreCodingAgent) - downstream patch & PR agent
