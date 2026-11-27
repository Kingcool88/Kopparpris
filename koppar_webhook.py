"""
Kopparpris till Discord via webhook – körs av GitHub Actions var 10:e minut.

- Hämtar kopparpris (COMEX HG=F via Yahoo Finance), räknar om USD/lb -> USD/ton.
- Ritar tre grafer: senaste dygnet, senaste veckan och senaste månaden.
- Varje graf är ETT eget Discord-meddelande som redigeras vid varje körning.

Miljövariabler (sätts i GitHub under Settings -> Secrets and variables -> Actions):
  DISCORD_WEBHOOK_URL  (Secret)    – webhook-länken från Discord
  MESSAGE_ID           (Variable)  – ID på dygnsgrafens meddelande
  MESSAGE_ID_WEEK      (Variable)  – ID på veckografens meddelande
  MESSAGE_ID_MONTH     (Variable)  – ID på månadsgrafens meddelande
  RUN_SOURCE           (sätts av workflowet) – "cron" när cron-job.org startar körningen
  HISTORY_HOURS        (Variable)  – valfri, dygnsgrafens längd i timmar, standard 24

Saknas ett MESSAGE_ID skapas det meddelandet vid nästa MANUELLA körning
(Run workflow), och ID:t visas överst på körningens sida i GitHub.
Automatiska körningar skapar aldrig nya meddelanden – de uppdaterar bara de som finns.
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
# Automatiska körningar: GitHubs schema ("schedule") eller cron-job.org ("cron").
# Bara MANUELLA körningar får skapa nya meddelanden.
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

VECKODAGAR = ["mån", "tis", "ons", "tors", "fre", "lör", "sön"]
MANADER = ["jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]

# De tre graferna, i den ordning de skapas (och ligger) i kanalen.
#   interval/range: vad som hämtas från Yahoo
#   hours:          hur långt bakåt grafen visar, räknat från senaste noteringen
PERIODS = [
    {
        "key": "dag",
        "var": "MESSAGE_ID",
        "label": f"senaste {HISTORY_HOURS} h" if HISTORY_HOURS != 24 else "senaste dygnet",
        "hours": HISTORY_HOURS,
        "interval": "5m",
        "range": "5d" if HISTORY_HOURS <= 96 else "1mo",
        "axis": "tid",
    },
    {
        "key": "vecka",
        "var": "MESSAGE_ID_WEEK",
        "label": "senaste veckan",
        "hours": 7 * 24,
        "interval": "30m",
        "range": "1mo",
        "axis": "veckodag",
    },
    {
        "key": "manad",
        "var": "MESSAGE_ID_MONTH",
        "label": "senaste månaden",
        "hours": 30 * 24,
        "interval": "60m",
        "range": "3mo",
        "axis": "datum",
    },
]
for _p in PERIODS:
    _p["message_id"] = os.environ.get(_p["var"], "").strip()
    _p["filename"] = f"koppar_{_p['key']}.png"


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
def fetch_copper(interval: str, rng: str, hours: int) -> dict:
    params = {"interval": interval, "range": rng, "includePrePost": "false"}
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

                cutoff = points[-1][0] - timedelta(hours=hours)
                points = [p for p in points if p[0] >= cutoff]

                # Pris och tid tas från SENASTE PUNKTEN i kursserien, så att rubrikpris,
                # graf, högsta/lägsta och förändring alltid hänger ihop. Yahoos separata
                # fält "regularMarketPrice" kan visa något annat (t.ex. vid kontraktsbyte
                # eller helgdagar) – det loggas bara som varning.
                price = points[-1][1]
                market_time = points[-1][0]

                price_lb = meta.get("regularMarketPrice")
                if price_lb:
                    meta_price = price_lb * LB_PER_TON
                    diff = abs(meta_price - price) / price * 100
                    if diff > 1.0:
                        gh_warning(
                            f"Yahoo-fältet regularMarketPrice ({fmt(meta_price)} $/t) skiljer sig "
                            f"{fmt(diff, 1)} % från senaste noteringen i kursserien "
                            f"({fmt(price)} $/t) – kursserien används ({interval})."
                        )
                return {"price": price, "points": points, "market_time": market_time}
            except Exception as exc:
                last_err = f"{host}: {exc}"
        time.sleep(5 * (attempt + 1))

    raise RuntimeError(last_err)


# ---------------------------------------------------------------------------
# Graf + embed
# ---------------------------------------------------------------------------
def _day_label(d: datetime, kind: str) -> str:
    if kind == "veckodag":
        return f"{VECKODAGAR[d.weekday()]} {d.day}/{d.month}"
    return f"{d.day} {MANADER[d.month - 1]}"


def _trading_day_ticks(times: list, kind: str, max_ticks: int = 7) -> tuple[list, list]:
    """Positioner + etiketter vid första noteringen varje handelsdag."""
    starts = [i for i, t in enumerate(times) if i == 0 or t.date() != times[i - 1].date()]
    # Minsta avstånd mellan etiketter, så att korta dagar (t.ex. en bit fredag
    # längst till vänster) inte ger etiketter ovanpå varandra
    min_gap = len(times) / (max_ticks + 1)
    kept: list = []
    for i in reversed(starts):                       # nyaste dagarna prioriteras
        if not kept or kept[-1] - i >= min_gap:
            kept.append(i)
    kept.reverse()
    return kept, [_day_label(times[i], kind) for i in kept]


def build_chart(points: list, up: bool, period: dict) -> bytes:
    times = [t.astimezone(LOCAL_TZ) for t, _ in points]
    vals = [v for _, v in points]

    bg = "#2b2d31"
    text = "#b5bac1"
    color = "#57F287" if up else "#ED4245"

    fig, ax = plt.subplots(figsize=(8, 3.6), dpi=120)
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)

    if period["axis"] == "tid":
        # Dygnsgrafen: riktig tidsaxel
        xs = times
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=LOCAL_TZ))
    else:
        # Vecka/månad: en punkt per notering, så att helger och stängd börs
        # inte ritas som långa raka streck
        xs = list(range(len(vals)))
        ticks, labels = _trading_day_ticks(times, period["axis"])
        ax.set_xticks(ticks)
        ax.set_xticklabels(labels)
        ax.set_xlim(-0.5, len(vals) - 0.5)

    ax.plot(xs, vals, color=color, linewidth=2 if period["axis"] == "tid" else 1.6)
    ax.fill_between(xs, vals, min(vals), color=color, alpha=0.15)
    ax.scatter([xs[-1]], [vals[-1]], color=color, s=25, zorder=3)

    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: fmt(v)))
    ax.tick_params(colors=text, labelsize=9)
    ax.grid(color="#3f4147", linewidth=0.6)
    for spine in ax.spines.values():
        spine.set_visible(False)

    ax.set_ylabel("USD/ton", color=text, fontsize=9)
    ax.set_title(f"Koppar – {period['label']}", color="#f2f3f5", fontsize=11)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=bg)
    plt.close(fig)
    return buf.getvalue()


def build_embed(data: dict, period: dict) -> tuple[dict, bytes]:
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

    title = "Kopparpris (COMEX)" if period["key"] == "dag" else f"Kopparpris (COMEX) – {period['label']}"
    embed = {
        "title": title,
        "description": (
            f"## {fmt(price)} USD/ton\n"
            f"{arrow} {sign}{fmt(abs(change))} USD ({sign}{fmt(abs(pct), 2)} %) "
            f"{period['label']}"
        ),
        "color": 0x57F287 if up else 0xED4245,
        "fields": fields,
        "image": {"url": f"attachment://{period['filename']}"},
        "footer": {"text": "Uppdateras ca var 10:e minut • Källa: Yahoo Finance (~10 min fördröjt)"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return embed, build_chart(points, up, period)


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
def discord_request(method: str, url: str, payload: dict, png: bytes, filename: str) -> requests.Response:
    """Skickar multipart (embed + bild). Hanterar Discords rate limit."""
    for _ in range(3):
        r = requests.request(
            method,
            url,
            data={"payload_json": json.dumps(payload)},
            files={"files[0]": (filename, png, "image/png")},
            timeout=30,
        )
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 2))
            time.sleep(min(wait, 30) + 0.5)
            continue
        return r
    return r


def update_period(period: dict, created: list) -> bool:
    """Uppdaterar (eller skapar) en grafs meddelande. Returnerar False vid fel."""
    name = period["label"]
    msg_id = period["message_id"]

    # Utan ID skapas meddelandet bara vid MANUELL körning,
    # annars skulle schemat posta ett nytt inlägg var 10:e minut.
    if not msg_id and AUTOMATIC:
        gh_warning(f"{period['var']} är inte satt ({name}) – kör workflowet manuellt en gång (Run workflow).")
        return True

    try:
        data = fetch_copper(period["interval"], period["range"], period["hours"])
    except Exception as exc:
        # Inget fel: inlägget ligger kvar med förra priset och tidsstämpeln
        # visar hur gammalt det är. Undviker felmejl från GitHub vid tillfälliga avbrott.
        gh_warning(f"Kunde inte hämta kopparpris för {name} denna gång: {exc}")
        return True

    embed, png = build_embed(data, period)
    payload = {
        "embeds": [embed],
        "attachments": [{"id": 0, "filename": period["filename"]}],
        "allowed_mentions": {"parse": []},
    }

    if msg_id:
        r = discord_request("PATCH", f"{WEBHOOK_URL}/messages/{msg_id}", payload, png, period["filename"])
        if r.status_code == 404:
            gh_error(
                f"Meddelandet för {name} finns inte längre (raderat?). Töm variabeln "
                f"{period['var']} och kör workflowet manuellt för att skapa ett nytt."
            )
            return False
        if not r.ok:
            gh_error(f"Discord svarade {r.status_code} för {name}: {r.text[:300]}")
            return False
        print(f"Uppdaterat ({name}): {fmt(data['price'])} USD/ton")
        return True

    # Manuell körning utan ID: skapa meddelandet
    if WEBHOOK_NAME:
        payload["username"] = WEBHOOK_NAME
    r = discord_request("POST", f"{WEBHOOK_URL}?wait=true", payload, png, period["filename"])
    if not r.ok:
        gh_error(f"Discord svarade {r.status_code} för {name}: {r.text[:300]}")
        return False

    new_id = r.json()["id"]
    created.append((period["var"], name, new_id))
    gh_notice(f"Nytt meddelande för {name} skapat. Lägg in variabeln {period['var']} = {new_id}")
    return True


def main() -> int:
    if not WEBHOOK_URL.startswith("https://") or "/api/webhooks/" not in WEBHOOK_URL:
        gh_error("DISCORD_WEBHOOK_URL saknas eller är felaktig (lägg in den som Secret).")
        return 1

    created: list = []
    ok = True
    for i, period in enumerate(PERIODS):
        if i:
            time.sleep(1)  # snällt mot både Yahoo och Discord
        ok = update_period(period, created) and ok

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if created and summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## Nya prismeddelanden skapade\n\n"
                    "Lägg in dessa som **Variables** (Settings → Secrets and variables → "
                    "Actions → Variables):\n\n| Namn | Graf | Värde |\n|---|---|---|\n")
            for var, name, new_id in created:
                f.write(f"| `{var}` | {name} | `{new_id}` |\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
