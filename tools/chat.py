import httpx
from config import CHAT_BASE_URL
from tools.utils import bearer_headers


async def chat(message: str = "", context: str = "") -> str:
    """
    Have a conversation with Xelta's assistant (xelta_chat, a GPT-4o-mini react
    agent) — for plain messages that aren't a specific generation request.

    Stateless on the server side: it has no memory of its own. To keep a
    conversation going, pass the prior exchange (or a running summary) as
    `context` on each call — omit it to start fresh. Not credit-billed.

    Args:
        message: What to say.
        context: Optional prior-conversation text for the assistant to see
            (e.g. the last few turns, or a running summary).
    """
    if not message:
        return "Please provide a message to chat with."

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{CHAT_BASE_URL}/api/v1/chat",
            json={"message": message, "context": context or None},
            headers=bearer_headers(),
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()

    return data["reply"]
