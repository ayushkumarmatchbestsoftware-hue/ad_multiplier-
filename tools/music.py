import asyncio
import httpx
from config import MUSIC_BASE_URL
from tools.utils import bearer_headers, QUICK_POLL_ITERATIONS


async def _music_credit_guard(client: httpx.AsyncClient) -> str | None:
    """Returns an error string if the user has no music credits, else None.

    The music service (music.xelta.ai) tracks its own credit balance via
    /api/credits — separate from the shared pmt.xelta.ai balance the other
    tools guard against — so we check the balance that actually gets deducted
    for a song here.
    """
    r = await client.get(
        f"{MUSIC_BASE_URL}/api/credits",
        headers=bearer_headers(),
        timeout=10,
    )
    r.raise_for_status()
    balance = r.json().get("balance", 0)
    if balance <= 0:
        return (
            f"You have {balance} Xelta music credits. "
            "Top up at https://www.xelta.ai to continue."
        )
    return None


def _audio_url(song_id: str) -> str:
    """Build the public audio URL for a song.

    The service stores an internal audioUrl (e.g. https://0.0.0.0:3000/...),
    which is not reachable, so we always construct the public URL from the
    song id. This endpoint serves audio/wav and needs no auth, so the URL is
    directly playable/shareable.
    """
    return f"{MUSIC_BASE_URL.rstrip('/')}/api/audio/{song_id}"


async def _find_song(client: httpx.AsyncClient, song_id: str) -> dict | None:
    """Fetch history and return the job dict matching song_id, or None.

    The /api/history/{id} route returns the whole history list (the id is
    ignored), so we fetch /api/history and match on songId ourselves.
    """
    r = await client.get(
        f"{MUSIC_BASE_URL}/api/history",
        headers=bearer_headers(),
        timeout=15,
    )
    r.raise_for_status()
    for job in r.json().get("jobs", []):
        if job.get("songId") == song_id:
            return job
    return None


def _format_song(job: dict) -> str:
    song_id = job.get("songId", "")
    lines = ["Music ready!"]
    if job.get("title"):
        lines.append(f"Title: {job['title']}")
    tags = job.get("styleTags") or {}
    if tags:
        lines.append("Style: " + ", ".join(f"{k}: {v}" for k, v in tags.items()))
    dur = job.get("targetDurationMs")
    if dur:
        lines.append(f"Duration: {round(dur / 1000)}s")
    lines.append(f"Audio: {_audio_url(song_id)}")
    if job.get("lyrics") and not job.get("instrumental"):
        lines.append(f"\nLyrics:\n{job['lyrics']}")
    lines.append(f"\nSong ID: {song_id}")
    return "\n".join(lines)


async def generate_music(
    prompt: str = "",
    lyrics: str = "",
    title: str = "",
    genre: str = "",
    mood: str = "",
    tempo: str = "",
    language: str = "",
    instrumental: bool = False,
    duration_seconds: int = 0,
    song_id: str = "",
) -> str:
    """Generate a song from a text prompt (and optional lyrics/style) via ElevenLabs.

    Submits the job, polls briefly, and returns a resumable song_id if it's
    still rendering when this call has to return.
    """
    async with httpx.AsyncClient(timeout=120) as client:
        if not song_id:
            if not prompt and not lyrics and not instrumental:
                return (
                    "Please provide a prompt describing the music (or lyrics, "
                    "or set instrumental=true) to generate a song."
                )

            guard = await _music_credit_guard(client)
            if guard:
                return guard

            style_tags: dict[str, str] = {}
            if genre:
                style_tags["genre"] = genre
            if mood:
                style_tags["mood"] = mood
            if tempo:
                style_tags["tempo"] = tempo
            if language:
                style_tags["language"] = language

            payload: dict = {"instrumental": instrumental}
            if prompt:
                payload["prompt"] = prompt
            if lyrics:
                payload["lyrics"] = lyrics
            if title:
                payload["title"] = title
            if style_tags:
                payload["styleTags"] = style_tags
            if duration_seconds > 0:
                payload["targetDurationMs"] = duration_seconds * 1000

            r = await client.post(
                f"{MUSIC_BASE_URL}/api/songs",
                json=payload,
                headers={**bearer_headers(), "Content-Type": "application/json"},
                timeout=120,
            )
            r.raise_for_status()
            data = r.json()

            # The API may return the finished song synchronously or just an id
            # to poll — handle both. Pull the song id from the common shapes.
            song_id = (
                data.get("songId")
                or data.get("id")
                or (data.get("job") or {}).get("songId")
                or (data.get("song") or {}).get("songId")
                or ""
            )

            # If the response already carries a finished song, return it now.
            status = str(data.get("status", "")).lower()
            if data.get("audioUrl") or status in ("completed", "done", "success"):
                if not song_id:
                    au = str(data.get("audioUrl", ""))
                    song_id = au.rstrip("/").split("/")[-1] if au else ""
                if song_id:
                    job = await _find_song(client, song_id) or {"songId": song_id, **data}
                    return _format_song(job)

            if not song_id:
                return f"Unexpected response: {data}"

        # Poll history briefly — stay under MCP client tool-call timeouts.
        for _ in range(QUICK_POLL_ITERATIONS):
            await asyncio.sleep(5)
            job = await _find_song(client, song_id)
            if job:
                status = str(job.get("status", "")).lower()
                progress = job.get("progress", 0) or 0
                if status in ("completed", "done", "success") or progress >= 100:
                    return _format_song(job)
                if status in ("failed", "error", "cancelled"):
                    return f"Music generation failed: {job.get('error') or job}"

        return (
            f"Still generating (this can take a few minutes). "
            f"song_id={song_id} — call this tool again with that song_id "
            f"to pick up right where this left off."
        )


async def generate_lyrics(prompt: str = "") -> str:
    """Generate song lyrics from a text prompt, without rendering audio."""
    if not prompt:
        return "Please provide a prompt describing the song to generate lyrics for."

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{MUSIC_BASE_URL}/api/lyrics",
            json={"prompt": prompt},
            headers={**bearer_headers(), "Content-Type": "application/json"},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()

        # Response shape can vary — pull the lyrics text defensively.
        lyrics = (
            data.get("lyrics")
            or data.get("text")
            or (data.get("data") or {}).get("lyrics")
            or data.get("result")
            or ""
        )
        if isinstance(lyrics, str) and lyrics.strip():
            return lyrics
        return f"Lyrics generated: {data}"
