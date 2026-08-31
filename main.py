import os
import re
import requests
from datetime import datetime, timezone

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


if not GITHUB_TOKEN:
    raise RuntimeError("Missing Railway variable: GITHUB_TOKEN")

if not GITHUB_REPO:
    raise RuntimeError("Missing Railway variable: GITHUB_REPO")

if not APIFY_TOKEN:
    raise RuntimeError("Missing Railway variable: APIFY_TOKEN")


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


def clean_price(value):

    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    cleaned = re.sub(r"[^\d.]", "", value)

    if not cleaned:
        return None

    try:
        return int(float(cleaned))

    except ValueError:
        return None


def clean_address(value):

    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    value = value.replace("|", ", ")
    value = re.sub(r"\s+", " ", value)

    return value.strip()


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

    if len(full_number) == 11 and full_number.startswith("1"):
        full_number = full_number[1:]

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
        "format": "json"
    }

    response = requests.get(
        url,
        params=params,
        timeout=60
    )

    if response.status_code != 200:

        raise HTTPException(
            status_code=500,
            detail=(
                f"Could not download Apify dataset. "
                f"Status: {response.status_code}"
            )
        )

    data = response.json()

    if not isinstance(data, list):

        raise HTTPException(
            status_code=500,
            detail="Apify dataset did not return a list"
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
            "Bedrooms": clean_text(bedrooms),
            "FirstName": clean_text(first_name),
            "LastName": clean_text(last_name),
            "Phone": phone,
            "Address": clean_address(address),
            "Price": clean_price(price),
            "Website": clean_text(website),
        })

    return rows


# =========================================================
# CLEAN DATA
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
# HEALTH CHECK
# =========================================================

@app.get("/")
def health():

    return {
        "status": "ok",
        "service": "Apify Realtor CSV Cleaner",
        "webhook": "/webhook"
    }


# =========================================================
# APIFY WEBHOOK
# =========================================================

@app.post("/webhook")
async def apify_webhook(request: Request):

    # -----------------------------------------
    # 1. READ APIFY WEBHOOK BODY
    # -----------------------------------------

    try:
        payload = await request.json()

    except Exception:

        raise HTTPException(
            status_code=400,
            detail="Invalid JSON received"
        )


    # -----------------------------------------
    # 2. FIND DATASET ID
    # -----------------------------------------

    dataset_id = get_nested(
        payload,
        [
            "eventData",
            "defaultDatasetId"
        ]
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
    # 3. DOWNLOAD ACTUAL REALTOR DATA
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
    # 4. EXTRACT FIELDS
    # -----------------------------------------

    rows = extract_records(
        listings
    )


    if not rows:

        raise HTTPException(
            status_code=400,
            detail="No Realtor records found"
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
            detail="No usable records after cleaning"
        )


    # -----------------------------------------
    # 6. SPLIT INTO TWO CSV FILES
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


    # -----------------------------------------
    # 7. CREATE CSV CONTENT
    # -----------------------------------------

    with_website_csv = (
        with_website
        .to_csv(index=False)
    )


    without_website_csv = (
        without_website
        .to_csv(index=False)
    )


    # -----------------------------------------
    # 8. FILENAMES
    # -----------------------------------------

    timestamp = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d_%H-%M-%S-%f"
    )


    with_website_filename = (
        f"{GITHUB_FOLDER}/"
        f"leads_with_website_{timestamp}.csv"
    )


    without_website_filename = (
        f"{GITHUB_FOLDER}/"
        f"leads_without_website_{timestamp}.csv"
    )


    # -----------------------------------------
    # 9. UPLOAD BOTH TO GITHUB
    # -----------------------------------------

    try:

        repo = github.get_repo(
            GITHUB_REPO
        )


        repo.create_file(
            path=with_website_filename,
            message=(
                f"Add leads with website "
                f"{timestamp}"
            ),
            content=with_website_csv,
            branch=GITHUB_BRANCH,
        )


        repo.create_file(
            path=without_website_filename,
            message=(
                f"Add leads without website "
                f"{timestamp}"
            ),
            content=without_website_csv,
            branch=GITHUB_BRANCH,
        )


    except GithubException as exc:

        print(
            "GitHub error:",
            exc
        )

        raise HTTPException(
            status_code=500,
            detail=f"GitHub error: {exc.data}"
        )


    # -----------------------------------------
    # 10. SUCCESS
    # -----------------------------------------

    return {

        "success": True,

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
    }
