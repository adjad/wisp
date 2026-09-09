"""A reminder receipt, not a model-written claim, is the notification payload."""
import re

from service.workflows.compiler import extract_channel, extract_recipient
from service.workflows.models import WorkflowPlan


def receipt_notification(request: str, receipt: str) -> WorkflowPlan:
    match = re.search(r"\b(?:let|notify|tell|text|message|email)\s+(?:my\s+)?"
                      r"(.+?)(?=\s+(?:know|about|that|via|through|using)\b|[.!?]|$)", request, re.I)
    recipient = extract_recipient(request)
    if not recipient and match and len(match.group(1).split()) <= 3:
        recipient = match.group(1).strip()
    plan = WorkflowPlan(recipient=recipient, channel=extract_channel(request),
                        original_request=request, artifact_text=receipt)
    plan.recompute_status()
    return plan
