"""
Media library — a media_id for everything a user uploads, imports or generates.

Modelled on Higgsfield's MCP: tools take a media_id (or the job_id of an earlier
generation), never a raw URL. An id is short, survives in the chat transcript,
and names one exact file, so "edit the second one" or "use my cat photo again"
resolves without asking the user to upload twice.

Local server only: the library is a JSON file next to config.py, shared by every
server process on the machine and keyed by Xelta user id.

Entry lifecycle for uploads: pending (slot issued) -> uploaded (bytes stored) ->
confirmed (usable). Generated and imported media are created confirmed.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from config import R2_PUBLIC_BASE_URL, R2_UPLOAD_PREFIX

LIBRARY_FILE = Path(
    os.getenv("XELTA_MEDIA_LIBRARY_FILE", "")
    or Path(__file__).resolve().parent.parent / ".xelta_media_library.json"
)
# The earlier recent-media history, migrated in on first load.
_LEGACY_HISTORY_FILE = Path(__file__).resolve().parent.parent / ".xelta_media_history.json"

MAX_PER_USER = 500
_lock = threading.Lock()


class MediaError(ValueError):
    """A media reference the caller can fix — the message says how."""


def new_id() -> str:
    return str(uuid.uuid4())


# ── storage ───────────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        return json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _migrate_legacy()
    except Exception:
        return {"users": {}}


def _save(data: dict) -> None:
    tmp = LIBRARY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, LIBRARY_FILE)


def _user(data: dict, user_id: str) -> dict:
    return data.setdefault("users", {}).setdefault(user_id, {"items": {}, "order": []})


def _put(data: dict, user_id: str, entry: dict) -> None:
    user = _user(data, user_id)
    user["items"][entry["media_id"]] = entry
    user["order"] = [entry["media_id"]] + [m for m in user["order"] if m != entry["media_id"]]
    for dropped in user["order"][MAX_PER_USER:]:
        user["items"].pop(dropped, None)
    user["order"] = user["order"][:MAX_PER_USER]


def _migrate_legacy() -> dict:
    data = {"users": {}}
    try:
        legacy = json.loads(_LEGACY_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return data
    for user_id, entries in legacy.items():
        for e in reversed(entries):  # oldest first, so order ends newest-first
            _put(data, user_id, _entry(
                url=e["url"], kind=e.get("kind", "image"), source=e.get("source", "upload"),
                status="confirmed", caption=e.get("caption", ""), created=e.get("at"),
            ))
    _save(data)
    return data


def _entry(*, url: str = "", kind: str, source: str, status: str, caption: str = "",
           filename: str = "", key: str = "", created: float | None = None,
           media_id: str = "", size: int = 0, duration: float | None = None,
           width: int = 0, height: int = 0) -> dict:
    return {
        "media_id": media_id or new_id(),
        "type": kind,
        "source": source,
        "status": status,
        "url": url,
        "key": key,
        "filename": filename,
        "caption": caption[:200],
        "size": size,
        "duration": duration,
        "width": width,
        "height": height,
        "created": int(created if created is not None else time.time()),
    }


def public_url(key: str) -> str:
    return f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key}"


def is_own_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    return parsed.scheme == "https" and parsed.hostname == urlparse(R2_PUBLIC_BASE_URL).hostname


# ── upload slots ──────────────────────────────────────────────────────────────

def new_upload(user_id: str, filename: str, kind: str) -> dict:
    entry = _entry(kind=kind, source="upload", status="pending", filename=filename[:200])
    with _lock:
        data = _load()
        _put(data, user_id, entry)
        _save(data)
    return entry


def upload_key(user_id: str, media_id: str, kind: str, ext: str) -> str:
    folder = "src" if kind == "video" else "ref"
    return f"{R2_UPLOAD_PREFIX}/u/{user_id}/{folder}/{media_id}.{ext}"


def mark_uploaded(user_id: str, media_id: str, *, key: str, kind: str, size: int,
                  duration: float | None, width: int = 0, height: int = 0) -> dict:
    with _lock:
        data = _load()
        entry = _user(data, user_id)["items"].get(media_id)
        if not entry:
            raise MediaError(f"Unknown media_id {media_id}.")
        entry.update({"status": "uploaded", "key": key, "url": public_url(key),
                      "type": kind, "size": size, "duration": duration,
                      "width": width, "height": height})
        _save(data)
        return dict(entry)


def confirm(user_id: str, media_id: str) -> dict:
    with _lock:
        data = _load()
        entry = _user(data, user_id)["items"].get(media_id)
        if not entry:
            raise MediaError(f"Unknown media_id {media_id}.")
        if entry["status"] == "pending":
            raise MediaError(f"media_id {media_id} has no uploaded file yet — upload the bytes before confirming.")
        entry["status"] = "confirmed"
        _save(data)
        return dict(entry)


# ── generated / imported ──────────────────────────────────────────────────────

def add(user_id: str, url: str, kind: str, source: str, *, caption: str = "",
        filename: str = "", key: str = "", created: float | None = None,
        size: int = 0, duration: float | None = None, media_id: str = "",
        width: int = 0, height: int = 0) -> dict:
    """Register confirmed media. Re-adding the same URL returns the existing id."""
    with _lock:
        data = _load()
        user = _user(data, user_id)
        for existing in user["items"].values():
            if existing.get("url") == url and existing["status"] == "confirmed":
                return dict(existing)
        entry = _entry(url=url, kind=kind, source=source, status="confirmed", caption=caption,
                       filename=filename, key=key, created=created, size=size,
                       duration=duration, media_id=media_id, width=width, height=height)
        _put(data, user_id, entry)
        _save(data)
        return dict(entry)


def set_prepared(user_id: str, media_id: str, plan_key: str, prepared: dict) -> None:
    """Remember a converted copy of a video (see tools/video_prep) under what it was converted to."""
    with _lock:
        data = _load()
        entry = _user(data, user_id)["items"].get(media_id)
        if entry:
            entry.setdefault("prepared", {})[plan_key] = prepared
            _save(data)


# ── lookup ────────────────────────────────────────────────────────────────────

def get(user_id: str, media_id: str) -> dict | None:
    entry = _load().get("users", {}).get(user_id, {}).get("items", {}).get(media_id)
    return dict(entry) if entry else None


def list_media(user_id: str, kind: str = "image", size: int = 24, cursor: int = 0) -> tuple[list[dict], int | None]:
    user = _load().get("users", {}).get(user_id, {"items": {}, "order": []})
    confirmed = [user["items"][m] for m in user["order"]
                 if m in user["items"] and user["items"][m]["status"] == "confirmed"
                 and (not kind or user["items"][m]["type"] == kind)]
    page = confirmed[cursor:cursor + size]
    next_cursor = cursor + size if cursor + size < len(confirmed) else None
    return [dict(e) for e in page], next_cursor


def resolve(user_id: str, value: str, *, job_lookup=None) -> dict:
    """A confirmed entry for a media_id, a completed job_id, or one of our own URLs."""
    value = (value or "").strip()
    if not value:
        raise MediaError("Empty media reference.")

    entry = get(user_id, value)
    if entry:
        if entry["status"] != "confirmed":
            raise MediaError(f"media_id {value} isn't confirmed yet — finish the upload first.")
        return entry

    if job_lookup is not None:
        job = job_lookup(value)
        if job is not None:
            if job.get("status") != "completed" or not job.get("results"):
                raise MediaError(f"Job {value} hasn't finished yet — wait for it before using its result.")
            media = get(user_id, job["results"][0]["media_id"])
            if media:
                return media

    if value.startswith(("http://", "https://")):
        if is_own_url(value):
            kind = "video" if urlparse(value).path.lower().endswith((".mp4", ".mov", ".m4v", ".webm")) else "image"
            return add(user_id, value, kind, "imported")
        raise MediaError(
            "Pass a media_id, not a URL. For a web image or video, call media_import_url first "
            "and use the media_id it returns."
        )
    raise MediaError(
        f"No media or job with id {value}. For the user's own file call media_upload_widget; "
        "to find something uploaded earlier call show_medias."
    )
