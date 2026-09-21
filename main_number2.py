import os
import re
import csv
import requests

from fastapi import FastAPI, HTTPException, Body
from typing import Any


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="Canada Lead Processor",
    version="1.0.0"
)


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "Canada Lead Processor"
    }


@app.get("/health")
def health():
    return {
        "status": "healthy"
    }


# ============================================================
# CONFIG
# ============================================================

NUMVERIFY_API_KEY = os.getenv("NUMVERIFY_API_KEY")

NUMVERIFY_URL = "https://apilayer.net/api/validate"


# ============================================================
# PHONE NORMALIZATION
# ============================================================

def normalize_phone(area_code, phone_number):
    """
    Converts Canadian/NANP numbers into:

    14036134163

    No +, spaces, brackets or dashes.
    """

    area_code = str(area_code or "")
    phone_number = str(phone_number or "")

    digits = re.sub(
        r"\D",
        "",
        area_code + phone_number
    )

    # 4036134163 -> 14036134163
    if len(digits) == 10:
        return "1" + digits

    # Already has country code
    if len(digits) == 11 and digits.startswith("1"):
        return digits

    return None


# ============================================================
# NUMVERIFY
# ============================================================

def verify_phone_numverify(phone):
    """
    Verify a phone number with Numverify.
    """

    if not phone:
        return None

    if not NUMVERIFY_API_KEY:
        raise RuntimeError(
            "NUMVERIFY_API_KEY environment variable is missing."
        )

    try:

        response = requests.get(
            NUMVERIFY_URL,
            params={
                "access_key": NUMVERIFY_API_KEY,
                "number": phone,
                "country_code": "CA",
                "format": 1,
            },
            timeout=15,
        )

        response.raise_for_status()

        data = response.json()

        # Numverify API-level error
        if data.get("success") is False:

            print(
                f"Numverify API error for {phone}: "
                f"{data.get('error')}"
            )

            return None

        return {
            "valid": bool(
                data.get("valid")
            ),

            "country_code": (
                data.get("country_code")
            ),

            "carrier": (
                data.get("carrier")
            ),

            "line_type": str(
                data.get("line_type") or ""
            ).lower().strip(),
        }

    except requests.RequestException as exc:

        print(
            f"Numverify request failed "
            f"for {phone}: {exc}"
        )

        return None


# ============================================================
# ADDRESS
# ============================================================

def shorten_address(address):
    """
    Example:

    5207 28 Avenue SE|Calgary, Alberta T2B1N3

    becomes:

    5207 28 Avenue SE
    """

    if not address:
        return None

    address = str(address).strip()

    address = address.split("|")[0].strip()

    return address or None


# ============================================================
# WEBSITE CLEANING
# ============================================================

def clean_website(website):

    if not website:
        return None

    website = str(website).strip()

    if not website:
        return None

    return website


# ============================================================
# INDIVIDUAL PHONE
# ============================================================

def get_first_phone(individual):
    """
    Gets the first usable phone belonging to
    the individual Realtor/agent.
    """

    phones = individual.get("Phones") or []

    for phone in phones:

        normalized = normalize_phone(
            phone.get("AreaCode"),
            phone.get("PhoneNumber")
        )

        if normalized:
            return normalized

    return None


# ============================================================
# WEBSITE
# ============================================================

def get_website(listing, individual):
    """
    Look for a website on the individual,
    listing or organization.
    """

    candidates = [
        individual.get("Website"),
        individual.get("WebsiteURL"),
        individual.get("WebSite"),
        listing.get("Website"),
        listing.get("WebsiteURL"),
    ]

    organizations = (
        listing.get("Organization") or []
    )

    for organization in organizations:

        candidates.extend([
            organization.get("Website"),
            organization.get("WebsiteURL"),
            organization.get("WebSite"),
        ])

    for website in candidates:

        cleaned = clean_website(
            website
        )

        if cleaned:
            return cleaned

    return None


# ============================================================
# EXTRACT LEAD
# ============================================================

def extract_lead(listing):
    """
    Extract the information we need from
    one Realtor listing.
    """

    individuals = (
        listing.get("Individual") or []
    )

    if not individuals:
        return None

    # Use individual Realtor/agent
    individual = individuals[0]

    first_name = (
        individual.get("FirstName")
    )

    phone = get_first_phone(
        individual
    )

    website = get_website(
        listing,
        individual
    )

    # --------------------------------------------------------
    # PROPERTY
    # --------------------------------------------------------

    more_details = (
        listing.get("moreDetails") or {}
    )

    property_data = (
        more_details.get("Property") or {}
    )

    address_data = (
        property_data.get("Address") or {}
    )

    city = (
        address_data.get("City")
    )

    community = (
        address_data.get("CommunityName")
        or property_data.get("CommunityName")
        or more_details.get("CommunityName")
    )

    full_address = (
        address_data.get("AddressText")
        or address_data.get("Address")
    )

    address = shorten_address(
        full_address
    )

    return {
        "phone": phone,
        "first_name": first_name,
        "city": city,
        "community": community,
        "address": address,
        "website": website,
    }


# ============================================================
# PROCESS LISTINGS
# ============================================================

def process_listings(listings):

    mobile_leads = []
    fb_leads = []

    # Cache Numverify results so we don't
    # pay to verify the same number repeatedly.
    verification_cache = {}

    # Deduplication
    mobile_seen = set()
    fb_seen = set()

    for listing in listings:

        lead = extract_lead(
            listing
        )

        if not lead:
            continue

        phone = lead["phone"]
        website = lead["website"]

        verification = None

        # ====================================================
        # VERIFY PHONE
        # ====================================================

        if phone:

            if phone in verification_cache:

                verification = (
                    verification_cache[phone]
                )

            else:

                print(
                    f"Verifying phone: {phone}"
                )

                verification = (
                    verify_phone_numverify(
                        phone
                    )
                )

                verification_cache[
                    phone
                ] = verification

        # ====================================================
        # CHECK IF VERIFIED CANADIAN MOBILE
        # ====================================================

        is_mobile = False

        if verification:

            valid = (
                verification.get("valid")
            )

            country = (
                verification.get(
                    "country_code"
                )
            )

            line_type = (
                verification.get(
                    "line_type"
                )
            )

            if (
                valid is True
                and country == "CA"
                and line_type == "mobile"
            ):
                is_mobile = True

        # ====================================================
        # MOBILE LEADS
        # ====================================================

        if is_mobile:

            if phone not in mobile_seen:

                mobile_seen.add(
                    phone
                )

                mobile_leads.append({
                    "phone": phone,
                    "first_name": lead[
                        "first_name"
                    ],
                    "city": lead[
                        "city"
                    ],
                    "community": lead[
                        "community"
                    ],
                    "address": lead[
                        "address"
                    ],
                    "carrier": verification.get(
                        "carrier"
                    ),
                })

            # A mobile lead should NOT also
            # appear in FB.
            continue

        # ====================================================
        # FB LEADS
        # ====================================================
        #
        # If it is NOT a verified mobile
        # but it DOES have a website,
        # send it to FB.
        #
        # This includes verified landlines
        # with websites.
        # ====================================================

        if website:

            # Use website as dedupe key.
            key = website.lower()

            if key not in fb_seen:

                fb_seen.add(
                    key
                )

                fb_leads.append({

                    "phone": phone,

                    "first_name": lead[
                        "first_name"
                    ],

                    "city": lead[
                        "city"
                    ],

                    "community": lead[
                        "community"
                    ],

                    "address": lead[
                        "address"
                    ],

                    "website": website,

                    "phone_valid": (
                        verification.get(
                            "valid"
                        )
                        if verification
                        else False
                    ),

                    "phone_type": (
                        verification.get(
                            "line_type"
                        )
                        if verification
                        else None
                    ),

                    "carrier": (
                        verification.get(
                            "carrier"
                        )
                        if verification
                        else None
                    ),
                })

    return mobile_leads, fb_leads


# ============================================================
# SAVE CSV
# ============================================================

def save_csv(filename, rows, fields):
    """
    Save results to CSV.
    """

    with open(
        filename,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fields
        )

        writer.writeheader()

        for row in rows:
            writer.writerow(row)


# ============================================================
# RUN PROCESSOR
# ============================================================

def run(listings):

    mobile_leads, fb_leads = (
        process_listings(listings)
    )

    # ========================================================
    # MOBILE CSV
    # ========================================================

    save_csv(
        "mobile_leads.csv",
        mobile_leads,
        [
            "phone",
            "first_name",
            "city",
            "community",
            "address",
            "carrier",
        ]
    )

    # ========================================================
    # FB CSV
    # ========================================================

    save_csv(
        "FB.csv",
        fb_leads,
        [
            "phone",
            "first_name",
            "city",
            "community",
            "address",
            "website",
            "phone_valid",
            "phone_type",
            "carrier",
        ]
    )

    print()
    print(
        "===================================="
    )
    print(
        "PROCESSING COMPLETE"
    )
    print(
        "===================================="
    )
    print(
        f"Verified mobile leads: "
        f"{len(mobile_leads)}"
    )
    print(
        f"FB / website leads: "
        f"{len(fb_leads)}"
    )
    print(
        "===================================="
    )

    return mobile_leads, fb_leads


# ============================================================
# API - PROCESS APIFY PAYLOAD
# ============================================================

@app.post("/process")
async def process_payload(payload: Any = Body(...)):
    """
    Receive Realtor/Apify listings and process them.

    Supports either:

    [
        {...},
        {...}
    ]

    OR:

    {
        "listings": [
            {...},
            {...}
        ]
    }
    """

    try:

        # Direct array from Apify
        if isinstance(payload, list):

            listings = payload

        # Object containing listings
        elif isinstance(payload, dict):

            listings = payload.get(
                "listings",
                []
            )

        else:

            raise HTTPException(
                status_code=400,
                detail=(
                    "Payload must be a list "
                    "or an object containing listings."
                )
            )

        if not listings:

            raise HTTPException(
                status_code=400,
                detail="No listings found in payload."
            )

        print(
            f"Received {len(listings)} listings."
        )

        # Run lead processor
        mobile_leads, fb_leads = run(
            listings
        )

        # ====================================================
        # RETURN RESULTS
        # ====================================================

        return {

            "success": True,

            "stats": {

                "input_leads": len(
                    listings
                ),

                "verified_mobile_leads": len(
                    mobile_leads
                ),

                "fb_leads": len(
                    fb_leads
                ),
            },

            "mobile_leads": (
                mobile_leads
            ),

            "fb_leads": (
                fb_leads
            ),
        }

    except HTTPException:
        raise

    except Exception as exc:

        print(
            f"PROCESSING ERROR: {exc}"
        )

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )
