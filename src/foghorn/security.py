"""LLM-app security primitives: untrusted-content envelope, PII/secret redaction (logs + traces)."""

from __future__ import annotations

import re

from langsmith.anonymizer import StringNodeRule, create_anonymizer

MAX_TOOL_CHARS = 8_000
_INJECTION_MARKERS = re.compile(
    r"(ignore (all )?(previous|prior) instructions|system prompt|you are now|<\s*/?\s*system\s*>)", re.I
)

PII_PATTERNS: list[StringNodeRule] = [  # ORDER MATTERS: SSN before PHONE (a phone pattern also matches 123-45-6789)
    {"pattern": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "replace": "<EMAIL>"},
    {"pattern": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "replace": "<SSN>"},
    {"pattern": re.compile(r"\b(?:\+?\d[\d -]{8,}\d)\b"), "replace": "<PHONE>"},
    {"pattern": re.compile(r"\b(?:sk|pk|lsv2)[-_][A-Za-z0-9_-]{16,}\b"), "replace": "<API_KEY>"},
]
#: pass to langsmith.Client(anonymizer=...) / tracing_context to scrub traces BEFORE upload
trace_anonymizer = create_anonymizer(PII_PATTERNS)


def redact(text: str) -> str:
    out = text
    for p in PII_PATTERNS:
        out = p["pattern"].sub(p["replace"], out)
    return out


def wrap_untrusted(source: str, text: str, *, max_chars: int = MAX_TOOL_CHARS) -> str:
    """Envelope for tool/web/retrieval output before it re-enters the prompt.

    Does not 'defuse' injections (nothing reliably can); it (1) bounds size, (2) makes the data/instruction
    boundary explicit for the model, (3) neutralises the closing tag so content cannot break out of the
    envelope, (4) flags suspicious phrases so evals/alerts can count them. The real defence is capability
    limiting: untrusted text never selects tools or arguments for side effects without HITL approval.
    """
    body = text[:max_chars].replace("</untrusted>", "<\\/untrusted>")
    flag = ' flagged="possible-injection"' if _INJECTION_MARKERS.search(body) else ""
    trunc = ' truncated="true"' if len(text) > max_chars else ""
    return f'<untrusted source="{source}"{flag}{trunc}>\n{body}\n</untrusted>'
