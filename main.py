import os
import re
import time
from io import StringIO

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
    """Return prices in $123456 format."""
    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    cleaned = re.sub(r"[^\d.]", "", value)

    if not cleaned:
        return None

    try:
        amount = int(float(cleaned))
        return f"${amount}"

    except ValueError:
        return None


def clean_bedrooms(value):
    """
    Normalize bedroom counts.

    Examples:
        3 + 2 -> 5
        2+1   -> 3
        4     -> 4

    If the value is not a simple numeric addition, keep the original text.
    """
    if value is None:
        return None

    text = str(value).strip()

    if not text:
        return None

    # Add bedroom components when Realtor returns values such as "3 + 2".
    if re.fullmatch(r"\d+\s*(?:\+\s*\d+)+", text):
        return str(sum(int(number) for number in re.findall(r"\d+", text)))

    # Keep a normal single bedroom count clean.
    if re.fullmatch(r"\d+", text):
        return text

    # Safe fallback: preserve unexpected source values instead of guessing.
    return text


# =========================================================
# ADDRESS CLEANER
# =========================================================

def clean_address(value):
    """
    Clean Realtor.ca street addresses while keeping useful fallback details.

    Rules:
      5214 ADMIRAL WALTER HOSE ST NW -> 5214 Admiral Walter Hose
      10109 80 ST NW                -> 10109 80 St Nw
      404 NW                        -> 404 Nw
      404                           -> 404

    If a real street name is present, trailing street type/direction tokens
    are removed. If the address is only a numbered street (no street name),
    those tokens are retained because they help identify the road.
    """

    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    # AddressText can contain extra location information after "|" or ",".
    street = value.split("|")[0]
    street = street.split(",")[0]
    street = street.strip()

    # Remove common leading unit/suite formats.
    street = re.sub(
        r"^\s*#\s*[A-Za-z0-9-]+\s+",
        "",
        street,
        flags=re.IGNORECASE
    )
    street = re.sub(
        r"^\s*(?:UNIT|SUITE|APT|APARTMENT)\s+[A-Za-z0-9-]+\s+",
        "",
        street,
        flags=re.IGNORECASE
    )
    street = re.sub(
        r"^\s*[A-Za-z0-9]+\s*-\s*(?=\d+\b)",
        "",
        street
    )

    street = re.sub(r"\s+", " ", street).strip()

    # Find the civic/building number.
    number_match = re.search(r"\b(\d+[A-Za-z]?)\b", street)
    if not number_match:
        return street.title() or None

    number = number_match.group(1)
    remainder = street[number_match.end():].strip(" -")

    if not remainder:
        return number

    direction_pattern = r"(?:NW|NE|SW|SE|N|S|E|W)"
    street_type_pattern = (
        r"(?:ST|STREET|AVE|AVENUE|RD|ROAD|DR|DRIVE|BLVD|BOULEVARD|"
        r"TRAIL|TRL|WAY|CRES|CRESCENT|CRT|COURT|PL|PLACE|LN|LANE|"
        r"HWY|HIGHWAY|PKWY|PARKWAY|TER|TERRACE|CIR|CIRCLE|"
        r"GDNS|GARDENS|GRV|GROVE|MEWS|RISE|ROW|SQ|SQUARE)"
    )

    # Remove trailing compass direction temporarily so we can inspect the
    # actual street portion.
    without_direction = re.sub(
        rf"\s+{direction_pattern}$",
        "",
        remainder,
        flags=re.IGNORECASE
    ).strip()

    # Remove a trailing road-type token temporarily as well.
    name_candidate = re.sub(
        rf"\s+{street_type_pattern}$",
        "",
        without_direction,
        flags=re.IGNORECASE
    ).strip()

    # A true named street contains letters before the street type. Numeric
    # roads such as "80 ST NW" or "17 AVE SW" do not, so keep their suffixes.
    has_named_street = bool(re.search(r"[A-Za-z]", name_candidate))

    if has_named_street:
        # Named street: omit ST/AVE/etc. and compass direction, and normalize
        # Realtor.ca's all-caps text to readable title case.
        return f"{number} {name_candidate.title()}".strip()

    # No street name found: keep the useful road type/direction as a fallback,
    # but convert ALL CAPS to normal display casing.
    fallback = remainder.title()
    return f"{number} {fallback}".strip()


# =========================================================
# PHONE CLEANER
# =========================================================

def build_phone(area_code, phone_number):

    if area_code is None or phone_number is None:
        return None

    area_code = re.sub(
        r"\D",
        "",
        str(area_code)
    )

    phone_number = re.sub(
        r"\D",
        "",
        str(phone_number)
    )

    if not area_code or not phone_number:
        return None

    full_number = area_code + phone_number

    if (
        len(full_number) == 11
        and full_number.startswith("1")
    ):
        full_number = full_number[1:]

    if len(full_number) != 10:
        return None

    return "1" + full_number


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
# ACTOR-PROVIDED EMAIL EXTRACTOR
# =========================================================

EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)


def extract_email_from_value(value):
    """Return the first real email string found inside a nested actor value."""
    if value is None:
        return None

    if isinstance(value, str):
        match = EMAIL_PATTERN.search(value)
        return match.group(0).lower() if match else None

    if isinstance(value, dict):
        # Prefer fields explicitly named like email/emailAddress when present.
        preferred_keys = (
            "Email", "email", "EmailAddress", "emailAddress",
            "EmailAddressText", "email_address", "value", "Value",
        )
        for key in preferred_keys:
            if key in value:
                found = extract_email_from_value(value.get(key))
                if found:
                    return found

        for nested in value.values():
            found = extract_email_from_value(nested)
            if found:
                return found
        return None

    if isinstance(value, list):
        for item in value:
            found = extract_email_from_value(item)
            if found:
                return found
        return None

    return None


def extract_actor_email(listing):
    """
    Extract an ACTUAL email supplied by the Realtor.ca actor for the primary agent.

    Realtor.ca often exposes only an Emails[].ContactId. A ContactId is not an
    email address and is intentionally never sent to Bouncer.

    Some actor runs may expose an Email/EmailAddress field directly. The actor
    payload can also contain malformed Website values such as
    http://name@example.com/, so the primary-agent object is scanned for a real
    email-shaped string as a safe fallback.
    """
    primary_agents = [
        get_nested(listing, ["Individual", 0]),
        get_nested(listing, ["moreDetails", "Individual", 0]),
    ]

    for agent in primary_agents:
        if not isinstance(agent, dict):
            continue
        found = extract_email_from_value(agent)
        if found:
            return found

    return None


# =========================================================
# EXTRACT REALTOR DATA
# =========================================================

def extract_records(listings):

    rows = []

    for listing in listings:

        if not isinstance(listing, dict):
            continue

        bedrooms = get_nested(
            listing,
            ["Building", "Bedrooms"]
        )

        first_name = get_nested(
            listing,
            ["Individual", 0, "FirstName"]
        )

        last_name = get_nested(
            listing,
            ["Individual", 0, "LastName"]
        )

        area_code = get_nested(
            listing,
            [
                "Individual",
                0,
                "Phones",
                0,
                "AreaCode"
            ]
        )

        phone_number = get_nested(
            listing,
            [
                "Individual",
                0,
                "Phones",
                0,
                "PhoneNumber"
            ]
        )

        address = get_nested(
            listing,
            [
                "Property",
                "Address",
                "AddressText"
            ]
        )

        # Realtor.ca city field:
        # moreDetails/Property/Address/City
        city = get_nested(
            listing,
            [
                "moreDetails",
                "Property",
                "Address",
                "City"
            ]
        )

        price = get_nested(
            listing,
            ["Property", "Price"]
        )

        website = get_nested(
            listing,
            [
                "Individual",
                0,
                "Websites",
                0,
                "Website"
            ]
        )

        public_remarks = get_nested(
            listing,
            [
                "moreDetails",
                "PublicRemarks"
            ]
        )

        # Use only a real email string supplied by the actor. Realtor ContactId
        # values are identifiers, not email addresses, so they are ignored.
        email = extract_actor_email(listing)

        phone = build_phone(
            area_code,
            phone_number
        )

        rows.append({
            "Bedrooms": clean_bedrooms(bedrooms),
            "FirstName": clean_text(first_name),
            "LastName": clean_text(last_name),
            "Phone": phone,
            "Address": clean_address(address),
            "City": clean_text(city),
            "Price": clean_price(price),
            "Website": clean_text(website),
            "Email": clean_text(email),
            "PublicRemarks": clean_text(public_remarks),
        })

    return rows


# =========================================================
# CLEAN DATAFRAME
# =========================================================

def clean_dataframe(rows):

    columns = [
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
    ]

    df = pd.DataFrame(
        rows,
        columns=columns
    )

    if df.empty:
        return df

    df = df.dropna(how="all")

    for column in [
        "FirstName",
        "LastName"
    ]:
        df[column] = (
            df[column]
            .astype("string")
            .str.strip()
        )

    df["Phone"] = (
        df["Phone"]
        .astype("string")
    )

    df["Website"] = (
        df["Website"]
        .astype("string")
        .str.strip()
    )

    df["Website"] = df["Website"].replace({
        "": pd.NA,
        "None": pd.NA,
        "none": pd.NA,
        "nan": pd.NA,
        "<NA>": pd.NA,
    })

    df["Email"] = (
        df["Email"]
        .astype("string")
        .str.strip()
        .str.lower()
    )

    df["Email"] = df["Email"].replace({
        "": pd.NA,
        "None": pd.NA,
        "none": pd.NA,
        "nan": pd.NA,
        "<NA>": pd.NA,
    })

    df = df.drop_duplicates(
        subset=[
            "FirstName",
            "LastName",
            "Phone",
            "Address"
        ],
        keep="first"
    )

    df = df.dropna(
        subset=[
            "FirstName",
            "LastName"
        ],
        how="all"
    )

    return df.reset_index(drop=True)


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
                    + "\n\nIMPORTANT: Return one COMPLETE casual human reaction, ideally 7-12 words and never more than 15 words, "
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
# UPDATE VALID EMAILS WITHOUT PERSONALIZATION
# =========================================================

def update_no_personalization_file(repo, leads):
    path = f"{GITHUB_FOLDER}/Emails Valid no personlised sentence.csv"
    columns = [
        "Bedrooms", "FirstName", "LastName", "Phone", "Address", "City",
        "Price", "Website", "Email", "PublicRemarks", "BouncerStatus",
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
# HEALTH CHECK
# =========================================================

@app.get("/")
def health():

    return {
        "status": "ok",
        "service": (
            "Apify Realtor + Bouncer + OpenAI"
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
    # 5. SPLIT WEBSITE / NO WEBSITE (MASTER EXPORTS ONLY)
    # =====================================================

    # Keep the existing Canada master-file split for compatibility. Email
    # verification no longer depends on having a website.
    with_website = df[
        df["Website"].notna()
    ].copy().reset_index(drop=True)

    without_website = df[
        df["Website"].isna()
    ].copy().reset_index(drop=True)

    print("Leads with website:", len(with_website))
    print("Leads without website:", len(without_website))


    # =====================================================
    # 6. BOUNCER - VERIFY ACTOR-PROVIDED EMAILS
    # =====================================================

    actor_email_count = int(df["Email"].notna().sum())
    actor_no_email_count = len(df) - actor_email_count

    print("Actor-provided emails:", actor_email_count)
    print("Actor rows without a real email string:", actor_no_email_count)
    print("Starting Bouncer verification...")

    enriched_leads = enrich_leads_with_bouncer(df)

    normalized_bouncer_status = (
        enriched_leads["BouncerStatus"]
        .astype("string")
        .str.strip()
        .str.lower()
        .fillna("")
    )

    normalized_bouncer_outcome = (
        enriched_leads["BouncerOutcome"]
        .astype("string")
        .str.strip()
        .str.lower()
        .fillna("")
    )

    # ONLY exact Bouncer deliverable emails continue to OpenAI.
    bouncer_deliverable_leads = (
        enriched_leads[
            enriched_leads["Email"].notna()
            & (normalized_bouncer_status == "deliverable")
        ]
        .copy()
        .reset_index(drop=True)
    )

    # =====================================================
    # 7. OPENAI PERSONALIZATION - DELIVERABLE EMAILS ONLY
    # =====================================================

    personalized_valid_leads = enrich_personalization(
        bouncer_deliverable_leads
    )

    normalized_personalization = (
        personalized_valid_leads["PersonalizationOutcome"]
        .astype("string")
        .str.strip()
        .str.lower()
        .fillna("")
    )

    new_email_leads = (
        personalized_valid_leads[
            normalized_personalization == "found"
        ]
        .copy()
        .reset_index(drop=True)
    )

    no_personalization_leads = (
        personalized_valid_leads[
            normalized_personalization != "found"
        ]
        .copy()
        .reset_index(drop=True)
    )

    # FB leads = no real actor email OR Bouncer verified a non-deliverable status.
    # API errors are intentionally not treated as normal FB outcomes.
    new_fb_leads = (
        enriched_leads[
            (normalized_bouncer_outcome == "not_found")
            | (
                (normalized_bouncer_outcome == "verified")
                & (normalized_bouncer_status != "deliverable")
            )
        ]
        .copy()
        .reset_index(drop=True)
    )

    bouncer_deliverable = int(
        (normalized_bouncer_status == "deliverable").sum()
    )
    bouncer_non_deliverable = int(
        (
            (normalized_bouncer_outcome == "verified")
            & (normalized_bouncer_status != "deliverable")
        ).sum()
    )
    bouncer_not_found = int(
        (normalized_bouncer_outcome == "not_found").sum()
    )
    bouncer_errors = int(
        (normalized_bouncer_outcome == "api_error").sum()
    )

    print("Bouncer deliverable:", bouncer_deliverable)
    print("Bouncer non-deliverable:", bouncer_non_deliverable)
    print("No actor email:", bouncer_not_found)
    print("Bouncer API errors:", bouncer_errors)
    print("Personalized deliverable email leads:", len(new_email_leads))
    print("Deliverable emails with no usable personalization:", len(no_personalization_leads))
    print("New FB leads:", len(new_fb_leads))

    # =====================================================
    # 8. CREATE MASTER CSV FILES
    #
    # Use fixed filenames so every webhook run updates the
    # same files instead of creating timestamped CSVs.
    # =====================================================

    enriched_with_website = (
        enriched_leads[enriched_leads["Website"].notna()]
        .copy()
        .reset_index(drop=True)
    )

    enriched_without_website = (
        enriched_leads[enriched_leads["Website"].isna()]
        .copy()
        .reset_index(drop=True)
    )

    with_website_csv = (
        enriched_with_website
        .to_csv(index=False)
    )


    without_website_csv = (
        enriched_without_website
        .to_csv(index=False)
    )


    with_website_filename = (
        f"{GITHUB_FOLDER}/leads_with_website.csv"
    )


    without_website_filename = (
        f"{GITHUB_FOLDER}/leads_without_website.csv"
    )


    # =====================================================
    # 9. GITHUB
    # =====================================================

    try:

        repo = github.get_repo(
            GITHUB_REPO
        )


        # -----------------------------------------
        # MASTER WEBSITE FILE
        #
        # Contains ALL Realtor data
        # + actor email / Bouncer results
        # -----------------------------------------

        try:
            existing_with_website = repo.get_contents(
                with_website_filename,
                ref=GITHUB_BRANCH
            )

            repo.update_file(
                path=with_website_filename,
                message="Update leads_with_website.csv",
                content=with_website_csv,
                sha=existing_with_website.sha,
                branch=GITHUB_BRANCH,
            )

            print(
                "Updated:",
                with_website_filename
            )

        except GithubException as exc:
            if exc.status == 404:
                repo.create_file(
                    path=with_website_filename,
                    message="Create leads_with_website.csv",
                    content=with_website_csv,
                    branch=GITHUB_BRANCH,
                )

                print(
                    "Created:",
                    with_website_filename
                )
            else:
                raise


        # -----------------------------------------
        # NO WEBSITE FILE
        # -----------------------------------------

        try:
            existing_without_website = repo.get_contents(
                without_website_filename,
                ref=GITHUB_BRANCH
            )

            repo.update_file(
                path=without_website_filename,
                message="Update leads_without_website.csv",
                content=without_website_csv,
                sha=existing_without_website.sha,
                branch=GITHUB_BRANCH,
            )

            print(
                "Updated:",
                without_website_filename
            )

        except GithubException as exc:
            if exc.status == 404:
                repo.create_file(
                    path=without_website_filename,
                    message="Create leads_without_website.csv",
                    content=without_website_csv,
                    branch=GITHUB_BRANCH,
                )

                print(
                    "Created:",
                    without_website_filename
                )
            else:
                raise


        # -----------------------------------------
        # EMAILLEADS.CSV
        #
        # Actor supplied an email, Bouncer marked it deliverable,
        # and OpenAI produced a usable personalized line.
        # -----------------------------------------

        total_email_leads = (
            update_email_leads_file(
                repo,
                new_email_leads
            )
        )


        # -----------------------------------------
        # VALID EMAILS WITHOUT PERSONALIZATION
        # -----------------------------------------

        total_no_personalization_leads = (
            update_no_personalization_file(
                repo,
                no_personalization_leads
            )
        )


        # -----------------------------------------
        # FBLEADS.CSV
        #
        # No actor email OR Bouncer verified a non-deliverable
        # status (risky, undeliverable, unknown, etc.).
        # -----------------------------------------

        total_fb_leads = (
            update_fb_leads_file(
                repo,
                new_fb_leads
            )
        )


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

        "success":
            True,

        "dataset_id":
            dataset_id,

        "records_downloaded":
            len(listings),

        "records_after_cleaning":
            len(df),

        "with_website":
            len(with_website),

        "without_website":
            len(without_website),

        "actor_emails_found":
            actor_email_count,

        "actor_no_email":
            actor_no_email_count,

        "bouncer_deliverable":
            bouncer_deliverable,

        "bouncer_non_deliverable":
            bouncer_non_deliverable,

        "bouncer_api_errors":
            bouncer_errors,

        "new_email_leads":
            len(new_email_leads),

        "total_email_leads":
            total_email_leads,

        "email_leads_file":
            f"{GITHUB_FOLDER}/EmailLeads.csv",

        "valid_emails_without_personalization":
            len(no_personalization_leads),

        "total_valid_emails_without_personalization":
            total_no_personalization_leads,

        "no_personalization_file":
            (
                f"{GITHUB_FOLDER}/"
                "Emails Valid no personlised sentence.csv"
            ),

        "new_fb_leads":
            len(new_fb_leads),

        "total_fb_leads":
            total_fb_leads,

        "with_website_file":
            with_website_filename,

        "without_website_file":
            without_website_filename,

        "fb_leads_file":
            f"{GITHUB_FOLDER}/FBleads.csv",
    }
