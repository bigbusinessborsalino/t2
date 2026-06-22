# Telegram Video Downloader Bot — Render Free Tier

Tap-driven Telegram bot that scrapes `freepornvideos.xxx`, lets users pick
qualities, and uploads the file back into the chat (up to 2 GB).
Deployed on **Render Free Tier** with **UptimeRobot** keeping it warm.

## Repo layout
```
.
├── main.py              # Bot + HTTP health server (single entry point)
├── video_scraper.py     # Standalone CLI scraper utility (optional)
├── requirements.txt     # Python deps
├── render.yaml          # Render Blueprint config
├── runtime.txt          # Python version pin
├── .gitignore
└── README.md
```

## 1. Set up Telegram
1. Go to https://my.telegram.org → **API development tools** → create an app.
   Copy `api_id` and `api_hash`.
2. Open Telegram, talk to **@BotFather**, run `/newbot`, copy the bot token.

## 2. Push to GitHub
```bash
git init
git add .
git commit -m "initial"
git branch -M main
git remote add origin git@github.com:YOUR_USER/telegram-video-bot.git
git push -u origin main
```

## 3. Deploy on Render
Option A — Blueprint (recommended):
- Render Dashboard → **New** → **Blueprint**.
- Connect this repo. Render reads `render.yaml` and provisions the service.
- After the first deploy, go to **Environment** and set:
  - `API_ID`
  - `API_HASH`
  - `BOT_TOKEN`
- Click **Save Changes** → manual deploy (or wait for auto-deploy).

Option B — Manual:
- **New** → **Web Service** → connect repo.
- Runtime: **Python**
- Build: `pip install --upgrade pip && pip install -r requirements.txt`
- Start: `python main.py`
- Instance type: **Free**
- Health check path: `/health`
- Add the three env vars above.

## 4. Keep it alive with UptimeRobot
- Render free tier spins down after **15 min** of no HTTP traffic.
- Sign up at https://uptimerobot.com (free).
- **Add New Monitor** → **HTTP(s)**.
- URL: `https://<your-service>.onrender.com/health`
- Monitoring interval: **5 minutes**.
- That's it — the service stays warm 24/7.

## 5. Talk to the bot
- Open Telegram, find your bot, send `/start`.
- Tap a video → tap a quality → bot downloads + uploads.

## Bot commands
- `/start`  — first page of videos
- `/refresh` — drop catalog cache, refetch
- `/help`    — usage
- `/stats`   — runtime + cache stats

## Why this works on free tier
- The HTTP server on `$PORT` gives UptimeRobot something to ping.
- The free tier's 0.1 CPU + 512 MB RAM is enough for 2 concurrent downloads
  (`MAX_PARALLEL=2`). Bump it down to 1 if you see OOM kills.
- Files land in `/tmp/tg_dl` (the only writable dir on Render). A background
  loop wipes anything older than 1 hour.
- `cloudscraper` handles Cloudflare challenges without a real browser, so
  no Playwright/Chromium build is required.

## Env vars
| Name             | Required | Default     | Notes                                  |
|------------------|----------|-------------|----------------------------------------|
| `API_ID`         | ✅       | —           | From my.telegram.org                   |
| `API_HASH`       | ✅       | —           | From my.telegram.org                   |
| `BOT_TOKEN`      | ✅       | —           | From @BotFather                        |
| `MAX_PARALLEL`   | ❌       | `2`         | Concurrent downloads                   |
| `USER_COOLDOWN`  | ❌       | `1.5` sec   | Per-user throttle                      |
| `LOG_LEVEL`      | ❌       | `INFO`      | `DEBUG` for verbose                    |
| `PORT`           | ❌       | `10000`     | Auto-set by Render                     |
| `DOWNLOAD_DIR`   | ❌       | `/tmp/tg_dl` | Temp download location               |

## Local dev
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export API_ID=... API_HASH=... BOT_TOKEN=...
export PORT=10000
python main.py
```
Then poke `http://localhost:10000/health` and chat with the bot.
