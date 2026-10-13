# Stake Code Tracker — your own CodeStats-style live feed

Polls CodeStats.gg's **public** live feed (free to view, no account needed)
every 60 seconds, stores every code in SQLite, and serves a live dashboard +
JSON API.

**Live right now:** the dashboard at `/`, the API at `/api/codes`.

## What it tracks

| Field | Meaning |
|---|---|
| `code` / `platform` / `value` | the drop code, stake.us / stake.com / shuffle, its $/SC value |
| `status: live` | on CodeStats' feed right now — still claimable |
| `status: gone` | dropped off the feed — fully claimed or expired |
| `first_seen` / `last_seen` | your own 36h-style history archive |

## API

- `GET /api/codes` — `?platform=stake.us` `?live=1` `?limit=200`
- `GET /api/stats` — totals, live count, per-platform breakdown

Example: `/api/codes?platform=stake.us&live=1` → every live US code as JSON.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python app.py   # → http://localhost:8000
```

## Deploy free on Railway

Railway: free Trial (no card, one-time $5 credit), then a permanent $0 free
plan with $1/month credit — enough for this tiny app. (Verified Oct 2026.)

1. Push these files to a GitHub repo: `app.py`, `Procfile`,
   `requirements.txt`, `README.md`, `.gitignore`. The `data/` folder is
   gitignored — the database rebuilds itself on first poll.
2. https://railway.app → New Project → Deploy from GitHub repo.
3. Railway auto-detects the Procfile and starts a **web service**. No env vars
   needed. Open the generated domain — that's your tracker.
4. Optional: set `DATA_DIR` to a Railway volume path if you want the code
   history to survive redeploys (default is ephemeral disk; the DB just
   re-seeds from the live feed).

## Honest limits

- **It reads CodeStats' public feed; it is not CodeStats.** Their private
  auto-validation and auto-claimer are their own product — this tracker shows
  you what's live and archives what disappeared, but it never claims a code
  for you. Claiming still needs your own logged-in Stake session.
- A code marked `gone` means it left the feed (claimed out or expired) —
  the tracker can't tell which.
- Polite 60-second polling with an identified user agent. Don't lower it;
  hammering their server gets you blocked.
- Per your standing rule, this posts nothing anywhere — it's your private
  dashboard. Fresh finds still go to you in chat first, never auto-posted.
