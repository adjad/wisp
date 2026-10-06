"""Fail-closed cloud billing gate. A process timeout does NOT stop VM billing."""
import argparse
import datetime as dt
import json
import math
from pathlib import Path


def validate(receipt, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if receipt.get("phase") != "running-instance":
        raise ValueError("This is a runtime gate; a prelaunch estimate cannot authorize model work")
    required_true = ["authenticated_balance_verified", "credits_eligible", "all_charges_included", "auto_recharge_disabled", "no_card_charges", "remote_access_verified", "private_backup_destination_verified", "independent_instance_termination_scheduled", "runtime_recipe_qualified", "source_data_checks_passed"]
    for key in required_true:
        if receipt.get(key) is not True:
            raise ValueError("Launch blocked: " + key)
    if receipt.get("gpu") != "A6000" or receipt.get("gpu_count") != 1 or receipt.get("vram_gb") != 48:
        raise ValueError("Only the scoped single A6000 48GB offer is admitted")
    for key in ("credit_balance_usd", "hourly_usd", "smoke_cap_usd", "total_cap_usd", "reserved_noncompute_usd", "reserved_teardown_usd", "earlier_spend_usd"):
        v = receipt.get(key)
        if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or v < 0:
            raise ValueError("Invalid finite nonnegative amount: " + key)
    if receipt["hourly_usd"] <= 0 or receipt["smoke_cap_usd"] > 5 or receipt["total_cap_usd"] > 30:
        raise ValueError("Budget exceeds authorized scope")
    if receipt["credit_balance_usd"] < receipt["smoke_cap_usd"] or receipt["earlier_spend_usd"] + receipt["smoke_cap_usd"] > receipt["total_cap_usd"]:
        raise ValueError("Insufficient remaining credit budget")
    if receipt["reserved_teardown_usd"] < 0.50:
        raise ValueError("Reserve at least $0.50 for teardown/billing latency")
    quote = dt.datetime.fromisoformat(receipt["quote_verified_at_utc"])
    start = dt.datetime.fromisoformat(receipt["billing_start_at_utc"])
    stop = dt.datetime.fromisoformat(receipt["termination_at_utc"])
    if any(t.tzinfo is None for t in (quote, start, stop)):
        raise ValueError("All times require timezone offsets")
    if start > now:
        raise ValueError("Running-instance billing start cannot be in the future")
    allocation = receipt.get("allocation_evidence", {})
    termination = receipt.get("termination_evidence", {})
    for field in ("instance_id", "provider", "offer_id"):
        if not receipt.get(field) or allocation.get(field) != receipt[field] or termination.get(field) != receipt[field]:
            raise ValueError("Allocation/termination instance binding mismatch: " + field)
    if allocation.get("billing_start_at_utc") != receipt["billing_start_at_utc"]:
        raise ValueError("Billing start differs from observed allocation")
    if termination.get("termination_at_utc") != receipt["termination_at_utc"]:
        raise ValueError("Termination schedule differs from observed proof")
    if not 0 <= (now - quote).total_seconds() <= 3600:
        raise ValueError("Refresh quote/credit verification within one hour")
    seconds = (stop - start).total_seconds()
    if seconds <= 0 or stop <= now:
        raise ValueError("Termination must follow billing start and current time")
    quantum = receipt.get("billing_quantum_seconds")
    if type(quantum) is not int or quantum < 1 or quantum > 3600:
        raise ValueError("Verified billing quantum is required")
    billed_seconds = math.ceil(seconds / quantum) * quantum
    estimated = billed_seconds / 3600 * receipt["hourly_usd"] + receipt["reserved_noncompute_usd"] + receipt["reserved_teardown_usd"]
    if estimated > receipt["smoke_cap_usd"]:
        raise ValueError("Termination schedule exceeds inclusive smoke budget")
    for key in ("provider", "offer_id", "termination_receipt", "backup_destination"):
        if not isinstance(receipt.get(key), str) or not receipt[key].strip():
            raise ValueError("Missing verified " + key)
    return {"estimated_inclusive_max_usd": estimated, "termination_at_utc": stop.isoformat(), "seconds_until_termination": (stop-now).total_seconds(), "process_timeout_stops_billing": False}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("receipt", type=Path)
    a = p.parse_args()
    print(json.dumps(validate(json.loads(a.receipt.read_text())), indent=2))
