"""
SMS HARVESTER
=============

Apify webhook -> Railway -> fetch Realtor.ca dataset
-> clean leads -> NumVerify -> route leads

ROUTING
-------
Valid mobile:
    -> SMS Leads

Valid landline + website:
    -> Facebook Leads

Valid landline + no website:
    -> Discard

Invalid:
    -> Discard

NumVerify failure:
    -> Verification Errors


FINAL COLUMN ORDER
------------------
first_name
phone_number
city
community
price
address
bedrooms
website
registration_number


RAILWAY ENVIRONMENT VARIABLES
-----------------------------
NUMVERIFY_API_KEY
APIFY_API_TOKEN
"""

import os
import re
import time
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, Request


# ============================================================
# CONFIG
# ============================================================

NUMVERIFY_API_KEY = os.getenv("NUMVERIFY_API_KEY", "")
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN", "")

NUMVERIFY_URL = "https://apilayer.net/api/validate"

REQUEST_TIMEOUT = 30


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
    """
    Convert null / blank / NaN-like values into empty strings.
    """

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
# NESTED + FLATTENED APIFY READER
# ============================================================

def get_nested(data, path):
    """
    Supports BOTH formats.

    FLATTENED:
        {
            "Individual/0/Phones/0/AreaCode": "403"
        }

    NESTED:
        {
            "Individual": [
                {
                    "Phones": [
                        {
                            "AreaCode": "403"
                        }
                    ]
                }
            ]
        }

    This is important because CSV exports and live Apify JSON
    can have different structures.
    """

    # --------------------------------------------------------
    # TRY EXACT FLATTENED KEY FIRST
    # --------------------------------------------------------

    if isinstance(data, dict) and path in data:
        return data[path]

    # --------------------------------------------------------
    # OTHERWISE WALK NESTED JSON
    # --------------------------------------------------------

    current = data

    for part in path.split("/"):

        # Dictionary
        if isinstance(current, dict):

            if part not in current:
                return None

            current = current[part]

        # List
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
    """
    Return the first populated value.

    Works with both:
    - flattened Apify records
    - nested Apify JSON
    """

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

    # Fallback to full name
    full_name = first_value(
        data,
        "Individual/0/Name",
        "moreDetails/Individual/0/Name",
    )

    if full_name:
        return full_name.split()[0]

    return ""


# ============================================================
# REGISTRATION NUMBER / REALTOR INDIVIDUAL ID
# ============================================================

def get_registration_number(data):

    return first_value(
        data,
        "Individual/0/IndividualID",
        "moreDetails/Individual/0/IndividualID",
    )


# ============================================================
# PHONE
# ============================================================

def get_phone(data):
    """
    Get the INDIVIDUAL Realtor's phone.

    We do NOT intentionally use:
        Organization/Phones

    because that could be the brokerage phone.
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

    # Remove formatting
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

    # --------------------------------------------------------
    # NORMAL US / CANADA NUMBER
    # --------------------------------------------------------

    if len(number) == 10:
        return "1" + number

    # --------------------------------------------------------
    # ALREADY HAS +1 / COUNTRY CODE
    # --------------------------------------------------------

    if (
        len(number) == 11
        and number.startswith("1")
    ):
        return number

    # --------------------------------------------------------
    # BAD / MISSING NUMBER
    # --------------------------------------------------------

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

    # Prefer clean numeric price
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

    # --------------------------------------------------------
    # FALLBACK TO FORMATTED PRICE
    # --------------------------------------------------------

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
# SHORTENED PROPERTY ADDRESS
# ============================================================

def get_address(data):
    """
    Example:

    23 Penworth Crescent SE|Calgary, Alberta T2A4C5

    becomes:

    23 Penworth Crescent SE
    """

    address = first_value(
        data,
        "Property/Address/AddressText",
        "moreDetails/Property/Address/AddressText",
    )

    if not address:
        return ""

    # Realtor.ca separator
    address = address.split("|")[0]

    # Additional safety
    address = address.split(",")[0]

    address = address.strip()

    # Remove unit / apartment / suite information
    address = re.sub(
        r"\s+(?:apt|apartment|unit|suite|ste|#)"
        r"\s*[\w-]+.*$",
        "",
        address,
        flags=re.IGNORECASE,
    )

    return address.strip()


# ============================================================
# BEDROOMS
# ============================================================

def get_bedrooms(data):
    """
    Realtor examples:

        3       -> 3
        3 + 2   -> 5
        4 + 1   -> 5

    Returns TOTAL bedrooms.
    """

    value = first_value(
        data,

        # IMPORTANT:
        # This exists in your actual Realtor/Apify data.
        "Building/Bedrooms",

        # Fallback
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
# AGENT WEBSITE
# ============================================================

def get_website(data):
    """
    Get the INDIVIDUAL agent website.

    Do not intentionally substitute the brokerage website.
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
    """
    Creates the final nine columns in the exact required order.
    """

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
    """
    Python calls NumVerify directly.

    We use:
        valid
        line_type
        country_code
        carrier
    """

    if not phone_number:

        return {
            "success": True,
            "valid": False,
            "line_type": "",
            "country_code": "",
            "carrier": "",
        }

    if not NUMVERIFY_API_KEY:

        raise RuntimeError(
            "NUMVERIFY_API_KEY is not configured in Railway."
        )

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
            "error": f"Invalid NumVerify JSON: {error}",
            "valid": False,
            "line_type": "",
            "country_code": "",
            "carrier": "",
        }

    # --------------------------------------------------------
    # NUMVERIFY API ERROR
    # --------------------------------------------------------

    if result.get("success") is False:

        return {
            "success": False,
            "error": result.get("error", {}),
            "valid": False,
            "line_type": "",
            "country_code": "",
            "carrier": "",
        }

    return {
        "success": True,

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
# ROUTE ONE LEAD
# ============================================================

def route_lead(data):
    """
    FINAL ROUTING LOGIC:

    VALID MOBILE
        -> SMS

    VALID LANDLINE + WEBSITE
        -> FACEBOOK

    VALID LANDLINE + NO WEBSITE
        -> DISCARD

    INVALID
        -> DISCARD

    UNKNOWN / OTHER LINE TYPE
        -> DISCARD

    NUMVERIFY FAILURE
        -> VERIFICATION ERROR
    """

    lead = build_lead(data)

    phone = lead["phone_number"]

    # --------------------------------------------------------
    # NO USABLE PHONE
    # --------------------------------------------------------

    if not phone:

        return (
            "discard",
            lead,
            "missing_or_bad_phone",
        )

    # --------------------------------------------------------
    # CALL NUMVERIFY
    # --------------------------------------------------------

    verification = numverify(phone)

    # --------------------------------------------------------
    # NUMVERIFY FAILED
    # --------------------------------------------------------

    if not verification["success"]:

        return (
            "verification_error",
            lead,
            "numverify_error",
        )

    # --------------------------------------------------------
    # INVALID NUMBER
    # --------------------------------------------------------

    if not verification["valid"]:

        return (
            "discard",
            lead,
            "invalid_phone",
        )

    # --------------------------------------------------------
    # ONLY US / CANADA
    # --------------------------------------------------------

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

    # ========================================================
    # MOBILE -> SMS
    # ========================================================

    if line_type == "mobile":

        return (
            "sms",
            lead,
            "valid_mobile",
        )

    # ========================================================
    # LANDLINE -> NEVER SMS
    # ========================================================

    if line_type == "landline":

        # ----------------------------------------------------
        # LANDLINE + WEBSITE -> FACEBOOK
        # ----------------------------------------------------

        if clean(lead["website"]):

            return (
                "facebook",
                lead,
                "landline_with_website",
            )

        # ----------------------------------------------------
        # LANDLINE + NO WEBSITE -> DISCARD
        # ----------------------------------------------------

        return (
            "discard",
            lead,
            "landline_without_website",
        )

    # --------------------------------------------------------
    # OTHER TYPES DO NOT GO TO SMS
    # --------------------------------------------------------

    return (
        "discard",
        lead,
        f"unsupported_line_type_{line_type or 'unknown'}",
    )


# ============================================================
# PROCESS ALL REALTOR RECORDS
# ============================================================

def process_payload(payload):

    if isinstance(payload, dict):
        records = [payload]

    elif isinstance(payload, list):
        records = payload

    else:
        raise ValueError(
            "Dataset must contain a dictionary or list."
        )

    sms_leads = []
    facebook_leads = []
    verification_errors = []

    stats = {
        "received": len(records),
        "sms": 0,
        "facebook": 0,
        "discarded": 0,
        "verification_errors": 0,
    }

    for index, record in enumerate(
        records,
        start=1,
    ):

        destination, lead, reason = route_lead(
            record
        )

        # ----------------------------------------------------
        # SMS
        # ----------------------------------------------------

        if destination == "sms":

            sms_leads.append(lead)

            stats["sms"] += 1

        # ----------------------------------------------------
        # FACEBOOK
        # ----------------------------------------------------

        elif destination == "facebook":

            facebook_leads.append(lead)

            stats["facebook"] += 1

        # ----------------------------------------------------
        # NUMVERIFY ERROR
        # ----------------------------------------------------

        elif destination == "verification_error":

            verification_errors.append({
                **lead,
                "error_reason": reason,
            })

            stats["verification_errors"] += 1

        # ----------------------------------------------------
        # DISCARD
        # ----------------------------------------------------

        else:

            stats["discarded"] += 1

        # ----------------------------------------------------
        # RAILWAY LOG
        # ----------------------------------------------------

        print(
            f"[{index}/{len(records)}] "
            f"{lead['first_name']} | "
            f"{lead['phone_number']} | "
            f"{destination} | "
            f"{reason}",
            flush=True,
        )

        # Small pause for NumVerify
        time.sleep(0.1)

    return {
        "folder_name": "SMS Leads",

        "columns": COLUMNS,

        "sms_sheet": {
            "name": "SMS Leads",
            "columns": COLUMNS,
            "rows": sms_leads,
        },

        "facebook_sheet": {
            "name": "Facebook Leads",
            "columns": COLUMNS,
            "rows": facebook_leads,
        },

        "verification_errors":
            verification_errors,

        "stats":
            stats,
    }


# ============================================================
# FIND APIFY DATASET ID
# ============================================================

def find_dataset_id(payload):
    """
    Apify's webhook may contain the Actor run information
    rather than the actual Realtor records.

    Find defaultDatasetId from common locations.
    """

    if not isinstance(payload, dict):
        return ""

    # --------------------------------------------------------
    # RESOURCE
    # --------------------------------------------------------

    resource = payload.get("resource")

    if isinstance(resource, dict):

        dataset_id = clean(
            resource.get("defaultDatasetId")
        )

        if dataset_id:
            return dataset_id

    # --------------------------------------------------------
    # EVENT DATA
    # --------------------------------------------------------

    event_data = payload.get("eventData")

    if isinstance(event_data, dict):

        dataset_id = clean(
            event_data.get("defaultDatasetId")
        )

        if dataset_id:
            return dataset_id

    # --------------------------------------------------------
    # DIRECT
    # --------------------------------------------------------

    dataset_id = clean(
        payload.get("defaultDatasetId")
    )

    if dataset_id:
        return dataset_id

    return ""


# ============================================================
# FETCH ACTUAL APIFY DATASET
# ============================================================

def fetch_apify_dataset(dataset_id):
    """
    Fetch the actual dataset items after receiving the
    Actor webhook.
    """

    if not dataset_id:

        raise ValueError(
            "No Apify dataset ID supplied."
        )

    url = (
        f"https://api.apify.com/v2/datasets/"
        f"{dataset_id}/items"
    )

    params = {
        "clean": "true",
        "format": "json",
    }

    # Required for private datasets
    if APIFY_API_TOKEN:
        params["token"] = APIFY_API_TOKEN

    response = requests.get(
        url,
        params=params,
        timeout=60,
    )

    response.raise_for_status()

    data = response.json()

    if not isinstance(data, list):

        raise ValueError(
            "Apify dataset response was not a list."
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
    """
    Detect Realtor records in either:

    - flattened CSV-like format
    - nested live Apify JSON
    """

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
            get_nested(payload, field)
        )

        if value:
            return True

    return False


# ============================================================
# MAIN PROCESSOR
# ============================================================

def main(payload):
    """
    Supports:

    OPTION 1:
    Apify sends a list of actual dataset records.

    OPTION 2:
    Apify sends one actual Realtor record.

    OPTION 3:
    Apify sends Actor webhook containing defaultDatasetId.

    For option 3 we automatically fetch the dataset.
    """

    # --------------------------------------------------------
    # ACTUAL DATASET LIST
    # --------------------------------------------------------

    if isinstance(payload, list):

        print(
            f"Received {len(payload)} dataset records directly.",
            flush=True,
        )

        return process_payload(payload)

    # --------------------------------------------------------
    # ONE REALTOR RECORD
    # --------------------------------------------------------

    if looks_like_realtor_record(payload):

        print(
            "Received Realtor record directly.",
            flush=True,
        )

        return process_payload(payload)

    # --------------------------------------------------------
    # APIFY ACTOR EVENT
    # --------------------------------------------------------

    dataset_id = find_dataset_id(payload)

    if dataset_id:

        print(
            f"Apify webhook received. "
            f"Fetching dataset: {dataset_id}",
            flush=True,
        )

        dataset = fetch_apify_dataset(
            dataset_id
        )

        return process_payload(dataset)

    # --------------------------------------------------------
    # UNKNOWN
    # --------------------------------------------------------

    raise ValueError(
        "Webhook received, but no Realtor records "
        "or Apify defaultDatasetId were found."
    )


# ============================================================
# FASTAPI / RAILWAY
# ============================================================

app = FastAPI(
    title="SMS Harvester",
    version="1.1.0",
)


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/")
def health_check():

    return {
        "status": "online",
        "service": "SMS Harvester",
        "version": "1.1.0",
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
async def apify_webhook(request: Request):
    """
    Apify HTTP webhook destination.

    Flow:

    APIFY
        ↓
    Railway
        ↓
    /apify-webhook
        ↓
    Get dataset
        ↓
    Extract Realtor data
        ↓
    NumVerify
        ↓
    MOBILE → SMS
    LANDLINE + WEBSITE → FACEBOOK
    EVERYTHING ELSE → DISCARD
    """

    # --------------------------------------------------------
    # READ JSON
    # --------------------------------------------------------

    try:

        payload: Any = await request.json()

    except Exception:

        raise HTTPException(
            status_code=400,
            detail="Webhook body must contain valid JSON.",
        )

    # --------------------------------------------------------
    # PROCESS
    # --------------------------------------------------------

    try:

        result = main(payload)

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
