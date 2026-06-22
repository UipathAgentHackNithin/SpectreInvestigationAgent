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

1. **Fetch logs** — 3-layer fallback strategy, all scoped to the resolved folder:
   - **Folder resolution** — numeric ID extracted from process name → keyword fallback → best-match across all folders; without a folder, Orchestrator calls have no scope, so resolution failure is treated as a hard blocker
   - Layer 1: Queue item by Reference — uses start/end timestamps for a precise log window
   - Layer 2: Transaction ID in log messages — bounded by JobKey + "Transaction Ended" marker (performer jobs only; dispatcher falls through)
   - Layer 3: SpecificContent parallel search — one thread per status (Failed / New / InProgress) fetches the 20 most-recent queue items each and filters SpecificContent in Python (OData does not support filtering on large data fields); returns status-aware messages for New/InProgress items
   - If the transaction is not found in any layer, the agent returns an early exit with a user-friendly message — no LLM call is made
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
  "recommended_action": "Rotate the SAP credential asset in Orchestrator and retry",
  "issue_type": "credentials"
}
```

| Field | Values | Description |
|---|---|---|
| `issue_type` | `credentials` / `timeout` / `business_exception` / `system_error` / `unknown` | Triage classification — use this in Maestro to route between CodingAgent and user escalation |

### Maestro routing logic

```
error_found=True  AND issue_type != "business_exception"  → SpectreCodingAgent (code fix)
error_found=True  AND issue_type == "business_exception"  → escalate to user (data/business rule issue)
error_found=False                                          → escalate to user (no error found)
```

Business exceptions (data validation failures, missing data, business rule violations) cannot be fixed by patching XAML — they require end-user action on the data. CodingAgent should not be invoked for these.

## Local Setup

```bash
uv sync
cp .env.example .env   # fill in UIPATH_PAT and UIPATH_URL
uv run uipath run main '{"transaction_id": "INV-001", "description": "...", "team": "Finance", "process_name": "ICSAUTO-3201 Invoice Processing Performer", "channel_id": "C123", "thread_ts": "123.456"}'
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `UIPATH_PAT` | Yes (local) | Personal Access Token for Orchestrator API calls — loaded from `SPECTRE_PAT` credential asset on robot |
| `UIPATH_REFRESH_TOKEN` | Yes (local) | Refresh token for LLM Gateway — loaded from `SPECTRE_REFRESH_TOKEN` credential asset on robot |
| `UIPATH_URL` | No | Orchestrator base URL (defaults to staging tenant) |
| `SPECTRE_FOLDER_ID` | No | Orchestrator folder ID for Spectre assets (defaults to `3087542`) — override when deploying to a different tenant |

> On robot runtime, `UIPATH_PAT` and `UIPATH_REFRESH_TOKEN` are **not** set as env vars — they are read at startup from Orchestrator credential assets via `sdk.assets.retrieve_credential()`. `UIPATH_ACCESS_TOKEN` is no longer used.

## Orchestrator Assets

| Asset | Type | Folder | Description |
|---|---|---|---|
| `SPECTRE_PAT` | Credential | `Shared/Specter` | Personal Access Token for Orchestrator API calls — read at agent startup |
| `SPECTRE_REFRESH_TOKEN` | Credential | `Shared/Specter` | Refresh token exchanged for an LLM-scoped JWT — read and written back at runtime |
| `SPECTRE_SUPPORT_HANDLE` | Text | `Shared/Specter` | Slack user group tag for support contact (e.g. `<!subteam^S0BBTE9DA0N>`) — rendered as `@rpa-support` in failure messages |

> All credential assets must have **AllowDirectApiAccess** enabled in Orchestrator UI.

### Refreshing the LLM token

The agent **self-rotates** the refresh token on every run — the rotated token is automatically written back to the `SPECTRE_REFRESH_TOKEN` Orchestrator asset via PAT, so no manual refresh is needed for normal operation.

Run `refresh_token.ps1` from the project root only after publishing a new version to Orchestrator (publishing invalidates the current token):
1. Forces a fresh `uipath auth` login to obtain a new refresh token
2. Updates `.env` locally with the new tokens (no BOM)
3. Looks up the `SPECTRE_REFRESH_TOKEN` asset ID dynamically and writes the new token back as a Credential asset

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
  auth.py           — token management (reads UIPATH_PAT and UIPATH_REFRESH_TOKEN from env; self-rotates refresh token)
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
