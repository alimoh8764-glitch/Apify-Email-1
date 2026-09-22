"""
SMS HARVESTER
=============

Apify -> Railway webhook -> Realtor.ca lead extraction
-> NumVerify -> SMS / Facebook routing

FINAL OUTPUT COLUMNS:
1. first_name
2. phone_number
3. city
4. community
5. price
6. address
7. bedrooms
8. website
9. registration_number

ROUTING:
- Valid MOBILE -> SMS Leads
- Valid LANDLINE + website -> Facebook Leads
- Valid LANDLINE + no website -> Discard
- Invalid -> Discard
- Unsupported line type -> Discard
- NumVerify API failure -> Verification Errors

ENVIRONMENT VARIABLES:
NUMVERIFY_API_KEY=your_numverify_key
APIFY_API_TOKEN=your_apify_token
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
# GENERAL CLEANING
# ============================================================

def clean(value):
    """
    Convert null / blank / NaN-like values into an empty string.
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


def first_value(data, *keys):
    """
    Return the first populated field from the supplied keys.

    Supports both the standard and moreDetails Realtor.ca
    fields from the Apify output.
    """

    for key in keys:

        value = clean(data.get(key))

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
# REGISTRATION / REALTOR INDIVIDUAL ID
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
    Get the INDIVIDUAL agent's phone.

    We deliberately avoid using the brokerage/organization
    phone number.
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

    # Standard 10-digit US/Canadian number
    if len(number) == 10:
        return "1" + number

    # Already has North American country code
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

    # Fallback to formatted price
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
# SHORTENED ADDRESS
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

    # Additional fallback
    address = address.split(",")[0]

    address = address.strip()

    # Remove apartment/unit information
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
# AGENT WEBSITE
# ============================================================

def get_website(data):
    """
    Individual agent website.

    Does not intentionally substitute the brokerage website.
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
    """
    Python calls NumVerify directly.

    We care about:
    - valid
    - line_type
    - country_code
    - carrier
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

    # NumVerify API-level error
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
    FINAL ROUTING:

    VALID + MOBILE
        -> SMS

    VALID + LANDLINE + WEBSITE
        -> FACEBOOK

    VALID + LANDLINE + NO WEBSITE
        -> DISCARD

    INVALID
        -> DISCARD

    UNKNOWN/OTHER LINE TYPE
        -> DISCARD

    NUMVERIFY FAILURE
        -> VERIFICATION ERROR
    """

    lead = build_lead(data)

    phone = lead["phone_number"]

    # --------------------------------------------------------
    # NO PHONE
    # --------------------------------------------------------

    if not phone:

        return (
            "discard",
            lead,
            "missing_or_bad_phone",
        )

    # --------------------------------------------------------
    # NUMVERIFY
    # --------------------------------------------------------

    verification = numverify(phone)

    # --------------------------------------------------------
    # API ERROR
    # --------------------------------------------------------

    if not verification["success"]:

        return (
            "verification_error",
            lead,
            "numverify_error",
        )

    # --------------------------------------------------------
    # INVALID
    # --------------------------------------------------------

    if not verification["valid"]:

        return (
            "discard",
            lead,
            "invalid_phone",
        )

    # --------------------------------------------------------
    # US / CANADA ONLY
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

        # Landline WITH website -> Facebook
        if clean(lead["website"]):

            return (
                "facebook",
                lead,
                "landline_with_website",
            )

        # Landline WITHOUT website -> discard
        return (
            "discard",
            lead,
            "landline_without_website",
        )

    # --------------------------------------------------------
    # ALL OTHER LINE TYPES
    # --------------------------------------------------------

    return (
        "discard",
        lead,
        f"unsupported_line_type_{line_type or 'unknown'}",
    )


# ============================================================
# PROCESS ALL RECORDS
# ============================================================

def process_payload(payload):
    """
    Process actual Realtor/Apify dataset records.
    """

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

        print(
            f"[{index}/{len(records)}] "
            f"{lead['first_name']} | "
            f"{lead['phone_number']} | "
            f"{destination} | "
            f"{reason}"
        )

        # Small pause between NumVerify requests
        time.sleep(0.1)

    return {
        "folder_name": "SMS Leads",

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
# APIFY DATASET FETCHING
# ============================================================

def find_dataset_id(payload):
    """
    Find defaultDatasetId in common Apify webhook structures.

    Apify webhooks may contain the Actor run information
    rather than the actual dataset rows.
    """

    if not isinstance(payload, dict):
        return ""

    # Common location
    resource = payload.get("resource")

    if isinstance(resource, dict):

        dataset_id = clean(
            resource.get("defaultDatasetId")
        )

        if dataset_id:
            return dataset_id

    # Alternative eventData location
    event_data = payload.get("eventData")

    if isinstance(event_data, dict):

        dataset_id = clean(
            event_data.get("defaultDatasetId")
        )

        if dataset_id:
            return dataset_id

    # Direct location fallback
    dataset_id = clean(
        payload.get("defaultDatasetId")
    )

    if dataset_id:
        return dataset_id

    return ""


def fetch_apify_dataset(dataset_id):
    """
    Fetch actual dataset items from Apify.

    APIFY_API_TOKEN is optional for public datasets
    but required for private datasets.
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

    return data


# ============================================================
# DETECT ACTUAL DATASET PAYLOAD
# ============================================================

def looks_like_realtor_record(payload):
    """
    Determine whether Apify sent an actual Realtor dataset
    record rather than an Actor event.
    """

    if not isinstance(payload, dict):
        return False

    possible_fields = {
        "Individual/0/FirstName",
        "moreDetails/Individual/0/FirstName",
        "Property/Address/AddressText",
        "moreDetails/Property/Address/AddressText",
        "Building/Bedrooms",
        "moreDetails/Building/Bedrooms",
    }

    return any(
        key in payload
        for key in possible_fields
    )


# ============================================================
# MAIN PROCESSOR
# ============================================================

def main(payload):
    """
    Supports BOTH:

    1. Apify sends actual dataset records

    OR

    2. Apify sends an Actor webhook containing
       defaultDatasetId.

    In case #2, Python automatically fetches the dataset.
    """

    # --------------------------------------------------------
    # APIFY SENT A LIST OF DATASET ITEMS
    # --------------------------------------------------------

    if isinstance(payload, list):

        return process_payload(payload)

    # --------------------------------------------------------
    # APIFY SENT ONE ACTUAL REALTOR RECORD
    # --------------------------------------------------------

    if looks_like_realtor_record(payload):

        return process_payload(payload)

    # --------------------------------------------------------
    # APIFY SENT ACTOR RUN WEBHOOK
    # --------------------------------------------------------

    dataset_id = find_dataset_id(payload)

    if dataset_id:

        print(
            f"Apify webhook received. "
            f"Fetching dataset: {dataset_id}"
        )

        dataset = fetch_apify_dataset(
            dataset_id
        )

        return process_payload(dataset)

    # --------------------------------------------------------
    # UNKNOWN PAYLOAD
    # --------------------------------------------------------

    raise ValueError(
        "Webhook received, but no Realtor records or "
        "Apify defaultDatasetId were found."
    )


# ============================================================
# FASTAPI / RAILWAY
# ============================================================

app = FastAPI(
    title="SMS Harvester",
    version="1.0.0",
)


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/")
def health_check():

    return {
        "status": "online",
        "service": "SMS Harvester",
        "version": "1.0.0",
    }


@app.get("/health")
def health():

    return {
        "ok": True,
    }


# ============================================================
# APIFY WEBHOOK ENDPOINT
# ============================================================

@app.post("/apify-webhook")
async def apify_webhook(request: Request):
    """
    PUBLIC ENDPOINT FOR APIFY.

    Apify sends an HTTP POST here.

    The endpoint:
    1. Reads JSON body
    2. Determines whether it contains dataset rows or an
       Actor run event
    3. Fetches dataset if necessary
    4. Cleans Realtor data
    5. Runs NumVerify
    6. Routes leads
    7. Returns SMS/Facebook results
    """

    try:

        payload: Any = await request.json()

    except Exception:

        raise HTTPException(
            status_code=400,
            detail="Webhook body must contain valid JSON.",
        )

    try:

        result = main(payload)

        return {
            "success": True,
            "result": result,
        }

    except Exception as error:

        print(
            f"WEBHOOK ERROR: {error}"
        )

        raise HTTPException(
            status_code=500,
            detail=str(error),
        )
