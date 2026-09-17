import sys
import asyncio
import httpx
from config import auth_headers, cookie_headers, CREDITS_BASE_URL

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://www.xelta.ai",
    "Referer": "https://www.xelta.ai/",
}


def bearer_headers() -> dict:
    return {**BROWSER_HEADERS, **auth_headers()}


def jar_headers() -> dict:
    """Cookie-based auth (lipsync, comic, etc.)."""
    return {**BROWSER_HEADERS, **cookie_headers()}


async def check_credits_guard(client: httpx.AsyncClient) -> str | None:
    """Returns an error string if the user has no credits, else None.

    Non-fatal on service errors: if the credits endpoint is unreachable or
    returns a 5xx / times out, we log a warning and allow the request to
    proceed rather than blocking the tool. The downstream generation service
    still enforces and deducts credits, so this only skips the pre-flight
    balance check when pmt.xelta.ai itself is failing — it does not grant free
    generations. Only a successful response reporting a zero/negative balance
    actually blocks.
    """
    try:
        # Auth-only headers on purpose. The shared BROWSER_HEADERS include an
        # `Origin: https://www.xelta.ai` header, and pmt.xelta.ai/api/users/credits
        # returns 500 deterministically whenever Origin is present (verified:
        # with Origin -> 500 every time, without -> 200 every time). Other Xelta
        # services tolerate Origin fine; the credits endpoint does not — so this
        # request sends only the bearer token.
        r = await client.get(
            f"{CREDITS_BASE_URL}/api/users/credits",
            headers=auth_headers(),
            timeout=10,
        )
        r.raise_for_status()
        balance = r.json().get("data", 0)
    except Exception as e:
        print(
            f"[credits] balance check skipped — credits service error: {e}",
            file=sys.stderr,
            flush=True,
        )
        return None
    if balance <= 0:
        return (
            f"You have {balance} Xelta credits. "
            "Top up at https://www.xelta.ai to continue."
        )
    return None


async def load_file(client: httpx.AsyncClient, path_or_url: str) -> tuple[bytes, str]:
    """Load a file from a local path or URL, return (bytes, filename)."""
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        r = await client.get(path_or_url, follow_redirects=True, timeout=60)
        r.raise_for_status()
        filename = path_or_url.split("/")[-1].split("?")[0] or "file"
        return r.content, filename
    else:
        import os
        path = os.path.normpath(path_or_url)
        if not os.path.exists(path):
            raise FileNotFoundError(f"File not found: {path}")
        with open(path, "rb") as f:
            return f.read(), os.path.basename(path)


# Keep old name as alias so existing code doesn't break
download_file = load_file


def format_tool_error(e: Exception) -> str:
    """Turn a caught exception into an actionable message for the calling LLM.

    Shared across every @mcp.tool() wrapper so error handling doesn't drift
    between server.py, server_local.py, and server_stdio.py.
    """
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status == 401:
            return "Error: Xelta authentication failed (401) — the sign-in has expired or was revoked. Run xelta_login to sign in again, then retry."
        if status == 403:
            return "Error: Access denied (403). The JWT token may not have permission for this service."
        if status == 404:
            return "Error: Resource not found (404). Double-check any ID or URL passed to this tool."
        if status == 429:
            return "Error: Rate limited (429) by Xelta. Wait a moment before retrying."
        if status >= 500:
            return f"Error: Xelta service error ({status}). Try again shortly."
        return f"Error: Request failed with status {status}: {e.response.text[:200]}"
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request to Xelta timed out. Try again, or retry with a smaller/simpler request."
    if isinstance(e, httpx.ConnectError):
        return "Error: Could not reach the Xelta service. Check network connectivity and try again."
    return f"Error: {e}"


# MCP clients typically enforce a tool-call timeout well under a minute, so
# long-running tools must not block on in-process polling for minutes at a
# time. These constants cap in-call polling; tools that don't finish in time
# should return a resumable "still processing, call me again with this ID"
# message instead of blocking further or raising.
QUICK_POLL_TIMEOUT = 45  # seconds — used with poll_job()
QUICK_POLL_ITERATIONS = 9  # 9 * 5s sleep = 45s — used by custom polling loops


async def poll_job(
    client: httpx.AsyncClient,
    status_url: str,
    *,
    headers: dict | None = None,
    interval: int = 5,
    timeout: int = 300,
) -> dict:
    """Poll a job status URL until completed or failed."""
    hdrs = headers if headers is not None else bearer_headers()
    elapsed = 0
    while elapsed < timeout:
        await asyncio.sleep(interval)
        elapsed += interval
        r = await client.get(status_url, headers=hdrs, timeout=15)
        r.raise_for_status()
        data = r.json()
        status = str(
            data.get("status")
            or data.get("state")
            or data.get("task_status", "")
        ).lower()
        if status in ("completed", "done", "success", "finished"):
            return data
        if status in ("failed", "error", "cancelled"):
            raise RuntimeError(f"Job failed: {data.get('error') or data}")
    raise TimeoutError(f"Job did not complete within {timeout}s")
