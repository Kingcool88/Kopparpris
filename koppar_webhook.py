"""
Kopparpris till Discord via webhook – körs av GitHub Actions var 10:e minut.

- Hämtar kopparpris (COMEX HG=F via Yahoo Finance), räknar om USD/lb -> USD/ton.
- Ritar graf över senaste HISTORY_HOURS timmar.
- Redigerar ETT och samma Discord-meddelande (MESSAGE_ID) varje körning.

Miljövariabler (sätts i GitHub under Settings -> Secrets and variables -> Actions):
  DISCORD_WEBHOOK_URL  (Secret)    – webhook-länken från Discord
  MESSAGE_ID           (Variable)  – ID på prismeddelandet (fås vid första manuella körningen)
  RUN_SOURCE           (sätts av workflowet) – "cron" när cron-job.org startar körningen
  HISTORY_HOURS        (Variable)  – valfri, standard 24
"""

import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

# ---------------------------------------------------------------------------
# Inställningar
# ---------------------------------------------------------------------------
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip().split("?")[0].rstrip("/")
MESSAGE_ID = os.environ.get("MESSAGE_ID", "").strip()
# Automatiska körningar: GitHubs schema ("schedule") eller cron-job.org ("cron").
# Bara MANUELLA körningar får skapa ett nytt meddelande.
EVENT = (os.environ.get("RUN_SOURCE") or os.environ.get("GITHUB_EVENT_NAME") or "manuell").strip()
AUTOMATIC = EVENT in ("schedule", "cron")
HISTORY_HOURS = max(1, int(os.environ.get("HISTORY_HOURS") or 24))
WEBHOOK_NAME = os.environ.get("WEBHOOK_NAME", "Kopparpris").strip()
LOCAL_TZ = ZoneInfo(os.environ.get("TIMEZONE") or "Europe/Stockholm")

LB_PER_TON = 2204.62262
SYMBOL = "HG=F"
YAHOO_HOSTS = ["query1", "query2"]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
STALE_MINUTES = 30


def gh_notice(text: str) -> None:
    """Syns som en ruta överst på körningens sida i GitHub."""
    print(f"::notice::{text}")


def gh_warning(text: str) -> None:
    print(f"::warning::{text}")


def gh_error(text: str) -> None:
    print(f"::error::{text}")


def fmt(value: float, decimals: int = 0) -> str:
    """9845.3 -> '9 845'."""
    s = f"{value:,.{decimals}f}"
    return s.replace(",", " ").replace(".", ",")


# ---------------------------------------------------------------------------
# Hämta pris
# ---------------------------------------------------------------------------
def fetch_copper() -> dict:
    params = {"interval": "5m", "range": "5d", "includePrePost": "false"}
    headers = {"User-Agent": USER_AGENT}
    last_err = "okänt fel"

    for attempt in range(3):
        for host in YAHOO_HOSTS:
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{SYMBOL}"
            try:
                r = requests.get(url, params=params, headers=headers, timeout=20)
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code} från {host}"
                    continue

                result = r.json()["chart"]["result"][0]
                meta = result["meta"]
                timestamps = result.get("timestamp") or []
                closes = result["indicators"]["quote"][0].get("close") or []

                points = [
                    (datetime.fromtimestamp(t, tz=timezone.utc), c * LB_PER_TON)
                    for t, c in zip(timestamps, closes)
                    if c is not None
                ]
                if not points:
                    last_err = f"Ingen prisdata från {host}"
                    continue

                cutoff = points[-1][0] - timedelta(hours=HISTORY_HOURS)
                points = [p for p in points if p[0] >= cutoff]

                price_lb = meta.get("regularMarketPrice")
                price = price_lb * LB_PER_TON if price_lb else points[-1][1]

                mt = meta.get("regularMarketTime")
                market_time = (
                    datetime.fromtimestamp(mt, tz=timezone.utc) if mt else points[-1][0]
                )
                return {"price": price, "points": points, "market_time": market_time}
            except Exception as exc:
                last_err = f"{host}: {exc}"
        time.sleep(5 * (attempt + 1))

    raise RuntimeError(last_err)


# ---------------------------------------------------------------------------
# Graf + embed
# ---------------------------------------------------------------------------
def build_chart(points: list, up: bool) -> bytes:
    times = [t.astimezone(LOCAL_TZ) for t, _ in points]
    vals = [v for _, v in points]

    bg = "#2b2d31"
    text = "#b5bac1"
    color = "#57F287" if up else "#ED4245"

    fig, ax = plt.subplots(figsize=(8, 3.6), dpi=120)
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)

    ax.plot(times, vals, color=color, linewidth=2)
    ax.fill_between(times, vals, min(vals), color=color, alpha=0.15)
    ax.scatter([times[-1]], [vals[-1]], color=color, s=25, zorder=3)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=LOCAL_TZ))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: fmt(v)))
    ax.tick_params(colors=text, labelsize=9)
    ax.grid(color="#3f4147", linewidth=0.6)
    for spine in ax.spines.values():
        spine.set_visible(False)

    ax.set_ylabel("USD/ton", color=text, fontsize=9)
    ax.set_title(f"Koppar – senaste {HISTORY_HOURS} h", color="#f2f3f5", fontsize=11)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=bg)
    plt.close(fig)
    return buf.getvalue()


def build_embed(data: dict) -> tuple[dict, bytes]:
    points = data["points"]
    price = data["price"]
    start = points[0][1]
    change = price - start
    pct = (change / start * 100) if start else 0.0
    up = change >= 0

    high = max(v for _, v in points)
    low = min(v for _, v in points)
    arrow = "🟢 ▲" if up else "🔴 ▼"
    sign = "+" if up else "−"

    fields = [
        {"name": "Högsta", "value": f"{fmt(high)} $/t", "inline": True},
        {"name": "Lägsta", "value": f"{fmt(low)} $/t", "inline": True},
        {"name": "USD/lb", "value": fmt(price / LB_PER_TON, 4), "inline": True},
    ]

    age = datetime.now(timezone.utc) - data["market_time"]
    if age > timedelta(minutes=STALE_MINUTES):
        local_mt = data["market_time"].astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
        fields.append(
            {"name": "⏸ Marknaden stängd", "value": f"Senaste notering {local_mt}", "inline": False}
        )

    embed = {
        "title": "Kopparpris (COMEX)",
        "description": (
            f"## {fmt(price)} USD/ton\n"
            f"{arrow} {sign}{fmt(abs(change))} USD ({sign}{fmt(abs(pct), 2)} %) "
            f"senaste {HISTORY_HOURS} h"
        ),
        "color": 0x57F287 if up else 0xED4245,
        "fields": fields,
        "image": {"url": "attachment://koppar.png"},
        "footer": {"text": "Uppdateras ca var 10:e minut • Källa: Yahoo Finance (~10 min fördröjt)"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return embed, build_chart(points, up)


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
def discord_request(method: str, url: str, payload: dict, png: bytes) -> requests.Response:
    """Skickar multipart (embed + bild). Hanterar Discords rate limit."""
    for _ in range(3):
        r = requests.request(
            method,
            url,
            data={"payload_json": json.dumps(payload)},
            files={"files[0]": ("koppar.png", png, "image/png")},
            timeout=30,
        )
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 2))
            time.sleep(min(wait, 30) + 0.5)
            continue
        return r
    return r


def main() -> int:
    if not WEBHOOK_URL.startswith("https://") or "/api/webhooks/" not in WEBHOOK_URL:
        gh_error("DISCORD_WEBHOOK_URL saknas eller är felaktig (lägg in den som Secret).")
        return 1

    # Utan MESSAGE_ID skapas bara ett nytt meddelande vid MANUELL körning,
    # annars skulle schemat posta ett nytt inlägg var 10:e minut.
    if not MESSAGE_ID and AUTOMATIC:
        gh_warning("MESSAGE_ID är inte satt – kör workflowet manuellt en gång (Run workflow).")
        return 0

    try:
        data = fetch_copper()
    except Exception as exc:
        # Avsluta utan fel: inlägget ligger kvar med förra priset och tidsstämpeln
        # visar hur gammalt det är. Undviker felmejl från GitHub vid tillfälliga avbrott.
        gh_warning(f"Kunde inte hämta kopparpris denna gång: {exc}")
        return 0

    embed, png = build_embed(data)
    payload = {
        "embeds": [embed],
        "attachments": [{"id": 0, "filename": "koppar.png"}],
        "allowed_mentions": {"parse": []},
    }

    if MESSAGE_ID:
        r = discord_request("PATCH", f"{WEBHOOK_URL}/messages/{MESSAGE_ID}", payload, png)
        if r.status_code == 404:
            gh_error(
                "Prismeddelandet finns inte längre (raderat?). Töm variabeln MESSAGE_ID "
                "och kör workflowet manuellt för att skapa ett nytt."
            )
            return 1
        if not r.ok:
            gh_error(f"Discord svarade {r.status_code}: {r.text[:300]}")
            return 1
        print(f"Uppdaterat: {fmt(data['price'])} USD/ton")
        return 0

    # Första körningen (manuell): skapa meddelandet
    if WEBHOOK_NAME:
        payload["username"] = WEBHOOK_NAME
    r = discord_request("POST", f"{WEBHOOK_URL}?wait=true", payload, png)
    if not r.ok:
        gh_error(f"Discord svarade {r.status_code}: {r.text[:300]}")
        return 1

    new_id = r.json()["id"]
    gh_notice(f"Nytt prismeddelande skapat. Lägg in variabeln MESSAGE_ID = {new_id}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(
                "## Prismeddelandet är skapat\n\n"
                f"Lägg in detta som **Variable** med namnet `MESSAGE_ID`:\n\n```\n{new_id}\n```\n"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
