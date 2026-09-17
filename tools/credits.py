import httpx
from config import CREDITS_BASE_URL, auth_headers


async def check_balance() -> dict:
    async with httpx.AsyncClient() as client:
        # Auth-only headers (no Origin). pmt.xelta.ai/api/users/credits returns
        # 500 whenever the Origin header from BROWSER_HEADERS is present — see the
        # note in tools.utils.check_credits_guard. So this sends just the bearer
        # token, same as the guard.
        r = await client.get(
            f"{CREDITS_BASE_URL}/api/users/credits",
            headers=auth_headers(),
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
