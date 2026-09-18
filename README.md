# Xelta Ad Multiplier — MCP Server

Turn one ad into many edited variants without leaving Claude. Upload an image or
video ad, describe the edits one per line (swap the person, the outfit, the
product, the background) and each variant renders in the chat as it finishes,
powered by [Xelta.ai](https://www.xelta.ai) generation models.

Built on the [Model Context Protocol](https://modelcontextprotocol.io) with
[MCP Apps](https://github.com/modelcontextprotocol/ext-apps) widgets for upload,
sign-in and live results.

---

## Features

- **Ad multiplier** (`multiply_ad`): one output per edit, each rendered
  independently. Reference images are matched to edits by position.
  - **Video ads** run on **Kling O3 Pro Video Editor** (2,670 credits per variant).
    It keeps the source audio and uses up to the first 10 seconds.
  - **Image ads** run on **Gemini 2.5 Flash** (59 credits per variant) by default,
    with the output sized to the source's aspect ratio.
- **Any video in:** any resolution, frame rate, format (MP4, MOV, WebM, MKV, AVI),
  HDR or phone rotation. Clips are probed and, only when needed, converted with
  ffmpeg to what Kling accepts (720–2160 px per side, 24–60 fps, 3–10 s, ≤ 200 MB).
  Each clip is converted once and the copy is reused.
- **Image generation and editing** (`generate_image`) with any Xelta image model.
  Requests are built from each model's own input schema.
- **Video length choice:** Kling returns as much video as it's given, so for clips
  longer than about 5 s Claude first asks how long the result should be (5 s or
  10 s, or the whole clip), with buttons in the chat. Clips are trimmed to that length.
- **In-chat widgets:** an upload box, a Xelta sign-in card, and result cards in the
  style of Higgsfield: the prompt (with *Show more*), chips for model, aspect, length
  and audio, the player, then **Download** (saves the file) and **Recreate** (asks
  before spending credits again).
- **Non-blocking jobs:** tools return job ids immediately. Results are recovered
  from storage if the API gateway cuts a request off at 29 s or the server restarts.
- **Xelta account sign-in** through a browser session, prompted automatically
  before the first Xelta action. No tokens to copy.

## How it works

```
Upload widget ──PUT──▶ local listener ──▶ R2  ad-multiplier/u/<user>/{src,ref}/
     │ media_id
     ▼
multiply_ad ──▶ one job per variant
                  ├─ video_prep: probe → convert if outside Kling's limits → R2
                  └─ Xelta API: Kling O3 (video) / image model (image)
     ▼
Generation widget ──polls job_status──▶ result shown in the chat (new media_id)
```

- **Media are referenced by `media_id`, never by raw URL.** An id survives in the
  transcript, so "edit the second one" always resolves. Web links go through
  `media_import_url`.
- **Uploads go through a loopback listener** started by the server, which streams
  them to Cloudflare R2. The bucket's CORS policy doesn't admit widget origins.
- **Xelta stores every result** under `uploads/creations/<user>/<model>/`, which is
  how jobs cut off by the gateway are recovered.

---

## Requirements

- Python 3.11+
- [ffmpeg](https://ffmpeg.org) with ffprobe, on `PATH` or set via `FFMPEG_PATH`
- A Xelta account with credits
- Cloudflare R2 credentials for the media bucket
- [Claude Desktop](https://claude.ai/download)

## Setup (Claude Desktop)

1. **Clone and install**

   ```bash
   git clone https://github.com/ayushkumarmatchbestsoftware-hue/ad_multiplier-.git
   cd ad_multiplier-
   pip install -r requirements.txt
   ```

2. **Install ffmpeg**

   ```bash
   winget install Gyan.FFmpeg      # Windows
   brew install ffmpeg             # macOS
   sudo apt install ffmpeg         # Debian / Ubuntu
   ```

3. **Configure:** copy `.env.example` to `.env` and fill in the `R2_*` values.
   Everything else has working defaults.

4. **Register the server** in `claude_desktop_config.json`:

   ```json
   {
     "mcpServers": {
       "xelta": {
         "command": "python",
         "args": ["/absolute/path/to/ad_multiplier-/server_local.py"]
       }
     }
   }
   ```

   | Platform | Config file |
   |----------|-------------|
   | Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
   | Windows (Microsoft Store build) | `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json` |
   | macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |

5. **Restart Claude Desktop** fully. On startup the server log
   (`mcp-server-xelta.log`) shows the upload listener and
   `[xelta] video conversion: <path to ffmpeg>`.

6. **Sign in:** start a chat. Before the first Xelta action, Claude shows a
   **Sign in with Xelta** card. Click it, finish signing in in the browser tab that
   opens, and the card switches to *Connected to Xelta*, then Claude carries on with
   your request. The sign-in is saved on this computer, so later chats skip this step.

## Example prompts

```
Multiply this video ad:
change his shirt to a red hoodie
put him on a beach at sunset
make it night time with neon lights

Make a 10 second version where the background is a snowy street

Edit this photo so it's snowing

Check my Xelta credits
```

---

## Tools (`server_local.py`)

| Tool | Description |
|------|-------------|
| `multiply_ad` | Several edited variants of one image or video ad; one job per edit |
| `generate_image` | Generate an image, or edit one passed as a `media_id` |
| `job_status` | Status and result of a generation job (the widget polls this) |
| `job_recreate` | Run a result again with the same inputs (the card's Recreate button) |
| `media_download` | One-hour download link for a result (the card's Download button; hidden from Claude) |
| `media_upload_widget` | Show the upload box; the user's file comes back as a `media_id` |
| `media_upload` / `media_confirm` | Upload protocol used by the widget |
| `media_import_url` | Import an image or video from a public https URL |
| `show_medias` | List earlier uploads and results |
| `xelta_login` / `xelta_logout` | Sign in or out of the Xelta account |
| `check_balance` | Xelta credit balance |
| `create_reel` | Short AI video reel from a prompt |

## Configuration

All settings are environment variables, read from `.env` next to `config.py`.
See `.env.example` for the full list.

| Variable | Default | Purpose |
|----------|---------|---------|
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | (none) | R2 credentials; uploads are disabled without them |
| `R2_BUCKET_NAME` / `R2_PUBLIC_BASE_URL` / `R2_UPLOAD_PREFIX` | `xelta-ai` / `https://media.xelta.ai` / `ad-multiplier` | Where media is stored and served |
| `MEDIA_UPLOAD_MAX_MB` | `20` | Largest image upload |
| `MEDIA_INTAKE_VIDEO_MAX_MB` / `MEDIA_INTAKE_VIDEO_MAX_SECONDS` | `500` / `120` | Largest video upload (longer clips are trimmed before sending) |
| `FFMPEG_PATH` | auto-detected | Folder or path of ffmpeg, if it isn't on `PATH` |
| `XELTA_JWT_TOKEN` | (none) | Development fallback when not signed in |

The local server keeps its state in git-ignored files next to `config.py`:
`.xelta_session.json` (sign-in), `.xelta_media_library.json` (media ids) and
`.xelta_jobs.json` (jobs).

---

## Hosted server (`server.py`)

`server.py` is the multi-user HTTP deployment behind `mcp.xelta.ai`. It uses
OAuth 2.1 (including the device flow) and exposes `check_balance`,
`xelta_connection_status`, `upload_media`, `generate_image` and `create_reel`,
plus a bot-facing endpoint at `/bot-mcp`. It does not include the ad multiplier.

```bash
docker compose up -d --build     # expects .env and the external cloudflare_net network
```

## Project structure

```
├── server_local.py            # Claude Desktop entry point (stdio) with the ad multiplier
├── server.py                  # Hosted HTTP server with OAuth
├── server_stdio.py            # Minimal stdio server (balance and connection status)
├── config.py                  # Settings and Xelta auth resolution
├── oauth/                     # OAuth 2.1 provider, consent page, device flow
├── tools/
│   ├── ad_multiplier.py       # Kling O3 model, prompt construction, provider calls
│   ├── video_prep.py          # Probe and convert source clips to Kling's limits
│   ├── generation.py          # Request validation, one job per variant
│   ├── jobs.py                # Background jobs, status and recovery
│   ├── media_library.py       # media_id registry
│   ├── media_tools.py         # Upload, confirm, URL import, listing
│   ├── image_gen.py           # Schema-driven image generation
│   ├── creation_recovery.py   # Recover results cut off by the API gateway
│   ├── xelta_session.py       # Browser-session sign-in
│   ├── widgets.py             # Widget resources, CSP and the inlined logo
│   ├── *_widget.html          # Upload, sign-in and results widgets
│   └── assets/xelta-logo.png  # Server icon and sign-in card logo
└── tests/
```

## Development

```bash
pytest
```

Under stdio, stdout carries the MCP protocol: log to stderr only, and never let a
subprocess inherit stdin or stdout.
