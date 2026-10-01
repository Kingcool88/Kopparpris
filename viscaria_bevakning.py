#!/usr/bin/env python3
"""
Viscaria-bevakning
==================
Bevakar två myndighetsdiarier och postar nyheter till en Discord-kanal via webhook.

  1. Länsstyrelsen i Norrbottens län (diarium.lansstyrelsen.se)
     - nya ärenden med "Viscaria" i ärenderubriken (alla kommuner)
     - nya handlingar i bevakade ärenden (ny rad i "Handlingar")
     - ändrad status / beslutsdatum

  2. Bergsstatens diarium (apps.sgu.se/bergsstatens-diarium)
     - nya ärenden med "Viscaria" i beskrivningen
     - ändrat beslutsdatum / beslutstyp

Tillstånd sparas i state.json (committas tillbaka av GitHub Actions).

Första körningen per källa: postar ett inlägg per befintligt ärende, men
markerar befintliga handlingar som redan sedda (postas inte).

Miljövariabler:
  DISCORD_WEBHOOK_URL   webhook-adress (krävs om inte --dry-run)

Användning:
  python viscaria_bevakning.py              # normal körning
  python viscaria_bevakning.py --dry-run    # skriv ut i terminalen, posta/spara inget
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ─────────────────────────── Inställningar ───────────────────────────

LST_BASE = "https://diarium.lansstyrelsen.se/"
LST_CASE_URL = "https://diarium.lansstyrelsen.se/Case/CaseInfo.aspx?caseID={case_id}"
LST_DIARY_ID = "21"            # Länsstyrelsen i Norrbottens län
LST_TITLE = "Viscaria"         # söks i ärenderubrik (delsträng)
LST_MUNICIPALITY = "-1"        # -1 = alla kommuner (många Viscaria-ärenden saknar kommun)
LST_PREFIX = "ctl00$SearchPlaceHolder$CaseSearch$"

BGS_REST = "https://apps.sgu.se/bergsstatens-diarium-search/rest"
BGS_PAGE = "https://apps.sgu.se/bergsstatens-diarium/"
BGS_TERM = "Viscaria"          # söks i beskrivning (delsträng)

STATE_FILE = Path(__file__).with_name("state.json")

HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
PAUSE_BETWEEN_CASES = 0.7      # sekunder mellan ärendesidor (snällt mot servern)
PAUSE_BETWEEN_POSTS = 2.0      # sekunder mellan Discord-inlägg
MAX_DOCS_SEPARATE = 8          # fler nya handlingar än så i ett ärende → ett samlat inlägg

COLOR_LST = 0x005EB8           # Länsstyrelsen-blå
COLOR_BGS = 0xC8102E           # röd
COLOR_STATUS = 0xF2A900        # gul (statusändring)

USER_AGENT = "Mozilla/5.0 (compatible; Viscaria-bevakning/1.0; privat diariebevakning)"


# ─────────────────────────── Hjälpfunktioner ───────────────────────────

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def cut(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


def http(session: requests.Session, method: str, url: str, **kw) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            r = session.request(method, url, timeout=HTTP_TIMEOUT, **kw)
            if r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except (requests.RequestException,) as e:
            last_exc = e
            log(f"  försök {attempt}/{HTTP_RETRIES} misslyckades mot {url}: {e}")
            time.sleep(3 * attempt)
    raise RuntimeError(f"Kunde inte hämta {url}: {last_exc}")


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "sv-SE,sv;q=0.9"})
    return s


# ─────────────────────────── Länsstyrelsen ───────────────────────────

def _form_fields(html: str) -> dict[str, str]:
    """Plockar alla fält ur form1 som webbläsaren skulle skicka (utom knappar)."""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", id="form1")
    if form is None:
        raise RuntimeError("Länsstyrelsen: hittade inte form1 på sidan")
    fields: dict[str, str] = {}
    for el in form.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        if not name:
            continue
        if el.name == "input":
            if (el.get("type") or "text").lower() in ("button", "submit", "image", "checkbox", "radio", "reset"):
                continue
            fields[name] = el.get("value", "")
        elif el.name == "select":
            opt = el.find("option", selected=True) or el.find("option")
            fields[name] = opt.get("value", opt.get_text()) if opt else ""
        else:
            fields[name] = el.get_text()
    if "__VIEWSTATE" not in fields:
        raise RuntimeError("Länsstyrelsen: __VIEWSTATE saknas")
    return fields


def parse_lst_results(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ")
    m = re.search(r"Sökresultat:\s*(?:(\d+)\s*-\s*(\d+)\s*av\s*(\d+)|(\d+))", text)
    if not m:
        raise RuntimeError("Länsstyrelsen: hittade inget 'Sökresultat' – sidan har ändrats eller sökningen misslyckades")
    total = int(m.group(3) or m.group(4))
    if total == 0:
        return []
    table = soup.find("table", id="SearchPlaceHolder_caseGridView")
    if table is None:
        raise RuntimeError("Länsstyrelsen: resultattabellen saknas trots träffar")
    rows = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 8:
            continue
        a = tds[0].find("a", href=True)
        cid = None
        if a:
            mm = re.search(r"caseID=(\d+)", a["href"])
            cid = mm.group(1) if mm else None
        rows.append({
            "case_id": cid,
            "diarienummer": clean(tds[0].get_text()),
            "status": clean(tds[1].get_text()),
            "datum": clean(tds[2].get_text()),
            "rubrik": clean(tds[3].get_text()),
            "avsandare": clean(tds[4].get_text()),
            "postort": clean(tds[5].get_text()),
            "kommun": clean(tds[6].get_text()),
            "beslutsdatum": clean(tds[7].get_text()),
        })
    if len(rows) < total:
        log(f"  VARNING: Länsstyrelsen anger {total} träffar men tabellen har {len(rows)} rader (paginering?)")
    return rows


def parse_lst_case(html: str) -> dict | None:
    """Returnerar ärendeinfo + handlingar, eller None om ärendet inte finns/visas."""
    soup = BeautifulSoup(html, "html.parser")
    det = soup.find("table", id="SearchPlaceHolder_caseDetailsView")
    if det is None:
        raise RuntimeError("Länsstyrelsen: ärendetabellen saknas – sidan har ändrats")
    info: dict[str, str] = {}
    for tr in det.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) >= 2:
            info[clean(cells[0].get_text())] = clean(cells[1].get_text())
    if not info.get("Diarienummer"):
        return None  # tomt ärende = borttaget/ej publicerat

    docs = []
    deed = soup.find("table", id="SearchPlaceHolder_deedGridView")
    if deed is not None:
        for tr in deed.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue  # rubrikrad (th)
            docs.append({
                "nr": clean(tds[0].get_text()),
                "rubrik": clean(tds[1].get_text()),
                "datum": clean(tds[2].get_text()),
                "avsandare": clean(tds[3].get_text()),
            })
    return {"info": info, "handlingar": docs}


def fetch_lst() -> list[dict]:
    """Söker fram alla Viscaria-ärenden och hämtar varje ärendesida."""
    s = new_session()
    P = LST_PREFIX

    # 1) Startsidan → viewstate
    r = http(s, "GET", LST_BASE)
    fields = _form_fields(r.text)

    # 2) Välj diarium (motsvarar ddDiaryIndexChange → btnUpdateOrgUnit)
    fields.update({
        P + "ddDiaryID": LST_DIARY_ID,
        P + "txtHiddenDiaryID": LST_DIARY_ID,
        P + "txtHiddenOrgUnit": "0",
        P + "btnUpdateOrgUnit": "",
    })
    r = http(s, "POST", LST_BASE, data=fields)
    fields = _form_fields(r.text)

    # 3) Sök (motsvarar Search() → btnHiddenSearch). Svaret är en 302 till CaseSearchResult.aspx
    fields.update({
        P + "ddDiaryID": LST_DIARY_ID,
        P + "txtHiddenDiaryID": LST_DIARY_ID,
        P + "txtHiddenOrgUnit": "0",
        P + "diaryNO": "",
        P + "title": LST_TITLE,
        P + "ddlStatus": "0",
        P + "ddlOrgUnit": "0",
        P + "ddMunicipality": LST_MUNICIPALITY,
        P + "ddDatefrom": "",
        P + "ddDateto": "",
        P + "ddlDatefrom": "",
        P + "ddlDateto": "",
        P + "btnHiddenSearch": "",
    })
    r = http(s, "POST", LST_BASE, data=fields)
    if "CaseSearchResult" not in r.url:
        raise RuntimeError("Länsstyrelsen: sökningen gav ingen omdirigering till resultatsidan")
    cases = parse_lst_results(r.text)
    log(f"Länsstyrelsen: {len(cases)} ärenden i sökresultatet")

    # 4) Varje ärendesida
    out = []
    for c in cases:
        if not c["case_id"]:
            log(f"  {c['diarienummer']}: saknar caseID, hoppar över ärendesidan")
            c["handlingar"] = None
            out.append(c)
            continue
        time.sleep(PAUSE_BETWEEN_CASES)
        r = http(s, "GET", LST_CASE_URL.format(case_id=c["case_id"]))
        parsed = parse_lst_case(r.text)
        if parsed is None:
            log(f"  {c['diarienummer']}: ärendesidan tom, hoppar över")
            c["handlingar"] = None
        else:
            info = parsed["info"]
            # Ärendesidan är färskast – använd den för status/beslutsdatum
            c["status"] = info.get("Status", c["status"])
            c["beslutsdatum"] = info.get("Beslutsdatum", c["beslutsdatum"])
            c["handlingar"] = parsed["handlingar"]
        out.append(c)
    return out


# ─────────────────────────── Bergsstaten ───────────────────────────

def fetch_bgs() -> list[dict]:
    s = new_session()
    body = {
        "diarienummer": None,
        "beskrivning": BGS_TERM,
        "ankomstDatumFran": None,
        "ankomstDatumTill": None,
        "beslutsDatumFran": None,
        "beslutsDatumTill": None,
        "endastOppnaArenden": "false",   # ska vara en sträng
    }
    r = http(s, "POST", BGS_REST, json=body, headers={
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://apps.sgu.se",
        "Referer": BGS_PAGE,
    })
    data = r.json()
    if "searchResults" not in data:
        raise RuntimeError("Bergsstaten: oväntat svar (searchResults saknas)")
    res = [{k: clean(str(v or "")) for k, v in item.items()} for item in data["searchResults"]]
    log(f"Bergsstaten: {len(res)} ärenden")
    return res


# ─────────────────────────── Discord ───────────────────────────

class Discord:
    def __init__(self, webhook: str | None, dry_run: bool):
        self.webhook = webhook
        self.dry_run = dry_run
        self.session = requests.Session()
        self.count = 0

    def post(self, embed: dict) -> None:
        embed.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        if self.dry_run:
            print("\n──── [DRY-RUN] Discord-inlägg ────")
            print(json.dumps(embed, ensure_ascii=False, indent=2))
            self.count += 1
            return
        if self.count:
            time.sleep(PAUSE_BETWEEN_POSTS)
        payload = {"username": "Viscaria-bevakning", "embeds": [embed],
                   "allowed_mentions": {"parse": []}}
        for attempt in range(5):
            r = self.session.post(self.webhook, json=payload, timeout=HTTP_TIMEOUT)
            if r.status_code == 429:
                wait = float(r.json().get("retry_after", 5))
                log(f"  Discord rate limit, väntar {wait:.1f}s")
                time.sleep(wait + 0.5)
                continue
            if r.status_code >= 500:
                time.sleep(5)
                continue
            r.raise_for_status()
            self.count += 1
            return
        raise RuntimeError("Discord: kunde inte posta efter flera försök")


def _fields(pairs: list[tuple[str, str, bool]]) -> list[dict]:
    return [{"name": n, "value": cut(v, 1024), "inline": i} for n, v, i in pairs if v]


def embed_lst_new_case(c: dict, first_run: bool) -> dict:
    docs = c.get("handlingar") or []
    pairs = [
        ("Diarienummer", c["diarienummer"], True),
        ("Status", c["status"], True),
        ("Inkommet/upprättat", c["datum"], True),
        ("Avsändare/mottagare", c["avsandare"], True),
        ("Kommun", c["kommun"], True),
        ("Beslutsdatum", c["beslutsdatum"], True),
    ]
    if docs:
        latest = docs[-1]
        lines = [f"`{d['nr']}` {d['datum']} – {d['rubrik']}" for d in docs[-5:]]
        more = f"\n…och {len(docs) - 5} tidigare" if len(docs) > 5 else ""
        pairs.append((f"Handlingar ({len(docs)} st, senaste {latest['datum']})", "\n".join(lines) + more, False))
    return {
        "author": {"name": "Länsstyrelsen i Norrbottens län"},
        "title": cut(("Bevakat ärende: " if first_run else "Nytt ärende: ") + c["rubrik"], 256),
        "url": LST_CASE_URL.format(case_id=c["case_id"]) if c.get("case_id") else LST_BASE,
        "color": COLOR_LST,
        "fields": _fields(pairs),
    }


def embed_lst_new_doc(c: dict, d: dict) -> dict:
    return {
        "author": {"name": "Länsstyrelsen i Norrbottens län – ny handling"},
        "title": cut(d["rubrik"] or d["nr"], 256),
        "url": LST_CASE_URL.format(case_id=c["case_id"]),
        "color": COLOR_LST,
        "description": cut(f"I ärende **{c['diarienummer']}**: {c['rubrik']}", 4096),
        "fields": _fields([
            ("Handlingsnummer", d["nr"], True),
            ("Datum", d["datum"], True),
            ("Avsändare/mottagare", d["avsandare"], True),
        ]),
    }


def embed_lst_many_docs(c: dict, docs: list[dict]) -> dict:
    lines = [f"`{d['nr']}` {d['datum']} – {d['rubrik']} ({d['avsandare']})" for d in docs]
    return {
        "author": {"name": f"Länsstyrelsen i Norrbottens län – {len(docs)} nya handlingar"},
        "title": cut(f"{c['diarienummer']}: {c['rubrik']}", 256),
        "url": LST_CASE_URL.format(case_id=c["case_id"]),
        "color": COLOR_LST,
        "description": cut("\n".join(lines), 4096),
    }


def embed_lst_status(c: dict, changes: list[str]) -> dict:
    return {
        "author": {"name": "Länsstyrelsen i Norrbottens län – ärendet ändrat"},
        "title": cut(f"{c['diarienummer']}: {c['rubrik']}", 256),
        "url": LST_CASE_URL.format(case_id=c["case_id"]) if c.get("case_id") else LST_BASE,
        "color": COLOR_STATUS,
        "description": "\n".join(changes),
    }


def embed_bgs_new(c: dict, first_run: bool) -> dict:
    return {
        "author": {"name": "Bergsstaten (SGU)"},
        "title": cut(("Bevakat ärende: " if first_run else "Nytt ärende: ") + c.get("beskrivning", ""), 256),
        "url": BGS_PAGE,
        "color": COLOR_BGS,
        "fields": _fields([
            ("Diarienummer", c.get("diarienummer", ""), True),
            ("Ankomstdatum", c.get("ankomstDatum", ""), True),
            ("Namn", c.get("namn", ""), True),
            ("Beslutsdatum", c.get("beslutsDatum", ""), True),
            ("Beslutstyp", c.get("beslutsTyp", ""), True),
        ]),
        "footer": {"text": "Bergsstaten har ingen ärendesida – sök på diarienumret i länken"},
    }


def embed_bgs_changed(c: dict, changes: list[str]) -> dict:
    return {
        "author": {"name": "Bergsstaten (SGU) – ärendet ändrat"},
        "title": cut(f"{c.get('diarienummer', '')}: {c.get('beskrivning', '')}", 256),
        "url": BGS_PAGE,
        "color": COLOR_STATUS,
        "description": "\n".join(changes),
    }


# ─────────────────────────── Jämförelse ───────────────────────────

def _change(label: str, old: str, new: str) -> str | None:
    if (old or "") == (new or ""):
        return None
    return f"**{label}:** {old or '–'} → **{new or '–'}**"


def _sort_key(c: dict, date_key: str) -> tuple:
    dnr = c.get("diarienummer", "")
    m = re.match(r"(\d+)-(\d{4})", dnr)
    return (c.get(date_key, ""), int(m.group(2)) if m else 0, int(m.group(1)) if m else 0)


def process_lst(state: dict, discord: Discord) -> None:
    src = state.setdefault("lst", {"initierad": False, "arenden": {}})
    first_run = not src["initierad"]
    known: dict = src["arenden"]
    cases = fetch_lst()

    for c in sorted(cases, key=lambda c: _sort_key(c, "datum")):
        dnr = c["diarienummer"]
        docs = c.get("handlingar")
        old = known.get(dnr)

        if old is None:
            discord.post(embed_lst_new_case(c, first_run))
            known[dnr] = {
                "case_id": c["case_id"], "rubrik": c["rubrik"], "status": c["status"],
                "beslutsdatum": c["beslutsdatum"], "datum": c["datum"],
                # None = ärendesidan gick inte att läsa; handlingarna markeras då som sedda nästa gång
                "handlingar": [d["nr"] for d in docs] if docs is not None else None,
                "forst_sedd": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            }
            continue

        # Status / beslutsdatum
        changes = [x for x in (
            _change("Status", old.get("status", ""), c["status"]),
            _change("Beslutsdatum", old.get("beslutsdatum", ""), c["beslutsdatum"]),
        ) if x]
        if changes:
            discord.post(embed_lst_status(c, changes))
            old["status"] = c["status"]
            old["beslutsdatum"] = c["beslutsdatum"]

        # Nya handlingar
        if docs is not None and old.get("handlingar") is None:
            old["handlingar"] = [d["nr"] for d in docs]
        elif docs is not None:
            seen = set(old.get("handlingar", []))
            new_docs = [d for d in docs if d["nr"] and d["nr"] not in seen]
            if new_docs:
                if len(new_docs) > MAX_DOCS_SEPARATE:
                    discord.post(embed_lst_many_docs(c, new_docs))
                    old.setdefault("handlingar", []).extend(d["nr"] for d in new_docs)
                else:
                    for d in new_docs:
                        discord.post(embed_lst_new_doc(c, d))
                        old.setdefault("handlingar", []).append(d["nr"])
        old["case_id"] = c["case_id"] or old.get("case_id")
        old["rubrik"] = c["rubrik"]

    src["initierad"] = True


def process_bgs(state: dict, discord: Discord) -> None:
    src = state.setdefault("bgs", {"initierad": False, "arenden": {}})
    first_run = not src["initierad"]
    known: dict = src["arenden"]
    cases = fetch_bgs()

    for c in sorted(cases, key=lambda c: _sort_key(c, "ankomstDatum")):
        dnr = c.get("diarienummer", "")
        if not dnr:
            continue
        old = known.get(dnr)
        if old is None:
            discord.post(embed_bgs_new(c, first_run))
            known[dnr] = dict(c, forst_sedd=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
            continue
        changes = [x for x in (
            _change("Beslutsdatum", old.get("beslutsDatum", ""), c.get("beslutsDatum", "")),
            _change("Beslutstyp", old.get("beslutsTyp", ""), c.get("beslutsTyp", "")),
            _change("Beskrivning", old.get("beskrivning", ""), c.get("beskrivning", "")),
        ) if x]
        if changes:
            discord.post(embed_bgs_changed(c, changes))
            old.update({k: c.get(k, "") for k in ("beslutsDatum", "beslutsTyp", "beskrivning", "namn")})

    src["initierad"] = True


# ─────────────────────────── Huvudprogram ───────────────────────────

def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"version": 1}


def save_state(path: Path, state: dict) -> None:
    # Datum (inte klockslag) så att filen ändras högst en gång per dygn när inget händer.
    # Det håller repot "aktivt" så att GitHub inte stänger av schemat efter 60 dagar.
    state["senast_kontrollerad"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Bevakar Viscaria i Länsstyrelsens och Bergsstatens diarier")
    ap.add_argument("--dry-run", action="store_true", help="skriv ut inlägg i terminalen, posta och spara inget")
    ap.add_argument("--state", type=Path, default=STATE_FILE, help="sökväg till state.json")
    args = ap.parse_args()

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook and not args.dry_run:
        log("FEL: DISCORD_WEBHOOK_URL saknas (eller kör med --dry-run)")
        return 2

    state = load_state(args.state)
    discord = Discord(webhook, args.dry_run)
    errors = 0

    for name, func in (("Länsstyrelsen", process_lst), ("Bergsstaten", process_bgs)):
        try:
            func(state, discord)
        except Exception as e:  # en trasig källa ska inte stoppa den andra
            errors += 1
            log(f"FEL i {name}: {e}")
        finally:
            # Spara efter varje källa så att redan postade nyheter inte postas igen
            if not args.dry_run:
                save_state(args.state, state)

    log(f"Klart: {discord.count} inlägg, {errors} fel")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
