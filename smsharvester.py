import os
import re
import time
import requests


# ============================================================
# CONFIG
# ============================================================

# Recommended:
# Set NUMVERIFY_API_KEY as an environment variable.
#
# Or, if your automation platform requires it, replace:
# YOUR_NUMVERIFY_API_KEY
# with the actual key.

NUMVERIFY_API_KEY = os.getenv(
    "NUMVERIFY_API_KEY",
    "YOUR_NUMVERIFY_API_KEY"
)

NUMVERIFY_URL = "https://apilayer.net/api/validate"

REQUEST_TIMEOUT = 20


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
    """Convert null/blank/NaN-like values into empty strings."""

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
    Return the first populated field.

    Your Apify/Realtor data contains fields in slightly
    different locations, so this supports both versions.
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

    # Fallback if only full name exists

    full_name = first_value(
        data,
        "Individual/0/Name",
        "moreDetails/Individual/0/Name",
    )

    if full_name:
        return full_name.split()[0]

    return ""


# ============================================================
# REGISTRATION / INDIVIDUAL ID
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
        area_code
    )

    phone_digits = re.sub(
        r"\D",
        "",
        phone
    )

    number = area_digits + phone_digits

    # Canada / US number without country prefix
    if len(number) == 10:
        return "1" + number

    # Already correctly formatted
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
        value
    )

    if digits:
        return int(digits)

    return ""


# ============================================================
# ADDRESS
# ============================================================

def get_address(data):
    """
    Convert:

    23 Penworth Crescent SE|Calgary, Alberta T2A4C5

    into:

    23 Penworth Crescent SE
    """

    address = first_value(
        data,
        "Property/Address/AddressText",
        "moreDetails/Property/Address/AddressText",
    )

    if not address:
        return ""

    # Realtor.ca format
    address = address.split("|")[0]

    # Additional fallback
    address = address.split(",")[0]

    address = address.strip()

    # Remove unit/apartment information if present

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
    Realtor values:

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
        value
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
    Agent website — not intentionally the brokerage website.
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
    Python itself sends the phone to NumVerify.

    Returns:
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

    if (
        not NUMVERIFY_API_KEY
        or NUMVERIFY_API_KEY == "YOUR_NUMVERIFY_API_KEY"
    ):

        raise RuntimeError(
            "NumVerify API key has not been configured."
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

    except (requests.RequestException, ValueError) as error:

        return {
            "success": False,
            "error": str(error),
            "valid": False,
            "line_type": "",
            "country_code": "",
            "carrier": "",
        }

    # NumVerify API error
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
# ROUTING
# ============================================================

def route_lead(data):
    """
    FINAL RULES:

    VALID MOBILE
        -> SMS

    VALID LANDLINE + WEBSITE
        -> FACEBOOK

    VALID LANDLINE + NO WEBSITE
        -> DISCARD

    INVALID
        -> DISCARD

    ANY OTHER LINE TYPE
        -> DISCARD

    NUMVERIFY FAILURE
        -> ERROR / RETRY
    """

    lead = build_lead(data)

    phone = lead["phone_number"]

    # ----------------------------------------
    # No usable phone
    # ----------------------------------------

    if not phone:

        return (
            "discard",
            lead,
            "missing_phone"
        )

    # ----------------------------------------
    # Call NumVerify
    # ----------------------------------------

    verification = numverify(phone)

    # ----------------------------------------
    # API failure
    # ----------------------------------------

    if not verification["success"]:

        return (
            "verification_error",
            lead,
            "numverify_error"
        )

    # ----------------------------------------
    # Invalid phone
    # ----------------------------------------

    if not verification["valid"]:

        return (
            "discard",
            lead,
            "invalid_phone"
        )

    # ----------------------------------------
    # Must be US or Canada
    # ----------------------------------------

    if verification["country_code"] not in {
        "US",
        "CA",
    }:

        return (
            "discard",
            lead,
            "wrong_country"
        )

    line_type = verification["line_type"]

    # ========================================
    # MOBILE
    # ========================================

    if line_type == "mobile":

        return (
            "sms",
            lead,
            "valid_mobile"
        )

    # ========================================
    # LANDLINE
    # ========================================

    if line_type == "landline":

        # Has website
        if clean(lead["website"]):

            return (
                "facebook",
                lead,
                "landline_with_website"
            )

        # No website
        return (
            "discard",
            lead,
            "landline_without_website"
        )

    # ========================================
    # EVERYTHING ELSE
    # ========================================

    return (
        "discard",
        lead,
        f"unsupported_line_type_{line_type or 'unknown'}"
    )


# ============================================================
# PROCESS COMPLETE APIFY PAYLOAD
# ============================================================

def process_payload(payload):

    # Allow either:
    # single Apify object
    # OR list of Apify objects

    if isinstance(payload, dict):
        records = [payload]

    elif isinstance(payload, list):
        records = payload

    else:
        raise ValueError(
            "Payload must be a dictionary or list of dictionaries."
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
        start=1
    ):

        destination, lead, reason = route_lead(
            record
        )

        # ====================================
        # SMS
        # ====================================

        if destination == "sms":

            sms_leads.append(lead)

            stats["sms"] += 1

        # ====================================
        # FACEBOOK
        # ====================================

        elif destination == "facebook":

            facebook_leads.append(lead)

            stats["facebook"] += 1

        # ====================================
        # NUMVERIFY ERROR
        # ====================================

        elif destination == "verification_error":

            verification_errors.append({
                **lead,
                "error_reason": reason,
            })

            stats["verification_errors"] += 1

        # ====================================
        # DISCARD
        # ====================================

        else:

            stats["discarded"] += 1

        print(
            f"[{index}/{len(records)}] "
            f"{lead['first_name']} | "
            f"{lead['phone_number']} | "
            f"{destination} | "
            f"{reason}"
        )

        # Avoid hammering NumVerify.
        # Adjust based on your API plan.
        time.sleep(0.1)

    # ========================================
    # FINAL OUTPUT
    # ========================================

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

        "verification_errors": verification_errors,

        "stats": stats,
    }


# ============================================================
# MAIN ENTRY POINT
# ============================================================

def main(payload):
    """
    This is the function your automation should call.

    INPUT:
        Raw Apify Actor payload

    OUTPUT:
        Ready-to-write SMS Leads
        Ready-to-write Facebook Leads
    """

    return process_payload(payload)
