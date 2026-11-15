#!/usr/bin/env python3
"""
Viscaria – bevakning av exportkreditgarantierna (Finnvera och Euler Hermes)
==========================================================================
Bevakar de sidor där Finnvera (Finland) och Euler Hermes / Exportkreditgarantien
(Tyskland) publicerar Kategori A-projekt och beviljade garantier, och postar i
Discord när något som rör Viscaria ändras.

Bakgrund: Kategori A-projekt måste publiceras minst 30 dagar före ett slutligt
garantibeslut (OECD Common Approaches).
  - Euler Hermes publicerade Viscaria 2026-10-16
  - Finnvera publicerade Viscaria 2026-10-28/29

Bevakade sidor
--------------
  1. Finnvera – listan "Guaranteed transactions" (sida 1, nyaste först)
       → ny post som nämner Viscaria/Kiruna/Sverige = troligen garantibeslut
       → ändrad text i Viscaria-kortet (t.ex. att "Category A" försvinner)
  2. Finnvera – Viscarias egen projektsida
       → ändrad text, eller att sidan tas bort
  3. Finnvera – Newsroom
       → nya nyheter som nämner Viscaria/Kiruna
  4. Euler Hermes – "Kategorie-A-Projekte vor Entscheidung"
       → Viscaria-posten ändras eller tas bort
  5. Euler Hermes – "Abgesicherte Projekte" (beviljade garantier, ~3 mån fördröjning)
       → ny post som nämner Schweden/Kiruna/Kupfer/Viscaria
       → kort info när en ny månad publiceras

Första körningen sparar bara nuläget och postar ETT sammanfattande inlägg.
Tillstånd sparas i finansiering_state.json (committas av GitHub Actions).

Om en sida inte går att läsa på 12 körningar i rad (~12 h) postas en varning i
Discord, och ett meddelande när den fungerar igen.

Miljövariabler:
  DISCORD_WEBHOOK_URL   webhook-adress (krävs om inte --dry-run)
  ALERT_MENTION         vem som pingas i inlägg som gäller Viscaria.
                        Standard: "@everyone". Kan sättas till t.ex. "@here",
                        "<@&ROLL-ID>" eller "ingen" för att stänga av pingning.

Inlägg som pingar (🚨): allt som rör Viscaria.
Inlägg som INTE pingar: startinlägget, "ny månad publicerad" och tekniska varningar.

Användning:
  python finansiering_bevakning.py              # normal körning
  python finansiering_bevakning.py --dry-run    # skriv ut i loggen, posta/spara inget
  python finansiering_bevakning.py --test-ping  # posta ett TEST-inlägg med pingning
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

# ─────────────────────────── Inställningar ───────────────────────────

FIN_LIST_URL = ("https://www.finnvera.fi/eng/export-and-internationalisation/"
                "finnvera-as-an-export-finance-provider/guaranteed-transactions")
FIN_VISCARIA_URL = FIN_LIST_URL + "/category-a-projects-under-consideration-viscaria-copper-iron-project"
FIN_NEWS_URL = "https://www.finnvera.fi/eng/finnvera/newsroom"

EKG_A_URL = "https://www.exportkreditgarantien.de/de/nachhaltigkeit/vertrauen/kategorie-a-projekte.html"
EKG_DONE_URL = "https://www.exportkreditgarantien.de/de/nachhaltigkeit/vertrauen/abgesicherte-projekte.html"

# Ord som räknas som "rör Viscaria"
VISCARIA_RE = re.compile(r"viscaria|kiruna|norrbotten", re.I)
# Bredare filter för listor med beviljade garantier (sv/en/fi/de)
SWEDEN_RE = re.compile(r"viscaria|kiruna|norrbotten|\bschweden\b|\bsweden\b|\bruotsi\b|kupfer", re.I)

MONTHS_DE = ("Januar|Februar|März|April|Mai|Juni|Juli|August|"
             "September|Oktober|November|Dezember")
MONTH_RE = re.compile(rf"^({MONTHS_DE})\s+20\d\d$")
EKG_A_HEADING_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}")

STATE_FILE = Path(__file__).with_name("finansiering_state.json")

HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
PAUSE_BETWEEN_POSTS = 2.0
FAIL_ALERT_AFTER = 12          # antal misslyckade körningar i rad innan varning i Discord
MAX_DIFF_LINES = 15

COLOR_ALERT = 0xED4245         # röd – rör Viscaria
COLOR_INFO = 0x5865F2          # blå – information
COLOR_WARN = 0xF2A900          # gul – teknisk varning

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ─────────────────────────── Hjälpfunktioner ───────────────────────────

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def gh_warning(msg: str) -> None:
    print(f"::warning::{msg}", flush=True)


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def cut(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class PageGone(Exception):
    """Sidan svarar 404/410 – den finns inte längre."""


class ParseError(Exception):
    """Sidan gick att hämta men innehållet såg inte ut som väntat."""


def fetch(session: requests.Session, url: str) -> str:
    last: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            r = session.get(url, timeout=HTTP_TIMEOUT)
            if r.status_code in (404, 410):
                raise PageGone(f"HTTP {r.status_code}")
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            r.encoding = r.encoding or r.apparent_encoding
            return r.text
        except PageGone:
            raise
        except requests.RequestException as e:
            last = e
            log(f"  försök {attempt}/{HTTP_RETRIES} mot {url}: {e}")
            time.sleep(4 * attempt)
    raise RuntimeError(f"kunde inte hämta sidan ({last})")


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8,de;q=0.7",
    })
    return s


def main_content(html: str) -> BeautifulSoup:
    """Plockar ut sidans huvudinnehåll utan meny, sidfot, skript och formulär."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "svg", "iframe",
                     "nav", "header", "footer", "aside", "form", "button"]):
        tag.decompose()
    for el in soup.select('[class*="cookie" i], [id*="cookie" i], [class*="breadcrumb" i]'):
        el.decompose()
    main = (soup.find("main") or soup.find(attrs={"role": "main"})
            or soup.find(id=re.compile(r"^(main|content|maincontent)$", re.I))
            or soup.body or soup)
    return main


def text_lines(el) -> list[str]:
    seen, out = set(), []
    for raw in el.get_text("\n").split("\n"):
        line = clean(raw)
        if line and line not in seen:
            seen.add(line)
            out.append(line)
    return out


def digest(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


def diff_lines(old: list[str], new: list[str]) -> tuple[list[str], list[str]]:
    old_set, new_set = set(old), set(new)
    return [l for l in new if l not in old_set], [l for l in old if l not in new_set]


def diff_text(added: list[str], removed: list[str]) -> str:
    parts = []
    if added:
        parts.append("**Nytt:**\n" + "\n".join(f"➕ {cut(l, 300)}" for l in added[:MAX_DIFF_LINES]))
        if len(added) > MAX_DIFF_LINES:
            parts.append(f"…och {len(added) - MAX_DIFF_LINES} rader till")
    if removed:
        parts.append("**Borttaget:**\n" + "\n".join(f"➖ {cut(l, 300)}" for l in removed[:MAX_DIFF_LINES]))
        if len(removed) > MAX_DIFF_LINES:
            parts.append(f"…och {len(removed) - MAX_DIFF_LINES} rader till")
    return "\n".join(parts)


# ─────────────────────────── Discord ───────────────────────────

class Discord:
    def __init__(self, webhook: str | None, dry_run: bool, mention: str):
        self.webhook = webhook
        self.dry_run = dry_run
        self.mention = mention.strip()
        self.session = requests.Session()
        self.count = 0

    def post(self, embed: dict, alert: bool = False) -> None:
        embed.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        payload: dict = {"username": "Viscaria – finansiering", "embeds": [embed]}
        if alert and self.mention:
            payload["content"] = self.mention
            # Tillåt bara den sortens pingning som faktiskt används
            parse = []
            if "@everyone" in self.mention or "@here" in self.mention:
                parse.append("everyone")
            if "<@&" in self.mention:
                parse.append("roles")
            elif "<@" in self.mention:
                parse.append("users")
            payload["allowed_mentions"] = {"parse": parse}
        else:
            payload["allowed_mentions"] = {"parse": []}

        if self.dry_run:
            print("\n──── [DRY-RUN] Discord-inlägg ────")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            self.count += 1
            return
        if self.count:
            time.sleep(PAUSE_BETWEEN_POSTS)
        for _ in range(5):
            r = self.session.post(self.webhook, json=payload, timeout=HTTP_TIMEOUT)
            if r.status_code == 429:
                wait = float(r.json().get("retry_after", 5))
                time.sleep(min(wait, 60) + 0.5)
                continue
            if r.status_code >= 500:
                time.sleep(5)
                continue
            r.raise_for_status()
            self.count += 1
            return
        raise RuntimeError("Discord: kunde inte posta efter flera försök")


def embed(source: str, title: str, url: str, description: str = "",
          color: int = COLOR_INFO, fields: list[tuple[str, str]] | None = None,
          footer: str = "") -> dict:
    e = {
        "author": {"name": source},
        "title": cut(title, 256),
        "url": url,
        "color": color,
    }
    if description:
        e["description"] = cut(description, 4000)
    if fields:
        e["fields"] = [{"name": cut(n, 256), "value": cut(v, 1024), "inline": False}
                       for n, v in fields if v]
    if footer:
        e["footer"] = {"text": cut(footer, 2048)}
    return e


SRC_FIN = "Finnvera (Finland)"
SRC_EKG = "Euler Hermes / Exportkreditgarantien (Tyskland)"


# ─────────────────────────── Källor ───────────────────────────
# Varje källa: fetch_xxx(session) -> data  (kastar vid fel)
#              check_xxx(old, new, discord) -> None  (postar ändringar)
#              summary_xxx(new) -> str  (rad till startinlägget)

# 1) Finnvera – Guaranteed transactions ---------------------------------

def fetch_fin_list(s: requests.Session) -> dict:
    main = main_content(fetch(s, FIN_LIST_URL))
    base_path = urlsplit(FIN_LIST_URL).path.rstrip("/") + "/"
    cards: dict[str, dict] = {}

    def is_entry(a) -> bool:
        p = urlsplit(urljoin(FIN_LIST_URL, a["href"])).path
        return p.startswith(base_path) and len(p) > len(base_path)

    for a in main.find_all("a", href=True):
        if not is_entry(a):
            continue
        url = urljoin(FIN_LIST_URL, a["href"]).split("#")[0].split("?")[0]
        if url in cards:
            continue
        # Klättra uppåt till det största elementet som bara innehåller denna post
        card = a
        while card.parent is not None and card.parent is not main:
            links = {urljoin(FIN_LIST_URL, x["href"]).split("#")[0].split("?")[0]
                     for x in card.parent.find_all("a", href=True) if is_entry(x)}
            if len(links) > 1:
                break
            card = card.parent
        heading = card.find(["h2", "h3", "h4"])
        title = clean(heading.get_text(" ") if heading else a.get_text(" ")) or url.rsplit("/", 1)[-1]
        cards[url] = {"title": title, "text": clean(card.get_text(" "))}

    if not cards:
        raise ParseError("hittade inga projekt i listan")
    return {"cards": cards, "order": list(cards)}


def check_fin_list(old: dict, new: dict, d: Discord) -> None:
    old_cards = old.get("cards", {})
    for url in reversed(new["order"]):          # äldst först
        c = new["cards"][url]
        prev = old_cards.get(url)
        hit = bool(SWEDEN_RE.search(c["text"]))
        if prev is None:
            if hit:
                d.post(embed(
                    SRC_FIN, "🚨 Ny post om Viscaria/Sverige: " + c["title"], url,
                    cut(c["text"], 1500), COLOR_ALERT,
                    footer="Ny post i Finnveras lista över garanterade affärer – kan betyda garantibeslut"),
                    alert=True)
            else:
                log(f"  Finnvera: ny post utan Viscaria-koppling: {c['title']}")
        elif prev["text"] != c["text"] and (hit or SWEDEN_RE.search(prev["text"])):
            d.post(embed(
                SRC_FIN, "🚨 Viscaria-posten i listan har ändrats", url,
                f"**Före:** {cut(prev['text'], 900)}\n\n**Nu:** {cut(c['text'], 900)}",
                COLOR_ALERT,
                footer="Finnvera – Guaranteed transactions"), alert=True)


def summary_fin_list(new: dict) -> str:
    vis = [c for c in new["cards"].values() if VISCARIA_RE.search(c["text"])]
    if vis:
        cat_a = " (Category A – under prövning)" if "category a" in vis[0]["text"].lower() else ""
        return f"Viscaria finns i listan{cat_a}. {len(new['cards'])} poster på sida 1."
    return f"Viscaria syns inte på sida 1 ({len(new['cards'])} poster)."


# 2) Finnvera – Viscarias projektsida ----------------------------------

def fetch_fin_page(s: requests.Session) -> dict:
    try:
        lines = text_lines(main_content(fetch(s, FIN_VISCARIA_URL)))
    except PageGone:
        return {"gone": True, "lines": [], "hash": ""}
    if len(lines) < 3 or not any(VISCARIA_RE.search(l) for l in lines):
        raise ParseError("sidan saknar väntat innehåll om Viscaria")
    return {"gone": False, "lines": lines, "hash": digest(lines)}


def check_fin_page(old: dict, new: dict, d: Discord) -> None:
    if new["gone"] and not old.get("gone"):
        d.post(embed(
            SRC_FIN, "🚨 Viscarias projektsida hos Finnvera har tagits bort", FIN_LIST_URL,
            "Sidan *Category A projects under consideration: Viscaria Copper-Iron Project* svarar "
            "inte längre. Det kan betyda att beslut är fattat och posten flyttats, eller att "
            "ansökan dragits tillbaka. Kontrollera listan.", COLOR_ALERT), alert=True)
        return
    if old.get("gone") and not new["gone"]:
        d.post(embed(SRC_FIN, "Viscarias projektsida hos Finnvera finns igen", FIN_VISCARIA_URL,
                     cut("\n".join(new["lines"][:10]), 1500), COLOR_INFO))
        return
    if not new["gone"] and old.get("hash") != new["hash"]:
        added, removed = diff_lines(old.get("lines", []), new["lines"])
        if added or removed:
            d.post(embed(SRC_FIN, "🚨 Viscarias projektsida hos Finnvera har ändrats",
                         FIN_VISCARIA_URL, diff_text(added, removed), COLOR_ALERT), alert=True)


def summary_fin_page(new: dict) -> str:
    if new["gone"]:
        return "Projektsidan svarar inte (borttagen)."
    pub = next((l for l in new["lines"] if re.search(r"date of publication", l, re.I)), "")
    return "Projektsidan finns." + (f" {pub}" if pub else "")


# 3) Finnvera – Newsroom ------------------------------------------------

def fetch_fin_news(s: requests.Session) -> dict:
    main = main_content(fetch(s, FIN_NEWS_URL))
    base_path = urlsplit(FIN_NEWS_URL).path.rstrip("/") + "/"
    items: dict[str, dict] = {}
    for a in main.find_all("a", href=True):
        url = urljoin(FIN_NEWS_URL, a["href"]).split("#")[0].split("?")[0]
        p = urlsplit(url).path
        deeper = p.startswith(base_path) and p.count("/") >= base_path.count("/") + 1
        # Viscaria-nyheter kan länka till andra delar av sajten – ta med dem ändå
        if url in items or not (deeper or VISCARIA_RE.search(a.get_text(" "))):
            continue
        box = a.parent if a.parent is not None and a.parent is not main else a
        title = clean(a.get_text(" ")) or clean(box.get_text(" "))
        if title:
            items[url] = {"title": title, "text": clean(box.get_text(" "))}
    if not items:
        raise ParseError("hittade inga nyheter")
    return {"items": items}


def check_fin_news(old: dict, new: dict, d: Discord) -> None:
    seen = old.get("items", {})
    for url, it in new["items"].items():
        if url in seen:
            continue
        if VISCARIA_RE.search(it["text"]) or VISCARIA_RE.search(url):
            d.post(embed(SRC_FIN, "📰 Finnvera-nyhet om Viscaria: " + it["title"], url,
                         cut(it["text"], 1500), COLOR_ALERT,
                         footer="Finnvera – Newsroom"), alert=True)


def summary_fin_news(new: dict) -> str:
    n = sum(1 for it in new["items"].values() if VISCARIA_RE.search(it["text"]))
    return f"{len(new['items'])} nyheter på sidan, varav {n} om Viscaria."


# 4) Euler Hermes – Kategorie-A-Projekte vor Entscheidung ---------------

def fetch_ekg_a(s: requests.Session) -> dict:
    main = main_content(fetch(s, EKG_A_URL))
    sections: dict[str, str] = {}
    for h in main.find_all(["h3", "h4"]):
        heading = clean(h.get_text(" "))
        if not EKG_A_HEADING_RE.match(heading):
            continue
        body = []
        for sib in h.find_next_siblings():
            if sib.name in ("h1", "h2", "h3", "h4"):
                break
            t = clean(sib.get_text(" "))
            if t:
                body.append(t)
        if not body and h.parent is not None and h.parent is not main:
            # Rubriken ligger ensam i en egen behållare – ta behållarens text
            body.append(clean(h.parent.get_text(" ")).replace(heading, "", 1).strip())
        sections[heading] = " ".join(body)
    if not sections:
        raise ParseError("hittade inga daterade projekt")
    return {"sections": sections}


def _ekg_viscaria(sections: dict) -> dict:
    return {h: t for h, t in sections.items() if VISCARIA_RE.search(h) or VISCARIA_RE.search(t)}


def check_ekg_a(old: dict, new: dict, d: Discord) -> None:
    old_v = _ekg_viscaria(old.get("sections", {}))
    new_v = _ekg_viscaria(new["sections"])
    for h, t in new_v.items():
        if h not in old_v:
            d.post(embed(SRC_EKG, "🚨 Ny Viscaria-post bland A-projekt före beslut: " + h, EKG_A_URL,
                         cut(t, 1500), COLOR_ALERT), alert=True)
        elif old_v[h] != t:
            d.post(embed(SRC_EKG, "🚨 Viscaria-posten bland A-projekt har ändrats", EKG_A_URL,
                         f"**{h}**\n\n**Före:** {cut(old_v[h], 900)}\n\n**Nu:** {cut(t, 900)}",
                         COLOR_ALERT), alert=True)
    for h in old_v:
        if h not in new_v:
            d.post(embed(
                SRC_EKG, "🚨 Viscaria har tagits bort från listan \"A-Projekte vor Entscheidung\"",
                EKG_A_URL,
                f"Posten **{h}** finns inte längre på sidan.\n\nSidan visar projekt *före* beslut, "
                "så det kan betyda att beslut är fattat – men posten kan också ha flyttats till "
                "arkivet (sker t.ex. vid årsskiftet). Beviljade garantier syns först på sidan "
                "*Abgesicherte Projekte*, med ungefär tre månaders fördröjning.",
                COLOR_ALERT), alert=True)


def summary_ekg_a(new: dict) -> str:
    v = _ekg_viscaria(new["sections"])
    if v:
        return f"Viscaria listad: *{next(iter(v))}*."
    return f"Viscaria syns inte bland {len(new['sections'])} projekt."


# 5) Euler Hermes – Abgesicherte Projekte -------------------------------

def fetch_ekg_done(s: requests.Session) -> dict:
    lines = text_lines(main_content(fetch(s, EKG_DONE_URL)))
    months = [l for l in lines if MONTH_RE.match(l)]
    if not months:
        raise ParseError("hittade inga månadsrubriker")
    return {"lines": lines, "months": months}


def _context(lines: list[str], idx: int) -> str:
    """Raderna runt en träff, avgränsat av närmaste månadsrubrik."""
    start = idx
    while start > 0 and not MONTH_RE.match(lines[start]) and idx - start < 12:
        start -= 1
    end = min(len(lines), idx + 12)
    for j in range(idx + 1, end):
        if MONTH_RE.match(lines[j]):
            end = j
            break
    return "\n".join(lines[start:end])


def check_ekg_done(old: dict, new: dict, d: Discord) -> None:
    old_set = set(old.get("lines", []))
    hits = [i for i, l in enumerate(new["lines"]) if l not in old_set and SWEDEN_RE.search(l)]
    posted_blocks = set()
    for i in hits:
        block = _context(new["lines"], i)
        if block in posted_blocks:
            continue
        posted_blocks.add(block)
        d.post(embed(
            SRC_EKG, "🚨 Beviljad garanti som nämner Sverige/koppar", EKG_DONE_URL,
            cut(block, 3000), COLOR_ALERT,
            footer="Euler Hermes publicerar beviljade affärer med ca tre månaders fördröjning, "
                   "och bara om exportören godkänner det"), alert=True)

    new_months = [m for m in new["months"] if m not in old.get("months", [])]
    if new_months and not hits:
        d.post(embed(
            SRC_EKG, "Ny månad publicerad bland beviljade garantier: " + ", ".join(new_months),
            EKG_DONE_URL, "Ingen post nämner Viscaria, Kiruna, Sverige eller koppar.",
            COLOR_INFO,
            footer="Sidan uppdateras månadsvis med ca tre månaders fördröjning"))


def summary_ekg_done(new: dict) -> str:
    hit = any(SWEDEN_RE.search(l) for l in new["lines"])
    latest = new["months"][0] if new["months"] else "–"
    return (f"Senaste publicerade månad: {latest}. "
            + ("⚠️ Någon post nämner Sverige/koppar redan." if hit else "Inget om Viscaria."))


# ─────────────────────────── Huvudprogram ───────────────────────────

SOURCES = [
    # nyckel, namn, url, fetch, check, summary
    ("fin_list", "Finnvera – Guaranteed transactions", FIN_LIST_URL,
     fetch_fin_list, check_fin_list, summary_fin_list),
    ("fin_page", "Finnvera – Viscarias projektsida", FIN_VISCARIA_URL,
     fetch_fin_page, check_fin_page, summary_fin_page),
    ("fin_news", "Finnvera – Newsroom", FIN_NEWS_URL,
     fetch_fin_news, check_fin_news, summary_fin_news),
    ("ekg_a", "Euler Hermes – A-Projekte vor Entscheidung", EKG_A_URL,
     fetch_ekg_a, check_ekg_a, summary_ekg_a),
    ("ekg_done", "Euler Hermes – Abgesicherte Projekte", EKG_DONE_URL,
     fetch_ekg_done, check_ekg_done, summary_ekg_done),
]


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"version": 1, "kallor": {}, "fel": {}}


def save_state(path: Path, state: dict) -> None:
    state["senast_kontrollerad"] = today()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Bevakar Finnvera och Euler Hermes för Viscaria")
    ap.add_argument("--dry-run", action="store_true", help="skriv ut inlägg, posta och spara inget")
    ap.add_argument("--test-ping", action="store_true",
                    help="posta ett TEST-inlägg med pingning (rör inte state-filen)")
    ap.add_argument("--state", type=Path, default=STATE_FILE)
    args = ap.parse_args()

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook and not args.dry_run:
        log("FEL: DISCORD_WEBHOOK_URL saknas (eller kör med --dry-run)")
        return 2

    mention = os.environ.get("ALERT_MENTION", "").strip() or "@everyone"
    if mention.lower() in ("ingen", "none", "off", "av"):
        mention = ""
    discord = Discord(webhook, args.dry_run, mention)

    if args.test_ping:
        discord.post(embed(
            SRC_FIN, "🧪 TEST – 🚨 Ny post om Viscaria/Sverige: Finnvera guarantees Viscaria", FIN_LIST_URL,
            "Det här är ett **testinlägg**. Så här ser det ut när bevakningen hittar något om "
            "Viscaria. Ingen riktig ändring har hittats.", COLOR_ALERT,
            footer="Testinlägg från Viscaria – finansiering"), alert=True)
        log(f"Testinlägg skickat (pingar: {mention or 'ingen'})")
        return 0

    state = load_state(args.state)
    state.setdefault("kallor", {})
    state.setdefault("fel", {})
    session = new_session()
    started: list[str] = []

    for key, name, url, fetch_fn, check_fn, summary_fn in SOURCES:
        fel = state["fel"].setdefault(key, {"i_rad": 0, "varnat": False})
        try:
            data = fetch_fn(session)
        except Exception as e:
            fel["i_rad"] += 1
            gh_warning(f"{name}: {e} ({fel['i_rad']} gång(er) i rad)")
            if fel["i_rad"] >= FAIL_ALERT_AFTER and not fel["varnat"]:
                discord.post(embed(
                    "Viscaria – finansiering", f"⚠️ {name} går inte att läsa", url,
                    f"Sidan har inte kunnat läsas de senaste {fel['i_rad']} körningarna.\n"
                    f"Senaste fel: `{cut(str(e), 300)}`\n\n"
                    "Bevakningen av den här sidan står still tills det fungerar igen. "
                    "Om felet säger att innehållet saknas kan sidan ha gjorts om.",
                    COLOR_WARN))
                fel["varnat"] = True
            continue

        if fel["varnat"]:
            discord.post(embed("Viscaria – finansiering", f"✅ {name} fungerar igen", url,
                               color=COLOR_INFO))
        state["fel"][key] = {"i_rad": 0, "varnat": False}

        old = state["kallor"].get(key)
        if old is None:
            started.append(f"**{name}**\n{summary_fn(data)}")
            log(f"{name}: första körningen, sparar nuläget")
        else:
            try:
                check_fn(old, data, discord)
            except Exception as e:
                gh_warning(f"{name}: kunde inte jämföra ({e})")
                continue
            log(f"{name}: kontrollerad")
        state["kallor"][key] = data

        if not args.dry_run:
            save_state(args.state, state)     # spara efter varje källa

    if started:
        discord.post(embed(
            "Viscaria – finansiering", "Bevakningen av exportkreditgarantierna har startat",
            FIN_VISCARIA_URL,
            "\n\n".join(started) + "\n\nFrån och med nu postas bara förändringar som rör Viscaria.",
            COLOR_INFO,
            footer="Kategori A-projekt publiceras minst 30 dagar före garantibeslut "
                   "(Euler Hermes 2026-10-16, Finnvera 2026-10-28)"))

    if not args.dry_run:
        save_state(args.state, state)
    log(f"Klart: {discord.count} inlägg")
    return 0


if __name__ == "__main__":
    sys.exit(main())
