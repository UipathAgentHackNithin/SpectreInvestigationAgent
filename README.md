# SpectreInvestigationAgent

Python Coded Agent for SpectreAI — the AI-powered RPA support automation system built for UiPath AgentHack 2026.

SpectreInvestigationAgent diagnoses UiPath bot failures by fetching 3 layers of Orchestrator logs, querying SpectreKB, and producing a confidence-scored root cause analysis in seconds.

---

## Project Description

SpectreInvestigationAgent is one of three components in the SpectreAI multi-agent system. When a bot failure is reported via Slack, the Maestro orchestration layer triggers this agent to investigate.

The agent:
1. Fetches **job logs** from Orchestrator — what the process reported
2. Fetches **bot-reported exception logs** — what the bot explicitly flagged as errors
3. Fetches **transaction item logs** — what happened at the queue item level
4. Merges all three into a single context window
5. Queries **SpectreKB** — a knowledge base of past diagnoses — to inject relevant patterns
6. Calls the reasoning engine (GPT-4.1 mini) with a structured prompt
7. Returns a structured diagnosis: root cause, confidence score, affected file, recommended fix

If the confidence score is high, Maestro routes to SpectreCodingAgent for automated patching. If low, the diagnosis is posted to Slack with Escalate/Dismiss buttons for human review.

---

## UiPath Components Used

| Component | Usage |
|---|---|
| **UiPath Coded Agents (Python)** | This agent is implemented as a Python Coded Agent |
| **UiPath Orchestrator API** | Fetches job logs, business exception logs, and transaction item logs |
| **UiPath Integration Service — Slack** | Posts diagnosis updates and interactive buttons to Slack thread |
| **UiPath Maestro** | Triggers this agent and receives structured output for routing decisions |

---

## Agent Type

**Coded Agent (Python)**

This is a fully coded agent written in Python. It uses:
- `uipath` Python SDK for Orchestrator API access
- `openai` client for GPT-4.1 mini reasoning engine calls
- `slack_sdk` for Slack thread updates
- Custom `SpectreKB` class for knowledge base lookup

No low-code components — entirely Python.

---

## Repository Structure

```
SpectreInvestigationAgent/
├── src/
│   └── spectre_investigation/
│       ├── agent.py          # Main agent orchestration logic
│       ├── log_fetcher.py    # 3-layer Orchestrator log fetch
│       ├── spectrekb.py      # SpectreKB knowledge base lookup
│       ├── llm.py            # Reasoning engine (GPT-4.1 mini) calls
│       ├── slack_client.py   # Slack notification handler
│       └── auth.py           # Orchestrator authentication
├── tests/                    # Unit tests
├── uipath.json               # UiPath Coded Agent configuration
├── requirements.txt          # Python dependencies
└── README.md
```

---

## Setup Instructions

### Prerequisites

- Python 3.11+
- UiPath Orchestrator (cloud) account
- Slack Bot Token with `chat:write`, `channels:read` permissions
- OpenAI API key (GPT-4.1 mini)

### Step 1 — Clone the repository

```bash
git clone https://github.com/UipathAgentHackNithin/SpectreInvestigationAgent.git
cd SpectreInvestigationAgent
```

### Step 2 — Install dependencies

```bash
pip install -r requirements.txt
```

### Step 3 — Configure Orchestrator assets

Create the following assets in UiPath Orchestrator:

| Asset Name | Purpose |
|---|---|
| `ORCHESTRATOR_BASE_URL` | Your Orchestrator cloud URL |
| `UIPATH_ACCESS_TOKEN` | Orchestrator API access token |
| `SLACK_BOT_TOKEN` | Slack Bot OAuth token |
| `SLACK_SUPPORT_CHANNEL_ID` | Slack support channel ID |
| `JIRA_PROJECT_KEY` | Jira project key for ticket creation |

### Step 4 — Deploy as a Function process in Orchestrator

1. Open UiPath Studio
2. Open this project
3. Publish to Orchestrator as a **Function** process type
4. The process will be triggered by the Maestro orchestration layer

### Step 5 — Test

Trigger the Maestro process via the Slack shortcut with a Bug submission. Verify:
- Agent job starts in Orchestrator
- Logs are fetched (check job output)
- Diagnosis appears in the Slack thread

---

## Related Repositories

| Repository | Description |
|---|---|
| [SpectreAI-Maestro](https://github.com/UipathAgentHackNithin/SpectreAI-Maestro) | Maestro orchestration layer — triggers this agent |
| [SpectreCodingAgent](https://github.com/UipathAgentHackNithin/SpectreCodingAgent) | Python Coded Agent — XAML patching and Draft PR creation |
| [InvoiceProcessing-Performer](https://github.com/UipathAgentHackNithin/InvoiceProcessing-Performer) | Sample target bot used in demo |

---

## Demo

- **Demo Video:** https://www.youtube.com/watch?v=d64LqEl6M5Y
- **Devpost:** https://devpost.com/software/zeroday
- **UiPath Forum:** https://forum.uipath.com/t/spectreai-from-bots-down-in-slack-to-a-draft-pr-in-under-2-minutes-agenthack-2026/5755787

---

## Author

Nithin BR — Agentic Architect @ Persistent Systems
UiPath AgentHack 2026
