#!/usr/bin/env python3
import os, re, json, time, urllib.parse, urllib.request, unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "state_seen.json"
KEYWORDS_FILE = ROOT / "keywords.txt"
API_BASE = "https://public.mtender.gov.md"
THRESHOLD = 55
MAX_PAGES = 15

def norm(text):
    if text is None: return ""
    text = str(text).lower()
    text = "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()

def phrase_match(haystack, phrase):
    p = norm(phrase)
    return bool(p and re.search(rf"(?<![\w]){re.escape(p)}(?![\w])", haystack, flags=re.I|re.UNICODE))

def http_get_json(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent":"FierArtPrimBot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def http_post_json(url, payload, timeout=30):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body,
        headers={"Content-Type":"application/json; charset=utf-8","User-Agent":"FierArtPrimBot/1.0"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def nested(obj, path):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur: return None
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
            if isinstance(item, dict):
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
    "coș de gunoi","coșuri de gunoi","urnă stradală","urne stradale",
    "construcție metalică","construcții metalice","gard metalic","garduri metalice",
    "balustradă metalică","balustrade metalice","bancă stradală","bănci stradale",
    "bancă de parc","bănci de parc","dale cauciuc","dale din cauciuc",
    "pardoseală din cauciuc","suprafață din cauciuc"
]))

NEGATIVE_CONTEXTS = [
    "tehnica de calcul","echipamente it","calculator","calculatoare","laptop","laptopuri",
    "imprimanta","imprimante","server","servere","consumabile de laborator",
    "institutie medico-sanitara","spital","medical","carucior de interventie",
    "retele electrice","iluminarea stradala","iluminare stradala","lucrari electrice"
]

def score_relevance(sections, keywords):
    matches, score, core_strong = [], 0, False
    for kw in keywords:
        cls = "secondary" if norm(kw) in SECONDARY else "core"
        best = 0
        if phrase_match(sections["title"], kw):
            best = 95 if cls == "core" else 72
            if cls == "core": core_strong = True
        elif phrase_match(sections["description"], kw):
            best = 78 if cls == "core" else 30
            if cls == "core": core_strong = True
        elif phrase_match(sections["lotTitle"], kw):
            best = 85 if cls == "core" else 55
            if cls == "core": core_strong = True
        elif phrase_match(sections["lotDescription"], kw):
            best = 62 if cls == "core" else 22
            if cls == "core": core_strong = True
        elif phrase_match(sections["items"], kw):
            best = 48 if cls == "core" else 10
        if best:
            matches.append(kw)
            score = max(score, best)

    top_text = norm(sections["title"]+" "+sections["description"]+" "+sections["lotTitle"])
    negative = any(phrase_match(top_text, x) for x in NEGATIVE_CONTEXTS)
    if negative and not core_strong:
        score = min(score, 20)
    if not core_strong and score < 72:
        score = min(score, 45)

    unique = []
    for x in matches:
        if x not in unique: unique.append(x)
    if core_strong and len(unique) >= 2:
        score = min(100, score + 5)
    return int(score), unique

def parse_dt(value):
    if not value: return None
    s = str(value).strip()
    try:
        if s.endswith("Z"): s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None

def expired(value):
    dt = parse_dt(value)
    if not dt: return False
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt < datetime.now(timezone.utc) - timedelta(minutes=5)

def fmt_dt(value):
    dt = parse_dt(value)
    if not dt: return "nespecificat"
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M")

def fmt_amount(amount, currency):
    if amount is None or str(amount).strip() == "": return "nespecificată"
    try: s = f"{float(amount):,.2f}".replace(",", " ")
    except Exception: s = str(amount)
    return f"{s} {currency or 'MDL'}"

def title_of(record):
    return str(best_value(record, ["tender.title","tender.description"]) or "Licitație fără titlu detectat")

def authority_of(record):
    return str(best_value(record, ["tender.procuringEntity.name","buyer.name","procuringEntity.name"]) or "nespecificată")

def label(score):
    if score >= 90: return "FOARTE INTERESANTĂ"
    if score >= 70: return "INTERESANTĂ"
    return "POSIBIL RELEVANTĂ"

def send_telegram(token, chat_id, text):
    return http_post_json(f"https://api.telegram.org/bot{token}/sendMessage",
                          {"chat_id":chat_id,"text":text,"disable_web_page_preview":True})

def load_state():
    if not STATE_FILE.exists(): return {}
    try: return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception: return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

def load_keywords():
    return [x.strip() for x in KEYWORDS_FILE.read_text(encoding="utf-8-sig").splitlines()
            if x.strip() and not x.strip().startswith("#")]

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN","").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID","").strip()
    if not token or not chat_id:
        raise RuntimeError("Lipsesc secretele Telegram.")

    keywords, state = load_keywords(), load_state()
    offset = (datetime.now(timezone.utc)-timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%SZ")

    for page in range(1, MAX_PAGES+1):
        listing = http_get_json(f"{API_BASE}/tenders/?offset={urllib.parse.quote(offset, safe='')}")
        items = listing.get("data") or []
        if not items: break

        for item in items:
            ocid, date = str(item.get("ocid") or ""), str(item.get("date") or "")
            if not ocid or state.get(ocid) == date: continue

            try:
                record = http_get_json(f"{API_BASE}/tenders/{ocid}")
            except Exception:
                continue

            deadline = best_value(record, ["tender.tenderPeriod.endDate"])
            state[ocid] = date

            if expired(deadline): continue

            score, matches = score_relevance(text_sections(record), keywords)
            if score < THRESHOLD: continue

            title = title_of(record)
            buyer = authority_of(record)
            amount = best_value(record, ["tender.value.amount"])
            currency = best_value(record, ["tender.value.currency"])
            status = best_value(record, ["tender.statusDetails","tender.status"]) or "nespecificat"

            msg = (
                f"🔔 {label(score)}\n\n{title}\n\n"
                f"⭐ Scor relevanță: {score}/100\n"
                f"🏛 Autoritate: {buyer}\n"
                f"💰 Valoare estimată: {fmt_amount(amount,currency)}\n"
                f"⏰ Termen ofertare: {fmt_dt(deadline)}\n"
                f"📌 Statut: {status}\n\n"
                f"🎯 Potrivire: {', '.join(matches[:6])}\n\n"
                f"🔗 https://mtender.gov.md/tenders/{ocid}"
            )
            send_telegram(token, chat_id, msg)
            time.sleep(0.2)

        nxt = str(listing.get("offset") or "")
        if not nxt or nxt == offset: break
        offset = nxt

    save_state(state)

if __name__ == "__main__":
    main()
