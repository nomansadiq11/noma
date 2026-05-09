"""Simple policy helpers for mutation authorisation.

Namespace allowlist is configured via the ``MUTATION_ALLOWED_NAMESPACES``
environment variable (comma-separated list, or ``*`` to allow all).

User ownership check ensures that only the user who created a proposal
(or any anonymous user) can approve it.
"""
from __future__ import annotations

import os

_ALLOWED_ENV = "MUTATION_ALLOWED_NAMESPACES"


def _allowed_namespaces() -> set[str]:
    raw = os.getenv(_ALLOWED_ENV, "*").strip()
    if raw == "*":
        return {"*"}
    return {ns.strip() for ns in raw.split(",") if ns.strip()}


def namespace_mutation_allowed(namespace: str) -> bool:
    """Return True if mutations are permitted in *namespace*."""
    allowed = _allowed_namespaces()
    if "*" in allowed:
        return True
    return namespace in allowed


def user_may_approve(user_id: str | None, proposal_user_id: str | None) -> bool:
    """Return True if *user_id* is allowed to approve the proposal.

    Rules (all permissive by default for local dev):
    - If either side is None or the proposal was created anonymously, allow.
    - Otherwise the approving user must match the requesting user.
    """
    if user_id is None or proposal_user_id is None:
        return True
    if proposal_user_id == "anonymous":
        return True
    return user_id == proposal_user_id
