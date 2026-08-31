import os
import re
from io import BytesIO
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
    raise RuntimeError(
        "Missing Railway variable: GITHUB_TOKEN"
    )

if not GITHUB_REPO:
    raise RuntimeError(
        "Missing Railway variable: GITHUB_REPO"
    )


github = Github(GITHUB_TOKEN)


# =========================================================
# SAFELY GET NESTED APIFY VALUES
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


# =========================================================
# CLEAN TEXT
# =========================================================

def clean_text(value):

    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    return value


# =========================================================
# CLEAN PROPERTY PRICE
# =========================================================

def clean_price(value):
    """
    Example:

    $458,800

    becomes:

    458800
    """

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
# CLEAN ADDRESS
# =========================================================

def clean_address(value):
    """
    Example:

    8720 26 AV NW|Edmonton, Alberta T6K2X2

    becomes:

    8720 26 AV NW, Edmonton, Alberta T6K2X2
    """

    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    value = value.replace(
        "|",
        ", "
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value.strip()


# =========================================================
# BUILD NORTH AMERICAN PHONE NUMBER
# =========================================================

def build_phone(area_code, phone_number):
    """
    Combines AreaCode + PhoneNumber.

    Example:

    AreaCode:
    780

    PhoneNumber:
    907-0016

    Result:
    17809070016

    The leading 1 is added for US/Canada.
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


    # Remove leading 1 if source already includes it.
    # We will add exactly one below.
    if len(full_number) == 11 and full_number.startswith("1"):
        full_number = full_number[1:]


    # Standard US/Canada number should contain
    # 10 digits before country code.
    if len(full_number) != 10:
        return None


    return "1" + full_number


# =========================================================
# EXTRACT THE FIELDS WE WANT
# =========================================================

def extract_records(payload):

    if isinstance(payload, dict):

        if isinstance(
            payload.get("items"),
            list
        ):
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


        # Bedrooms
        bedrooms = get_nested(
            listing,
            [
                "Building",
                "Bedrooms"
            ]
        )


        # Agent first name
        first_name = get_nested(
            listing,
            [
                "Individual",
                0,
                "FirstName"
            ]
        )


        # Agent last name
        last_name = get_nested(
            listing,
            [
                "Individual",
                0,
                "LastName"
            ]
        )


        # Area code
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


        # Phone number
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


        # Property address
        address = get_nested(
            listing,
            [
                "Property",
                "Address",
                "AddressText"
            ]
        )


        # Property price
        price = get_nested(
            listing,
            [
                "Property",
                "Price"
            ]
        )


        # Agent website
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


        # Combine phone
        phone = build_phone(
            area_code,
            phone_number
        )


        row = {

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
        }


        rows.append(row)


    return rows


# =========================================================
# CLEAN DATA WITH PANDAS
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


    # Remove completely empty rows
    df = df.dropna(
        how="all"
    )


    # Clean name fields
    for column in [
        "FirstName",
        "LastName"
    ]:

        df[column] = (
            df[column]
            .astype("string")
            .str.strip()
        )


    # Keep phone numbers as text
    df["Phone"] = (
        df["Phone"]
        .astype("string")
    )


    # Clean websites
    df["Website"] = (
        df["Website"]
        .astype("string")
        .str.strip()
    )


    # Turn empty strings into missing values
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


    # Remove leads that have no agent name at all
    df = df.dropna(
        subset=[
            "FirstName",
            "LastName"
        ],
        how="all"
    )


    df = df.reset_index(
        drop=True
    )


    return df


# =========================================================
# CREATE EXCEL FILE WITH TWO SHEETS
# =========================================================

def create_excel_file(df):

    # -----------------------------------------
    # LEADS WITH WEBSITES
    # -----------------------------------------

    with_website = df[
        df["Website"].notna()
    ].copy()


    # -----------------------------------------
    # LEADS WITHOUT WEBSITES
    # -----------------------------------------

    no_website = df[
        df["Website"].isna()
    ].copy()


    # Reset row numbers
    with_website = with_website.reset_index(
        drop=True
    )

    no_website = no_website.reset_index(
        drop=True
    )


    # -----------------------------------------
    # CREATE EXCEL IN MEMORY
    # -----------------------------------------

    output = BytesIO()


    with pd.ExcelWriter(
        output,
        engine="openpyxl"
    ) as writer:


        with_website.to_excel(
            writer,
            sheet_name="With Website",
            index=False
        )


        no_website.to_excel(
            writer,
            sheet_name="No Website",
            index=False
        )


        # -------------------------------------
        # FORMAT BOTH SHEETS
        # -------------------------------------

        for sheet_name in [
            "With Website",
            "No Website"
        ]:

            worksheet = writer.sheets[
                sheet_name
            ]


            # Freeze header row
            worksheet.freeze_panes = "A2"


            # Add filters
            worksheet.auto_filter.ref = (
                worksheet.dimensions
            )


            # Column widths
            worksheet.column_dimensions[
                "A"
            ].width = 12

            worksheet.column_dimensions[
                "B"
            ].width = 18

            worksheet.column_dimensions[
                "C"
            ].width = 18

            worksheet.column_dimensions[
                "D"
            ].width = 18

            worksheet.column_dimensions[
                "E"
            ].width = 55

            worksheet.column_dimensions[
                "F"
            ].width = 15

            worksheet.column_dimensions[
                "G"
            ].width = 45


            # ---------------------------------
            # FORCE PHONE COLUMN TO TEXT
            # ---------------------------------

            for cell in worksheet["D"]:

                cell.number_format = "@"


    output.seek(0)

    return (
        output.getvalue(),
        len(with_website),
        len(no_website)
    )


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/")
def health():

    return {

        "status": "ok",

        "service":
            "Apify Realtor Lead Cleaner",

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
    # 1. RECEIVE JSON FROM APIFY
    # -----------------------------------------

    try:

        payload = await request.json()

    except Exception:

        raise HTTPException(
            status_code=400,
            detail="Invalid JSON received"
        )


    # -----------------------------------------
    # 2. EXTRACT WANTED FIELDS
    # -----------------------------------------

    rows = extract_records(
        payload
    )


    if not rows:

        raise HTTPException(
            status_code=400,
            detail=(
                "No Realtor listing records "
                "found in webhook payload."
            )
        )


    # -----------------------------------------
    # 3. CLEAN DATA
    # -----------------------------------------

    df = clean_dataframe(
        rows
    )


    if df.empty:

        raise HTTPException(
            status_code=400,
            detail=(
                "No usable records remained "
                "after cleaning."
            )
        )


    # -----------------------------------------
    # 4. CREATE EXCEL FILE
    # -----------------------------------------

    (
        excel_content,
        with_website_count,
        no_website_count

    ) = create_excel_file(df)


    # -----------------------------------------
    # 5. CREATE UNIQUE FILENAME
    # -----------------------------------------

    timestamp = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d_%H-%M-%S-%f"
    )


    filename = (
        f"{GITHUB_FOLDER}/"
        f"realtor_leads_{timestamp}.xlsx"
    )


    # -----------------------------------------
    # 6. PUSH EXCEL FILE TO GITHUB
    # -----------------------------------------

    try:

        repo = github.get_repo(
            GITHUB_REPO
        )


        repo.create_file(

            path=filename,

            message=(
                f"Add cleaned Realtor leads "
                f"{timestamp}"
            ),

            content=excel_content,

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
    # 7. SUCCESS RESPONSE
    # -----------------------------------------

    return {

        "success": True,

        "records_received":
            len(rows),

        "records_after_cleaning":
            len(df),

        "with_website":
            with_website_count,

        "without_website":
            no_website_count,

        "file":
            filename,

        "repository":
            GITHUB_REPO
    }
