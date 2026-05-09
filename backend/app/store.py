"""Redis-backed approval store.

Proposals are stored under ``noma:approval:<id>`` with a 1-hour TTL.
The ``_user_id`` key is embedded in the stored payload so ownership
can be checked on approval without a separate lookup.
"""
from __future__ import annotations

import json
import os
from typing import Any

import redis as _redis

_TTL_SECONDS = 3600  # proposals expire after 1 hour
_PREFIX = "noma:approval:"


def _client() -> _redis.Redis:
    url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    return _redis.from_url(url, decode_responses=True)


def store_proposal(
    proposal_id: str,
    proposal: dict[str, Any],
    user_id: str | None = None,
) -> None:
    """Persist a remediation proposal. ``user_id`` is embedded as ``_user_id``."""
    r = _client()
    payload = {**proposal, "_user_id": user_id or "anonymous"}
    r.setex(f"{_PREFIX}{proposal_id}", _TTL_SECONDS, json.dumps(payload))


def get_proposal(proposal_id: str) -> dict[str, Any] | None:
    """Return the stored proposal dict (including ``_user_id``) or None if absent/expired."""
    r = _client()
    raw = r.get(f"{_PREFIX}{proposal_id}")
    if not raw:
        return None
    return json.loads(raw)


def delete_proposal(proposal_id: str) -> None:
    """Remove a proposal after it has been applied or rejected."""
    r = _client()
    r.delete(f"{_PREFIX}{proposal_id}")
