import base64
import csv
import io
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI, HTTPException, Request

app = FastAPI(title="Realtor Actor Lead Router V2")

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()  # owner/repo
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

DATA_FOLDER = os.getenv("DATA_FOLDER", "DataShortDisgusting").strip().strip("/")
LEGACY_EMAIL_FILE = f"{DATA_FOLDER}/Email.csv"
WEEK1_FILE = f"{DATA_FOLDER}/Week1.csv"
WEEK2_FILE = f"{DATA_FOLDER}/Week2.csv"
WEEK3_FILE = f"{DATA_FOLDER}/Week3.csv"
FB_FILE = f"{DATA_FOLDER}/FB.csv"
SMS_FILE = f"{DATA_FOLDER}/SMS.csv"

OUTPUT_COLUMNS = [
    "FirstName", "LastName", "Email", "Phone", "Website",
    "Address", "City", "State", "Zip", "Price", "Bedrooms",
    "Bathrooms", "SqFt", "PropertyID",
    "ListingID", "PropertyURL", "ListDate", "DaysOnMarket",
    "Status", "OfficeName", "Source", "ProcessedAt"
]


def clean(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in {"nan", "none", "null", "<na>"} else s


def getv(item: Dict[str, Any], path: str, default: Any = "") -> Any:
    """Read either nested Apify JSON (agents[0].agent_email) or flattened CSV-style keys."""
    if path in item:
        return item.get(path, default)
    cur: Any = item
    for part in path.split("/"):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return default
        if cur is None:
            return default
    return cur


def first(item: Dict[str, Any], paths: List[str]) -> str:
    for p in paths:
        v = clean(getv(item, p))
        if v:
            return v
    return ""


def format_person_name(value: str) -> str:
    value = clean(value)
    if not value:
        return ""
    # Fix source data like BRAD / brad without mangling already mixed-case names.
    if value.isupper() or value.islower():
        return value.title()
    return value


def split_name(name: str):
    name = re.sub(r"\s+", " ", clean(name)).strip()
    if not name:
        return "", ""
    parts = name.split(" ")
    first = format_person_name(parts[0])
    last = format_person_name(" ".join(parts[1:])) if len(parts) > 1 else ""
    return first, last


def normalize_phone(phone: str) -> str:
    raw = clean(phone)
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return digits
    return raw


def normalize_email(email: str) -> str:
    return clean(email).lower()


def advertiser_phone(item: Dict[str, Any], idx: int) -> str:
    base = f"advertisers/{idx}/phones"
    phones = getv(item, base, "")
    if isinstance(phones, list):
        for p in phones:
            if isinstance(p, dict):
                val = clean(p.get("number") or p.get("phone") or p.get("value"))
            else:
                val = clean(p)
            if val:
                return val
    # flattened actor exports may expose deeper keys
    return first(item, [
        f"{base}/0/number", f"{base}/0/phone", f"{base}/0/value",
        f"{base}/0", f"advertisers/{idx}/office/phones/0/number"
    ])


def choose_contact(item: Dict[str, Any]):
    """
    Choose a person-level contact only when that SAME contact has a usable name.
    A nameless email/phone/website is deliberately ignored rather than being
    paired with a name from a different agent/advertiser.
    """

    # Prefer direct agent email + matching agent name/phone.
    for i in range(4):
        name = clean(getv(item, f"agents/{i}/agent_name"))
        email = clean(getv(item, f"agents/{i}/agent_email"))
        if name and email:
            return (
                name,
                email,
                clean(getv(item, f"agents/{i}/agent_phone")),
                ""
            )

    # Then advertiser email + matching advertiser name/site/phone.
    for i in range(2):
        name = clean(getv(item, f"advertisers/{i}/name"))
        email = clean(getv(item, f"advertisers/{i}/email"))
        if name and email:
            return (
                name,
                email,
                advertiser_phone(item, i),
                clean(getv(item, f"advertisers/{i}/href"))
            )

    # No email: use a named agent for FB/SMS routing.
    for i in range(4):
        name = clean(getv(item, f"agents/{i}/agent_name"))
        if name:
            phone = clean(getv(item, f"agents/{i}/agent_phone"))
            website = first(item, ["advertisers/0/href", "advertisers/1/href"])
            return name, "", phone, website

    # Or a named advertiser.
    for i in range(2):
        name = clean(getv(item, f"advertisers/{i}/name"))
        if name:
            website = clean(getv(item, f"advertisers/{i}/href"))
            phone = advertiser_phone(item, i)
            return name, "", phone, website

    # Absolutely no named person = throw the lead away.
    return "", "", "", ""


def listing_date(item: Dict[str, Any]) -> str:
    """
    Return the newest genuine 'Listed' event only.
    Price changes, listing removals, relists, pending/sold events, etc. are ignored.
    """
    candidates = []
    for i in range(40):
        event = clean(getv(item, f"history/{i}/event_name")).lower()
        date = clean(getv(item, f"history/{i}/date"))
        if date and event == "listed":
            try:
                candidates.append(datetime.fromisoformat(date[:10]).date())
            except ValueError:
                pass
    if not candidates:
        return ""
    return max(candidates).isoformat()


def listing_id(item: Dict[str, Any]) -> str:
    # Prefer the source listing ID attached to the current/recent history entry.
    for i in range(40):
        val = clean(getv(item, f"history/{i}/source_listing_id"))
        if val:
            return re.sub(r"\.0$", "", val)
    return first(item, ["listing_id", "mls_id", "mls"])


def property_url(item: Dict[str, Any]) -> str:
    val = first(item, ["property_url", "href"])
    if val:
        return val
    slug = clean(getv(item, "url"))
    if not slug:
        return ""
    if slug.startswith("http://") or slug.startswith("https://"):
        return slug
    return f"https://www.realtor.com/realestateandhomes-detail/{slug}"


def property_id(item: Dict[str, Any]) -> str:
    val = first(item, ["property_id", "propertyId", "id"])
    if val:
        return val
    # Realtor slug normally ends in its stable M... identifier.
    slug = clean(getv(item, "url"))
    m = re.search(r"_(M[\w-]+)$", slug)
    return m.group(1) if m else slug


def format_lead(item: Dict[str, Any]) -> Dict[str, str]:
    name, email, phone, contact_website = choose_contact(item)
    first_name, last_name = split_name(name)

    # Website means the agent/advertiser website for FB routing, not Realtor/property URL.
    website = contact_website or first(item, ["advertisers/0/href", "advertisers/1/href"])

    ld = listing_date(item)
    days = ""
    if ld:
        try:
            days = str(max(0, (datetime.now(timezone.utc).date() - datetime.fromisoformat(ld).date()).days))
        except ValueError:
            pass

    price = first(item, ["listPrice", "list_price", "price"])
    if price.endswith(".0"):
        price = price[:-2]

    return {
        "FirstName": first_name,
        "LastName": last_name,
        "Email": normalize_email(email),
        "Phone": normalize_phone(phone),
        "Website": website,
        "Address": first(item, ["address/street", "address/line"]),
        "City": first(item, ["address/locality", "address/city"]),
        "State": first(item, ["address/region", "address/state_code", "address/state"]),
        "Zip": first(item, ["address/postalCode", "address/postal_code"]),
        "Price": price,
        "Bedrooms": first(item, ["beds"]),
        "Bathrooms": first(item, ["baths", "baths_consolidated", "baths_total"]),
        "SqFt": first(item, ["sqft"]),
        "PropertyID": property_id(item),
        "ListingID": listing_id(item),
        "PropertyURL": property_url(item),
        "ListDate": ld,
        "DaysOnMarket": days,
        "Status": first(item, ["status"]),
        "OfficeName": first(item, ["agents/0/office_name", "advertisers/0/office/name"]),
        "Source": "Apify Realtor Scraper V2",
        "ProcessedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def route_lead(lead: Dict[str, str]) -> Optional[str]:
    # Hard gate: never save a lead without a person name.
    if not clean(lead.get("FirstName")):
        return None
    if lead["Email"]:
        return "EMAIL"
    if lead["Website"]:
        return FB_FILE
    if lead["Phone"]:
        return SMS_FILE
    return None


def refresh_days_on_market(row: Dict[str, str]) -> Optional[int]:
    """
    Recalculate age from ListDate every time a webhook runs.
    This is what makes a lead automatically move Week1 -> Week2 -> Week3 over time.
    """
    ld = clean(row.get("ListDate"))
    if not ld:
        return None
    try:
        listed = datetime.fromisoformat(ld[:10]).date()
    except ValueError:
        return None

    days = max(0, (datetime.now(timezone.utc).date() - listed).days)
    row["ListDate"] = listed.isoformat()
    row["DaysOnMarket"] = str(days)
    return days


def email_week_path(row: Dict[str, str]) -> Optional[str]:
    days = refresh_days_on_market(row)
    if days is None:
        return None
    if 0 <= days <= 7:
        return WEEK1_FILE
    if 8 <= days <= 14:
        return WEEK2_FILE
    if 15 <= days <= 21:
        return WEEK3_FILE
    return None


def dedupe_key(row: Dict[str, str]) -> str:
    for col in ("PropertyID", "ListingID", "PropertyURL"):
        if clean(row.get(col)):
            return f"{col}:{clean(row.get(col)).lower()}"
    address = "|".join(clean(row.get(c)).lower() for c in ("Address", "City", "State", "Zip"))
    if address.strip("|"):
        return f"address:{address}"
    return f"contact:{clean(row.get('Email')).lower()}|{normalize_phone(row.get('Phone', ''))}"


def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_read_csv(path: str):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    r = requests.get(url, headers=github_headers(), params={"ref": GITHUB_BRANCH}, timeout=30)
    if r.status_code == 404:
        return [], None
    r.raise_for_status()
    data = r.json()
    sha = data.get("sha")

    encoded = clean(data.get("content"))
    if encoded and data.get("encoding") == "base64":
        raw = base64.b64decode(encoded).decode("utf-8-sig")
    else:
        # GitHub may omit inline content for larger files. Fetch the raw file instead.
        download_url = clean(data.get("download_url"))
        if not download_url:
            return [], sha
        raw_r = requests.get(download_url, headers=github_headers(), timeout=60)
        raw_r.raise_for_status()
        raw = raw_r.content.decode("utf-8-sig")

    rows = list(csv.DictReader(io.StringIO(raw))) if raw.strip() else []
    return rows, sha


def github_write_csv(path: str, rows: List[Dict[str, str]], sha: Optional[str]):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    content = base64.b64encode(buf.getvalue().encode("utf-8")).decode("ascii")
    payload = {
        "message": f"Update {path} from Apify webhook",
        "content": content,
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    r = requests.put(url, headers=github_headers(), json=payload, timeout=30)
    r.raise_for_status()


def append_bucket(path: str, new_rows: List[Dict[str, str]]) -> int:
    if not new_rows:
        return 0
    existing, sha = github_read_csv(path)
    merged = existing + new_rows
    seen = set()
    final = []
    # Newest copy wins while preserving a stable cumulative file.
    for row in reversed(merged):
        key = dedupe_key(row)
        if key not in seen:
            seen.add(key)
            final.append({c: clean(row.get(c)) for c in OUTPUT_COLUMNS})
    final.reverse()
    github_write_csv(path, final, sha)
    return len(final) - len(existing)



def rebuild_email_week_buckets(new_email_rows: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Rebuild ALL email week buckets on every successful Actor run.

    Existing Week1/2/3 rows are loaded, combined with this run's email leads,
    deduped, their age is recalculated from ListDate using today's UTC date,
    and then the files are rewritten. Leads older than 21 days disappear from
    the active week buckets automatically.

    LEGACY_EMAIL_FILE is also imported if it exists so an older Email.csv can
    be migrated into the week system.
    """
    sources = [LEGACY_EMAIL_FILE, WEEK1_FILE, WEEK2_FILE, WEEK3_FILE]
    all_rows: List[Dict[str, str]] = []
    shas: Dict[str, Optional[str]] = {}

    for path in sources:
        rows, sha = github_read_csv(path)
        shas[path] = sha
        all_rows.extend(rows)

    all_rows.extend(new_email_rows)

    # Newest copy wins across all email files.
    deduped: Dict[str, Dict[str, str]] = {}
    order: List[str] = []
    for row in all_rows:
        normalized = {c: clean(row.get(c)) for c in OUTPUT_COLUMNS}
        key = dedupe_key(normalized)
        if key not in deduped:
            order.append(key)
        deduped[key] = normalized

    week_rows = {
        WEEK1_FILE: [],
        WEEK2_FILE: [],
        WEEK3_FILE: [],
    }
    aged_out = 0
    missing_list_date = 0

    for key in order:
        row = deduped[key]
        path = email_week_path(row)
        if path:
            week_rows[path].append(row)
        else:
            if clean(row.get("ListDate")):
                aged_out += 1
            else:
                missing_list_date += 1

    # Rewrite all week files every run, so movement/removal is automatic.
    for path in (WEEK1_FILE, WEEK2_FILE, WEEK3_FILE):
        _, sha = github_read_csv(path)
        github_write_csv(path, week_rows[path], sha)

    # If an old Email.csv exists, clear it after migration so it cannot become
    # a stale fourth email bucket.
    if shas.get(LEGACY_EMAIL_FILE):
        github_write_csv(LEGACY_EMAIL_FILE, [], shas[LEGACY_EMAIL_FILE])

    return {
        WEEK1_FILE: len(week_rows[WEEK1_FILE]),
        WEEK2_FILE: len(week_rows[WEEK2_FILE]),
        WEEK3_FILE: len(week_rows[WEEK3_FILE]),
        "aged_out_over_21_days": aged_out,
        "ignored_missing_list_date": missing_list_date,
    }


def extract_dataset_id(payload: Dict[str, Any]) -> str:
    paths = [
        "resource/defaultDatasetId",
        "resource/defaultDatasetId",  # common Apify webhook payload
        "eventData/actorRun/defaultDatasetId",
        "eventData/defaultDatasetId",
        "defaultDatasetId",
    ]
    for p in paths:
        val = clean(getv(payload, p))
        if val:
            return val

    run_id = first(payload, ["resource/id", "eventData/actorRunId", "actorRunId"])
    if run_id:
        r = requests.get(
            f"https://api.apify.com/v2/actor-runs/{run_id}",
            params={"token": APIFY_TOKEN}, timeout=30
        )
        r.raise_for_status()
        return clean(r.json().get("data", {}).get("defaultDatasetId"))
    return ""


def fetch_dataset(dataset_id: str) -> List[Dict[str, Any]]:
    url = f"https://api.apify.com/v2/datasets/{dataset_id}/items"
    params = {"clean": "true", "format": "json"}
    if APIFY_TOKEN:
        params["token"] = APIFY_TOKEN
    r = requests.get(url, params=params, timeout=180)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError("Apify dataset response was not a JSON list")
    return data


def check_config():
    missing = [name for name, val in {
        "APIFY_TOKEN": APIFY_TOKEN,
        "GITHUB_TOKEN": GITHUB_TOKEN,
        "GITHUB_REPO": GITHUB_REPO,
    }.items() if not val]
    if missing:
        raise RuntimeError("Missing Railway variables: " + ", ".join(missing))


@app.get("/")
def health():
    return {"ok": True, "service": "realtor-lead-router-v2"}


@app.post("/apify-webhook")
async def apify_webhook(request: Request):
    if WEBHOOK_SECRET and request.query_params.get("secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
    try:
        check_config()
        payload = await request.json()
        dataset_id = extract_dataset_id(payload)
        if not dataset_id:
            raise RuntimeError("Could not find defaultDatasetId in Apify webhook")

        items = fetch_dataset(dataset_id)
        email_rows: List[Dict[str, str]] = []
        buckets = {FB_FILE: [], SMS_FILE: []}
        ignored = 0
        run_seen = set()

        for item in items:
            lead = format_lead(item)
            key = dedupe_key(lead)
            if key in run_seen:
                continue
            run_seen.add(key)

            bucket = route_lead(lead)
            if bucket == "EMAIL":
                # Email leads require a valid Listed date and are assigned to
                # Week1/2/3 by current age.
                if email_week_path(lead):
                    email_rows.append(lead)
                else:
                    ignored += 1
            elif bucket:
                buckets[bucket].append(lead)
            else:
                ignored += 1

        # Critical behavior: every webhook recalculates ALL existing email leads,
        # not just the new batch. A 6-day-old Week1 lead will therefore become
        # Week2 automatically when a later batch runs after it reaches day 8.
        email_totals = rebuild_email_week_buckets(email_rows)

        totals = {
            "email_weeks": email_totals,
        }
        for path, rows in buckets.items():
            added = append_bucket(path, rows)
            totals[path] = {"routed_this_run": len(rows), "newly_added": added}

        return {
            "ok": True,
            "dataset_id": dataset_id,
            "dataset_items": len(items),
            "unique_items": len(run_seen),
            "ignored": ignored,
            "email_routed_this_run": len(email_rows),
            "files": totals,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
