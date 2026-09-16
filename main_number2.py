import os
import re
import time
from datetime import datetime, timezone
from io import StringIO
from urllib.parse import urlparse

import pandas as pd
import requests
from fastapi import FastAPI, Request, HTTPException
from github import Github, GithubException


app = FastAPI()


# =========================================================
# ENVIRONMENT VARIABLES
# =========================================================

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_FOLDER = os.getenv("GITHUB_FOLDER", "data")

APIFY_TOKEN = os.getenv("APIFY_TOKEN")
BOUNCER_API_KEY = os.getenv("BOUNCER_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")


if not GITHUB_TOKEN:
    raise RuntimeError("Missing Railway variable: GITHUB_TOKEN")

if not GITHUB_REPO:
    raise RuntimeError("Missing Railway variable: GITHUB_REPO")

if not APIFY_TOKEN:
    raise RuntimeError("Missing Railway variable: APIFY_TOKEN")

if not BOUNCER_API_KEY:
    raise RuntimeError("Missing Railway variable: BOUNCER_API_KEY")

if not OPENAI_API_KEY:
    raise RuntimeError("Missing Railway variable: OPENAI_API_KEY")


github = Github(GITHUB_TOKEN)


# =========================================================
# BASIC HELPERS
# =========================================================

def get_nested(data, path, default=None):
    current = data

    try:
        for key in path:

            if isinstance(key, int):
                if not isinstance(current, list):
                    return default

                if len(current) <= key:
                    return default

                current = current[key]

            else:
                if not isinstance(current, dict):
                    return default

                current = current.get(key)

                if current is None:
                    return default

        return current

    except (KeyError, IndexError, TypeError):
        return default


def clean_text(value):
    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    return value


def clean_price(value):
    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    cleaned = re.sub(r"[^\d.]", "", value)

    if not cleaned:
        return None

    try:
        return int(float(cleaned))

    except ValueError:
        return None


# =========================================================
# ADDRESS + PHONE HELPERS (ACTOR #2)
# =========================================================

def clean_address(value):
    """Actor #2 already provides a clean street address in address_line."""
    return clean_text(value)


def clean_phone(value):
    """Normalize a US/Canada 10-digit phone to 1XXXXXXXXXX."""
    if value is None:
        return None

    digits = re.sub(r"\D", "", str(value))

    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]

    if len(digits) != 10:
        return None

    return "1" + digits


def split_agent_name(full_name):
    """
    Split primary_agent_name into FirstName / LastName.
    Middle names/initials stay with LastName so no information is discarded.
    """
    name = clean_text(full_name)

    if not name:
        return None, None

    parts = name.split()

    if len(parts) == 1:
        # Some actor rows can contain names like "NICKALLEN".
        # Keep the value instead of guessing where the surname starts.
        return parts[0], None

    return parts[0], " ".join(parts[1:])


# =========================================================
# DOWNLOAD APIFY DATA
# =========================================================

def download_apify_dataset(dataset_id):

    url = (
        f"https://api.apify.com/v2/datasets/"
        f"{dataset_id}/items"
    )

    params = {
        "token": APIFY_TOKEN,
        "clean": "true",
        "format": "json",
    }

    response = requests.get(
        url,
        params=params,
        timeout=60
    )

    if response.status_code != 200:

        print(
            "Apify download failed:",
            response.status_code,
            response.text[:500]
        )

        raise HTTPException(
            status_code=500,
            detail=(
                "Could not download Apify dataset. "
                f"Status: {response.status_code}"
            )
        )

    try:
        data = response.json()

    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Apify dataset did not return valid JSON"
        )

    if not isinstance(data, list):
        raise HTTPException(
            status_code=500,
            detail="Apify dataset did not return a list"
        )

    return data


# =========================================================
# EXTRACT REALTOR.COM ACTOR #2 DATA
# =========================================================

def extract_records(listings):

    rows = []

    for listing in listings:

        if not isinstance(listing, dict):
            continue

        first_name, last_name = split_agent_name(
            listing.get("primary_agent_name")
        )

        rows.append({
            "Bedrooms": clean_text(listing.get("beds")),
            "FirstName": clean_text(first_name),
            "LastName": clean_text(last_name),
            "Phone": clean_phone(listing.get("primary_agent_phone")),
            "Address": clean_address(listing.get("address_line")),
            "City": clean_text(listing.get("address_city")),
            "Price": clean_price(listing.get("list_price")),
            "Website": clean_text(listing.get("primary_agent_href")),
            "Email": clean_text(listing.get("primary_agent_email")),
            "PublicRemarks": clean_text(listing.get("description_text")),
            # Listing identity / age / live-status fields from Actor #2.
            "PropertyID": clean_text(listing.get("property_id")),
            "ListingID": clean_text(listing.get("listing_id")),
            "PropertyURL": clean_text(listing.get("href")),
            "ListDate": clean_text(listing.get("list_date")),
            "ActorStatus": clean_text(listing.get("status")),
            "ActorDisplayStatus": clean_text(listing.get("display_status")),
            "ActorIsPending": listing.get("flag_is_pending"),
        })

    return rows


# =========================================================
# CLEAN DATAFRAME
# =========================================================

def clean_dataframe(rows):

    columns = [
        "Bedrooms", "FirstName", "LastName", "Phone", "Address", "City",
        "Price", "Website", "Email", "PublicRemarks", "PropertyID",
        "ListingID", "PropertyURL", "ListDate", "ActorStatus",
        "ActorDisplayStatus", "ActorIsPending",
    ]

    df = pd.DataFrame(rows, columns=columns)
    if df.empty:
        return df

    df = df.dropna(how="all")

    text_columns = [
        "FirstName", "LastName", "City", "Website", "Email", "PublicRemarks",
        "PropertyID", "ListingID", "PropertyURL", "ListDate", "ActorStatus",
        "ActorDisplayStatus",
    ]
    for column in text_columns:
        df[column] = df[column].astype("string").str.strip()

    df["Phone"] = df["Phone"].astype("string")

    for column in ["Website", "Email", "PropertyURL", "ListDate", "PropertyID", "ListingID"]:
        df[column] = df[column].replace({
            "": pd.NA, "None": pd.NA, "none": pd.NA, "nan": pd.NA, "<NA>": pd.NA,
        })

    df["Email"] = df["Email"].str.lower()

    # IMPORTANT: do not dedupe by email here. One agent can have several listings
    # with different ages/statuses. Keep one row per property/listing first; we
    # dedupe by email only after Week 2/3 + live-status filtering.
    with_property_id = df[df["PropertyID"].notna()].drop_duplicates(
        subset=["PropertyID"], keep="last"
    )
    without_property_id = df[df["PropertyID"].isna()].copy()
    if not without_property_id.empty:
        fallback_keys = ["ListingID", "PropertyURL", "FirstName", "LastName", "Address"]
        fallback_keys = [c for c in fallback_keys if c in without_property_id.columns]
        without_property_id = without_property_id.drop_duplicates(
            subset=fallback_keys, keep="last"
        )

    df = pd.concat([with_property_id, without_property_id], ignore_index=True)

    df = df.dropna(
        subset=["FirstName", "LastName", "Phone", "Email"], how="all"
    )
    return df.reset_index(drop=True)


# =========================================================
# WEBSITE -> DOMAIN
# =========================================================

def get_domain_from_website(website):
    if website is None or pd.isna(website):
        return None

    website = str(website).strip()

    if not website:
        return None

    if not website.startswith(("http://", "https://")):
        website = "https://" + website

    try:
        parsed = urlparse(website)
        domain = parsed.netloc.lower().strip()

        if domain.startswith("www."):
            domain = domain[4:]

        domain = domain.split(":")[0]
        return domain or None

    except Exception:
        return None


# =========================================================
# WEEK BUCKETS + REALTOR.COM LIVE STATUS GATE
# =========================================================

ACTIVE_REALTOR_STATUSES = {
    "for_sale", "for sale", "active", "ready_to_build", "ready to build",
}
INACTIVE_REALTOR_STATUSES = {
    "pending", "contingent", "sold", "off_market", "off market",
    "not_for_sale", "not for sale", "withdrawn", "expired", "closed",
}


def truthy(value):
    if value is None or pd.isna(value):
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "t"}


def normalize_status(value):
    value = clean_text(value)
    if not value:
        return ""
    return re.sub(r"[\s-]+", "_", value.lower())


def add_week_bucket(df, now=None):
    """Bucket listings by real list_date: Week 1=0-7, Week 2=8-14, Week 3=15-21 days."""
    result = df.copy()
    if now is None:
        now = datetime.now(timezone.utc)
    now_ts = pd.Timestamp(now)
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize("UTC")
    else:
        now_ts = now_ts.tz_convert("UTC")

    list_dates = pd.to_datetime(result["ListDate"], utc=True, errors="coerce")
    # Calendar age avoids a listing flipping buckets just because it was posted
    # a few hours earlier/later in the day.
    today = now_ts.normalize()
    list_days = list_dates.dt.normalize()
    age_days = (today - list_days).dt.days

    result["ListingAgeDays"] = age_days.astype("Int64")
    result["LeadWeek"] = pd.NA
    result.loc[age_days.between(0, 7, inclusive="both"), "LeadWeek"] = "Week 1"
    result.loc[age_days.between(8, 14, inclusive="both"), "LeadWeek"] = "Week 2"
    result.loc[age_days.between(15, 21, inclusive="both"), "LeadWeek"] = "Week 3"
    return result


def actor_status_allows_live_check(row):
    """Cheap pre-filter. Known pending/sold/off-market rows never hit Bouncer."""
    if truthy(row.get("ActorIsPending")):
        return False, "actor_pending"

    display_status = normalize_status(row.get("ActorDisplayStatus"))
    status = normalize_status(row.get("ActorStatus"))

    inactive = {normalize_status(x) for x in INACTIVE_REALTOR_STATUSES}
    if display_status in inactive:
        return False, f"actor_{display_status}"
    if status in inactive:
        return False, f"actor_{status}"

    return True, "actor_pass"


def _first_realtor_status_from_html(html):
    """Read the earliest main-property-looking status signal from Realtor HTML/JSON."""
    if not html:
        return None

    patterns = [
        r'["\\]display_status["\\]\s*:\s*["\\]([A-Za-z_ -]+)["\\]',
        r'["\\]displayStatus["\\]\s*:\s*["\\]([A-Za-z_ -]+)["\\]',
        r'["\\]status["\\]\s*:\s*["\\](for_sale|ready_to_build|pending|contingent|sold|off_market|not_for_sale|withdrawn|expired|closed)["\\]',
    ]

    matches = []
    for pattern in patterns:
        for match in re.finditer(pattern, html, flags=re.I):
            matches.append((match.start(), normalize_status(match.group(1))))

    if matches:
        matches.sort(key=lambda item: item[0])
        return matches[0][1]

    # Conservative text fallback. Only use strong page-level wording.
    head = html[:250000].lower()
    title_match = re.search(r"<title[^>]*>(.*?)</title>", head, flags=re.I | re.S)
    title = re.sub(r"<[^>]+>", " ", title_match.group(1)) if title_match else ""
    title = re.sub(r"\s+", " ", title).strip()

    if any(term in title for term in ["off market", "recently sold", "property sold", "pending"]):
        for term, status in [
            ("off market", "off_market"), ("recently sold", "sold"),
            ("property sold", "sold"), ("pending", "pending"),
        ]:
            if term in title:
                return status
    if "for sale" in title:
        return "for_sale"
    return None


def check_realtor_live_status(property_url):
    """
    Load the exact Actor #2 href immediately before Bouncer.
    Only an explicit active/for-sale result is contactable.
    404/410 and sold/pending/off-market are rejected.
    Blocks/timeouts/ambiguous pages are HELD, never treated as active.
    """
    property_url = clean_text(property_url)
    if not property_url:
        return {"contactable": False, "outcome": "missing_url", "status": None, "http_status": None}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }

    for attempt in range(3):
        try:
            response = requests.get(
                property_url, headers=headers, timeout=25, allow_redirects=True
            )
        except requests.RequestException as exc:
            print("Realtor live-check connection error:", property_url, exc)
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return {"contactable": False, "outcome": "request_error", "status": None, "http_status": None}

        code = response.status_code
        if code in (404, 410):
            return {"contactable": False, "outcome": "not_found", "status": "off_market", "http_status": code}

        if code in (408, 429, 500, 502, 503, 504):
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return {"contactable": False, "outcome": "request_error", "status": None, "http_status": code}

        # A block/challenge is NOT evidence that the listing is active.
        if code in (401, 403):
            return {"contactable": False, "outcome": "blocked", "status": None, "http_status": code}

        if code != 200:
            return {"contactable": False, "outcome": "http_error", "status": None, "http_status": code}

        status = _first_realtor_status_from_html(response.text)
        active = {normalize_status(x) for x in ACTIVE_REALTOR_STATUSES}
        inactive = {normalize_status(x) for x in INACTIVE_REALTOR_STATUSES}

        if status in active:
            return {"contactable": True, "outcome": "active", "status": status, "http_status": code}
        if status in inactive:
            return {"contactable": False, "outcome": "inactive", "status": status, "http_status": code}

        # Ambiguous 200 page: hold it. Never spend Bouncer credits or contact it.
        return {"contactable": False, "outcome": "ambiguous", "status": status, "http_status": code}

    return {"contactable": False, "outcome": "request_error", "status": None, "http_status": None}


def enrich_with_live_realtor_status(df):
    checked = df.copy()
    checked["LiveRealtorStatus"] = pd.NA
    checked["LiveStatusOutcome"] = pd.NA
    checked["LiveHTTPStatus"] = pd.NA
    checked["LiveContactable"] = False

    for index, row in checked.iterrows():
        actor_ok, actor_reason = actor_status_allows_live_check(row)
        if not actor_ok:
            checked.at[index, "LiveStatusOutcome"] = actor_reason
            checked.at[index, "LiveContactable"] = False
            continue

        result = check_realtor_live_status(row.get("PropertyURL"))
        checked.at[index, "LiveRealtorStatus"] = result.get("status")
        checked.at[index, "LiveStatusOutcome"] = result.get("outcome")
        checked.at[index, "LiveHTTPStatus"] = result.get("http_status")
        checked.at[index, "LiveContactable"] = bool(result.get("contactable"))

    return checked


def week_file_path(week_name):
    safe_week = week_name.replace("/", "-")
    return f"{GITHUB_FOLDER}/{safe_week} leads/leads.csv"


def read_github_csv(repo, path):
    """Read a GitHub CSV. Missing/empty files return an empty DataFrame."""
    try:
        content_file = repo.get_contents(path, ref=GITHUB_BRANCH)
        text = content_file.decoded_content.decode("utf-8")
        if not text.strip():
            return pd.DataFrame()
        return pd.read_csv(StringIO(text), dtype="string")
    except GithubException as exc:
        if exc.status == 404:
            return pd.DataFrame()
        raise


def dedupe_listing_rows(df):
    """Keep one current row per property, falling back safely when PropertyID is missing."""
    if df.empty:
        return df.copy()

    work = df.copy()
    for column in ["PropertyID", "ListingID", "PropertyURL", "Address", "FirstName", "LastName"]:
        if column not in work.columns:
            work[column] = pd.NA

    with_property = work[
        work["PropertyID"].notna() & (work["PropertyID"].astype("string").str.strip() != "")
    ].drop_duplicates(subset=["PropertyID"], keep="last")

    without_property = work[
        work["PropertyID"].isna() | (work["PropertyID"].astype("string").str.strip() == "")
    ].copy()
    fallback = ["ListingID", "PropertyURL", "Address", "FirstName", "LastName"]
    without_property = without_property.drop_duplicates(subset=fallback, keep="last")
    return pd.concat([with_property, without_property], ignore_index=True)


def has_value(series):
    """True where a pandas Series contains a real non-blank value."""
    return series.notna() & (series.astype("string").str.strip() != "")


def load_and_rebucket_week_leads(repo, fresh_df):
    """
    Rolling EMAIL conveyor belt.

    Week 1 / Week 2 / Week 3 contain ONLY leads that have an actor-provided email.
    Existing no-email rows already stored in old week files are removed on the next
    run and returned for FB/SMS routing.

    Email leads are re-aged from the ORIGINAL ListDate and move Week 1 -> 2 -> 3.
    Rows older than 21 days (or with invalid dates) fall out of the active buckets.
    """
    stored_frames = []
    for week_name in ["Week 1", "Week 2", "Week 3"]:
        existing = read_github_csv(repo, week_file_path(week_name))
        if not existing.empty:
            stored_frames.append(existing)

    frames = stored_frames + [fresh_df.copy()]
    combined = pd.concat(frames, ignore_index=True, sort=False) if frames else fresh_df.copy()
    combined = dedupe_listing_rows(combined)

    if "Email" not in combined.columns:
        combined["Email"] = pd.NA

    email_mask = has_value(combined["Email"])
    no_email = combined[~email_mask].copy().reset_index(drop=True)
    email_only = combined[email_mask].copy().reset_index(drop=True)

    email_only = add_week_bucket(email_only)

    week1 = email_only[email_only["LeadWeek"] == "Week 1"].copy().reset_index(drop=True)
    week2 = email_only[email_only["LeadWeek"] == "Week 2"].copy().reset_index(drop=True)
    week3 = email_only[email_only["LeadWeek"] == "Week 3"].copy().reset_index(drop=True)
    expired = email_only[email_only["LeadWeek"].isna()].copy().reset_index(drop=True)

    return week1, week2, week3, expired, no_email

def replace_week_file(repo, week_name, rows):
    """
    Replace (not append) a week's CSV with its CURRENT members.
    This is what physically removes a lead from Week 1 when it becomes Week 2,
    and from Week 2 when it becomes Week 3.
    """
    path = week_file_path(week_name)
    rows = rows.copy()
    csv_content = rows.to_csv(index=False)

    try:
        existing_file = repo.get_contents(path, ref=GITHUB_BRANCH)
        repo.update_file(
            path=path,
            message=f"Rebucket {week_name} leads",
            content=csv_content,
            sha=existing_file.sha,
            branch=GITHUB_BRANCH,
        )
    except GithubException as exc:
        if exc.status != 404:
            raise
        # GitHub has no real empty folders. Only create the path once there is
        # at least one lead; otherwise it will appear automatically later.
        if rows.empty:
            return 0
        repo.create_file(
            path=path,
            message=f"Create {week_name} leads",
            content=csv_content,
            branch=GITHUB_BRANCH,
        )
    return len(rows)


# =========================================================
# BOUNCER EMAIL VERIFIER
# =========================================================

def bouncer_verify_email(email):
    """Verify an actor-provided email with Bouncer's real-time API."""
    email = clean_text(email)
    if not email or "@" not in email:
        return {"outcome": "invalid_input", "email": email, "status": None, "reason": None}

    url = "https://api.usebouncer.com/v1/email/verify"
    headers = {"x-api-key": BOUNCER_API_KEY}
    params = {"email": email, "timeout": 30}

    for attempt in range(3):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=35)
        except requests.RequestException as exc:
            print("Bouncer connection error:", email, exc)
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return {"outcome": "api_error", "email": email, "status": None, "reason": None}

        if response.status_code == 200:
            try:
                payload = response.json()
            except Exception:
                return {"outcome": "api_error", "email": email, "status": None, "reason": None}

            if isinstance(payload, list):
                data = payload[0] if payload else {}
            elif isinstance(payload, dict):
                data = payload
            else:
                data = {}

            status = clean_text(data.get("status"))
            reason = clean_text(data.get("reason"))
            verified_email = clean_text(data.get("email")) or email
            print("Bouncer VERIFIED:", verified_email, "status:", status, "reason:", reason)
            return {
                "outcome": "verified",
                "email": verified_email,
                "status": status,
                "reason": reason,
            }

        if response.status_code in (408, 409, 429, 500, 502, 503, 504):
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return {"outcome": "api_error", "email": email, "status": None, "reason": None}

        print("Bouncer verifier error:", response.status_code, email, response.text[:300])
        return {"outcome": "invalid_input", "email": email, "status": None, "reason": None}

    return {"outcome": "api_error", "email": email, "status": None, "reason": None}


# =========================================================
# OPENAI PERSONALIZATION
# =========================================================

PERSONALIZATION_INSTRUCTIONS = """
You write ONE very short, casual personalization line for a real-estate cold email.

Your job is NOT to describe or sell the property. Your job is to sound like a real person who skimmed the listing, noticed one specific thing, and had a quick reaction to it.

STYLE:
- Pick ONE concrete, specific detail from the listing.
- Make a quick natural observation or reaction to it.
- OBSERVATION FIRST. A natural everyday connection is second. Complimenting the design is a distant third.
- When a detail is already unusual or interesting, simply react to what makes it unusual; do not tack on praise afterward.
- Do not finish an observation with generic approval just because the sentence needs an ending.
- LIGHT WIT is welcome when the feature naturally creates an obvious everyday connection.
- The wit should feel effortless and mildly amusing, not like a punchline.
- You may connect a listing feature to a common-sense everyday situation when the connection is obvious.
- Do NOT invent a detailed scenario, character, lifestyle, or story just to make the line funny.
- If no natural witty angle exists, use a sharp neutral observation instead.
- Compliment a feature only when praise genuinely feels natural and deserved.
- Excessive compliments sound salesy, so do not hunt for something to praise.
- Neutral reactions such as "that just makes sense", "that's a pretty rare combo", "opens up a lot of options", or "interesting having..." are preferred.
- Strong praise such as "genius", "brilliant", "amazing", "perfect", or "great setup" should be occasional, not the default.
- Sound casual, spontaneous, and slightly opinionated.
- Vary the reaction and sentence structure. Do not reuse the same reaction on every listing.
- Contractions and conversational wording are welcome.
- Prefer the shortest natural version of the thought.
- Aim for 7-12 words. Never intentionally pad a line just to reach a word count.
- The line must be a complete thought.
- The personalization is only a quick aside before the sender asks a question.

DETAIL SELECTION:
- Prefer unusual, clever, memorable, recently upgraded, or genuinely distinctive details.
- A detail that makes someone naturally think "that's smart", "that's unusual", or "that's cool" is better than a generic bedroom, countertop, or open-plan feature.
- Do not invent anything that is not clearly supported by the listing.

DO NOT:
- Do not sound like a property brochure, realtor, copywriter, or AI.
- Do not explain obvious benefits just to make the sentence longer.
- Avoid phrases like "real everyday value", "provides flexibility", "enhanced convenience", "strong selling point", "genuinely useful", "reassuring", "big-ticket updates", "ideal for", or "the next owner".
- Never use filler approval phrases such as "thoughtful touch", "nice touch", "nice detail", "thoughtful detail", "great touch", "great feature", "smart design", or "well thought out".
- Do not default to formulas like "X makes Y easier" or "X gives buyers Y".
- Do not mention the agent, address, price, greeting, or the rest of the email.
- Do not ask a question.
- Do not use quotation marks.
- Do not force a joke, pun, or exaggerated compliment.
- Do not create sitcom-style scenarios or made-up household stories.
- Do not assume specific hobbies, family situations, or buyer behavior unless the listing strongly supports the connection.
- A small smile is the goal; a punchline is not.
- Do not compliment every listing.
- Do not use praise merely to sound personalized.
- Avoid repeatedly calling features genius, brilliant, amazing, perfect, great, impressive, or fantastic.
- Do not make unsupported claims.

GOOD EXAMPLES:
Listing: laundry is upstairs beside the bedrooms
LINE: Laundry upstairs means no carrying baskets up and down all day.
CONFIDENCE: high

Listing: oversized walk-in pantry
LINE: That giant pantry could hide a serious snack problem.
CONFIDENCE: high

Listing: no carpet anywhere
LINE: No carpet anywhere? That's a dream for someone with dogs.
CONFIDENCE: high

Listing: property has no HOA and allows chickens
LINE: No HOA and chickens allowed is a pretty rare combo.
CONFIDENCE: high

Listing: separate basement entrance
LINE: That separate basement entrance opens up a lot of options.
CONFIDENCE: high

Listing: guest suite is completely separate downstairs
LINE: Interesting having the guest suite completely separate downstairs.
CONFIDENCE: high

Listing: laundry room can also be used as a desk/office area
LINE: That laundry room doubling as a desk is genius.
CONFIDENCE: high

Listing: 1,200-square-foot wired workshop with its own toilet
LINE: A wired workshop that size with its own toilet is pretty rare.
CONFIDENCE: high

Listing: three separate flex spaces
LINE: Three separate flex spaces gives the place plenty of wiggle room.
CONFIDENCE: high

Listing: two staircases lead to the second floor
LINE: Two separate staircases upstairs is pretty unusual, you don't see that often.
CONFIDENCE: high

BAD EXAMPLES:
LINE: Interesting having such a private guest suite—it's a thoughtful touch.
LINE: Two staircases to the second floor—that's an unusually thoughtful touch.
LINE: No carpet anywhere is a pretty nice detail.
LINE: Three-car garage? Someone's finally keeping the bikes out of the kitchen.
LINE: The laundry area gives that extra space real everyday value.
LINE: The new roof is reassuring for the next owner.
LINE: The separate entrance provides buyers with additional flexibility.
LINE: The quartz countertops are a strong selling point.
LINE: The workshop is genuinely useful for future homeowners.
LINE: Having the pantry near the kitchen should make everyday storage much easier.

OUTPUT EXACTLY:
LINE: <comment or NONE>
CONFIDENCE: <high/medium/low>
"""

def extract_openai_output_text(payload):
    parts = []
    for item in payload.get("output") or []:
        for content in item.get("content") or []:
            if content.get("type") == "output_text" and content.get("text"):
                parts.append(content["text"])
    return "\n".join(parts).strip()

def parse_personalization_response(text):
    """Parse and validate OpenAI personalization without saving broken/truncated lines."""
    raw = (text or "").strip()

    if not raw:
        return {"detail": "NONE", "confidence": "low", "outcome": "parse_error"}

    # Remove accidental Markdown code fences.
    raw = re.sub(r"^```(?:text)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw).strip()

    confidence_match = re.search(
        r"CONFIDENCE\s*:\s*(high|medium|low)",
        raw,
        re.I,
    )
    confidence = confidence_match.group(1).lower() if confidence_match else "medium"

    # Preferred format: LINE: <comment>
    line_match = re.search(
        r"(?:^|\n)\s*(?:[-*]\s*)?LINE\s*:\s*(.+?)(?=\n\s*(?:[-*]\s*)?CONFIDENCE\s*:|\Z)",
        raw,
        re.I | re.S,
    )

    if line_match:
        detail = line_match.group(1).strip()
    else:
        # Fallback: if OpenAI returned only the sentence, keep it.
        candidate_lines = []
        for line in raw.splitlines():
            cleaned = line.strip()
            if not cleaned:
                continue
            if re.match(r"^(?:[-*]\s*)?CONFIDENCE\s*:", cleaned, re.I):
                continue
            cleaned = re.sub(r"^(?:[-*]\s*)?(?:LINE\s*:)?\s*", "", cleaned, flags=re.I)
            if cleaned:
                candidate_lines.append(cleaned)

        detail = " ".join(candidate_lines).strip()

    detail = detail.strip().strip('"').strip("'").strip()

    if not detail or detail.upper() == "NONE":
        return {"detail": "NONE", "confidence": confidence, "outcome": "no_detail"}

    detail = re.sub(
        r"\s+CONFIDENCE\s*:\s*(high|medium|low)\s*$",
        "",
        detail,
        flags=re.I,
    ).strip()

    # Keep personalization concise.
    words = detail.split()
    word_count = len(words)
    if word_count < 6 or word_count > 15:
        return {"detail": "NONE", "confidence": confidence, "outcome": "parse_error"}

    # Reject obvious unfinished/truncated endings.
    bad_last_words = {
        "a", "an", "the", "and", "or", "but", "to", "from", "with", "for",
        "of", "in", "on", "at", "by", "into", "over", "under", "than", "that",
        "this", "those", "these", "their", "your", "his", "her", "its", "some",
        "any", "more"
    }

    last_word = re.sub(r"[^A-Za-z']", "", words[-1]).lower()
    if last_word in bad_last_words:
        return {"detail": "NONE", "confidence": confidence, "outcome": "parse_error"}

    # Reject trailing punctuation/patterns that strongly suggest the sentence was cut off.
    if re.search(r"[-–—,:;/]\s*$", detail):
        return {"detail": "NONE", "confidence": confidence, "outcome": "parse_error"}

    # Reject a few templated phrases we specifically want to avoid.
    lower = detail.lower()
    banned_phrases = [
        "strong selling point",
        "big-ticket updates",
        "genuinely useful",
        "nice property",
        "great feature",
        "real everyday value",
        "provides flexibility",
        "enhanced convenience",
        "reassuring for the next owner",
        "the next owner",
        "thoughtful touch",
        "nice touch",
        "nice detail",
        "thoughtful detail",
        "great touch",
        "great feature",
        "smart design",
        "well thought out",
    ]
    if any(phrase in lower for phrase in banned_phrases):
        return {"detail": "NONE", "confidence": confidence, "outcome": "parse_error"}

    return {"detail": detail, "confidence": confidence, "outcome": "found"}

def openai_extract_personalized_detail(public_remarks):
    public_remarks = clean_text(public_remarks)
    if not public_remarks:
        return {"detail": "NONE", "confidence": "low", "outcome": "invalid_input"}

    url = "https://api.openai.com/v1/responses"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": OPENAI_MODEL,
        "instructions": PERSONALIZATION_INSTRUCTIONS,
        "input": "PROPERTY LISTING DESCRIPTION:\n" + public_remarks,
        "max_output_tokens": 160,
    }

    for attempt in range(3):
        try:
            response = requests.post(url, headers=headers, json=body, timeout=45)
        except requests.RequestException as exc:
            print("OpenAI connection error:", exc)
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return {"detail": "NONE", "confidence": "low", "outcome": "api_error"}

        if response.status_code == 200:
            try:
                payload = response.json()
            except Exception:
                return {"detail": "NONE", "confidence": "low", "outcome": "api_error"}

            raw_output = extract_openai_output_text(payload)
            print("OpenAI raw personalization:", repr(raw_output[:500]))

            parsed = parse_personalization_response(raw_output)

            # If OpenAI returned something malformed, give it another chance instead of
            # immediately throwing a valid email into the no-personalization file.
            if parsed["outcome"] == "parse_error" and attempt < 2:
                print("OpenAI personalization parse error or truncated line; retrying...")
                # On retry, make the formatting/completeness requirement extra explicit.
                body["input"] = (
                    "PROPERTY LISTING DESCRIPTION:\n"
                    + public_remarks
                    + "\n\nIMPORTANT: Return one COMPLETE natural sentence, ideally 10-12 words and never more than 15 words, "
                      "then CONFIDENCE. Do not end mid-thought."
                )
                time.sleep(1)
                continue

            return parsed

        if response.status_code in (408, 409, 429, 500, 502, 503, 504):
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue

        print("OpenAI error:", response.status_code, response.text[:300])
        return {"detail": "NONE", "confidence": "low", "outcome": "api_error"}

    return {"detail": "NONE", "confidence": "low", "outcome": "api_error"}

def enrich_leads_with_bouncer(df):
    enriched = df.copy()
    enriched["BouncerStatus"] = pd.NA
    enriched["BouncerReason"] = pd.NA
    enriched["BouncerOutcome"] = pd.NA
    enriched["EmailSource"] = "actor"

    for index, row in enriched.iterrows():
        email = row.get("Email")
        if pd.isna(email) or str(email).strip() == "":
            enriched.at[index, "BouncerOutcome"] = "not_found"
            enriched.at[index, "EmailSource"] = "none"
            continue
        result = bouncer_verify_email(str(email))
        enriched.at[index, "BouncerOutcome"] = result["outcome"]
        if result.get("email"):
            enriched.at[index, "Email"] = str(result["email"]).strip().lower()
        if result.get("status") is not None:
            enriched.at[index, "BouncerStatus"] = result["status"]
        if result.get("reason") is not None:
            enriched.at[index, "BouncerReason"] = result["reason"]
    return enriched

def enrich_personalization(valid_email_leads):
    enriched = valid_email_leads.copy()
    enriched["PersonalizedLine"] = pd.NA
    enriched["PersonalizationConfidence"] = pd.NA
    enriched["PersonalizationOutcome"] = pd.NA
    for index, row in enriched.iterrows():
        result = openai_extract_personalized_detail(row.get("PublicRemarks"))
        enriched.at[index, "PersonalizedLine"] = result["detail"]
        enriched.at[index, "PersonalizationConfidence"] = result["confidence"]
        enriched.at[index, "PersonalizationOutcome"] = result["outcome"]
    return enriched

# =========================================================
# UPDATE PERSISTENT EMAILLEADS.CSV ON GITHUB
# =========================================================

def update_email_leads_file(
    repo,
    new_email_leads
):
    """
    EmailLeads.csv is cumulative.

    Contains ONLY actor-provided emails that Bouncer marked deliverable
    and for which OpenAI produced a usable personalization detail.

    Existing valid leads are preserved.
    New leads are appended.
    Duplicate emails are removed.
    """

    email_path = (
        f"{GITHUB_FOLDER}/EmailLeads.csv"
    )

    if new_email_leads.empty:

        print(
            "No new email leads this run."
        )

        # If EmailLeads.csv already exists, return its current count.
        try:
            existing_file = repo.get_contents(
                email_path,
                ref=GITHUB_BRANCH
            )

            existing_text = (
                existing_file
                .decoded_content
                .decode("utf-8")
            )

            if not existing_text.strip():
                return 0

            existing_df = pd.read_csv(
                StringIO(existing_text),
                dtype="string"
            )

            return len(existing_df)

        except GithubException as exc:
            if exc.status == 404:
                return 0
            raise

    new_email_leads = (
        new_email_leads.copy()
    )

    email_columns = [
        "Bedrooms",
        "FirstName",
        "LastName",
        "Phone",
        "Address",
        "City",
        "Price",
        "Website",
        "Email",
        "PublicRemarks",
        "PropertyID", "ListingID", "PropertyURL", "ListDate", "ListingAgeDays", "LeadWeek",
        "ActorStatus", "ActorDisplayStatus", "ActorIsPending", "LiveRealtorStatus",
        "LiveStatusOutcome", "LiveHTTPStatus", "LiveContactable",
        "BouncerStatus",
        "BouncerReason",
        "EmailSource",
        "PersonalizedLine",
        "PersonalizationConfidence",
        "PersonalizationOutcome",
    ]

    for column in email_columns:

        if column not in new_email_leads.columns:
            new_email_leads[column] = pd.NA

    new_email_leads = (
        new_email_leads[email_columns]
    )

    # Normalize email values before deduplication.
    new_email_leads["Email"] = (
        new_email_leads["Email"]
        .astype("string")
        .str.strip()
        .str.lower()
    )

    new_email_leads["BouncerStatus"] = (
        new_email_leads["BouncerStatus"]
        .astype("string")
        .str.strip()
        .str.lower()
    )

    new_email_leads = (
        new_email_leads[
            new_email_leads["Email"].notna()
            &
            (new_email_leads["Email"] != "")
            &
            (new_email_leads["BouncerStatus"] == "deliverable")
        ]
        .copy()
    )

    try:

        existing_file = (
            repo.get_contents(
                email_path,
                ref=GITHUB_BRANCH
            )
        )

        existing_text = (
            existing_file
            .decoded_content
            .decode("utf-8")
        )

        if existing_text.strip():

            existing_df = pd.read_csv(
                StringIO(existing_text),
                dtype="string"
            )

        else:

            existing_df = pd.DataFrame(
                columns=email_columns
            )

        for column in email_columns:

            if column not in existing_df.columns:
                existing_df[column] = pd.NA

        existing_df = (
            existing_df[email_columns]
        )

        existing_df["BouncerStatus"] = (
            existing_df["BouncerStatus"]
            .astype("string")
            .str.strip()
            .str.lower()
        )

        existing_df = (
            existing_df[
                existing_df["BouncerStatus"] == "deliverable"
            ]
            .copy()
        )

        existing_df["Email"] = (
            existing_df["Email"]
            .astype("string")
            .str.strip()
            .str.lower()
        )

        combined = pd.concat(
            [
                existing_df,
                new_email_leads
            ],
            ignore_index=True
        )

        combined = (
            combined[
                combined["Email"].notna()
                &
                (combined["Email"] != "")
            ]
            .copy()
        )

        combined = combined.drop_duplicates(
            subset=["Email"],
            keep="first"
        )

        csv_content = (
            combined
            .to_csv(index=False)
        )

        repo.update_file(
            path=email_path,
            message="Update EmailLeads.csv",
            content=csv_content,
            sha=existing_file.sha,
            branch=GITHUB_BRANCH,
        )

        print(
            "Updated:",
            email_path,
            "total email leads:",
            len(combined)
        )

        return len(combined)

    except GithubException as exc:

        if exc.status == 404:

            new_email_leads = (
                new_email_leads
                .drop_duplicates(
                    subset=["Email"],
                    keep="first"
                )
            )

            csv_content = (
                new_email_leads
                .to_csv(index=False)
            )

            repo.create_file(
                path=email_path,
                message="Create EmailLeads.csv",
                content=csv_content,
                branch=GITHUB_BRANCH,
            )

            print(
                "Created:",
                email_path,
                "email leads:",
                len(new_email_leads)
            )

            return len(new_email_leads)

        raise

# =========================================================
# UPDATE PERSISTENT FBLEADS.CSV ON GITHUB
# =========================================================

def update_fb_leads_file(
    repo,
    new_fb_leads
):
    """
    FBleads.csv is cumulative.

    Existing rows are preserved.
    New no-email leads are appended.
    Duplicates are removed.
    """

    fb_path = (
        f"{GITHUB_FOLDER}/FBleads.csv"
    )


    # No new Facebook leads this run
    if new_fb_leads.empty:

        print(
            "No new FB leads this run."
        )

        return 0


    new_fb_leads = (
        new_fb_leads.copy()
    )


    # Keep useful fields
    fb_columns = [
        "Bedrooms",
        "FirstName",
        "LastName",
        "Phone",
        "Address",
        "City",
        "Price",
        "Website",
        "Email",
        "PublicRemarks",
        "PropertyID", "ListingID", "PropertyURL", "ListDate", "ListingAgeDays", "LeadWeek",
        "ActorStatus", "ActorDisplayStatus", "ActorIsPending", "LiveRealtorStatus",
        "LiveStatusOutcome", "LiveHTTPStatus", "LiveContactable",
        "BouncerStatus",
        "BouncerReason",
        "BouncerOutcome",
        "EmailSource",
    ]


    for column in fb_columns:

        if column not in new_fb_leads.columns:
            new_fb_leads[column] = pd.NA


    new_fb_leads = (
        new_fb_leads[fb_columns]
    )


    try:

        existing_file = (
            repo.get_contents(
                fb_path,
                ref=GITHUB_BRANCH
            )
        )


        existing_text = (
            existing_file
            .decoded_content
            .decode("utf-8")
        )


        if existing_text.strip():

            existing_df = pd.read_csv(
                StringIO(existing_text),
                dtype="string"
            )

        else:

            existing_df = pd.DataFrame(
                columns=fb_columns
            )


        # Make sure older file has
        # all current columns
        for column in fb_columns:

            if column not in existing_df.columns:
                existing_df[column] = pd.NA


        existing_df = (
            existing_df[fb_columns]
        )


        combined = pd.concat(
            [
                existing_df,
                new_fb_leads
            ],
            ignore_index=True
        )


        # Remove duplicate people
        combined = combined.drop_duplicates(
            subset=[
                "FirstName",
                "LastName",
                "Phone",
                "Address",
                "Website"
            ],
            keep="first"
        )


        csv_content = (
            combined
            .to_csv(index=False)
        )


        repo.update_file(
            path=fb_path,
            message="Update FBleads.csv",
            content=csv_content,
            sha=existing_file.sha,
            branch=GITHUB_BRANCH,
        )


        print(
            "Updated:",
            fb_path,
            "total leads:",
            len(combined)
        )


        return len(combined)


    except GithubException as exc:

        # File does not exist yet
        if exc.status == 404:

            csv_content = (
                new_fb_leads
                .drop_duplicates(
                    subset=[
                        "FirstName",
                        "LastName",
                        "Phone",
                        "Address",
                        "Website"
                    ],
                    keep="first"
                )
                .to_csv(index=False)
            )


            repo.create_file(
                path=fb_path,
                message="Create FBleads.csv",
                content=csv_content,
                branch=GITHUB_BRANCH,
            )


            print(
                "Created:",
                fb_path,
                "leads:",
                len(new_fb_leads)
            )


            return len(new_fb_leads)


        raise


# =========================================================
# UPDATE PERSISTENT SMS.CSV ON GITHUB
# =========================================================

def update_sms_leads_file(repo, new_sms_leads):
    """
    SMS.csv is cumulative.

    Contains leads with:
    - NO email
    - NO website
    - a usable phone number
    """
    sms_path = f"{GITHUB_FOLDER}/SMS.csv"

    if new_sms_leads.empty:
        print("No new SMS leads this run.")
        try:
            existing_file = repo.get_contents(sms_path, ref=GITHUB_BRANCH)
            text = existing_file.decoded_content.decode("utf-8")
            if not text.strip():
                return 0
            return len(pd.read_csv(StringIO(text), dtype="string"))
        except GithubException as exc:
            if exc.status == 404:
                return 0
            raise

    new_sms_leads = new_sms_leads.copy()

    sms_columns = [
        "Bedrooms", "FirstName", "LastName", "Phone", "Address", "City",
        "Price", "Website", "Email", "PublicRemarks",
        "PropertyID", "ListingID", "PropertyURL", "ListDate", "ListingAgeDays", "LeadWeek",
        "ActorStatus", "ActorDisplayStatus", "ActorIsPending",
    ]
    for column in sms_columns:
        if column not in new_sms_leads.columns:
            new_sms_leads[column] = pd.NA
    new_sms_leads = new_sms_leads[sms_columns]

    # Enforce the SMS rule again at the file boundary.
    email_blank = ~has_value(new_sms_leads["Email"])
    website_blank = ~has_value(new_sms_leads["Website"])
    phone_present = has_value(new_sms_leads["Phone"])
    new_sms_leads = new_sms_leads[email_blank & website_blank & phone_present].copy()

    try:
        existing_file = repo.get_contents(sms_path, ref=GITHUB_BRANCH)
        text = existing_file.decoded_content.decode("utf-8")
        existing = pd.read_csv(StringIO(text), dtype="string") if text.strip() else pd.DataFrame(columns=sms_columns)

        for column in sms_columns:
            if column not in existing.columns:
                existing[column] = pd.NA

        combined = pd.concat([existing[sms_columns], new_sms_leads], ignore_index=True)
        combined = combined.drop_duplicates(
            subset=["Phone", "PropertyID", "ListingID", "Address"],
            keep="last",
        )
        repo.update_file(
            path=sms_path,
            message="Update SMS.csv",
            content=combined.to_csv(index=False),
            sha=existing_file.sha,
            branch=GITHUB_BRANCH,
        )
        print("Updated:", sms_path, "total SMS leads:", len(combined))
        return len(combined)

    except GithubException as exc:
        if exc.status == 404:
            initial = new_sms_leads.drop_duplicates(
                subset=["Phone", "PropertyID", "ListingID", "Address"],
                keep="last",
            )
            if initial.empty:
                return 0
            repo.create_file(
                path=sms_path,
                message="Create SMS.csv",
                content=initial.to_csv(index=False),
                branch=GITHUB_BRANCH,
            )
            print("Created:", sms_path, "SMS leads:", len(initial))
            return len(initial)
        raise


# =========================================================
# UPDATE VALID EMAILS WITHOUT PERSONALIZATION
# =========================================================

def update_no_personalization_file(repo, leads):
    path = f"{GITHUB_FOLDER}/Emails Valid no personlised sentence.csv"
    columns = [
        "Bedrooms", "FirstName", "LastName", "Phone", "Address", "City",
        "Price", "Website", "Email", "PublicRemarks",
        "PropertyID", "ListingID", "PropertyURL", "ListDate", "ListingAgeDays", "LeadWeek",
        "ActorStatus", "ActorDisplayStatus", "ActorIsPending", "LiveRealtorStatus",
        "LiveStatusOutcome", "LiveHTTPStatus", "LiveContactable", "BouncerStatus",
        "BouncerReason", "EmailSource", "PersonalizedLine",
        "PersonalizationConfidence", "PersonalizationOutcome",
    ]
    leads = leads.copy()
    for c in columns:
        if c not in leads.columns:
            leads[c] = pd.NA
    leads = leads[columns]
    leads["Email"] = leads["Email"].astype("string").str.strip().str.lower()

    try:
        existing_file = repo.get_contents(path, ref=GITHUB_BRANCH)
        text = existing_file.decoded_content.decode("utf-8")
        existing = pd.read_csv(StringIO(text), dtype="string") if text.strip() else pd.DataFrame(columns=columns)
        for c in columns:
            if c not in existing.columns:
                existing[c] = pd.NA
        combined = pd.concat([existing[columns], leads], ignore_index=True)
        combined = combined[combined["Email"].notna() & (combined["Email"] != "")].drop_duplicates(subset=["Email"], keep="last")
        repo.update_file(path=path, message="Update valid emails without personalization", content=combined.to_csv(index=False), sha=existing_file.sha, branch=GITHUB_BRANCH)
        return len(combined)
    except GithubException as exc:
        if exc.status == 404:
            initial = leads[leads["Email"].notna() & (leads["Email"] != "")].drop_duplicates(subset=["Email"], keep="last")
            repo.create_file(path=path, message="Create valid emails without personalization", content=initial.to_csv(index=False), branch=GITHUB_BRANCH)
            return len(initial)
        raise



# =========================================================
# UPDATE ONE PERSISTENT FILE PER CATEGORY
# =========================================================

def update_category_file(repo, path, new_rows, message_label):
    """Append new rows into one stable GitHub CSV and remove duplicates.

    This prevents timestamped file spam. Emails are the preferred dedupe key;
    rows without an email are deduplicated by agent/listing identity fields.
    """
    new_rows = new_rows.copy()

    try:
        existing_file = repo.get_contents(path, ref=GITHUB_BRANCH)
        text = existing_file.decoded_content.decode("utf-8")
        existing = (
            pd.read_csv(StringIO(text), dtype="string")
            if text.strip()
            else pd.DataFrame(columns=new_rows.columns)
        )
    except GithubException as exc:
        if exc.status != 404:
            raise
        existing_file = None
        existing = pd.DataFrame(columns=new_rows.columns)

    all_columns = list(dict.fromkeys(list(existing.columns) + list(new_rows.columns)))
    for column in all_columns:
        if column not in existing.columns:
            existing[column] = pd.NA
        if column not in new_rows.columns:
            new_rows[column] = pd.NA

    combined = pd.concat(
        [existing[all_columns], new_rows[all_columns]],
        ignore_index=True,
    )

    if "Email" in combined.columns:
        combined["Email"] = (
            combined["Email"].astype("string").str.strip().str.lower()
        )

        with_email = combined[
            combined["Email"].notna() & (combined["Email"] != "")
        ].drop_duplicates(subset=["Email"], keep="last")

        without_email = combined[
            combined["Email"].isna() | (combined["Email"] == "")
        ].copy()

        identity_columns = [
            c for c in ["FirstName", "LastName", "Phone", "Address", "Website"]
            if c in without_email.columns
        ]
        if identity_columns:
            without_email = without_email.drop_duplicates(
                subset=identity_columns, keep="last"
            )
        else:
            without_email = without_email.drop_duplicates(keep="last")

        combined = pd.concat([with_email, without_email], ignore_index=True)
    else:
        combined = combined.drop_duplicates(keep="last")

    csv_content = combined.to_csv(index=False)

    if existing_file is None:
        repo.create_file(
            path=path,
            message=f"Create {message_label}",
            content=csv_content,
            branch=GITHUB_BRANCH,
        )
    else:
        repo.update_file(
            path=path,
            message=f"Update {message_label}",
            content=csv_content,
            sha=existing_file.sha,
            branch=GITHUB_BRANCH,
        )

    print("Updated category file:", path, "total rows:", len(combined))
    return len(combined)


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/")
def health():

    return {
        "status": "ok",
        "service": (
            "Apify Realtor.com Actor #2 + Weekly Buckets + Live Status + Bouncer + OpenAI"
        ),
        "webhook": "/webhook"
    }


# =========================================================
# APIFY WEBHOOK
# =========================================================

@app.post("/webhook")
async def apify_webhook(
    request: Request
):

    # -----------------------------------------
    # 1. RECEIVE WEBHOOK
    # -----------------------------------------

    try:
        payload = await request.json()

    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON received"
        )


    if isinstance(payload, dict):

        print(
            "Webhook payload keys:",
            list(payload.keys())
        )


    # -----------------------------------------
    # 2. FIND APIFY DATASET
    # -----------------------------------------

    dataset_id = (

        get_nested(
            payload,
            ["resource", "defaultDatasetId"]
        )

        or get_nested(
            payload,
            ["eventData", "defaultDatasetId"]
        )

        or get_nested(
            payload,
            [
                "payload",
                "resource",
                "defaultDatasetId"
            ]
        )

        or get_nested(
            payload,
            ["data", "defaultDatasetId"]
        )

        or (
            payload.get("defaultDatasetId")
            if isinstance(payload, dict)
            else None
        )
    )


    if not dataset_id:

        raise HTTPException(
            status_code=400,
            detail=(
                "No defaultDatasetId found "
                "in Apify webhook"
            )
        )


    print(
        "Apify dataset ID:",
        dataset_id
    )


    # -----------------------------------------
    # 3. DOWNLOAD DATA
    # -----------------------------------------

    listings = download_apify_dataset(
        dataset_id
    )


    print(
        "Downloaded records:",
        len(listings)
    )


    if not listings:

        raise HTTPException(
            status_code=400,
            detail="Apify dataset is empty"
        )


    # -----------------------------------------
    # 4. EXTRACT + CLEAN
    # -----------------------------------------

    rows = extract_records(
        listings
    )


    if not rows:

        raise HTTPException(
            status_code=400,
            detail="No Realtor records found"
        )


    df = clean_dataframe(
        rows
    )


    if df.empty:

        raise HTTPException(
            status_code=400,
            detail=(
                "No usable records after cleaning"
            )
        )


    # =====================================================
    # 5. WEEK BUCKETS -> WEEK 2/3 -> LIVE REALTOR CHECK
    #    -> BOUNCER -> OPENAI
    # =====================================================

    # Load the EXISTING Week 1/2/3 files first, merge today's fresh scrape,
    # then re-age everything. This turns the week folders into a rolling
    # conveyor belt instead of permanent storage buckets.
    try:
        repo = github.get_repo(GITHUB_REPO)
        week1_leads, week2_leads, week3_leads, outside_21_days, no_email_leads = (
            load_and_rebucket_week_leads(repo, df)
        )
    except GithubException as exc:
        print("GitHub error while loading week buckets:", exc)
        raise HTTPException(status_code=500, detail=f"GitHub error: {exc.data}")

    print("Week 1 leads (0-7 days):", len(week1_leads))
    print("Week 2 leads (8-14 days):", len(week2_leads))
    print("Week 3 leads (15-21 days):", len(week3_leads))
    print("Outside 0-21 days / invalid list_date:", len(outside_21_days))

    # -----------------------------------------------------
    # CHANNEL ROUTING FOR LEADS WITH NO EMAIL
    # -----------------------------------------------------
    # Week buckets are email-only. No-email leads bypass Week 1/2/3 completely:
    #   website present -> FBLeads.csv
    #   no website + phone present -> SMS.csv
    #   no email + no website + no phone -> no-contact/discard
    if not no_email_leads.empty:
        # Add age/week metadata for reporting only; these rows do NOT enter week files.
        no_email_leads = add_week_bucket(no_email_leads)

    no_email_has_website = (
        has_value(no_email_leads["Website"])
        if not no_email_leads.empty else pd.Series(dtype=bool)
    )
    no_email_has_phone = (
        has_value(no_email_leads["Phone"])
        if not no_email_leads.empty else pd.Series(dtype=bool)
    )

    no_email_fb_leads = (
        no_email_leads[no_email_has_website].copy().reset_index(drop=True)
        if not no_email_leads.empty else no_email_leads.copy()
    )
    new_sms_leads = (
        no_email_leads[(~no_email_has_website) & no_email_has_phone].copy().reset_index(drop=True)
        if not no_email_leads.empty else no_email_leads.copy()
    )
    no_contact_leads = (
        no_email_leads[(~no_email_has_website) & (~no_email_has_phone)].copy().reset_index(drop=True)
        if not no_email_leads.empty else no_email_leads.copy()
    )

    print("No-email leads diverted out of Week buckets:", len(no_email_leads))
    print("No-email + website -> FB:", len(no_email_fb_leads))
    print("No-email + no website + phone -> SMS:", len(new_sms_leads))
    print("No email/website/phone -> no-contact:", len(no_contact_leads))

    # We STORE Week 1 but do not contact it. Only Week 2 + Week 3 are eligible.
    contact_candidates = pd.concat([week2_leads, week3_leads], ignore_index=True)

    # Prefer the older listing when the same email appears more than once, then
    # contact that agent only once in this run.
    if not contact_candidates.empty:
        contact_candidates = contact_candidates.sort_values(
            by="ListingAgeDays", ascending=False, na_position="last"
        )
        with_email = contact_candidates[
            contact_candidates["Email"].notna() & (contact_candidates["Email"] != "")
        ].drop_duplicates(subset=["Email"], keep="first")
        without_email = contact_candidates[
            contact_candidates["Email"].isna() | (contact_candidates["Email"] == "")
        ]
        contact_candidates = pd.concat([with_email, without_email], ignore_index=True)

    print("Week 2/3 contact candidates after email dedupe:", len(contact_candidates))
    print("Starting Realtor.com live-status gate BEFORE Bouncer...")

    live_checked_candidates = enrich_with_live_realtor_status(contact_candidates)
    live_contactable = live_checked_candidates[
        live_checked_candidates["LiveContactable"] == True
    ].copy().reset_index(drop=True)
    live_rejected_or_held = live_checked_candidates[
        live_checked_candidates["LiveContactable"] != True
    ].copy().reset_index(drop=True)

    print("Live Realtor active/contactable:", len(live_contactable))
    print("Live Realtor rejected/held:", len(live_rejected_or_held))

    # Only now count/spend on email verification.
    actor_email_count = int(live_contactable["Email"].notna().sum())
    actor_no_email_count = len(live_contactable) - actor_email_count

    print("Contactable actor-provided emails:", actor_email_count)
    print("Contactable rows without email:", actor_no_email_count)
    print("Starting Bouncer verification ONLY for live active Week 2/3 listings...")

    enriched_leads = enrich_leads_with_bouncer(live_contactable)

    normalized_bouncer_status = (
        enriched_leads["BouncerStatus"]
        .astype("string").str.strip().str.lower().fillna("")
    )
    normalized_outcome = (
        enriched_leads["BouncerOutcome"]
        .astype("string").str.strip().str.lower().fillna("")
    )

    bouncer_valid_leads = (
        enriched_leads[
            enriched_leads["Email"].notna()
            & (normalized_bouncer_status == "deliverable")
        ]
        .copy().reset_index(drop=True)
    )

    personalized_valid_leads = enrich_personalization(bouncer_valid_leads)
    normalized_personalization = (
        personalized_valid_leads["PersonalizationOutcome"]
        .astype("string").str.strip().str.lower().fillna("")
    )

    new_email_leads = (
        personalized_valid_leads[normalized_personalization == "found"]
        .copy().reset_index(drop=True)
    )
    no_personalization_leads = (
        personalized_valid_leads[normalized_personalization != "found"]
        .copy().reset_index(drop=True)
    )

    bouncer_fb_leads = (
        enriched_leads[
            (normalized_outcome == "verified") & (normalized_bouncer_status != "deliverable")
        ]
        .copy().reset_index(drop=True)
    )

    # FB receives:
    # 1) no-email leads that DO have a website, plus
    # 2) the existing fallback for Bouncer-completed non-deliverable emails.
    new_fb_leads = pd.concat(
        [no_email_fb_leads, bouncer_fb_leads],
        ignore_index=True,
        sort=False,
    )
    new_fb_leads = dedupe_listing_rows(new_fb_leads)

    bouncer_deliverable = int((normalized_bouncer_status == "deliverable").sum())
    bouncer_non_deliverable = int(((normalized_outcome == "verified") & (normalized_bouncer_status != "deliverable")).sum())
    bouncer_not_found = int((normalized_outcome == "not_found").sum())
    bouncer_errors = int((normalized_outcome == "api_error").sum())

    print("Bouncer deliverable:", bouncer_deliverable)
    print("Bouncer non-deliverable:", bouncer_non_deliverable)
    print("No actor email:", bouncer_not_found)
    print("Bouncer API errors:", bouncer_errors)
    print("New personalized email leads:", len(new_email_leads))
    print("Valid emails without personalization:", len(no_personalization_leads))
    print("New FB leads:", len(new_fb_leads))

    # =====================================================
    # 8. CREATE MASTER CSV FILES
    # =====================================================

    # Keep ONE persistent file per lead category.
    # These files are updated cumulatively instead of creating timestamped copies.
    enriched_filename = f"{GITHUB_FOLDER}/actor2_enriched_leads.csv"
    no_actor_email_filename = f"{GITHUB_FOLDER}/actor2_no_original_email.csv"

    no_actor_email_leads = (
        enriched_leads[
            enriched_leads["EmailSource"] != "actor"
        ]
        .copy()
        .reset_index(drop=True)
    )


    # =====================================================
    # 9. GITHUB
    # =====================================================

    try:

        # repo was already loaded before re-bucketing the existing Week files.

        # -----------------------------------------
        # WEEK 1 / WEEK 2 / WEEK 3 FOLDERS
        # Week 1/2/3 are EMAIL-ONLY. Week 1 is storage; Week 2/3 are the email outreach pool.
        # -----------------------------------------

        # IMPORTANT: replace each file with its current membership. Do not append.
        # This physically moves aging properties between folders and removes 22+ day rows.
        total_week1 = replace_week_file(repo, "Week 1", week1_leads)
        total_week2 = replace_week_file(repo, "Week 2", week2_leads)
        total_week3 = replace_week_file(repo, "Week 3", week3_leads)

        # Keep a separate audit file for Week 2/3 properties that were rejected
        # or held by the live Realtor.com gate. They never reach Bouncer.
        total_live_rejected = update_category_file(
            repo,
            f"{GITHUB_FOLDER}/Live Status Rejected or Held/leads.csv",
            live_rejected_or_held,
            "Live Status Rejected or Held",
        )

        # -----------------------------------------
        # ONE PERSISTENT FILE PER CATEGORY
        # -----------------------------------------

        total_enriched_master = update_category_file(
            repo,
            enriched_filename,
            enriched_leads,
            "Actor #2 enriched leads",
        )

        total_no_actor_email_master = update_category_file(
            repo,
            no_actor_email_filename,
            no_actor_email_leads,
            "Actor #2 no-original-email leads",
        )


        # -----------------------------------------
        # EMAILLEADS.CSV
        #
        # Actor-provided email that Bouncer marked deliverable
        # and OpenAI successfully personalized.
        # -----------------------------------------

        total_email_leads = (
            update_email_leads_file(
                repo,
                new_email_leads
            )
        )

        total_no_personalization_leads = update_no_personalization_file(
            repo, no_personalization_leads
        )


        # -----------------------------------------
        # FBLEADS.CSV
        #
        # No actor email or Bouncer-completed non-deliverable results.
        # -----------------------------------------

        total_fb_leads = (
            update_fb_leads_file(
                repo,
                new_fb_leads
            )
        )

        # -----------------------------------------
        # SMS.CSV
        #
        # No email + no website + usable phone.
        # -----------------------------------------
        total_sms_leads = update_sms_leads_file(repo, new_sms_leads)


    except GithubException as exc:

        print(
            "GitHub error:",
            exc
        )

        raise HTTPException(
            status_code=500,
            detail=(
                f"GitHub error: {exc.data}"
            )
        )


    # =====================================================
    # 10. DONE
    # =====================================================

    return {
        "success": True,
        "dataset_id": dataset_id,
        "records_downloaded": len(listings),
        "records_after_cleaning": len(df),
        "week_1_leads": len(week1_leads),
        "week_2_leads": len(week2_leads),
        "week_3_leads": len(week3_leads),
        "outside_21_days_or_invalid_date": len(outside_21_days),
        "week_2_3_contact_candidates": len(contact_candidates),
        "live_realtor_contactable": len(live_contactable),
        "live_realtor_rejected_or_held": len(live_rejected_or_held),
        "actor_provided_emails_after_live_gate": actor_email_count,
        "actor_rows_without_email_after_live_gate": actor_no_email_count,
        "total_week_1_file": total_week1,
        "total_week_2_file": total_week2,
        "total_week_3_file": total_week3,
        "total_live_rejected_or_held": total_live_rejected,
        "bouncer_deliverable": bouncer_deliverable,
        "bouncer_non_deliverable": bouncer_non_deliverable,
        "bouncer_api_errors": bouncer_errors,
        "new_email_leads": len(new_email_leads),
        "total_email_leads": total_email_leads,
        "email_leads_file": f"{GITHUB_FOLDER}/EmailLeads.csv",
        "valid_emails_without_personalization": len(no_personalization_leads),
        "total_valid_emails_without_personalization": total_no_personalization_leads,
        "no_personalization_file": f"{GITHUB_FOLDER}/Emails Valid no personlised sentence.csv",
        "new_fb_leads": len(new_fb_leads),
        "total_fb_leads": total_fb_leads,
        "fb_leads_file": f"{GITHUB_FOLDER}/FBleads.csv",
        "new_sms_leads": len(new_sms_leads),
        "total_sms_leads": total_sms_leads,
        "sms_leads_file": f"{GITHUB_FOLDER}/SMS.csv",
        "no_contact_leads": len(no_contact_leads),
        "enriched_file": enriched_filename,
        "total_enriched_master": total_enriched_master,
        "no_original_email_file": no_actor_email_filename,
        "total_no_original_email_master": total_no_actor_email_master,
    }
