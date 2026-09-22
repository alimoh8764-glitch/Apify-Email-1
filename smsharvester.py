"""
SMS HARVESTER
=============

Apify
  -> Railway webhook
  -> Fetch Realtor dataset
  -> Extract/clean leads
  -> NumVerify concurrently
  -> Route leads
  -> Save qualified leads to GitHub CSV files

OUTPUT
------
SMS Leads/SMS Leads.csv
SMS Leads/Facebook Leads.csv

ROUTING
-------
mobile
    -> SMS Leads.csv

landline + website
    -> Facebook Leads.csv

landline + no website
    -> discard

invalid / unsupported
    -> discard

NumVerify API failure
    -> verification error (not treated as invalid)


RAILWAY VARIABLES
-----------------
NUMVERIFY_API_KEY
APIFY_API_TOKEN

GITHUB_TOKEN
GITHUB_REPO
GITHUB_BRANCH

Optional:
NUMVERIFY_WORKERS
"""

import os
import re
import csv
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from fastapi import FastAPI, HTTPException, Request

from github import Github
from github import Auth
from github.GithubException import GithubException, UnknownObjectException


# ============================================================
# CONFIG
# ============================================================

NUMVERIFY_API_KEY = os.getenv("NUMVERIFY_API_KEY", "")
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN", "")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")

NUMVERIFY_URL = "https://apilayer.net/api/validate"

REQUEST_TIMEOUT = 30


# Keep this conservative initially.
# Increase later only if your NumVerify plan allows it.

try:
    NUMVERIFY_WORKERS = int(
        os.getenv("NUMVERIFY_WORKERS", "5")
    )
except ValueError:
    NUMVERIFY_WORKERS = 5

NUMVERIFY_WORKERS = max(
    1,
    min(NUMVERIFY_WORKERS, 20),
)


# ============================================================
# GITHUB OUTPUT
# ============================================================

GITHUB_FOLDER = "SMS Leads"

SMS_FILE = f"{GITHUB_FOLDER}/SMS Leads.csv"

FACEBOOK_FILE = (
    f"{GITHUB_FOLDER}/Facebook Leads.csv"
)


# ============================================================
# FINAL COLUMN ORDER
# ============================================================

COLUMNS = [
    "first_name",
    "phone_number",
    "city",
    "community",
    "price",
    "address",
    "bedrooms",
    "website",
    "registration_number",
]


# ============================================================
# BASIC CLEANING
# ============================================================

def clean(value):

    if value is None:
        return ""

    value = str(value).strip()

    if value.lower() in {
        "",
        "nan",
        "none",
        "null",
        "n/a",
        "na",
    }:
        return ""

    return value


# ============================================================
# READ FLATTENED OR NESTED APIFY DATA
# ============================================================

def get_nested(data, path):
    """
    Supports both:

    Flattened:
        Individual/0/Phones/0/AreaCode

    Nested:
        Individual:
          [
            {
              Phones:
                [
                  {
                    AreaCode: ...
                  }
                ]
            }
          ]
    """

    if isinstance(data, dict) and path in data:
        return data[path]

    current = data

    for part in path.split("/"):

        if isinstance(current, dict):

            if part not in current:
                return None

            current = current[part]

        elif isinstance(current, list):

            try:
                index = int(part)
            except ValueError:
                return None

            if index < 0 or index >= len(current):
                return None

            current = current[index]

        else:
            return None

    return current


def first_value(data, *keys):

    for key in keys:

        value = clean(
            get_nested(data, key)
        )

        if value:
            return value

    return ""


# ============================================================
# FIRST NAME
# ============================================================

def get_first_name(data):

    first_name = first_value(
        data,
        "Individual/0/FirstName",
        "moreDetails/Individual/0/FirstName",
    )

    if first_name:
        return first_name

    full_name = first_value(
        data,
        "Individual/0/Name",
        "moreDetails/Individual/0/Name",
    )

    if full_name:
        return full_name.split()[0]

    return ""


# ============================================================
# REGISTRATION NUMBER
# ============================================================

def get_registration_number(data):

    return first_value(
        data,
        "Individual/0/IndividualID",
        "moreDetails/Individual/0/IndividualID",
    )


# ============================================================
# PHONE NUMBER
# ============================================================

def get_phone(data):
    """
    Individual Realtor phone only.

    Does NOT intentionally use the brokerage /
    organization phone.
    """

    area_code = first_value(
        data,
        "Individual/0/Phones/0/AreaCode",
        "moreDetails/Individual/0/Phones/0/AreaCode",
    )

    phone = first_value(
        data,
        "Individual/0/Phones/0/PhoneNumber",
        "moreDetails/Individual/0/Phones/0/PhoneNumber",
    )

    area_digits = re.sub(
        r"\D",
        "",
        area_code,
    )

    phone_digits = re.sub(
        r"\D",
        "",
        phone,
    )

    number = area_digits + phone_digits

    # 10-digit North American number
    if len(number) == 10:
        return "1" + number

    # Already contains country code 1
    if (
        len(number) == 11
        and number.startswith("1")
    ):
        return number

    return ""


# ============================================================
# CITY
# ============================================================

def get_city(data):

    return first_value(
        data,
        "Property/Address/City",
        "moreDetails/Property/Address/City",
    )


# ============================================================
# COMMUNITY
# ============================================================

def get_community(data):

    return first_value(
        data,
        "Property/Address/CommunityName",
        "moreDetails/Property/Address/CommunityName",
    )


# ============================================================
# PRICE
# ============================================================

def get_price(data):

    value = first_value(
        data,
        "Property/PriceUnformattedValue",
        "moreDetails/Property/PriceUnformattedValue",
    )

    if value:

        try:
            return int(float(value))

        except (ValueError, TypeError):
            pass

    value = first_value(
        data,
        "Property/Price",
        "moreDetails/Property/Price",
    )

    digits = re.sub(
        r"[^\d]",
        "",
        value,
    )

    if digits:
        return int(digits)

    return ""


# ============================================================
# ADDRESS
# ============================================================

def get_address(data):
    """
    Clean Realtor.ca property address.

    Named roads lose trailing NE/NW/SE/SW.
    Numbered roads keep the direction.
    Condo/unit numbers are removed.
    """

    address = first_value(
        data,
        "Property/Address/AddressText",
        "moreDetails/Property/Address/AddressText",
    )

    if not address:
        return ""

    # Remove city / province / postal code.
    address = address.split("|", 1)[0].strip()

    # Remove leading condo/unit number.
    # 301, 55 Wolf Hollow Crescent SE -> 55 Wolf Hollow Crescent SE
    address = re.sub(
        r"^\s*(?:unit\s*)?#?\s*[A-Za-z0-9-]+\s*,\s*",
        "",
        address,
        count=1,
        flags=re.IGNORECASE,
    )

    # Remove explicit unit/suite suffix at the end.
    address = re.sub(
        r"\s+(?:apt|apartment|unit|suite|ste|#)\s*[A-Za-z0-9-]+\s*$",
        "",
        address,
        flags=re.IGNORECASE,
    )

    address = re.sub(r"\s+", " ", address).strip()

    # Look at the road portion after the house number.
    # If it begins with a number, it is a numbered road and
    # NE/NW/SE/SW should be kept.
    parts = address.split(maxsplit=1)

    if len(parts) == 2:
        road_part = parts[1]

        numbered_road = bool(
            re.match(
                r"^\d+(?:st|nd|rd|th)?\b",
                road_part,
                re.IGNORECASE,
            )
        )

        # Named road: remove trailing direction.
        if not numbered_road:
            address = re.sub(
                r"\s+(?:NE|NW|SE|SW)\s*$",
                "",
                address,
                flags=re.IGNORECASE,
            ).strip()

    return address


# ============================================================
# BEDROOMS
# ============================================================

def get_bedrooms(data):
    """
    Examples:

    3       -> 3
    3 + 2   -> 5
    4 + 1   -> 5
    """

    value = first_value(
        data,
        "Building/Bedrooms",
        "moreDetails/Building/Bedrooms",
    )

    if not value:
        return ""

    numbers = re.findall(
        r"\d+",
        value,
    )

    if not numbers:
        return ""

    return sum(
        int(number)
        for number in numbers
    )


# ============================================================
# WEBSITE
# ============================================================

def get_website(data):
    """
    Individual Realtor website.
    """

    return first_value(
        data,
        "Individual/0/Websites/0/Website",
        "moreDetails/Individual/0/Websites/0/Website",
    )


# ============================================================
# BUILD CLEAN LEAD
# ============================================================

def build_lead(data):

    return {
        "first_name":
            get_first_name(data),

        "phone_number":
            get_phone(data),

        "city":
            get_city(data),

        "community":
            get_community(data),

        "price":
            get_price(data),

        "address":
            get_address(data),

        "bedrooms":
            get_bedrooms(data),

        "website":
            get_website(data),

        "registration_number":
            get_registration_number(data),
    }


# ============================================================
# NUMVERIFY
# ============================================================

def numverify(phone_number):

    if not phone_number:

        return {
            "success": True,
            "valid": False,
            "line_type": "",
            "country_code": "",
            "carrier": "",
        }

    if not NUMVERIFY_API_KEY:

        return {
            "success": False,
            "error": "NUMVERIFY_API_KEY is missing.",
            "valid": False,
            "line_type": "",
            "country_code": "",
            "carrier": "",
        }

    try:

        response = requests.get(
            NUMVERIFY_URL,
            params={
                "access_key": NUMVERIFY_API_KEY,
                "number": phone_number,
            },
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()

        result = response.json()

    except requests.RequestException as error:

        return {
            "success": False,
            "error": str(error),
            "valid": False,
            "line_type": "",
            "country_code": "",
            "carrier": "",
        }

    except ValueError as error:

        return {
            "success": False,
            "error": (
                f"Invalid NumVerify JSON: {error}"
            ),
            "valid": False,
            "line_type": "",
            "country_code": "",
            "carrier": "",
        }

    if result.get("success") is False:

        return {
            "success": False,
            "error": result.get(
                "error",
                {},
            ),
            "valid": False,
            "line_type": "",
            "country_code": "",
            "carrier": "",
        }

    return {
        "success":
            True,

        "valid":
            result.get("valid") is True,

        "line_type":
            clean(
                result.get("line_type")
            ).lower(),

        "country_code":
            clean(
                result.get("country_code")
            ).upper(),

        "carrier":
            clean(
                result.get("carrier")
            ),
    }


# ============================================================
# ROUTING
# ============================================================

def route_built_lead(lead):
    """
    Route a lead that has already been extracted.
    """

    phone = lead["phone_number"]

    if not phone:

        return (
            "discard",
            lead,
            "missing_or_bad_phone",
        )

    verification = numverify(phone)

    # NumVerify itself failed
    if not verification["success"]:

        return (
            "verification_error",
            lead,
            "numverify_error",
        )

    # Invalid phone
    if not verification["valid"]:

        return (
            "discard",
            lead,
            "invalid_phone",
        )

    # US / Canada only
    if verification["country_code"] not in {
        "US",
        "CA",
    }:

        return (
            "discard",
            lead,
            "wrong_country",
        )

    line_type = verification["line_type"]

    # --------------------------------------------------------
    # MOBILE -> SMS
    # --------------------------------------------------------

    if line_type == "mobile":

        return (
            "sms",
            lead,
            "valid_mobile",
        )

    # --------------------------------------------------------
    # LANDLINE -> NEVER SMS
    # --------------------------------------------------------

    if line_type == "landline":

        if clean(lead["website"]):

            return (
                "facebook",
                lead,
                "landline_with_website",
            )

        return (
            "discard",
            lead,
            "landline_without_website",
        )

    # --------------------------------------------------------
    # UNKNOWN / VOIP / ETC
    # --------------------------------------------------------

    return (
        "discard",
        lead,
        (
            "unsupported_line_type_"
            f"{line_type or 'unknown'}"
        ),
    )


# ============================================================
# DEDUPLICATION
# ============================================================

def lead_identity(lead):
    """
    Prefer Realtor registration number.

    Fall back to phone number.

    This prevents the same Realtor from being repeatedly
    appended on later Apify runs.
    """

    registration = clean(
        lead.get("registration_number")
    )

    if registration:
        return f"registration:{registration}"

    phone = re.sub(
        r"\D",
        "",
        clean(
            lead.get("phone_number")
        ),
    )

    if phone:
        return f"phone:{phone}"

    return ""


def dedupe_rows(rows):

    output = []

    seen = set()

    for row in rows:

        identity = lead_identity(row)

        if identity:

            if identity in seen:
                continue

            seen.add(identity)

        output.append(row)

    return output


# ============================================================
# GITHUB CONNECTION
# ============================================================

def get_github_repo():

    if not GITHUB_TOKEN:

        raise RuntimeError(
            "GITHUB_TOKEN is missing in Railway."
        )

    if not GITHUB_REPO:

        raise RuntimeError(
            "GITHUB_REPO is missing in Railway. "
            "Use format: username/repository"
        )

    auth = Auth.Token(
        GITHUB_TOKEN
    )

    github_client = Github(
        auth=auth
    )

    return github_client.get_repo(
        GITHUB_REPO
    )


# ============================================================
# CSV HELPERS
# ============================================================

def csv_to_rows(content):

    if not content:
        return []

    reader = csv.DictReader(
        io.StringIO(content)
    )

    rows = []

    for row in reader:

        cleaned_row = {
            column: clean(
                row.get(column, "")
            )
            for column in COLUMNS
        }

        rows.append(
            cleaned_row
        )

    return rows


def rows_to_csv(rows):

    output = io.StringIO(
        newline=""
    )

    writer = csv.DictWriter(
        output,
        fieldnames=COLUMNS,
        extrasaction="ignore",
        lineterminator="\n",
    )

    writer.writeheader()

    for row in rows:

        writer.writerow({
            column:
                clean(
                    row.get(column, "")
                )
            for column in COLUMNS
        })

    return output.getvalue()


# ============================================================
# READ EXISTING GITHUB CSV
# ============================================================

def read_github_csv(repo, path):

    try:

        file = repo.get_contents(
            path,
            ref=GITHUB_BRANCH,
        )

        content = file.decoded_content.decode(
            "utf-8-sig"
        )

        return file, csv_to_rows(content)

    except UnknownObjectException:

        # File does not exist yet.
        return None, []


# ============================================================
# APPEND / UPDATE GITHUB CSV
# ============================================================

def save_leads_to_github(
    repo,
    path,
    new_rows,
    label,
):
    """
    Append new qualified leads to existing CSV.

    Existing leads are preserved.

    Duplicate Realtor registration IDs / phones are removed.
    """

    if not new_rows:

        print(
            f"GitHub: no new {label} leads to save.",
            flush=True,
        )

        return {
            "file": path,
            "received": 0,
            "added": 0,
            "total": None,
        }

    existing_file, existing_rows = read_github_csv(
        repo,
        path,
    )

    # Deduplicate existing file first
    existing_rows = dedupe_rows(
        existing_rows
    )

    existing_identities = {
        lead_identity(row)
        for row in existing_rows
        if lead_identity(row)
    }

    rows_to_add = []

    for row in new_rows:

        identity = lead_identity(row)

        # Duplicate
        if (
            identity
            and identity in existing_identities
        ):
            continue

        rows_to_add.append(
            row
        )

        if identity:
            existing_identities.add(
                identity
            )

    combined_rows = dedupe_rows(
        existing_rows + rows_to_add
    )

    csv_content = rows_to_csv(
        combined_rows
    )

    # --------------------------------------------------------
    # UPDATE EXISTING FILE
    # --------------------------------------------------------

    if existing_file:

        repo.update_file(
            path=path,
            message=(
                f"Update {label} leads "
                f"({len(rows_to_add)} new)"
            ),
            content=csv_content,
            sha=existing_file.sha,
            branch=GITHUB_BRANCH,
        )

        action = "updated"

    # --------------------------------------------------------
    # CREATE FILE / FOLDER
    # --------------------------------------------------------

    else:

        # GitHub automatically creates the virtual folder
        # when a file such as SMS Leads/SMS Leads.csv
        # is created.

        repo.create_file(
            path=path,
            message=(
                f"Create {label} leads file"
            ),
            content=csv_content,
            branch=GITHUB_BRANCH,
        )

        action = "created"

    print(
        f"GitHub: {action} {path} | "
        f"{len(rows_to_add)} new | "
        f"{len(combined_rows)} total",
        flush=True,
    )

    return {
        "file":
            path,

        "received":
            len(new_rows),

        "added":
            len(rows_to_add),

        "total":
            len(combined_rows),
    }


# ============================================================
# SAVE BOTH RESULT FILES
# ============================================================

def save_results_to_github(
    sms_leads,
    facebook_leads,
):

    repo = get_github_repo()

    sms_result = save_leads_to_github(
        repo=repo,
        path=SMS_FILE,
        new_rows=sms_leads,
        label="SMS",
    )

    facebook_result = save_leads_to_github(
        repo=repo,
        path=FACEBOOK_FILE,
        new_rows=facebook_leads,
        label="Facebook",
    )

    return {
        "sms": sms_result,
        "facebook": facebook_result,
    }


# ============================================================
# PROCESS DATASET CONCURRENTLY
# ============================================================

def process_payload(payload):

    if isinstance(payload, dict):
        records = [payload]

    elif isinstance(payload, list):
        records = payload

    else:
        raise ValueError(
            "Dataset must be a dictionary or list."
        )

    total = len(records)

    print(
        f"Processing {total} records with "
        f"{NUMVERIFY_WORKERS} NumVerify workers.",
        flush=True,
    )

    # --------------------------------------------------------
    # EXTRACT FIRST
    # --------------------------------------------------------

    leads = [
        build_lead(record)
        for record in records
    ]

    sms_leads = []
    facebook_leads = []
    verification_errors = []

    stats = {
        "received":
            total,

        "sms":
            0,

        "facebook":
            0,

        "discarded":
            0,

        "verification_errors":
            0,
    }

    # --------------------------------------------------------
    # RUN NUMVERIFY IN PARALLEL
    # --------------------------------------------------------

    with ThreadPoolExecutor(
        max_workers=NUMVERIFY_WORKERS
    ) as executor:

        future_map = {}

        for index, lead in enumerate(
            leads,
            start=1,
        ):

            future = executor.submit(
                route_built_lead,
                lead,
            )

            future_map[future] = index

        completed = 0

        for future in as_completed(
            future_map
        ):

            original_index = future_map[
                future
            ]

            completed += 1

            try:

                destination, lead, reason = (
                    future.result()
                )

            except Exception as error:

                lead = leads[
                    original_index - 1
                ]

                destination = (
                    "verification_error"
                )

                reason = (
                    f"worker_error: {error}"
                )

            # ------------------------------------------------
            # SMS
            # ------------------------------------------------

            if destination == "sms":

                sms_leads.append(
                    lead
                )

                stats["sms"] += 1

            # ------------------------------------------------
            # FACEBOOK
            # ------------------------------------------------

            elif destination == "facebook":

                facebook_leads.append(
                    lead
                )

                stats["facebook"] += 1

            # ------------------------------------------------
            # VERIFICATION ERROR
            # ------------------------------------------------

            elif (
                destination
                == "verification_error"
            ):

                verification_errors.append({
                    **lead,
                    "error_reason": reason,
                })

                stats[
                    "verification_errors"
                ] += 1

            # ------------------------------------------------
            # DISCARD
            # ------------------------------------------------

            else:

                stats["discarded"] += 1

            print(
                f"[{completed}/{total}] "
                f"{lead['first_name']} | "
                f"{lead['phone_number']} | "
                f"{destination} | "
                f"{reason}",
                flush=True,
            )

    # --------------------------------------------------------
    # REMOVE DUPLICATES WITHIN THIS RUN
    # --------------------------------------------------------

    sms_leads = dedupe_rows(
        sms_leads
    )

    facebook_leads = dedupe_rows(
        facebook_leads
    )

    # --------------------------------------------------------
    # SAVE QUALIFIED LEADS TO GITHUB
    # --------------------------------------------------------

    print(
        "NumVerify processing complete. "
        "Saving qualified leads to GitHub...",
        flush=True,
    )

    github_result = save_results_to_github(
        sms_leads=sms_leads,
        facebook_leads=facebook_leads,
    )

    print(
        "GitHub save complete.",
        flush=True,
    )

    print(
        f"FINAL: "
        f"{len(sms_leads)} SMS | "
        f"{len(facebook_leads)} Facebook | "
        f"{stats['discarded']} discarded | "
        f"{stats['verification_errors']} verification errors",
        flush=True,
    )

    return {
        "folder_name":
            GITHUB_FOLDER,

        "columns":
            COLUMNS,

        "sms_sheet": {
            "name":
                "SMS Leads",

            "rows":
                sms_leads,
        },

        "facebook_sheet": {
            "name":
                "Facebook Leads",

            "rows":
                facebook_leads,
        },

        "verification_errors":
            verification_errors,

        "stats":
            stats,

        "github":
            github_result,
    }


# ============================================================
# FIND APIFY DATASET
# ============================================================

def find_dataset_id(payload):

    if not isinstance(payload, dict):
        return ""

    resource = payload.get(
        "resource"
    )

    if isinstance(resource, dict):

        dataset_id = clean(
            resource.get(
                "defaultDatasetId"
            )
        )

        if dataset_id:
            return dataset_id

    event_data = payload.get(
        "eventData"
    )

    if isinstance(event_data, dict):

        dataset_id = clean(
            event_data.get(
                "defaultDatasetId"
            )
        )

        if dataset_id:
            return dataset_id

    dataset_id = clean(
        payload.get(
            "defaultDatasetId"
        )
    )

    return dataset_id


# ============================================================
# FETCH APIFY DATASET
# ============================================================

def fetch_apify_dataset(dataset_id):

    if not dataset_id:

        raise ValueError(
            "No Apify dataset ID supplied."
        )

    url = (
        "https://api.apify.com/v2/datasets/"
        f"{dataset_id}/items"
    )

    params = {
        "clean": "true",
        "format": "json",
    }

    if APIFY_API_TOKEN:

        params["token"] = (
            APIFY_API_TOKEN
        )

    response = requests.get(
        url,
        params=params,
        timeout=60,
    )

    response.raise_for_status()

    data = response.json()

    if not isinstance(data, list):

        raise ValueError(
            "Apify dataset response "
            "was not a list."
        )

    print(
        f"Downloaded {len(data)} records from Apify.",
        flush=True,
    )

    return data


# ============================================================
# DETECT REALTOR RECORD
# ============================================================

def looks_like_realtor_record(payload):

    if not isinstance(payload, dict):
        return False

    possible_fields = [
        "Individual/0/FirstName",
        "moreDetails/Individual/0/FirstName",

        "Property/Address/AddressText",
        "moreDetails/Property/Address/AddressText",

        "Building/Bedrooms",
        "moreDetails/Building/Bedrooms",
    ]

    for field in possible_fields:

        value = clean(
            get_nested(
                payload,
                field,
            )
        )

        if value:
            return True

    return False


# ============================================================
# MAIN
# ============================================================

def main(payload):

    # Actual list of dataset records
    if isinstance(payload, list):

        print(
            f"Received {len(payload)} "
            f"dataset records directly.",
            flush=True,
        )

        return process_payload(
            payload
        )

    # Single Realtor record
    if looks_like_realtor_record(
        payload
    ):

        print(
            "Received Realtor record directly.",
            flush=True,
        )

        return process_payload(
            payload
        )

    # Apify Actor webhook
    dataset_id = find_dataset_id(
        payload
    )

    if dataset_id:

        print(
            "Apify webhook received. "
            f"Fetching dataset: {dataset_id}",
            flush=True,
        )

        dataset = fetch_apify_dataset(
            dataset_id
        )

        return process_payload(
            dataset
        )

    raise ValueError(
        "Webhook received, but no Realtor "
        "records or Apify defaultDatasetId "
        "were found."
    )


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="SMS Harvester",
    version="2.0.0",
)


# ============================================================
# HEALTH
# ============================================================

@app.get("/")
def health_check():

    return {
        "status": "online",
        "service": "SMS Harvester",
        "version": "2.0.0",
        "numverify_workers":
            NUMVERIFY_WORKERS,
        "github_output":
            bool(
                GITHUB_TOKEN
                and GITHUB_REPO
            ),
    }


@app.get("/health")
def health():

    return {
        "ok": True,
    }


# ============================================================
# APIFY WEBHOOK
# ============================================================

@app.post("/apify-webhook")
async def apify_webhook(
    request: Request
):

    try:

        payload: Any = (
            await request.json()
        )

    except Exception:

        raise HTTPException(
            status_code=400,
            detail=(
                "Webhook body must "
                "contain valid JSON."
            ),
        )

    try:

        result = main(
            payload
        )

        return {
            "success": True,
            "result": result,
        }

    except Exception as error:

        print(
            f"WEBHOOK ERROR: {error}",
            flush=True,
        )

        raise HTTPException(
            status_code=500,
            detail=str(error),
        )
