import os
import re
import requests
from datetime import datetime, timezone
from urllib.parse import urlparse

import pandas as pd
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


if not GITHUB_TOKEN:
    raise RuntimeError("Missing Railway variable: GITHUB_TOKEN")

if not GITHUB_REPO:
    raise RuntimeError("Missing Railway variable: GITHUB_REPO")

if not APIFY_TOKEN:
    raise RuntimeError("Missing Railway variable: APIFY_TOKEN")

if not HUNTER_API_KEY:
    raise RuntimeError("Missing Railway variable: HUNTER_API_KEY")


github = Github(GITHUB_TOKEN)


# =========================================================
# HELPERS
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


# =========================================================
# CLEAN PRICE
# =========================================================

def clean_price(value):

    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    cleaned = re.sub(
        r"[^\d.]",
        "",
        value
    )

    if not cleaned:
        return None

    try:
        return int(float(cleaned))

    except ValueError:
        return None


# =========================================================
# CLEAN / SHORTEN ADDRESS
# =========================================================

def clean_address(value):
    """
    Examples:

    10109 80 ST NW, Edmonton, Alberta T6A3H9
    becomes:
    10109 NW

    #3410 10360 102 ST NW, Edmonton...
    becomes:
    10360 NW
    """

    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None


    # Keep only street portion
    street = value.split("|")[0]
    street = street.split(",")[0]
    street = street.strip()


    # Remove a leading unit/apartment number
    #
    # Example:
    # #3410 10360 102 ST NW
    # becomes:
    # 10360 102 ST NW

    street_without_unit = re.sub(
        r"^\s*#\s*\d+\s+",
        "",
        street
    )


    # Find first building/street number
    number_match = re.search(
        r"\b(\d+)\b",
        street_without_unit
    )


    if not number_match:
        return street


    number = number_match.group(1)


    # Find NW / NE / SW / SE
    direction_match = re.search(
        r"\b(NW|NE|SW|SE)\b",
        street_without_unit,
        re.IGNORECASE
    )


    if direction_match:

        direction = (
            direction_match
            .group(1)
            .upper()
        )

        return f"{number} {direction}"


    # If there is no direction,
    # just return building number
    return number


# =========================================================
# BUILD PHONE NUMBER
# =========================================================

def build_phone(area_code, phone_number):
    """
    Example:

    780 + 907-0016

    becomes:

    17809070016
    """

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


    full_number = (
        area_code
        + phone_number
    )


    # If source already includes 1
    if (
        len(full_number) == 11
        and full_number.startswith("1")
    ):
        full_number = full_number[1:]


    # North American number = 10 digits
    if len(full_number) != 10:
        return None


    return "1" + full_number


# =========================================================
# DOWNLOAD APIFY DATASET
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
            detail=(
                "Apify dataset did not "
                "return valid JSON"
            )
        )


    if not isinstance(data, list):

        raise HTTPException(
            status_code=500,
            detail=(
                "Apify dataset did not "
                "return a list"
            )
        )


    return data


# =========================================================
# EXTRACT REALTOR FIELDS
# =========================================================

def extract_records(listings):

    rows = []


    for listing in listings:

        if not isinstance(listing, dict):
            continue


        bedrooms = get_nested(
            listing,
            [
                "Building",
                "Bedrooms"
            ]
        )


        first_name = get_nested(
            listing,
            [
                "Individual",
                0,
                "FirstName"
            ]
        )


        last_name = get_nested(
            listing,
            [
                "Individual",
                0,
                "LastName"
            ]
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


        price = get_nested(
            listing,
            [
                "Property",
                "Price"
            ]
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


        phone = build_phone(
            area_code,
            phone_number
        )


        rows.append({

            "Bedrooms":
                clean_text(bedrooms),

            "FirstName":
                clean_text(first_name),

            "LastName":
                clean_text(last_name),

            "Phone":
                phone,

            "Address":
                clean_address(address),

            "Price":
                clean_price(price),

            "Website":
                clean_text(website),

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
        "Price",
        "Website",
    ]


    df = pd.DataFrame(
        rows,
        columns=columns
    )


    if df.empty:
        return df


    # Remove fully empty rows
    df = df.dropna(
        how="all"
    )


    # Clean names
    for column in [
        "FirstName",
        "LastName"
    ]:

        df[column] = (
            df[column]
            .astype("string")
            .str.strip()
        )


    # Phone as text
    df["Phone"] = (
        df["Phone"]
        .astype("string")
    )


    # Clean website
    df["Website"] = (
        df["Website"]
        .astype("string")
        .str.strip()
    )


    # Treat empty websites as missing
    df["Website"] = (
        df["Website"]
        .replace({
            "": pd.NA,
            "None": pd.NA,
            "none": pd.NA,
            "nan": pd.NA,
            "<NA>": pd.NA,
        })
    )


    # Remove duplicate leads
    df = df.drop_duplicates(
        subset=[
            "FirstName",
            "LastName",
            "Phone",
            "Address"
        ],
        keep="first"
    )


    # Remove rows with no agent name
    df = df.dropna(
        subset=[
            "FirstName",
            "LastName"
        ],
        how="all"
    )


    return df.reset_index(
        drop=True
    )


# =========================================================
# GET DOMAIN FROM WEBSITE
# =========================================================

def get_domain_from_website(website):
    """
    Example:

    http://www.chetaylor.com/

    becomes:

    chetaylor.com
    """

    if website is None:
        return None


    website = str(website).strip()


    if not website:
        return None


    # Add https:// if missing
    if not website.startswith(
        (
            "http://",
            "https://"
        )
    ):
        website = (
            "https://"
            + website
        )


    try:

        parsed = urlparse(
            website
        )


        domain = (
            parsed.netloc
            .lower()
            .strip()
        )


        if domain.startswith(
            "www."
        ):
            domain = domain[4:]


        # Remove port if somehow present
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
    Hunter gets:

    First Name
    Last Name
    Website Domain

    Hunter attempts to find
    that person's email.
    """

    domain = get_domain_from_website(
        website
    )


    if (
        not first_name
        or not last_name
        or not domain
    ):
        return None


    url = (
        "https://api.hunter.io/"
        "v2/email-finder"
    )


    params = {

        "domain":
            domain,

        "first_name":
            first_name,

        "last_name":
            last_name,

        "api_key":
            HUNTER_API_KEY,
    }


    try:

        response = requests.get(
            url,
            params=params,
            timeout=20
        )


        if response.status_code != 200:

            print(
                "Hunter search error:",
                response.status_code,
                first_name,
                last_name,
                domain
            )

            return None


        result = response.json()

        data = (
            result.get("data")
            or {}
        )


        email = data.get(
            "email"
        )


        score = data.get(
            "score"
        )


        if not email:

            print(
                "Hunter found no email:",
                first_name,
                last_name,
                domain
            )

            return None


        print(
            "Hunter found:",
            first_name,
            last_name,
            email,
            "score:",
            score
        )


        return {

            "email":
                email,

            "score":
                score,

            "domain":
                domain,
        }


    except Exception as exc:

        print(
            "Hunter request failed:",
            first_name,
            last_name,
            str(exc)
        )

        return None


# =========================================================
# SAVE RESULT TO HUNTER LEADS
# =========================================================

def save_to_hunter_leads(
    first_name,
    last_name,
    phone,
    hunter_result
):
    """
    Saves successfully found emails
    into Hunter under the list:

    Apify Realtor Leads
    """

    if not hunter_result:
        return False


    email = hunter_result.get(
        "email"
    )

    domain = hunter_result.get(
        "domain"
    )

    score = hunter_result.get(
        "score"
    )


    if not email:
        return False


    url = (
        "https://api.hunter.io/"
        "v2/leads"
    )


    params = {
        "api_key":
            HUNTER_API_KEY
    }


    lead_data = {

        "email":
            email,

        "first_name":
            first_name,

        "last_name":
            last_name,

        "website":
            domain,

        "leads_list_name":
            "Apify Realtor Leads",
    }


    if phone:
        lead_data[
            "phone_number"
        ] = phone


    if score is not None:

        try:

            lead_data[
                "confidence_score"
            ] = int(score)

        except (
            ValueError,
            TypeError
        ):
            pass


    try:

        # PUT helps avoid duplicate
        # Hunter leads with same email.
        response = requests.put(
            url,
            params=params,
            json=lead_data,
            timeout=20
        )


        if response.status_code not in (
            200,
            201
        ):

            print(
                "Hunter lead save error:",
                response.status_code,
                email,
                response.text[:300]
            )

            return False


        print(
            "Saved to Hunter Leads:",
            email
        )


        return True


    except Exception as exc:

        print(
            "Hunter lead save failed:",
            email,
            str(exc)
        )

        return False


# =========================================================
# SEND ONLY WEBSITE LEADS TO HUNTER
# =========================================================

def send_website_leads_to_hunter(
    with_website
):
    """
    IMPORTANT:

    This function receives ONLY
    the with_website dataframe.

    Leads without websites NEVER
    get sent to Hunter.
    """

    searched = 0
    found = 0
    saved = 0


    for _, row in (
        with_website.iterrows()
    ):


        first_name = row.get(
            "FirstName"
        )

        last_name = row.get(
            "LastName"
        )

        website = row.get(
            "Website"
        )

        phone = row.get(
            "Phone"
        )


        # Skip missing required data
        if (
            pd.isna(first_name)
            or pd.isna(last_name)
            or pd.isna(website)
        ):
            continue


        first_name = str(
            first_name
        ).strip()

        last_name = str(
            last_name
        ).strip()

        website = str(
            website
        ).strip()


        if (
            not first_name
            or not last_name
            or not website
        ):
            continue


        searched += 1


        result = hunter_find_email(
            first_name,
            last_name,
            website
        )


        if not result:
            continue


        found += 1


        if pd.isna(phone):
            phone_value = None

        else:
            phone_value = str(
                phone
            ).strip()


        if save_to_hunter_leads(
            first_name,
            last_name,
            phone_value,
            result
        ):

            saved += 1


    return (
        searched,
        found,
        saved
    )


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/")
def health():

    return {

        "status":
            "ok",

        "service":
            "Apify Realtor CSV + Hunter",

        "webhook":
            "/webhook"
    }


# =========================================================
# APIFY WEBHOOK
# =========================================================

@app.post("/webhook")
async def apify_webhook(
    request: Request
):


    # -----------------------------------------
    # 1. READ WEBHOOK BODY
    # -----------------------------------------

    try:

        payload = (
            await request.json()
        )

    except Exception:

        raise HTTPException(
            status_code=400,
            detail="Invalid JSON received"
        )


    if isinstance(
        payload,
        dict
    ):

        print(
            "Webhook payload keys:",
            list(payload.keys())
        )


    # -----------------------------------------
    # 2. FIND APIFY DATASET ID
    # -----------------------------------------

    dataset_id = (

        get_nested(
            payload,
            [
                "resource",
                "defaultDatasetId"
            ]
        )

        or get_nested(
            payload,
            [
                "eventData",
                "defaultDatasetId"
            ]
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
            [
                "data",
                "defaultDatasetId"
            ]
        )

        or (
            payload.get(
                "defaultDatasetId"
            )

            if isinstance(
                payload,
                dict
            )

            else None
        )
    )


    if not dataset_id:

        print(
            "Could not find "
            "defaultDatasetId."
        )

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
    # 3. DOWNLOAD APIFY DATA
    # -----------------------------------------

    listings = (
        download_apify_dataset(
            dataset_id
        )
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
    # 4. EXTRACT REALTOR FIELDS
    # -----------------------------------------

    rows = extract_records(
        listings
    )


    if not rows:

        raise HTTPException(
            status_code=400,
            detail=(
                "No Realtor records found"
            )
        )


    # -----------------------------------------
    # 5. CLEAN DATA
    # -----------------------------------------

    df = clean_dataframe(
        rows
    )


    if df.empty:

        raise HTTPException(
            status_code=400,
            detail=(
                "No usable records "
                "after cleaning"
            )
        )


    # -----------------------------------------
    # 6. SPLIT LEADS
    # -----------------------------------------

    with_website = df[
        df["Website"].notna()
    ].copy()


    without_website = df[
        df["Website"].isna()
    ].copy()


    with_website = (
        with_website
        .reset_index(drop=True)
    )


    without_website = (
        without_website
        .reset_index(drop=True)
    )


    print(
        "Leads with website:",
        len(with_website)
    )


    print(
        "Leads without website:",
        len(without_website)
    )


    # =====================================================
    # 7. CREATE THE TWO CSV FILES
    # =====================================================

    with_website_csv = (
        with_website
        .to_csv(index=False)
    )


    without_website_csv = (
        without_website
        .to_csv(index=False)
    )


    # -----------------------------------------
    # TIMESTAMP
    # -----------------------------------------

    timestamp = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d_%H-%M-%S-%f"
    )


    # -----------------------------------------
    # FILE NAMES
    # -----------------------------------------

    with_website_filename = (
        f"{GITHUB_FOLDER}/"
        f"leads_with_website_"
        f"{timestamp}.csv"
    )


    without_website_filename = (
        f"{GITHUB_FOLDER}/"
        f"leads_without_website_"
        f"{timestamp}.csv"
    )


    # =====================================================
    # 8. UPLOAD BOTH CSV FILES TO GITHUB
    # =====================================================

    try:

        repo = github.get_repo(
            GITHUB_REPO
        )


        repo.create_file(

            path=
                with_website_filename,

            message=(
                "Add leads with website "
                f"{timestamp}"
            ),

            content=
                with_website_csv,

            branch=
                GITHUB_BRANCH,
        )


        print(
            "Uploaded:",
            with_website_filename
        )


        repo.create_file(

            path=
                without_website_filename,

            message=(
                "Add leads without website "
                f"{timestamp}"
            ),

            content=
                without_website_csv,

            branch=
                GITHUB_BRANCH,
        )


        print(
            "Uploaded:",
            without_website_filename
        )


    except GithubException as exc:

        print(
            "GitHub error:",
            exc
        )

        raise HTTPException(
            status_code=500,
            detail=(
                f"GitHub error: "
                f"{exc.data}"
            )
        )


    # =====================================================
    # 9. HUNTER.IO
    #
    # ONLY SEND LEADS THAT HAVE WEBSITES
    # =====================================================

    print(
        "Starting Hunter.io..."
    )


    (
        hunter_searched,
        hunter_found,
        hunter_saved

    ) = send_website_leads_to_hunter(
        with_website
    )


    print(
        "Hunter searches:",
        hunter_searched
    )


    print(
        "Hunter emails found:",
        hunter_found
    )


    print(
        "Hunter leads saved:",
        hunter_saved
    )


    # =====================================================
    # 10. SUCCESS RESPONSE
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

        "with_website_file":
            with_website_filename,

        "without_website_file":
            without_website_filename,

        "hunter_searched":
            hunter_searched,

        "hunter_emails_found":
            hunter_found,

        "hunter_leads_saved":
            hunter_saved,
    }
