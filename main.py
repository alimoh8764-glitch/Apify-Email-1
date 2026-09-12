import os
import re
import time
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
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")


if not GITHUB_TOKEN:
    raise RuntimeError("Missing Railway variable: GITHUB_TOKEN")

if not GITHUB_REPO:
    raise RuntimeError("Missing Railway variable: GITHUB_REPO")

if not APIFY_TOKEN:
    raise RuntimeError("Missing Railway variable: APIFY_TOKEN")

if not HUNTER_API_KEY:
    raise RuntimeError("Missing Railway variable: HUNTER_API_KEY")

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
# WEBSITE -> DOMAIN
# =========================================================

def get_domain_from_website(website):
    """
    http://www.chetaylor.com/
    -> chetaylor.com
    """

    if website is None:
        return None

    website = str(website).strip()

    if not website:
        return None

    if not website.startswith(
        ("http://", "https://")
    ):
        website = "https://" + website

    try:

        parsed = urlparse(website)

        domain = (
            parsed.netloc
            .lower()
            .strip()
        )

        if domain.startswith("www."):
            domain = domain[4:]

        domain = domain.split(":")[0]

        return domain or None

    except Exception:
        return None


# =========================================================
# HUNTER EMAIL FINDER
# =========================================================

def hunter_find_email(
    first_name,
    last_name,
    website
):
    """
    Possible outcomes:

    found
    not_found
    invalid_input
    suppressed
    api_error
    """

    domain = get_domain_from_website(
        website
    )

    if (
        not first_name
        or not last_name
        or not domain
    ):
        return {
            "outcome": "invalid_input",
            "email": None,
            "score": None,
            "verification_status": None,
            "domain": domain,
        }

    url = (
        "https://api.hunter.io/"
        "v2/email-finder"
    )

    params = {
        "domain": domain,
        "first_name": str(first_name).strip(),
        "last_name": str(last_name).strip(),
        "api_key": HUNTER_API_KEY,
    }

    # Retry temporary errors
    for attempt in range(3):

        try:

            response = requests.get(
                url,
                params=params,
                timeout=30
            )

        except requests.RequestException as exc:

            print(
                "Hunter connection error:",
                first_name,
                last_name,
                domain,
                exc
            )

            if attempt < 2:
                time.sleep(2 ** attempt)
                continue

            return {
                "outcome": "api_error",
                "email": None,
                "score": None,
                "verification_status": None,
                "domain": domain,
            }


        # -----------------------------------------
        # SUCCESS
        # -----------------------------------------

        if response.status_code == 200:

            try:
                payload = response.json()

            except Exception:

                print(
                    "Hunter returned bad JSON:",
                    first_name,
                    last_name
                )

                return {
                    "outcome": "api_error",
                    "email": None,
                    "score": None,
                    "verification_status": None,
                    "domain": domain,
                }


            data = (
                payload.get("data")
                or {}
            )

            email = data.get("email")
            score = data.get("score")

            verification = (
                data.get("verification")
                or {}
            )

            verification_status = (
                verification.get("status")
            )


            # Email found
            if email:

                print(
                    "Hunter FOUND:",
                    first_name,
                    last_name,
                    email,
                    "status:",
                    verification_status,
                    "score:",
                    score
                )

                return {
                    "outcome": "found",
                    "email": email,
                    "score": score,
                    "verification_status":
                        verification_status,
                    "domain": domain,
                }


            # Search completed normally
            # but no email was found
            print(
                "Hunter NO EMAIL:",
                first_name,
                last_name,
                domain
            )

            return {
                "outcome": "not_found",
                "email": None,
                "score": None,
                "verification_status": None,
                "domain": domain,
            }


        # -----------------------------------------
        # PRIVACY / SUPPRESSION
        # -----------------------------------------

        if response.status_code == 451:

            print(
                "Hunter SUPPRESSED:",
                first_name,
                last_name,
                domain
            )

            return {
                "outcome": "suppressed",
                "email": None,
                "score": None,
                "verification_status": None,
                "domain": domain,
            }


        # -----------------------------------------
        # TEMPORARY ERROR - RETRY
        # -----------------------------------------

        if response.status_code in (
            429,
            500,
            502,
            503,
            504
        ):

            print(
                "Hunter temporary error:",
                response.status_code,
                "attempt:",
                attempt + 1
            )

            if attempt < 2:
                time.sleep(2 ** attempt)
                continue


            return {
                "outcome": "api_error",
                "email": None,
                "score": None,
                "verification_status": None,
                "domain": domain,
            }


        # -----------------------------------------
        # OTHER 400-LEVEL ERROR
        # -----------------------------------------

        print(
            "Hunter lookup error:",
            response.status_code,
            first_name,
            last_name,
            domain,
            response.text[:300]
        )

        return {
            "outcome": "invalid_input",
            "email": None,
            "score": None,
            "verification_status": None,
            "domain": domain,
        }


    return {
        "outcome": "api_error",
        "email": None,
        "score": None,
        "verification_status": None,
        "domain": domain,
    }


# =========================================================
# ENRICH WEBSITE LEADS WITH HUNTER
# =========================================================

def enrich_website_leads(with_website):
    """
    Hunter ONLY receives leads from
    the with_website dataframe.

    Original Realtor information stays
    on each row.
    """

    enriched = with_website.copy()

    enriched["Email"] = pd.NA
    enriched["HunterScore"] = pd.NA
    enriched["HunterStatus"] = pd.NA
    enriched["HunterOutcome"] = pd.NA


    for index, row in enriched.iterrows():

        first_name = row.get("FirstName")
        last_name = row.get("LastName")
        website = row.get("Website")


        if (
            pd.isna(first_name)
            or pd.isna(last_name)
            or pd.isna(website)
        ):
            enriched.at[
                index,
                "HunterOutcome"
            ] = "invalid_input"

            continue


        result = hunter_find_email(
            str(first_name),
            str(last_name),
            str(website)
        )


        enriched.at[
            index,
            "HunterOutcome"
        ] = result["outcome"]


        if result["email"]:

            enriched.at[
                index,
                "Email"
            ] = result["email"]


        if result["score"] is not None:

            enriched.at[
                index,
                "HunterScore"
            ] = result["score"]


        if (
            result["verification_status"]
            is not None
        ):

            enriched.at[
                index,
                "HunterStatus"
            ] = (
                result[
                    "verification_status"
                ]
            )


    return enriched





# =========================================================
# OPENAI PERSONALIZATION DETAIL EXTRACTOR
# =========================================================

PERSONALIZATION_INSTRUCTIONS = """You are a precise data-extraction assistant for a real estate cold-email tool. Your only job is to pull ONE concrete, specific, non-generic detail from a property listing description that could be referenced in a personalized email opener to the listing agent.

RULES:
- Only extract from these categories, in priority order:
  1. A named appliance, brand, or material upgrade (e.g. "LG WashTower", "quartz countertops", "stainless steel appliances")
  2. An unusual structural or layout feature (e.g. "up/down duplex", "in-law suite", "walkout basement", "2.5 storey")
  3. A notable build year if unusually old or new
  4. A specific outdoor feature (e.g. "pool", "walkout to backyard", "wraparound deck")
- NEVER use subjective/marketing adjectives from the listing (e.g. "beautiful", "stunning", "meticulously maintained", "charming", "cozy"). Only extract concrete nouns/facts.
- NEVER paraphrase the agent's sales language back — extract a plain factual detail, not a rephrased compliment.
- Output must be a short phrase, 6-10 words max, no full sentences, no punctuation at the end.
- If no detail fits these categories, or the listing is too generic, output exactly: NONE
- Do not invent or infer details not explicitly stated in the text.

Output format (strict):
DETAIL: <phrase or NONE>
CONFIDENCE: <high/medium/low>"""


def extract_openai_output_text(payload):
    """Extract assistant text from an OpenAI Responses API JSON payload."""
    pieces = []

    for item in payload.get("output", []) or []:
        if not isinstance(item, dict):
            continue

        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue

            if content.get("type") == "output_text":
                value = content.get("text")
                if value:
                    pieces.append(str(value))

    return "\n".join(pieces).strip()


def parse_personalization_response(text):
    """
    Parse the strict two-line response:
      DETAIL: ...
      CONFIDENCE: high/medium/low
    """
    if not text:
        return None

    detail_match = re.search(
        r"(?im)^DETAIL:\s*(.+?)\s*$",
        text
    )
    confidence_match = re.search(
        r"(?im)^CONFIDENCE:\s*(high|medium|low)\s*$",
        text
    )

    if not detail_match or not confidence_match:
        return None

    detail = detail_match.group(1).strip()
    confidence = confidence_match.group(1).strip().lower()

    if detail.upper() == "NONE":
        return {
            "detail": "NONE",
            "confidence": confidence,
            "outcome": "no_detail",
        }

    # Enforce the user's 6-10 word requirement ourselves as a safety check.
    word_count = len(re.findall(r"\b[\w'-]+\b", detail))

    if word_count < 6 or word_count > 10:
        return None

    # Remove accidental terminal punctuation without otherwise rewriting text.
    detail = detail.rstrip(" .,!?:;")

    return {
        "detail": detail,
        "confidence": confidence,
        "outcome": "found",
    }


def openai_extract_personalized_detail(public_remarks):
    """
    Send PublicRemarks to OpenAI and return one concrete personalization detail.

    Outcomes:
      found
      no_detail
      invalid_input
      api_error
    """
    if public_remarks is None:
        return {
            "detail": "NONE",
            "confidence": "low",
            "outcome": "invalid_input",
        }

    public_remarks = str(public_remarks).strip()

    if not public_remarks:
        return {
            "detail": "NONE",
            "confidence": "low",
            "outcome": "invalid_input",
        }

    url = "https://api.openai.com/v1/responses"

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    body = {
        "model": OPENAI_MODEL,
        "instructions": PERSONALIZATION_INSTRUCTIONS,
        "input": (
            "PROPERTY LISTING DESCRIPTION:\n"
            + public_remarks
        ),
        "max_output_tokens": 100,
    }

    for attempt in range(3):
        try:
            response = requests.post(
                url,
                headers=headers,
                json=body,
                timeout=45,
            )
        except requests.RequestException as exc:
            print(
                "OpenAI connection error:",
                exc
            )

            if attempt < 2:
                time.sleep(2 ** attempt)
                continue

            return {
                "detail": None,
                "confidence": None,
                "outcome": "api_error",
            }

        if response.status_code == 200:
            try:
                payload = response.json()
                output_text = extract_openai_output_text(payload)
                parsed = parse_personalization_response(output_text)
            except Exception as exc:
                print(
                    "OpenAI response parse error:",
                    exc
                )
                parsed = None

            if parsed is not None:
                return parsed

            print(
                "OpenAI returned unexpected format:",
                output_text[:300] if 'output_text' in locals() else ""
            )

            if attempt < 2:
                time.sleep(1)
                continue

            return {
                "detail": None,
                "confidence": None,
                "outcome": "api_error",
            }

        if response.status_code in (408, 409, 429, 500, 502, 503, 504):
            print(
                "OpenAI temporary error:",
                response.status_code,
                "attempt:",
                attempt + 1
            )

            if attempt < 2:
                time.sleep(2 ** attempt)
                continue

        else:
            print(
                "OpenAI request error:",
                response.status_code,
                response.text[:500]
            )

        return {
            "detail": None,
            "confidence": None,
            "outcome": "api_error",
        }

    return {
        "detail": None,
        "confidence": None,
        "outcome": "api_error",
    }


def enrich_personalization(valid_email_leads):
    """
    Run OpenAI ONLY on Hunter-valid email leads.

    Adds:
      PersonalizedDetail
      PersonalizationConfidence
      PersonalizationOutcome
    """
    enriched = valid_email_leads.copy()

    enriched["PersonalizedDetail"] = pd.NA
    enriched["PersonalizationConfidence"] = pd.NA
    enriched["PersonalizationOutcome"] = pd.NA

    for index, row in enriched.iterrows():
        remarks = row.get("PublicRemarks")

        if pd.isna(remarks):
            result = {
                "detail": "NONE",
                "confidence": "low",
                "outcome": "invalid_input",
            }
        else:
            result = openai_extract_personalized_detail(
                str(remarks)
            )

        enriched.at[
            index,
            "PersonalizedDetail"
        ] = result["detail"]

        enriched.at[
            index,
            "PersonalizationConfidence"
        ] = result["confidence"]

        enriched.at[
            index,
            "PersonalizationOutcome"
        ] = result["outcome"]

        print(
            "Personalization:",
            row.get("FirstName"),
            row.get("LastName"),
            result["outcome"],
            result["detail"]
        )

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

    Contains ONLY leads where Hunter found an email
    AND HunterStatus is exactly "valid".

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
        "PublicRemarks",
        "Email",
        "HunterScore",
        "HunterStatus",
        "PersonalizedDetail",
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

    # Safety: EmailLeads.csv should contain ONLY
    # Hunter-verified valid emails and never blank emails.
    new_email_leads["HunterStatus"] = (
        new_email_leads["HunterStatus"]
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
            (new_email_leads["HunterStatus"] == "valid")
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

        # Keep ONLY Hunter-verified valid emails from older runs too.
        existing_df["HunterStatus"] = (
            existing_df["HunterStatus"]
            .astype("string")
            .str.strip()
            .str.lower()
        )

        existing_df = (
            existing_df[
                existing_df["HunterStatus"] == "valid"
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
# UPDATE VALID EMAILS WITH NO PERSONALIZED SENTENCE
# =========================================================

def update_no_personalization_file(
    repo,
    leads
):
    """
    Cumulative holding file for Hunter-valid email leads that did not receive
    a usable personalization detail, including OpenAI/API failures so no valid
    email lead is silently lost.
    """

    path = (
        f"{GITHUB_FOLDER}/"
        "Emails Valid no personlised sentence.csv"
    )

    columns = [
        "Bedrooms",
        "FirstName",
        "LastName",
        "Phone",
        "Address",
        "City",
        "Price",
        "Website",
        "PublicRemarks",
        "Email",
        "HunterScore",
        "HunterStatus",
        "PersonalizedDetail",
        "PersonalizationConfidence",
        "PersonalizationOutcome",
    ]

    leads = leads.copy()

    for column in columns:
        if column not in leads.columns:
            leads[column] = pd.NA

    leads = leads[columns]

    if "Email" in leads.columns:
        leads["Email"] = (
            leads["Email"]
            .astype("string")
            .str.strip()
            .str.lower()
        )

    try:
        existing_file = repo.get_contents(
            path,
            ref=GITHUB_BRANCH
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
                columns=columns
            )

        for column in columns:
            if column not in existing_df.columns:
                existing_df[column] = pd.NA

        existing_df = existing_df[columns]

        combined = pd.concat(
            [existing_df, leads],
            ignore_index=True
        )

        combined = combined.drop_duplicates(
            subset=["Email"],
            keep="last"
        )

        repo.update_file(
            path=path,
            message=(
                "Update Emails Valid no "
                "personlised sentence.csv"
            ),
            content=combined.to_csv(index=False),
            sha=existing_file.sha,
            branch=GITHUB_BRANCH,
        )

        print(
            "Updated:",
            path,
            "total held leads:",
            len(combined)
        )

        return len(combined)

    except GithubException as exc:
        if exc.status == 404:
            leads = leads.drop_duplicates(
                subset=["Email"],
                keep="last"
            )

            repo.create_file(
                path=path,
                message=(
                    "Create Emails Valid no "
                    "personlised sentence.csv"
                ),
                content=leads.to_csv(index=False),
                branch=GITHUB_BRANCH,
            )

            print(
                "Created:",
                path,
                "held leads:",
                len(leads)
            )

            return len(leads)

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
        "HunterScore",
        "HunterStatus",
        "HunterOutcome",
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
            "Apify Realtor + Hunter Enrichment"
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


    # -----------------------------------------
    # 5. SPLIT WEBSITE / NO WEBSITE
    # -----------------------------------------

    with_website = df[
        df["Website"].notna()
    ].copy().reset_index(drop=True)


    without_website = df[
        df["Website"].isna()
    ].copy().reset_index(drop=True)


    print(
        "Leads with website:",
        len(with_website)
    )


    print(
        "Leads without website:",
        len(without_website)
    )


    # =====================================================
    # 6. HUNTER - WEBSITE LEADS ONLY
    # =====================================================

    print(
        "Starting Hunter.io enrichment..."
    )


    enriched_with_website = (
        enrich_website_leads(
            with_website
        )
    )


    # -----------------------------------------
    # COUNTS
    # -----------------------------------------

    hunter_found = len(
        enriched_with_website[
            enriched_with_website[
                "HunterOutcome"
            ] == "found"
        ]
    )


    hunter_not_found = len(
        enriched_with_website[
            enriched_with_website[
                "HunterOutcome"
            ] == "not_found"
        ]
    )


    hunter_errors = len(
        enriched_with_website[
            enriched_with_website[
                "HunterOutcome"
            ] == "api_error"
        ]
    )


    hunter_suppressed = len(
        enriched_with_website[
            enriched_with_website[
                "HunterOutcome"
            ] == "suppressed"
        ]
    )


    print(
        "Hunter emails found:",
        hunter_found
    )

    print(
        "Hunter no email:",
        hunter_not_found
    )

    print(
        "Hunter temporary errors:",
        hunter_errors
    )

    print(
        "Hunter suppressed:",
        hunter_suppressed
    )


    # =====================================================
    # 7. ROUTE HUNTER RESULTS
    #
    # EmailLeads.csv:
    #   ONLY HunterStatus == "valid"
    #
    # FBleads.csv:
    #   - no email found
    #   - accept_all
    #   - unknown
    #   - any other non-valid Hunter email status
    #
    # Suppressed/privacy responses and API errors are NOT
    # treated as FB leads because they were not a normal
    # completed "no valid email" result.
    # =====================================================

    normalized_hunter_status = (
        enriched_with_website["HunterStatus"]
        .astype("string")
        .str.strip()
        .str.lower()
    )

    new_email_leads = (
        enriched_with_website[
            (
                enriched_with_website[
                    "HunterOutcome"
                ] == "found"
            )
            &
            (
                enriched_with_website[
                    "Email"
                ].notna()
            )
            &
            (
                normalized_hunter_status == "valid"
            )
        ]
        .copy()
        .reset_index(drop=True)
    )


    new_fb_leads = (
        enriched_with_website[
            (
                enriched_with_website[
                    "HunterOutcome"
                ] == "not_found"
            )
            |
            (
                (
                    enriched_with_website[
                        "HunterOutcome"
                    ] == "found"
                )
                &
                (
                    normalized_hunter_status != "valid"
                )
            )
        ]
        .copy()
        .reset_index(drop=True)
    )


    print(
        "Hunter-valid email candidates:",
        len(new_email_leads)
    )

    # =====================================================
    # 7B. OPENAI PERSONALIZATION - VALID EMAIL LEADS ONLY
    # =====================================================

    personalized_valid_leads = enrich_personalization(
        new_email_leads
    )

    new_email_leads = (
        personalized_valid_leads[
            personalized_valid_leads[
                "PersonalizationOutcome"
            ] == "found"
        ]
        .copy()
        .reset_index(drop=True)
    )

    no_personalization_leads = (
        personalized_valid_leads[
            personalized_valid_leads[
                "PersonalizationOutcome"
            ] != "found"
        ]
        .copy()
        .reset_index(drop=True)
    )

    print(
        "Personalized valid email leads:",
        len(new_email_leads)
    )

    print(
        "Valid emails with no usable personalization:",
        len(no_personalization_leads)
    )

    print(
        "New FB leads (no email / non-valid status):",
        len(new_fb_leads)
    )


    # =====================================================
    # 8. CREATE MASTER CSV FILES
    #
    # Use fixed filenames so every webhook run updates the
    # same files instead of creating timestamped CSVs.
    # =====================================================

    with_website_csv = (
        enriched_with_website
        .to_csv(index=False)
    )


    without_website_csv = (
        without_website
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
        # + Hunter email results
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
        # Hunter successfully found an email
        # AND HunterStatus == valid.
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
        # Hunter returned no email OR returned
        # an email with a non-valid status
        # (accept_all, unknown, etc.).
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

        "hunter_emails_found":
            hunter_found,

        "hunter_no_email":
            hunter_not_found,

        "hunter_api_errors":
            hunter_errors,

        "hunter_suppressed":
            hunter_suppressed,

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
