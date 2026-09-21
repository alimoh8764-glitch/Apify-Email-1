import os
import re
import csv
import requests
from urllib.parse import urlparse


NUMVERIFY_API_KEY = os.getenv("NUMVERIFY_API_KEY")
NUMVERIFY_URL = "https://apilayer.net/api/validate"


# =========================================================
# PHONE NORMALIZATION
# =========================================================

def normalize_phone(area_code, phone_number):
    """
    Canadian/NANP number -> 1XXXXXXXXXX
    """

    area_code = str(area_code or "")
    phone_number = str(phone_number or "")

    digits = re.sub(r"\D", "", area_code + phone_number)

    if len(digits) == 10:
        return "1" + digits

    if len(digits) == 11 and digits.startswith("1"):
        return digits

    return None


# =========================================================
# NUMVERIFY
# =========================================================

def verify_phone_numverify(phone):
    if not phone:
        return None

    if not NUMVERIFY_API_KEY:
        raise RuntimeError(
            "NUMVERIFY_API_KEY is missing from environment variables."
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

        # Numverify API error
        if data.get("success") is False:
            print(f"Numverify API error: {data.get('error')}")
            return None

        return {
            "valid": bool(data.get("valid")),
            "country_code": data.get("country_code"),
            "carrier": data.get("carrier"),
            "line_type": str(
                data.get("line_type") or ""
            ).lower().strip(),
        }

    except requests.RequestException as exc:
        print(f"Numverify failed for {phone}: {exc}")
        return None


# =========================================================
# ADDRESS
# =========================================================

def shorten_address(address):
    """
    5207 28 Avenue SE|Calgary, Alberta T2B1N3
    ->
    5207 28 Avenue SE
    """

    if not address:
        return None

    address = str(address).strip()
    address = address.split("|")[0].strip()

    return address or None


# =========================================================
# WEBSITE CLEANING
# =========================================================

def clean_website(website):
    if not website:
        return None

    website = str(website).strip()

    if not website:
        return None

    return website


# =========================================================
# GET INDIVIDUAL PHONE
# =========================================================

def get_first_phone(individual):
    phones = individual.get("Phones") or []

    for phone in phones:
        normalized = normalize_phone(
            phone.get("AreaCode"),
            phone.get("PhoneNumber"),
        )

        if normalized:
            return normalized

    return None


# =========================================================
# FIND WEBSITE
# =========================================================

def get_website(listing, individual):
    """
    Realtor payloads can place websites in different
    locations, so check the common possibilities.

    Adjust/add fields here if your exact Apify payload
    uses another website field.
    """

    candidates = [
        individual.get("Website"),
        individual.get("WebsiteURL"),
        individual.get("WebSite"),
        listing.get("Website"),
        listing.get("WebsiteURL"),
    ]

    # Organization sometimes contains website
    organizations = listing.get("Organization") or []

    for org in organizations:
        candidates.extend([
            org.get("Website"),
            org.get("WebsiteURL"),
            org.get("WebSite"),
        ])

    for website in candidates:
        cleaned = clean_website(website)

        if cleaned:
            return cleaned

    return None


# =========================================================
# EXTRACT RAW LEAD
# =========================================================

def extract_lead(listing):
    individuals = listing.get("Individual") or []

    if not individuals:
        return None

    individual = individuals[0]

    first_name = individual.get("FirstName")
    phone = get_first_phone(individual)
    website = get_website(listing, individual)

    more_details = listing.get("moreDetails") or {}
    property_data = more_details.get("Property") or {}
    address_data = property_data.get("Address") or {}

    city = address_data.get("City")

    community = (
        address_data.get("CommunityName")
        or property_data.get("CommunityName")
        or more_details.get("CommunityName")
    )

    full_address = (
        address_data.get("AddressText")
        or address_data.get("Address")
    )

    address = shorten_address(full_address)

    return {
        "phone": phone,
        "first_name": first_name,
        "city": city,
        "community": community,
        "address": address,
        "website": website,
    }


# =========================================================
# PROCESS ALL LISTINGS
# =========================================================

def process_listings(listings):

    mobile_leads = []
    fb_leads = []

    # Prevent duplicate phone verification
    verification_cache = {}

    # Prevent duplicate output
    mobile_seen = set()
    fb_seen = set()

    for listing in listings:

        lead = extract_lead(listing)

        if not lead:
            continue

        phone = lead["phone"]
        website = lead["website"]

        verification = None

        # -------------------------------------------------
        # VERIFY PHONE
        # -------------------------------------------------

        if phone:

            if phone in verification_cache:
                verification = verification_cache[phone]

            else:
                print(f"Verifying {phone}...")

                verification = verify_phone_numverify(phone)

                verification_cache[phone] = verification

        # -------------------------------------------------
        # VERIFIED MOBILE
        # -------------------------------------------------

        is_mobile = False

        if verification:

            valid = verification.get("valid")
            country = verification.get("country_code")
            line_type = verification.get("line_type")

            if (
                valid is True
                and country == "CA"
                and line_type == "mobile"
            ):
                is_mobile = True

        if is_mobile:

            if phone not in mobile_seen:

                mobile_seen.add(phone)

                mobile_leads.append({
                    "phone": phone,
                    "first_name": lead["first_name"],
                    "city": lead["city"],
                    "community": lead["community"],
                    "address": lead["address"],
                    "carrier": verification.get("carrier"),
                })

            # If it's mobile, DON'T put it into FB.
            continue

        # -------------------------------------------------
        # FB LEADS
        # -------------------------------------------------
        #
        # Anything that isn't a verified mobile
        # but DOES have a website.
        #
        # This includes landlines with websites.
        # -------------------------------------------------

        if website:

            # Website is the best fallback dedupe key
            key = website.lower()

            if key not in fb_seen:

                fb_seen.add(key)

                fb_leads.append({
                    "phone": phone,
                    "first_name": lead["first_name"],
                    "city": lead["city"],
                    "community": lead["community"],
                    "address": lead["address"],
                    "website": website,
                    "phone_valid": (
                        verification.get("valid")
                        if verification
                        else False
                    ),
                    "phone_type": (
                        verification.get("line_type")
                        if verification
                        else None
                    ),
                    "carrier": (
                        verification.get("carrier")
                        if verification
                        else None
                    ),
                })

    return mobile_leads, fb_leads


# =========================================================
# SAVE CSV
# =========================================================

def save_csv(filename, rows, fields):

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


# =========================================================
# MAIN
# =========================================================

def run(listings):

    mobile_leads, fb_leads = process_listings(listings)

    # ---------------------------------------------
    # VERIFIED MOBILE LEADS
    # ---------------------------------------------

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
        ],
    )

    # ---------------------------------------------
    # WEBSITE / FB LEADS
    # ---------------------------------------------

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
        ],
    )

    print()
    print("==============================")
    print("PROCESSING COMPLETE")
    print("==============================")
    print(f"Verified mobile leads: {len(mobile_leads)}")
    print(f"FB / website leads:    {len(fb_leads)}")
    print("==============================")

    return mobile_leads, fb_leads
