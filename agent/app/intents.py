from __future__ import annotations

import re


def is_service_list_intent(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text:
        return False

    list_patterns = [
        r"\blist\b.*\bservices?\b",
        r"\bshow\b.*\bservices?\b",
        r"\bwhat\s+are\s+the\s+services?\b",
        r"\bwhat\s+services?\b",
        r"\ball\s+services?\b",
    ]

    diagnostic_terms = [
        "error",
        "failed",
        "failing",
        "issue",
        "problem",
        "down",
        "crash",
        "restart",
        "not working",
        "unhealthy",
        "timeout",
    ]

    has_list_phrase = any(re.search(pattern, text) for pattern in list_patterns)
    has_diagnostic_signal = any(term in text for term in diagnostic_terms)
    return has_list_phrase and not has_diagnostic_signal


def wants_all_namespaces(message: str) -> bool:
    text = (message or "").strip().lower()
    return "all namespaces" in text or "across namespaces" in text or "all services" in text


def is_error_check_intent(message: str) -> bool:
    text = (message or "").strip().lower()
    indicators = [
        "error",
        "failed",
        "failing",
        "issue",
        "problem",
        "down",
        "crash",
        "restart",
        "not working",
        "unhealthy",
        "timeout",
        "image",
    ]
    return any(word in text for word in indicators)
