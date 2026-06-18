# SpectreInvestigationAgent

## Overview

**SpectreInvestigationAgent** is a Python-based UiPath coded agent and the *diagnostic engine* of the **SpectreAI** autonomous RPA bot self-healing system. When a UiPath bot fails, this agent automatically retrieves the failure logs from Orchestrator, analyses the exception trace, identifies the root cause using an LLM, and emits a structured diagnosis ready for the downstream coding agent to act on.

---

## Role in the SpectreAI System

SpectreAI is a two-agent pipeline for autonomous RPA bot repair:

| Agent | Responsibility |
|---|---|
| **SpectreInvestigationAgent** *(this repo)* | Pulls Orchestrator logs, analyses exceptions, diagnoses root cause, produces a structured fix recommendation |
| **SpectreCodingAgent** | Consumes the diagnosis, fetches XAML source from GitHub, applies the LLM-generated patch, opens a draft PR |

```
Bot Failure (Orchestrator Job)
    |
    v
SpectreInvestigationAgent
    +-- Orchestrator API --> Job Logs & Exception Details
    +-- LLM Analysis    --> Root Cause Classification
    +-- Output          --> Structured Diagnosis JSON
                                    |
                                    v
                        SpectreCodingAgent (patch & PR)
```

---

## Architecture

1. **Trigger** - Invoked when an Orchestrator job enters a *Faulted* state (webhook, schedule, or manual trigger).
2. **Log Retrieval** - Calls the UiPath Orchestrator API to pull job logs and the full exception trace for the failed run.
3. **Exception Analysis** - Parses the stack trace to extract the failing activity, exception type, and contextual data.
4. **LLM Diagnosis** - Sends the exception context to an LLM to classify the root cause and recommend a concrete fix action.
5. **Structured Output** - Returns a JSON diagnosis document consumed by `SpectreCodingAgent`.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Runtime | Python 3.x |
| Agent Framework | UiPath Coded Workflows / Agent SDK |
| Orchestrator Integration | UiPath Orchestrator REST API |
| LLM Integration | UiPath AI / LLM connector |
| Orchestration | UiPath Orchestrator |

---

## Getting Started

### Prerequisites

- Python 3.9+
- UiPath Studio / Robot with Coded Workflows support
- UiPath Orchestrator API credentials (Client ID + Secret or PAT)
- LLM endpoint configured in UiPath AI Gateway

### Configuration

Set the following in UiPath Orchestrator Assets or environment variables:

| Asset | Description |
|---|---|
| `ORCHESTRATOR_URL` | Base URL of your UiPath Orchestrator instance |
| `ORCHESTRATOR_CLIENT_ID` | OAuth2 client ID for Orchestrator API access |
| `ORCHESTRATOR_CLIENT_SECRET` | OAuth2 client secret |
| `LLM_ENDPOINT` | UiPath AI Gateway or Azure OpenAI endpoint |

---

## Related Repositories

- [SpectreCodingAgent](../SpectreCodingAgent) - downstream patch & PR agent
