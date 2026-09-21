import os
import re
import requests


NUMVERIFY_API_KEY = os.getenv("NUMVERIFY_API_KEY")
NUMVERIFY_URL = "https://apilayer.net/api/validate"


def normalize_phone(area_code, phone_number):
    """
    Converts:
    (403) 613-4163
    403-613-4163
    +1 403 613 4163

    Into:
    14036134163
    """

    area_code = str(area_code or "")
    phone_number = str(phone_number or "")

    digits = re.sub(r"\D", "", area_code + phone_number)

    if len(digits) == 10:
        return "1" + digits

    if len(digits) == 11 and digits.startswith("1"):
        return digits

    return None


def verify_phone_numverify(phone):
    """
    Verify phone using Numverify.

    Returns:
    {
        "valid": True,
        "country_code": "CA",
        "carrier": "...",
        "line_type": "mobile"
    }
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
                "Numverify error:",
                data.get("error")
            )
            return None

        return {
            "valid": data.get("valid", False),
            "country_code": data.get("country_code"),
            "carrier": data.get("carrier"),
            "line_type": data.get("line_type"),
        }

    except requests.RequestException as exc:
        print(f"Numverify request failed for {phone}: {exc}")
        return None


def shorten_address(address):
    """
    5207 28 Avenue SE|Calgary, Alberta T2B1N3

    becomes:

    5207 28 Avenue SE
    """

    if not address:
        return None

    return str(address).split("|")[0].strip() or None


def get_first_phone(individual):
    """
    Gets the first valid-looking phone belonging
    to the individual Realtor/agent.
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


def transform_listing(listing):
    """
    Convert one Realtor listing into a cleaned lead.
    """

    individuals = listing.get("Individual") or []

    if not individuals:
        return None

    individual = individuals[0]

    first_name = individual.get("FirstName")

    phone = get_first_phone(individual)

    if not phone:
        return None

    # -------------------------
    # Verify with Numverify
    # -------------------------

    verification = verify_phone_numverify(phone)

    if not verification:
        return None

    # Reject invalid numbers
    if not verification["valid"]:
        return None

    # Make sure Numverify says Canada
    if verification["country_code"] != "CA":
        return None

    # -------------------------
    # Property
    # -------------------------

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

        # Numverify information
        "phone_valid": verification["valid"],
        "phone_type": verification["line_type"],
        "carrier": verification["carrier"],
    }


def transform_listings(listings):
    """
    Process full Apify payload.

    Also deduplicates agents by phone number so
    Numverify isn't called repeatedly for the same agent.
    """

    results = []

    seen_phones = set()

    for listing in listings:

        # Check phone BEFORE calling Numverify
        individuals = listing.get("Individual") or []

        if not individuals:
            continue

        raw_phone = get_first_phone(individuals[0])

        if not raw_phone:
            continue

        # Avoid duplicate agent + duplicate Numverify API charge
        if raw_phone in seen_phones:
            continue

        seen_phones.add(raw_phone)

        lead = transform_listing(listing)

        if lead:
            results.append(lead)

    return results
