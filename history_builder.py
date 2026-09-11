import json
import re
import urllib.parse
import urllib.request
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

CURSOR_FILE = DATA / "history_cursor.json"
HISTORY_FILE = DATA / "competitor_history.json"

API = "https://public.mtender.gov.md"

# 3 pagini x 100 proceduri la fiecare rulare.
# Istoricul se construiește treptat, fără să suprasolicităm MTender.
PAGES_PER_RUN = 6

START_OFFSET = "2026-01-01T00:00:00Z"

KEYWORDS = [
    "loc de joacă",
    "locuri de joacă",
    "teren de joacă",
    "terenuri de joacă",
    "complex de joacă",
    "complexe de joacă",
    "echipamente de joacă",
    "echipament de joacă",
    "mobilier urban",
    "mobilier stradal",
    "teren sportiv",
    "terenuri sportive",
    "fitness exterior",
    "foișor",
    "foișoare",
    "pergolă",
    "pergole",
    "leagăn",
    "leagăne",
    "tobogan",
    "tobogane",
    "carusel",
    "balansoar",
    "platforme pentru colectarea deșeurilor",
    "platformă pentru colectarea deșeurilor",
    "amenajare parc",
    "amenajarea parcului",
    "amenajare scuar",
    "amenajarea scuarului",
    "stație auto",
    "stații auto",
    "pavilion de așteptare",
    "pavilioane de așteptare",
    "gard metalic",
    "garduri metalice",
    "decorațiuni festive",
    "decorațiuni de iarnă",
    "dale din cauciuc",
    "pardoseală din cauciuc"
]


def norm(value):
    if value is None:
        return ""

    text = str(value).lower()

    text = "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )

    return re.sub(r"\s+", " ", text).strip()


NORMALIZED_KEYWORDS = [norm(x) for x in KEYWORDS]


def get_json(url):
    import time

    last_error = None

    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "FierArtPrimBot-History/1.1",
                    "Accept": "application/json"
                }
            )

            with urllib.request.urlopen(
                req,
                timeout=30
            ) as response:

                raw = response.read()

                if not raw:
                    raise ValueError(
                        "MTender a returnat răspuns gol"
                    )

                text = raw.decode(
                    "utf-8",
                    errors="replace"
                ).strip()

                if not text.startswith(("{", "[")):
                    print(
                        "Răspuns non-JSON:",
                        text[:200],
                        flush=True
                    )

                    raise ValueError(
                        "MTender nu a returnat JSON"
                    )

                return json.loads(text)

        except Exception as error:
            last_error = error

            print(
                f"Încercare {attempt}/3 eșuată:",
                url,
                "|",
                error,
                flush=True
            )

            if attempt < 3:
                time.sleep(attempt * 3)

    raise last_error


def records(record):
    result = record.get("records") or []
    return result if isinstance(result, list) else []


def compiled_releases(record):
    result = []

    for row in records(record):
        cr = row.get("compiledRelease")
        if isinstance(cr, dict):
            result.append(cr)

    return result


def best_value(record, path):
    parts = path.split(".")

    for cr in reversed(compiled_releases(record)):
        current = cr

        ok = True

        for part in parts:
            if not isinstance(current, dict) or part not in current:
                ok = False
                break

            current = current[part]

        if ok and current not in (None, ""):
            return current

    return None


def tender_text(record):
    chunks = []

    for cr in compiled_releases(record):
        tender = cr.get("tender") or {}

        chunks.append(str(tender.get("title") or ""))
        chunks.append(str(tender.get("description") or ""))

        for lot in tender.get("lots") or []:
            if isinstance(lot, dict):
                chunks.append(str(lot.get("title") or ""))
                chunks.append(str(lot.get("description") or ""))

        for item in tender.get("items") or []:
            if not isinstance(item, dict):
                continue

            chunks.append(str(item.get("description") or ""))

            classification = item.get("classification") or {}

            if isinstance(classification, dict):
                chunks.append(
                    str(classification.get("description") or "")
                )

    return norm(" ".join(chunks))


def relevant(record):
    text = tender_text(record)

    return any(
        keyword in text
        for keyword in NORMALIZED_KEYWORDS
    )


def get_cpv(record):
    for cr in reversed(compiled_releases(record)):
        tender = cr.get("tender") or {}

        for item in tender.get("items") or []:
            if not isinstance(item, dict):
                continue

            classification = item.get("classification") or {}

            if isinstance(classification, dict):
                cpv = classification.get("id")

                if cpv:
                    return str(cpv)

    return ""


def party_name(ref):
    if not isinstance(ref, dict):
        return ""

    return str(
        ref.get("name")
        or ref.get("identifier", {}).get("legalName")
        or ""
    ).strip()


def party_id(ref):
    if not isinstance(ref, dict):
        return ""

    identifier = ref.get("identifier") or {}

    if isinstance(identifier, dict):
        value = identifier.get("id")

        if value:
            return str(value)

    value = ref.get("id")

    return str(value or "")


def extract_bidders(record):
    found = {}

    for cr in compiled_releases(record):
        tender = cr.get("tender") or {}

        # OCDS standard tenderers.
        for bidder in tender.get("tenderers") or []:
            name = party_name(bidder)

            if name:
                key = norm(name)

                found[key] = {
                    "name": name,
                    "id": party_id(bidder),
                    "amount": None
                }

        # Unele publicări folosesc extensia bids.
        bids = cr.get("bids") or {}

        if isinstance(bids, dict):
            for bid in bids.get("details") or []:
                if not isinstance(bid, dict):
                    continue

                value = bid.get("value") or {}
                amount = (
                    value.get("amount")
                    if isinstance(value, dict)
                    else None
                )

                for bidder in bid.get("tenderers") or []:
                    name = party_name(bidder)

                    if not name:
                        continue

                    key = norm(name)

                    previous = found.get(key, {})

                    found[key] = {
                        "name": name,
                        "id": party_id(bidder) or previous.get("id", ""),
                        "amount": amount
                    }

    return list(found.values())


def extract_awards(record):
    found = []

    for cr in compiled_releases(record):
        for award in cr.get("awards") or []:
            if not isinstance(award, dict):
                continue

            status = str(award.get("status") or "")

            # Păstrăm inclusiv active/pending dacă există supplier.
            value = award.get("value") or {}

            amount = (
                value.get("amount")
                if isinstance(value, dict)
                else None
            )

            for supplier in award.get("suppliers") or []:
                name = party_name(supplier)

                if not name:
                    continue

                found.append({
                    "name": name,
                    "id": party_id(supplier),
                    "amount": amount,
                    "status": status
                })

    # eliminare duplicate
    unique = {}

    for item in found:
        key = (
            norm(item["name"]),
            str(item.get("amount"))
        )

        unique[key] = item

    return list(unique.values())


def load_json(path, default):
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, value):
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


cursor_data = load_json(
    CURSOR_FILE,
    {"offset": START_OFFSET}
)

cursor = cursor_data.get(
    "offset",
    START_OFFSET
)

history = load_json(
    HISTORY_FILE,
    []
)

by_ocid = {
    item.get("ocid"): item
    for item in history
    if item.get("ocid")
}

print("START CURSOR:", cursor, flush=True)

checked = 0
relevant_count = 0


for page_number in range(1, PAGES_PER_RUN + 1):

    url = (
        f"{API}/tenders/?offset="
        + urllib.parse.quote(cursor, safe="")
    )

        try:
            listing = get_json(url)
        except Exception as e:
            log(f"MTender indisponibil temporar: {e}")
            log("Rularea se oprește fără eroare și va încerca din nou ulterior.")
            listing = {"data": []}
        
            rows = listing.get("data") or []
        
            print(
                f"Pagina {page_number}: {len(rows)} proceduri",
                flush=True
            )
        
            if not rows:
                break
    
    
        for row in rows:
    
            ocid = str(
                row.get("ocid") or ""
            )
    
            if not ocid:
                continue
    
            checked += 1
    
            try:
                record = get_json(
                    f"{API}/tenders/{ocid}"
                )
            except Exception as error:
                print(
                    "EROARE",
                    ocid,
                    error,
                    flush=True
                )
    
                continue
    
    
            if not relevant(record):
                continue
    
    
            relevant_count += 1
    
            title = str(
                best_value(
                    record,
                    "tender.title"
                )
                or best_value(
                    record,
                    "tender.description"
                )
                or ""
            )
    
    
            buyer = str(
                best_value(
                    record,
                    "buyer.name"
                )
                or best_value(
                    record,
                    "tender.procuringEntity.name"
                )
                or ""
            )
    
    
            estimated = best_value(
                record,
                "tender.value.amount"
            )
    
    
            entry = {
                "ocid": ocid,
                "date": row.get("date"),
                "title": title,
                "buyer": buyer,
                "cpv": get_cpv(record),
                "estimated_amount": estimated,
                "bidders": extract_bidders(record),
                "awards": extract_awards(record)
            }
    
    
            by_ocid[ocid] = entry
    
            print(
                "RELEVANT:",
                title[:90],
                "| bidders:",
                len(entry["bidders"]),
                "| awards:",
                len(entry["awards"]),
                flush=True
            )
    
    
        next_cursor = str(
            listing.get("offset") or ""
        )
    
        if (
            not next_cursor
            or next_cursor == cursor
        ):
            break
    
        cursor = next_cursor


history = list(
    by_ocid.values()
)

history.sort(
    key=lambda x: str(
        x.get("date") or ""
    )
)


save_json(
    HISTORY_FILE,
    history
)

save_json(
    CURSOR_FILE,
    {
        "offset": cursor,
        "last_run": datetime.now(
            timezone.utc
        ).isoformat(),
        "records": len(history)
    }
)


print(
    "GATA | verificate:",
    checked,
    "| relevante:",
    relevant_count,
    "| total istoric:",
    len(history),
    "| cursor:",
    cursor,
    flush=True
)
