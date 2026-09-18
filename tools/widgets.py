"""
MCP Apps widgets served by the local server.

    upload      media_upload_widget  — media_upload -> PUT -> media_confirm -> hand off media_id
    generation  generate_image, multiply_ad, job_status — shows jobs, polls job_status
    login       xelta_login          — one button, waits for sign-in, hands back to Claude

Each widget calls server tools through the host (callServerTool), so the only
direct network access any of them needs is the upload PUT and loading media.
"""

import base64
from pathlib import Path
from urllib.parse import urlparse

from config import R2_PUBLIC_BASE_URL

_HERE = Path(__file__).resolve().parent
MIME_TYPE = "text/html;profile=mcp-app"
SDK_ORIGIN = "https://unpkg.com"

UPLOAD_URI = "ui://xelta/upload.html"
GENERATION_URI = "ui://xelta/generation.html"
LOGIN_URI = "ui://xelta/login.html"

# Inlined as a data URI: widgets can't load files from this server, and the logo
# is small enough (8 KB) that shipping it with the HTML is simplest.
LOGO_PNG = _HERE / "assets" / "xelta-logo.png"
LOGO_DATA_URI = "data:image/png;base64," + base64.b64encode(LOGO_PNG.read_bytes()).decode()
# The same logo as xelta.ai publishes it. Hosts that draw a server icon may only
# load https images, so it's listed first, with the inline copy as a fallback.
LOGO_URL = "https://xelta.ai/icons/apple-touch-icon.png"


def server_icons() -> list:
    from mcp.types import Icon
    return [Icon(src=LOGO_URL, mimeType="image/png", sizes=["180x180"]),
            Icon(src=LOGO_DATA_URI, mimeType="image/png", sizes=["90x90"])]

UPLOAD_HTML = (_HERE / "upload_widget.html").read_text(encoding="utf-8")
GENERATION_HTML = (_HERE / "generation_widget.html").read_text(encoding="utf-8")
LOGIN_HTML = (_HERE / "login_widget.html").read_text(encoding="utf-8").replace("{{XELTA_LOGO}}", LOGO_DATA_URI)


def _origin(url: str) -> str:
    return "{0.scheme}://{0.netloc}".format(urlparse(url))


def upload_csp(upload_origin: str) -> dict:
    """The upload PUT goes to this server; everything else goes through the host."""
    return {"resourceDomains": [SDK_ORIGIN], "connectDomains": [upload_origin]}


GENERATION_CSP = {"resourceDomains": [SDK_ORIGIN, _origin(R2_PUBLIC_BASE_URL)]}
LOGIN_CSP = {"resourceDomains": [SDK_ORIGIN]}


def ui_meta(uri: str) -> dict:
    return {"ui": {"resourceUri": uri}, "ui/resourceUri": uri}
