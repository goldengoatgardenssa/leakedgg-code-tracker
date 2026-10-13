#!/usr/bin/env python3
"""
leaked.gg code tracker — multi-source live drop-code feed.

WHAT IT DOES
  Every 120 seconds it polls every machine-readable drop-code source:
    - codestats.gg/                       (live validated feed)
    - codestats.gg/history/YYYY/MM/DD     (dated archive: values, caps, reqs)
    - t.me/s/StakeBonusDropsCodes         (Telegram bonus-drop channel)
    - instalker.org/qzinodrop             (Qzino's public X drop mirror)
  …stores every code in SQLite, and serves:
    - GET /            → live dashboard (auto-refreshes every 30s)
    - GET /api/codes   → JSON: ?platform=&source=&live=1&limit=
    - GET /api/stats   → JSON counts

  "live" is honest: a code is LIVE while seen on a source within the live
  window; when it drops off everywhere it's marked GONE (claimed-out or
  expired) with first/last seen timestamps.

HONEST LIMITS
  - Reads only public pages/feeds; never logs in, never claims a code.
  - stakestats.net and codez.gg render codes via JS/images — not scrapable
    with plain HTTP, so they are not polled (their drops flow into CodeStats
    anyway, which aggregates official Stake/Shuffle channels, X, IG, and
    community submissions).
  - "eddiescodez" could not be verified as a real site — not polled; Eddie's
    Kick-stream Saturday codes surface via CodeStats' social monitoring.
  - A code marked GONE left every feed (claimed out or expired) — the
    tracker cannot tell which.
  - Polite 120s cycle with an identified user agent. Don't hammer sources.

Run locally:  pip install -r requirements.txt && python3 app.py
Deploy:       Railway web service (see README.md)
"""

import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tracker")

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "codes.db"

UA = "leakedgg-code-tracker/2.0 (multi-source poller)"
POLL_SECONDS = 120
LIVE_WINDOW_SECONDS = 300  # seen within this on any source = live

STAKE_RE = re.compile(r"\bstake(?:com|us|tr|pl)[a-z0-9]+\b", re.IGNORECASE)
BACKTICK_CODE_RE = re.compile(r"`([A-Za-z0-9]{6,20})`")
SHUFFLE_TOKEN_RE = re.compile(r"\b[A-Z0-9]{6,12}\b")
VALUE_RE = re.compile(r"(\$[\d.,]+|\d+(?:\.\d+)?\s*SC\b)")


def classify_stake(code: str) -> str:
    low = code.lower()
    if low.startswith("stakeus"):
        return "stake.us"
    if low.startswith("staketr"):
        return "stake.tr"
    if low.startswith("stakepl"):
        return "stake.pl"
    return "stake.com"


def fetch(url: str, ua: str = UA) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", "replace")


def strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "\n", html)


def normalize_code_tags(html: str) -> str:
    """Turn <code>…</code> into backticks before tag-stripping, so code
    tokens survive as `CODE` in the plain text."""
    return re.sub(r"</?code[^>]*>", "`", html)


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


# ---------------------------------------------------------------- sources ---

def parse_codestats_live(html: str) -> list[dict]:
    """codes + nearby values from the codestats.gg live front page."""
    text = strip_tags(html)
    shuffle_idx = -1
    hm = re.search(r"(?m)^\s*Shuffle\s*$", text)
    if hm:
        shuffle_idx = hm.end()
    found: dict[tuple[str, str], dict] = {}
    now = datetime.now(timezone.utc).isoformat()
    SHUFFLE_STOP = {"SHUFFLE", "CODES", "HISTORY", "RECENT", "DROPS",
                    "BONUS", "CLAIM"}

    def add(code, platform, value, source):
        key = (code, platform)
        if key not in found:
            found[key] = {"code": code, "platform": platform,
                          "value": value, "detail": "",
                          "source": source, "at": now}

    for m in STAKE_RE.finditer(text):
        code = m.group(0)
        tail = text[m.end():m.end() + 60]
        vm = VALUE_RE.search(tail)
        add(code, classify_stake(code), vm.group(1) if vm else "",
            "codestats.gg")

    if shuffle_idx != -1:
        section = text[shuffle_idx:shuffle_idx + 3000]
        for m in SHUFFLE_TOKEN_RE.finditer(section):
            code = m.group(0)
            if code in SHUFFLE_STOP:
                continue
            if not any(c.isdigit() for c in code) and len(code) < 8:
                continue
            tail = section[m.end():m.end() + 60]
            vm = VALUE_RE.search(tail)
            add(code, "shuffle", vm.group(1) if vm else "", "codestats.gg")
    return list(found.values())


def parse_codestats_archive(html: str) -> list[dict]:
    """Dated archive blocks: HH:MM UTC headers with value/cap/requirements."""
    text = strip_tags(normalize_code_tags(html))
    parts = re.split(r"(?m)^(\d{2}:\d{2}) UTC\s*$", text)
    now = datetime.now(timezone.utc).isoformat()
    found: dict[tuple[str, str], dict] = {}

    def clean(s: str) -> str:
        s = re.sub(r"\*+", "", s).strip()
        return re.sub(r"\s+", " ", s)

    for i in range(1, len(parts) - 1, 2):
        block = parts[i + 1]
        codes: set[str] = set(STAKE_RE.findall(block))
        codes |= set(BACKTICK_CODE_RE.findall(block))
        if not codes:
            continue
        vm = re.search(r"Value\**\s*:?\**\s*([^\n]+)", block)
        cm = re.search(r"(?:Total Drop Limit|Claims)\**\s*:?\**\s*([^\n]+)",
                       block)
        rm = re.search(r"Requirements\**\s*:?\**\s*([^\n]+)", block)
        value = clean(vm.group(1)) if vm else ""
        # value line may contain "10 SC + 100,000 GC" — keep whole thing
        detail_bits = []
        if cm:
            detail_bits.append("cap: " + clean(cm.group(1)))
        if rm:
            detail_bits.append("req: " + clean(rm.group(1)))
        detail = " | ".join(detail_bits)
        for code in codes:
            if STAKE_RE.fullmatch(code):
                platform = classify_stake(code)
            else:
                platform = "shuffle"  # backtick codes in archive = shuffle
            key = (code, platform)
            if key not in found:
                found[key] = {"code": code, "platform": platform,
                              "value": value, "detail": detail,
                              "source": "codestats.gg/archive", "at": now}
    return list(found.values())


def parse_telegram(html: str) -> list[dict]:
    """t.me/s/StakeBonusDropsCodes — 'Bonus Code Drop' + Code: `x`.

    NOTE: the channel tags every post "➤ stake.com" generically, so the
    code's own prefix (stakecom/stakeus/staketr/stakepl) is authoritative
    for the platform — the label is ignored."""
    text = strip_tags(normalize_code_tags(html))
    now = datetime.now(timezone.utc).isoformat()
    found: dict[tuple[str, str], dict] = {}
    chunks = re.split(r"Bonus Code Drop", text)
    for chunk in chunks[1:]:
        cm = re.search(r"Code:\s*`([a-z0-9]+)`", chunk)
        if not cm:
            continue
        code = cm.group(1)
        if not STAKE_RE.fullmatch(code):
            continue  # skip junk/test posts
        platform = classify_stake(code)
        key = (code, platform)
        if key not in found:
            found[key] = {"code": code, "platform": platform, "value": "",
                          "detail": "", "source": "t.me/StakeBonusDropsCodes",
                          "at": now}
    return list(found.values())


QZINO_PATTERNS = [
    re.compile(r"Code Drop[^\n\S]*⬇️?\s*\n\s*\n\s*([A-Z0-9]{7})\b"),
    re.compile(r"Code Drop:\s*([A-Z0-9]{7})\b"),
    re.compile(r"Code Drop to celebrate:\s*([A-Z0-9]{7})\b"),
]


def parse_qzino(html: str) -> list[dict]:
    """instalker.org/qzinodrop — 7-char codes with value/claims/requirements."""
    text = strip_tags(html)
    now = datetime.now(timezone.utc).isoformat()
    found: dict[tuple[str, str], dict] = {}
    for pat in QZINO_PATTERNS:
        for m in pat.finditer(text):
            code = m.group(1)
            tail = text[m.end():m.end() + 400]
            vm = re.search(r"Value:\s*(\$[\d.,]+)", tail)
            cm = re.search(r"Total Drop:\s*([^\n]+)", tail)
            rm = re.search(r"Requirements:\s*([^\n]+)", tail)
            detail_bits = []
            if cm:
                detail_bits.append("cap: " + cm.group(1).strip())
            if rm:
                detail_bits.append("req: " + rm.group(1).strip())
            key = (code, "qzino")
            if key not in found:
                found[key] = {
                    "code": code, "platform": "qzino",
                    "value": vm.group(1) if vm else "",
                    "detail": " | ".join(detail_bits),
                    "source": "qzinodrop (X mirror)", "at": now}
    return list(found.values())


SOURCES = [
    # (name, url, parser, user-agent) — url None means the dated archive URL
    ("codestats.gg", "https://codestats.gg/", parse_codestats_live, UA),
    ("codestats.gg/archive", None, parse_codestats_archive, UA),
    # NOTE: Qzino's X mirrors (instalker/twstalker) are Cloudflare-blocked for
    # plain-HTTP polling, so qzinodrop is not in the poll list — the hourly
    # qzino-drop-watch cron covers Qzino drops via the text-fetch reader and
    # pings Ash in chat. parse_qzino is kept for manual runs.
    ("t.me/StakeBonusDropsCodes", "https://t.me/s/StakeBonusDropsCodes",
     parse_telegram, UA),
]


def archive_urls() -> list[str]:
    """Today + yesterday (UTC): catches late-day drops still on live feeds."""
    now = datetime.now(timezone.utc)
    out = []
    for delta in (0, 1):
        d = now - timedelta(days=delta)
        out.append(d.strftime("https://codestats.gg/history/%Y/%m/%d"))
    return out


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS codes (
             code TEXT, platform TEXT, value TEXT, source TEXT,
             detail TEXT DEFAULT '',
             first_seen TEXT, last_seen TEXT,
             PRIMARY KEY (code, platform))"""
    )
    # migrate older DBs missing the detail column
    cols = [r[1] for r in conn.execute("PRAGMA table_info(codes)")]
    if "detail" not in cols:
        conn.execute("ALTER TABLE codes ADD COLUMN detail TEXT DEFAULT ''")
    return conn


def store(items: list[dict]) -> int:
    conn = db()
    n = 0
    for it in items:
        conn.execute(
            """INSERT INTO codes (code, platform, value, source, detail,
                                  first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (code, platform) DO UPDATE SET
                 last_seen = excluded.last_seen,
                 value = CASE WHEN excluded.value != ''
                              THEN excluded.value ELSE codes.value END,
                 detail = CASE WHEN excluded.detail != ''
                              THEN excluded.detail ELSE codes.detail END,
                 source = CASE WHEN codes.source NOT LIKE '%' ||
                                            excluded.source || '%'
                              THEN codes.source || '+' || excluded.source
                              ELSE codes.source END""",
            (it["code"], it["platform"], it["value"], it["source"],
             it.get("detail", ""), it["at"], it["at"]))
        n += 1
    conn.commit()
    conn.close()
    return n


def poll_once() -> dict:
    totals: dict[str, int] = {}
    for name, url, parser, ua in SOURCES:
        # url None = dated archive: poll today + yesterday (UTC)
        targets = archive_urls() if url is None else [url]
        count, ok = 0, True
        for target in targets:
            try:
                count += store(parser(fetch(target, ua)))
            except Exception as exc:
                log.warning("Source %s (%s) failed: %s", name, target, exc)
                ok = False
            time.sleep(2)  # be polite between sources
        totals[name] = count if ok else -1
    log.info("Poll: %s", totals)
    return totals


def poller() -> None:
    while True:
        poll_once()
        time.sleep(POLL_SECONDS)


# ------------------------------------------------------------------- API ---
app = FastAPI(title="leaked.gg code tracker")


def rows_to_json(rows) -> list[dict]:
    now = datetime.now(timezone.utc).timestamp()
    out = []
    for code, platform, value, source, detail, first_seen, last_seen in rows:
        try:
            age_s = now - datetime.fromisoformat(last_seen).timestamp()
        except Exception:
            age_s = 10 ** 9
        out.append({
            "code": code, "platform": platform, "value": value,
            "source": source, "detail": detail,
            "first_seen": first_seen, "last_seen": last_seen,
            "status": "live" if age_s < LIVE_WINDOW_SECONDS else "gone",
        })
    return out


@app.get("/api/codes")
def api_codes(platform: str = Query(default=""),
              source: str = Query(default=""),
              live: int = Query(default=0),
              limit: int = Query(default=200, le=1000)):
    conn = db()
    q = ("SELECT code, platform, value, source, detail, first_seen, "
         "last_seen FROM codes")
    clauses, args = [], []
    if platform:
        clauses.append("platform = ?")
        args.append(platform)
    if source:
        clauses.append("source LIKE ?")
        args.append("%" + source + "%")
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY last_seen DESC LIMIT ?"
    args.append(limit)
    rows = rows_to_json(conn.execute(q, args).fetchall())
    conn.close()
    if live:
        rows = [r for r in rows if r["status"] == "live"]
    return JSONResponse(rows)


@app.get("/api/stats")
def api_stats():
    conn = db()
    total = conn.execute("SELECT COUNT(*) FROM codes").fetchone()[0]
    by_plat = dict(conn.execute(
        "SELECT platform, COUNT(*) FROM codes GROUP BY platform").fetchall())
    by_src = dict(conn.execute(
        "SELECT source, COUNT(*) FROM codes GROUP BY source").fetchall())
    conn.close()
    codes = json.loads(api_codes().body)
    live = sum(1 for c in codes if c["status"] == "live")
    return {"total": total, "live": live, "by_platform": by_plat,
            "by_source": by_src, "poll_seconds": POLL_SECONDS}


DASHBOARD = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>leaked.gg — live code feed</title>
<style>
body{background:#0d1117;color:#e6edf3;font-family:system-ui,sans-serif;
     margin:0;padding:16px}
h1{font-size:20px;margin:0 0 4px}.sub{color:#8b949e;font-size:13px;margin-bottom:12px}
.chips button{background:#161b22;color:#e6edf3;border:1px solid #30363d;
     border-radius:20px;padding:6px 14px;margin:0 6px 12px 0;cursor:pointer}
.chips button.on{background:#1f6feb;border-color:#1f6feb}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px;border-bottom:1px solid #21262d;vertical-align:top}
code{background:#161b22;padding:3px 8px;border-radius:6px;cursor:pointer;
     font-size:13px;white-space:nowrap}
.badge{border-radius:10px;padding:2px 10px;font-size:12px;white-space:nowrap}
.live{background:#1a7f37}.gone{background:#6e7681}
.pl{color:#8b949e;font-size:12px}.dt{color:#8b949e;font-size:11px;max-width:260px}
</style></head><body>
<h1>⚡ leaked.gg — live code feed</h1>
<div class="sub" id="meta">loading…</div>
<div class="chips" id="chips"></div>
<table><thead><tr><th>Code</th><th>Platform</th><th>Value</th><th>Detail</th>
<th>Source</th><th>Status</th><th>Last seen</th></tr></thead>
<tbody id="rows"></tbody></table>
<script>
let plat="";
const PLATS=["","stake.us","stake.com","shuffle","qzino"];
async function load(){
  const r=await fetch("/api/codes?limit=300&platform="+plat);
  const d=await r.json();
  const s=await (await fetch("/api/stats")).json();
  document.getElementById("meta").textContent=
    `${s.live} live · ${s.total} tracked · sources checked every 2 min`;
  document.getElementById("chips").innerHTML=PLATS.map(p=>
    `<button class="${p===plat?'on':''}" data-p="${p}">${p||"all"}</button>`).join("");
  document.querySelectorAll("#chips button").forEach(b=>
    b.onclick=()=>{plat=b.dataset.p;load();});
  document.getElementById("rows").innerHTML=d.map(c=>`<tr>
    <td><code title="tap to copy" onclick="navigator.clipboard.writeText('${c.code}')">${c.code}</code></td>
    <td class="pl">${c.platform}</td><td>${c.value||"—"}</td>
    <td class="dt">${c.detail||"—"}</td><td class="pl">${c.source}</td>
    <td><span class="badge ${c.status}">${c.status}</span></td>
    <td class="pl">${new Date(c.last_seen).toLocaleString()}</td></tr>`).join("");
}
load();setInterval(load,30000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD


@app.on_event("startup")
def startup():
    db().close()
    t = threading.Thread(target=poller, daemon=True)
    t.start()
    log.info("Poller started (%ds).", POLL_SECONDS)


if __name__ == "__main__":
    import uvicorn
    poll_once()  # seed the DB immediately
    uvicorn.run(app, host="0.0.0.0",
                port=int(os.getenv("PORT", "8000")))
