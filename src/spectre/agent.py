import asyncio
import json
from pydantic import BaseModel
from uipath.platform import UiPath
try:
    from .auth import get_pat, get_llm_token
    from .orchestrator import fetch_logs, fetch_recent_failures
    from .llm import triage, diagnose, diagnose_targeted
    from .logger import get_logger
except ImportError:
    from auth import get_pat, get_llm_token
    from orchestrator import fetch_logs, fetch_recent_failures
    from llm import triage, diagnose, diagnose_targeted
    from logger import get_logger

log = get_logger("spectre.agent")

_CG_INDEX_NAME = "SpectreKB"
_CG_FOLDER_PATH = "Shared/Specter"


class InvestigateIn(BaseModel):
    transaction_id: str
    description: str
    team: str
    process_name: str = ""
    channel_id: str
    thread_ts: str


class InvestigateOut(BaseModel):
    diagnosis: str
    bot_name: str
    confidence: str
    error_found: bool
    recommended_action: str


async def investigate(input: InvestigateIn) -> InvestigateOut:
    return await _run(input)


def _search_kb(sdk: UiPath, description: str, issue_type: str) -> str:
    """Search SpectreKB context grounding index for similar past incidents."""
    try:
        query = f"{issue_type}: {description}"
        results = sdk.context_grounding.search(
            name=_CG_INDEX_NAME,
            query=query,
            number_of_results=1,
            folder_path=_CG_FOLDER_PATH
        )
        if results:
            top = results[0]
            content = getattr(top, "text", None) or getattr(top, "content", None) or str(top)
            log.info(f"KB match found: {content[:100]}")
            return f"Similar past incident from knowledge base:\n{content}"
    except Exception as e:
        log.warning(f"KB search failed: {e}")
    return ""


def _ingest_to_kb(sdk: UiPath, llm_token: str, base_url: str, input: InvestigateIn, result: dict) -> None:
    """Upload investigation outcome to SpectreKB bucket and trigger ingestion."""
    try:
        record = {
            "transaction_id": input.transaction_id,
            "process_name": input.process_name,
            "issue_type": result.get("issue_type", "unknown"),
            "description": input.description,
            "diagnosis": result.get("diagnosis", ""),
            "recommended_action": result.get("recommended_action", ""),
            "bot_name": result.get("bot_name", ""),
            "confidence": result.get("confidence", ""),
        }
        file_name = f"{input.process_name.replace(' ', '_')}_{input.transaction_id}.json"
        sdk.buckets.upload(
            name="Spectre AI",
            blob_file_path=file_name,
            content=json.dumps(record, indent=2),
            content_type="application/json",
            folder_path=_CG_FOLDER_PATH
        )
        log.info(f"KB ingest: uploaded {file_name}")
        user_sdk = UiPath(base_url=base_url, secret=llm_token)
        user_sdk.context_grounding.ingest_by_name(name=_CG_INDEX_NAME, folder_path=_CG_FOLDER_PATH)
        log.info("KB ingest: ingestion triggered")
    except Exception as e:
        log.warning(f"KB ingest failed: {e}")


async def _run(input: InvestigateIn) -> InvestigateOut:
    log.info(f"Starting investigation — team={input.team} process={input.process_name} transaction={input.transaction_id}")

    pat, base_url = get_pat()
    llm_token, _ = get_llm_token()
    sdk = UiPath()

    # Step 1: Fetch logs
    log.info("Fetching Orchestrator logs...")
    logs, log_source = fetch_logs(pat, base_url, input.transaction_id, input.process_name)
    log.info(f"Logs fetched via '{log_source}' — {len(logs.splitlines())} lines")

    # Step 2: Triage — classify issue type
    log.info("Triaging issue type...")
    issue_type = "unknown"
    triage_notes = ""
    try:
        triage_result = await triage(llm_token, base_url, input.description, logs)
        issue_type = triage_result.get("issue_type", "unknown")
        triage_notes = triage_result.get("triage_notes", "")
        log.info(f"Triage complete — issue_type={issue_type} | {triage_notes}")
    except Exception as e:
        log.warning(f"Triage failed: {e}")

    # Step 3: Search knowledge base for similar past incidents
    log.info("Searching knowledge base for similar past incidents...")
    past_resolution = _search_kb(sdk, input.description, issue_type)
    if past_resolution:
        log.info("Past resolution found in KB")
    else:
        log.info("No past resolution found in KB")

    # Step 4: Cross-transaction analysis — check if other transactions also failed
    log.info("Checking for other recent failures in same process...")
    cross_transaction_summary = ""
    try:
        recent_failures = fetch_recent_failures(pat, base_url, input.process_name, input.transaction_id)
        if recent_failures:
            cross_transaction_summary = f"Found {len(recent_failures)} other recent failures in the same process: {', '.join(recent_failures[:5])}. This may be a systemic issue."
            log.info(f"Cross-transaction: {cross_transaction_summary}")
    except Exception as e:
        log.warning(f"Cross-transaction analysis failed: {e}")

    # Step 5: Primary LLM diagnosis
    log.info("Sending to LLM for diagnosis...")
    try:
        result = await diagnose(
            access_token=llm_token,
            base_url=base_url,
            team=input.team,
            process_name=input.process_name,
            transaction_id=input.transaction_id,
            description=input.description,
            logs=logs,
            log_source=log_source,
            issue_type=issue_type,
            past_resolution=past_resolution,
            cross_transaction_summary=cross_transaction_summary,
        )
    except Exception as e:
        log.error(f"LLM diagnosis failed: {e}")
        return InvestigateOut(
            diagnosis=f"LLM diagnosis failed: {str(e)}",
            bot_name="Unknown",
            confidence="Low",
            error_found=False,
            recommended_action="Contact support team"
        )

    # Step 6: Confidence loop — retry with targeted prompt if Low confidence
    if result.get("confidence") == "Low" and issue_type != "unknown":
        log.info(f"Confidence is Low — retrying with targeted prompt for issue_type={issue_type}")
        try:
            targeted_result = await diagnose_targeted(
                access_token=llm_token,
                base_url=base_url,
                team=input.team,
                transaction_id=input.transaction_id,
                description=input.description,
                logs=logs,
                process_name=input.process_name,
                log_source=log_source,
                issue_type=issue_type,
                first_diagnosis=result,
            )
            if targeted_result.get("confidence") in ("High", "Medium"):
                log.info(f"Targeted retry improved confidence to {targeted_result.get('confidence')}")
                result = targeted_result
            else:
                log.info("Targeted retry did not improve confidence, keeping original")
        except Exception as e:
            log.warning(f"Targeted retry failed: {e}")

    # Step 7: Ingest outcome to knowledge base for future reference
    if result.get("error_found"):
        log.info("Ingesting outcome to knowledge base...")
        result["issue_type"] = issue_type
        _ingest_to_kb(sdk, llm_token, base_url, input, result)

    out = InvestigateOut(
        diagnosis=result.get("diagnosis", "Unable to diagnose"),
        bot_name=result.get("bot_name", "Unknown"),
        confidence=result.get("confidence", "Low"),
        error_found=result.get("error_found", False),
        recommended_action=result.get("recommended_action", "")
    )
    log.info(f"Investigation complete — error_found={out.error_found} confidence={out.confidence} bot={out.bot_name}")
    return out


if __name__ == "__main__":
    result = asyncio.run(investigate(InvestigateIn(
        transaction_id="TXN-001",
        description="Bot is failing at the login step with a timeout error",
        team="Finance",
        process_name="ICSAUTO-3201 Invoice Processing",
        channel_id="C123",
        thread_ts="123456.789"
    )))
    print(result)
