"""Deriving a failed sign-in from Microsoft Graph's two timestamps.

Graph never reports "this sign-in failed". It reports two stamps per activity block:

* ``lastSignInDateTime``           — the last ATTEMPT, success or failure
* ``lastSuccessfulSignInDateTime`` — the last success

A failure is therefore an inference, not a field: **if the last attempt is stamped later
than the last success, that attempt cannot have been the success**, so it failed. When the
two are equal the last attempt is the success, and there is no evidence of a recent failure.

This lives in one module because two features (Entra applications and App Registrations)
must answer the question identically — a grid that disagrees with a workbook about whether
an application is failing is worse than either answer alone.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _parse(value: str | None) -> datetime | None:
    """ISO-8601 -> aware datetime, or None. Never raises on Graph junk."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def failed_signin(attempt: str | None, success: str | None) -> str:
    """The last attempt that demonstrably did not succeed, or "".

    Compares parsed datetimes rather than the raw strings. Lexical comparison is wrong here:
    Graph sometimes includes fractional seconds, and ``"...:00.5Z" < "...:00Z"`` because
    ``'.'`` sorts below ``'Z'`` — which would silently invert the result.

    A caveat worth knowing: ``lastSuccessfulSignInDateTime`` is not backfilled (it starts
    December 2023). An application whose last success predates that reports an attempt with
    no success and is treated as failing. Over a 30-day reporting window that cannot happen,
    which is the only window these callers use.
    """
    if not attempt:
        return ""
    attempted = _parse(attempt)
    if attempted is None:
        return ""
    succeeded = _parse(success)
    if succeeded is None:
        return str(attempt)
    return str(attempt) if attempted > succeeded else ""
