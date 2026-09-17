"""
Recover generations whose submit request was cut off by the API gateway.

The generation API sits behind AWS API Gateway (…execute-api…amazonaws.com),
whose HTTP APIs end any request after 29 seconds with 503 {"message":"Service
Unavailable"}. The generation itself keeps running behind the gateway, finishes,
and is billed — but the client never receives a job id or a URL. Observed
2026-09-12: two Wan video submissions returned 503 and each still produced a
finished video in the creations folder.

Xelta stores every result in the same R2 bucket we already read, at

    uploads/creations/<user_id>/<model_id>/<start_ms>-<hash>.<ext>

where <start_ms> is when the job started and each result is written twice with
the same <hash>. So a cut-off submission is recovered by looking for the first
result for that user and model that started after the submission was sent.
"""

from __future__ import annotations

import asyncio
import re

from config import R2_BUCKET_NAME, R2_PUBLIC_BASE_URL

# Gateway cut-offs, not real failures.
GATEWAY_TIMEOUT_STATUSES = (502, 503, 504)

# Job-id prefix for a submission being recovered from storage: "r2:<since_ms>".
RECOVER_PREFIX = "r2:"

# The job's recorded start can land slightly before our local clock read of the
# send time; submissions in a batch are 29s+ apart, so this can't cross them.
_CLOCK_SLACK_MS = 5000

_NAME_RE = re.compile(r"^(\d{13})-([0-9a-f]{6,})\.[A-Za-z0-9]+$")


def recover_job_id(since_ms: int) -> str:
    return f"{RECOVER_PREFIX}{since_ms}"


def parse_recover_job_id(job_id: str) -> int | None:
    if not job_id.startswith(RECOVER_PREFIX):
        return None
    try:
        return int(job_id[len(RECOVER_PREFIX):])
    except ValueError:
        return None


def _list_after(prefix: str, start_after: str) -> list[dict]:
    from tools.media_upload import _r2

    out, token = [], None
    while True:
        kwargs = {"Bucket": R2_BUCKET_NAME, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        else:
            kwargs["StartAfter"] = start_after
        page = _r2().list_objects_v2(**kwargs)
        out += page.get("Contents", [])
        if not page.get("IsTruncated"):
            return out
        token = page["NextContinuationToken"]


async def find_creations(user_id: str, model_id: str, since_ms: int) -> list[tuple[int, str, str]]:
    """Distinct results (start_ms, hash, url) for this user+model started at or
    after since_ms, oldest first, one entry per hash."""
    if not user_id:
        return []
    prefix = f"uploads/creations/{user_id}/{model_id}/"
    floor = max(0, since_ms - _CLOCK_SLACK_MS)
    # Names begin with a 13-digit millisecond timestamp, so lexical order is
    # chronological and StartAfter skips everything older in one request.
    objects = await asyncio.to_thread(_list_after, prefix, f"{prefix}{floor - 1}")

    earliest: dict[str, tuple[int, str]] = {}
    for obj in objects:
        match = _NAME_RE.match(obj["Key"].rsplit("/", 1)[-1])
        if not match:
            continue
        started, digest = int(match.group(1)), match.group(2)
        if started < floor:
            continue
        if digest not in earliest or started < earliest[digest][0]:
            earliest[digest] = (started, f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{obj['Key']}")
    return sorted((started, digest, url) for digest, (started, url) in earliest.items())


async def recover_one(user_id: str, model_id: str, since_ms: int, exclude: set[str] | None = None) -> str:
    """URL of the first unclaimed result since since_ms, or ''."""
    for _started, digest, url in await find_creations(user_id, model_id, since_ms):
        if exclude is not None and digest in exclude:
            continue
        if exclude is not None:
            exclude.add(digest)
        return url
    return ""
