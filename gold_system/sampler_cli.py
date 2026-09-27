"""CLI 組裝層；只有明確的 Demo 執行指令才讀 vault。

[Author: Antigravity | Date: 2026-09-27]
"""
from datetime import datetime, timezone
from pathlib import Path

from .sampler_plan import Archive, SampleError, account_lease, diagnostic_lease, digest, read_plan, utc


async def run_demo(plan_file, expected_hash, output_name, *, confirmed, exclusive_account):
    if not confirmed or not exclusive_account:
        raise SampleError("EXPLICIT_DEMO_AND_EXCLUSIVITY_REQUIRED")
    plan = read_plan(plan_file)
    if digest(plan) != expected_hash:
        raise SampleError("PLAN_HASH_MISMATCH")
    now = datetime.now(timezone.utc)
    if not utc(plan["start"]) <= now < utc(plan["queries"][0]["at"]):
        raise SampleError("PLAN_NOT_CURRENT")
    root = Path(__file__).resolve().parent.parent / "runtime"
    archive = Archive(root, output_name)
    with diagnostic_lease(root):
        from .capital import load_credentials
        from .sampler_reader import DemoHistoryReader
        from .history_sampler import SystemClock, capture
        credentials = load_credentials()
        try:
            with account_lease(credentials.get("identifier")):
                reader = DemoHistoryReader(credentials)
                return await capture(plan, reader, archive, SystemClock())
        finally:
            credentials.clear()
