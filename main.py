import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from spectre.agent import investigate, InvestigateIn, InvestigateOut

__all__ = ["investigate", "InvestigateIn", "InvestigateOut"]

if __name__ == "__main__":
    import asyncio
    result = asyncio.run(investigate(InvestigateIn(
        transaction_id="TXN-001",
        description="Bot is failing at the login step with a timeout error",
        team="Finance",
        process_name="ICSAUTO-3201 Invoice Performer",
        channel_id="C123",
        thread_ts="123456.789"
    )))
    print(result)
