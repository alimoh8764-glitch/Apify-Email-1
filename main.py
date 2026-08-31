import os
import re
from datetime import datetime, timezone

import pandas as pd
from fastapi import FastAPI, Request, HTTPException
from github import Github, GithubException


app = FastAPI()


# =========================================================
# RAILWAY ENVIRONMENT VARIABLES
# =========================================================

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_FOLDER = os.getenv("GITHUB_FOLDER", "data")


if not GITHUB_TOKEN:
    raise RuntimeError("Missing Railway variable: GITHUB_TOKEN")

if not GITHUB_REPO:
    raise RuntimeError("Missing Railway variable: GITHUB_REPO")


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
    """
    Example:

    780 + 907-0016
    becomes
    17809070016
    """

    if area_code is None or phone_number is None:
        return None

    area_code = re.sub(r"\D", "", str(area_code))
    phone_number = re.sub(r"\D", "", str(phone_number))

    if not area_code or not phone_number:
        return None

    full_number = area_code + phone_number

    # If number somehow already includes leading 1
    if len(full_number) == 11 and full_number.startswith("1"):
        full_number = full_number[1:]

    # US/Canada number should be 10 digits before country code
    if len(full_number) != 10:
        return None

    return "1" + full_number


# =========================================================
# EXTRACT ONLY WANTED FIELDS
# =========================================================

def extract_records(payload):

    if isinstance(payload, dict):

        if isinstance(payload.get("items"), list):
            listings = payload["items"]

        else:
            listings = [payload]

    elif isinstance(payload, list):
        listings = payload

    else:
        return []


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
            ["Individual", 0, "Phones", 0, "AreaCode"]
        )


        phone_number = get_nested(
            listing,
            ["Individual", 0, "Phones", 0, "PhoneNumber"]
        )


        address = get_nested(
            listing,
            ["Property", "Address", "AddressText"]
        )


        price = get_nested(
            listing,
            ["Property", "Price"]
        )


        website = get_nested(
            listing,
            ["Individual", 0, "Websites", 0, "Website"]
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
# CLEAN WITH PANDAS
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
    df = df.dropna(how="all")


    # Clean names
    for column in ["FirstName", "LastName"]:

        df[column] = (
            df[column]
            .astype("string")
            .str.strip()
        )


    # Keep phone as string
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


    df["Website"] = df["Website"].replace(
        {
            "": pd.NA,
            "None": pd.NA,
            "none": pd.NA,
            "nan": pd.NA,
            "<NA>": pd.NA,
        }
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


    # Remove rows with no name at all
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

    # Receive JSON
    try:
        payload = await request.json()

    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON received"
        )


    # Extract fields
    rows = extract_records(payload)


    if not rows:
        raise HTTPException(
            status_code=400,
            detail="No Realtor listing records found"
        )


    # Clean data
    df = clean_dataframe(rows)


    if df.empty:
        raise HTTPException(
            status_code=400,
            detail="No usable records remained after cleaning"
        )


    # =====================================================
    # SPLIT INTO TWO DATAFRAMES
    # =====================================================

    with_website = df[
        df["Website"].notna()
    ].copy()


    without_website = df[
        df["Website"].isna()
    ].copy()


    with_website = with_website.reset_index(
        drop=True
    )

    without_website = without_website.reset_index(
        drop=True
    )


    # =====================================================
    # CREATE CSV TEXT
    # =====================================================

    with_website_csv = with_website.to_csv(
        index=False
    )


    without_website_csv = without_website.to_csv(
        index=False
    )


    # =====================================================
    # TIMESTAMP
    # =====================================================

    timestamp = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d_%H-%M-%S-%f"
    )


    # =====================================================
    # FILENAMES
    # =====================================================

    with_website_filename = (
        f"{GITHUB_FOLDER}/"
        f"leads_with_website_{timestamp}.csv"
    )


    without_website_filename = (
        f"{GITHUB_FOLDER}/"
        f"leads_without_website_{timestamp}.csv"
    )


    # =====================================================
    # PUSH BOTH FILES TO GITHUB
    # =====================================================

    try:

        repo = github.get_repo(
            GITHUB_REPO
        )


        # File 1: leads with websites
        repo.create_file(
            path=with_website_filename,
            message=(
                f"Add leads with website {timestamp}"
            ),
            content=with_website_csv,
            branch=GITHUB_BRANCH,
        )


        # File 2: leads without websites
        repo.create_file(
            path=without_website_filename,
            message=(
                f"Add leads without website {timestamp}"
            ),
            content=without_website_csv,
            branch=GITHUB_BRANCH,
        )


    except GithubException as exc:

        print("GitHub error:", exc)

        raise HTTPException(
            status_code=500,
            detail=f"GitHub error: {exc.data}"
        )


    # =====================================================
    # SUCCESS
    # =====================================================

    return {

        "success": True,

        "records_received":
            len(rows),

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

        "repository":
            GITHUB_REPO
    }
