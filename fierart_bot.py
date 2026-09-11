#!/usr/bin/env python3
import os
import re
import json
import time
import urllib.parse
import urllib.request
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
DATA_DIR = ROOT / "data"
STATE_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "state_seen.json"
LATEST_FILE = DATA_DIR / "latest_tenders.json"
KEYWORDS_FILE = ROOT / "keywords.txt"

API_BASE = "https://public.mtender.gov.md"
THRESHOLD = 55

def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')}  {msg}", flush=True)

def norm(text):
    if text is None:
        return ""
    text = str(text).lower()
    text = "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()

def phrase_match(haystack, phrase):
    p = norm(phrase)
    if not p:
        return False
    return re.search(
        rf"(?<![\w]){re.escape(p)}(?![\w])",
        haystack,
        flags=re.I | re.UNICODE
    ) is not None

def http_get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "FierArtPrimBot/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def http_post_json(url, payload, timeout=20):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "FierArtPrimBot/2.0"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def nested(obj, path):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur

def records_of(record):
    rs = record.get("records") or []
    return rs if isinstance(rs, list) else []

def best_value(record, paths):
    for r in reversed(records_of(record)):
        cr = r.get("compiledRelease") or {}
        for path in paths:
            v = nested(cr, path)
            if v is not None and str(v).strip():
                return v
    return None

def add_text(lst, value):
    if value is not None and str(value).strip():
        lst.append(str(value))

def text_sections(record):
    title, desc, lot_title, lot_desc, items = [], [], [], [], []

    for r in records_of(record):
        cr = r.get("compiledRelease") or {}
        tender = cr.get("tender") or {}

        add_text(title, tender.get("title"))
        add_text(desc, tender.get("description"))

        for lot in tender.get("lots") or []:
            if isinstance(lot, dict):
                add_text(lot_title, lot.get("title"))
                add_text(lot_desc, lot.get("description"))

        for item in tender.get("items") or []:
            if not isinstance(item, dict):
                continue
            add_text(items, item.get("description"))
            cl = item.get("classification") or {}
            if isinstance(cl, dict):
                add_text(items, cl.get("description"))

    return {
        "title": norm(" | ".join(title)),
        "description": norm(" | ".join(desc)),
        "lotTitle": norm(" | ".join(lot_title)),
        "lotDescription": norm(" | ".join(lot_desc)),
        "items": norm(" | ".join(items)),
    }

SECONDARY = set(map(norm, [
    "coș de gunoi", "coșuri de gunoi", "urnă stradală", "urne stradale",
    "construcție metalică", "construcții metalice",
    "gard metalic", "garduri metalice",
    "balustradă metalică", "balustrade metalice",
    "bancă stradală", "bănci stradale",
    "bancă de parc", "bănci de parc",
    "dale cauciuc", "dale din cauciuc",
    "pardoseală din cauciuc", "suprafață din cauciuc"
]))

NEGATIVE_CONTEXTS = [
    "tehnica de calcul", "echipamente it", "calculator", "calculatoare",
    "laptop", "laptopuri", "imprimanta", "imprimante", "server", "servere",
    "consumabile de laborator", "institutie medico-sanitara",
    "spital", "medical", "carucior de interventie",
    "retele electrice", "iluminarea stradala",
    "iluminare stradala", "lucrari electrice"
]

def score_relevance(sections, keywords):
    matches = []
    score = 0
    core_strong = False

    for kw in keywords:
        cls = "secondary" if norm(kw) in SECONDARY else "core"
        best = 0

        if phrase_match(sections["title"], kw):
            best = 95 if cls == "core" else 72
            if cls == "core":
                core_strong = True
        elif phrase_match(sections["description"], kw):
            best = 78 if cls == "core" else 30
            if cls == "core":
                core_strong = True
        elif phrase_match(sections["lotTitle"], kw):
            best = 85 if cls == "core" else 55
            if cls == "core":
                core_strong = True
        elif phrase_match(sections["lotDescription"], kw):
            best = 62 if cls == "core" else 22
            if cls == "core":
                core_strong = True
        elif phrase_match(sections["items"], kw):
            best = 48 if cls == "core" else 10

        if best:
            matches.append(kw)
            score = max(score, best)

    top_text = norm(
        sections["title"] + " " +
        sections["description"] + " " +
        sections["lotTitle"]
    )

    negative = any(phrase_match(top_text, x) for x in NEGATIVE_CONTEXTS)

    if negative and not core_strong:
        score = min(score, 20)

    if not core_strong and score < 72:
        score = min(score, 45)

    unique = []
    for x in matches:
        if x not in unique:
            unique.append(x)

    if core_strong and len(unique) >= 2:
        score = min(100, score + 5)

    return int(score), unique

def parse_dt(value):
    if not value:
        return None
    s = str(value).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None

def expired(value):
    dt = parse_dt(value)
    if not dt:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt < datetime.now(timezone.utc) - timedelta(minutes=5)

def fmt_dt(value):
    dt = parse_dt(value)
    if not dt:
        return "nespecificat"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M")

def fmt_amount(amount, currency):
    if amount is None or str(amount).strip() == "":
        return "nespecificată"
    try:
        s = f"{float(amount):,.2f}".replace(",", " ")
    except Exception:
        s = str(amount)
    return f"{s} {currency or 'MDL'}"

def title_of(record):
    return str(
        best_value(record, ["tender.title", "tender.description"])
        or "Licitație fără titlu detectat"
    )

def authority_of(record):
    return str(
        best_value(record, [
            "tender.procuringEntity.name",
            "buyer.name",
            "procuringEntity.name"
        ])
        or "nespecificată"
    )

def label(score):
    if score >= 90:
        return "FOARTE INTERESANTĂ"
    if score >= 70:
        return "INTERESANTĂ"
    return "POSIBIL RELEVANTĂ"

def send_telegram(token, chat_id, text):
    try:
        return http_post_json(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True
            }
        )
    except Exception as e:
        print("EROARE TELEGRAM:", e, flush=True)

        if hasattr(e, "read"):
            try:
                print(
                    "RASPUNS TELEGRAM:",
                    e.read().decode("utf-8", errors="ignore"),
                    flush=True
                )
            except Exception:
                pass

        raise

def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False),
        encoding="utf-8"
    )

def load_latest():
    if not LATEST_FILE.exists():
        return []
    try:
        data = json.loads(LATEST_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []

def save_latest(items):
    unique = {}
    for item in items:
        ocid = item.get("ocid")
        if ocid:
            unique[ocid] = item

    ordered = sorted(
        unique.values(),
        key=lambda x: x.get("source_date", ""),
        reverse=True
    )[:30]

    LATEST_FILE.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def load_keywords():
    return [
        x.strip()
        for x in KEYWORDS_FILE.read_text(encoding="utf-8-sig").splitlines()
        if x.strip() and not x.strip().startswith("#")
    ]

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        raise RuntimeError("Lipsesc TELEGRAM_BOT_TOKEN sau TELEGRAM_CHAT_ID.")

    keywords = load_keywords()
    state = load_state()
    latest = load_latest()

    # Prima rulare: doar ultimele 90 minute, ca să pornim rapid.
    # După ce există state_seen.json: verificăm ultimele 6 ore,
    # dar descărcăm detalii doar pentru procedurile noi/modificate.
    first_run = not STATE_FILE.exists()
    lookback_hours = 1.5 if first_run else 6
    max_pages = 4 if first_run else 10

    offset = (
        datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    log(
        f"START | {'prima rulare' if first_run else 'rulare normală'} | "
        f"lookback={lookback_hours}h"
    )

    listed = 0
    fetched = 0
    sent = 0
    skipped_seen = 0
    skipped_expired = 0

    for page in range(1, max_pages + 1):
        url = f"{API_BASE}/tenders/?offset={urllib.parse.quote(offset, safe='')}"

        try:
            listing = http_get_json(url)
        except Exception as e:
            log(f"EROARE listă MTender: {e}")
            break

        items = listing.get("data") or []
        listed += len(items)
        log(f"Pagina {page}: {len(items)} înregistrări")

        if not items:
            break

        for item in items:
            ocid = str(item.get("ocid") or "")
            date = str(item.get("date") or "")

            if not ocid:
                continue

            if state.get(ocid) == date:
                skipped_seen += 1
                continue

            try:
                record = http_get_json(f"{API_BASE}/tenders/{ocid}")
                fetched += 1
            except Exception as e:
                log(f"Nu pot citi {ocid}: {e}")
                continue

            # Marcăm versiunea procesată chiar dacă nu e relevantă.
            state[ocid] = date

            deadline = best_value(record, ["tender.tenderPeriod.endDate"])

            if expired(deadline):
                skipped_expired += 1
                continue

            score, matches = score_relevance(
                text_sections(record),
                keywords
            )

            if score < THRESHOLD:
                continue

            title = title_of(record)
            buyer = authority_of(record)
            amount = best_value(record, ["tender.value.amount"])
            currency = best_value(record, ["tender.value.currency"])
            status = (
                best_value(record, ["tender.statusDetails", "tender.status"])
                or "nespecificat"
            )

            url_tender = f"https://mtender.gov.md/tenders/{ocid}"

            latest_entry = {
                "ocid": ocid,
                "title": title,
                "authority": buyer,
                "amount": amount,
                "currency": currency or "MDL",
                "deadline": str(deadline or ""),
                "deadline_text": fmt_dt(deadline),
                "status": str(status),
                "score": score,
                "label": label(score),
                "matches": matches[:6],
                "url": url_tender,
                "source_date": date,
                "saved_at": datetime.now(timezone.utc).isoformat()
            }

            latest = [x for x in latest if x.get("ocid") != ocid]
            latest.append(latest_entry)

            msg = (
                f"🔔 {label(score)}\n\n"
                f"{title}\n\n"
                f"⭐ Scor relevanță: {score}/100\n"
                f"🏛 Autoritate: {buyer}\n"
                f"💰 Valoare estimată: {fmt_amount(amount, currency)}\n"
                f"⏰ Termen ofertare: {fmt_dt(deadline)}\n"
                f"📌 Statut: {status}\n\n"
                f"🎯 Potrivire: {', '.join(matches[:6])}\n\n"
                f"🔗 {url_tender}"
            )

            try:
                send_telegram(token, chat_id, msg)
                sent += 1
                log(f"TRIMIS {score}/100 | {title[:80]}")
            except Exception as e:
                log(f"EROARE Telegram {ocid}: {e}")

            time.sleep(0.15)

        nxt = str(listing.get("offset") or "")
        if not nxt or nxt == offset:
            break
        offset = nxt

    save_state(state)
    save_latest(latest)

    log(
        f"GATA | listate={listed} | detalii_citite={fetched} | "
        f"deja_văzute={skipped_seen} | expirate={skipped_expired} | "
        f"trimise={sent} | lista_meniu={len(load_latest())}"
    )

if __name__ == "__main__":
    main()
