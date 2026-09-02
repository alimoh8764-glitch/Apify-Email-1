import os
import re
import time
from io import StringIO
from datetime import datetime, timezone
from urllib.parse import urlparse

import pandas as pd
import requests
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
# BASIC HELPERS
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


# =========================================================
# ADDRESS + PHONE HELPERS (ACTOR #2)
# =========================================================

def clean_address(value):
    """Actor #2 already provides a clean street address in address_line."""
    return clean_text(value)


def clean_phone(value):
    """Normalize a US/Canada 10-digit phone to 1XXXXXXXXXX."""
    if value is None:
        return None

    digits = re.sub(r"\D", "", str(value))

    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]

    if len(digits) != 10:
        return None

    return "1" + digits


def split_agent_name(full_name):
    """
    Split primary_agent_name into FirstName / LastName.
    Middle names/initials stay with LastName so no information is discarded.
    """
    name = clean_text(full_name)

    if not name:
        return None, None

    parts = name.split()

    if len(parts) == 1:
        # Some actor rows can contain names like "NICKALLEN".
        # Keep the value instead of guessing where the surname starts.
        return parts[0], None

    return parts[0], " ".join(parts[1:])


# =========================================================
# DOWNLOAD APIFY DATA
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
            detail="Apify dataset did not return valid JSON"
        )

    if not isinstance(data, list):
        raise HTTPException(
            status_code=500,
            detail="Apify dataset did not return a list"
        )

    return data


# =========================================================
# EXTRACT REALTOR.COM ACTOR #2 DATA
# =========================================================

def extract_records(listings):

    rows = []

    for listing in listings:

        if not isinstance(listing, dict):
            continue

        first_name, last_name = split_agent_name(
            listing.get("primary_agent_name")
        )

        rows.append({
            "Bedrooms": clean_text(listing.get("beds")),
            "FirstName": clean_text(first_name),
            "LastName": clean_text(last_name),
            "Phone": clean_phone(listing.get("primary_agent_phone")),
            "Address": clean_address(listing.get("address_line")),
            "City": clean_text(listing.get("address_city")),
            "Price": clean_price(listing.get("list_price")),
            "Website": clean_text(listing.get("primary_agent_href")),
            "Email": clean_text(listing.get("primary_agent_email")),
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
        "City",
        "Price",
        "Website",
        "Email",
    ]

    df = pd.DataFrame(rows, columns=columns)

    if df.empty:
        return df

    df = df.dropna(how="all")

    for column in ["FirstName", "LastName", "City", "Website", "Email"]:
        df[column] = df[column].astype("string").str.strip()

    df["Phone"] = df["Phone"].astype("string")

    for column in ["Website", "Email"]:
        df[column] = df[column].replace({
            "": pd.NA,
            "None": pd.NA,
            "none": pd.NA,
            "nan": pd.NA,
            "<NA>": pd.NA,
        })

    df["Email"] = df["Email"].str.lower()

    # Prefer deduplication by email when present, while still protecting
    # against duplicate listing-agent rows with no email.
    with_email = df[df["Email"].notna()].drop_duplicates(
        subset=["Email"], keep="first"
    )

    without_email = df[df["Email"].isna()].drop_duplicates(
        subset=["FirstName", "LastName", "Phone", "Address"],
        keep="first"
    )

    df = pd.concat([with_email, without_email], ignore_index=True)

    # Keep a row if we have at least some agent identity/contact information.
    df = df.dropna(
        subset=["FirstName", "LastName", "Phone", "Email"],
        how="all"
    )

    return df.reset_index(drop=True)


# =========================================================
# WEBSITE -> DOMAIN
# =========================================================

def get_domain_from_website(website):
    if website is None or pd.isna(website):
        return None

    website = str(website).strip()

    if not website:
        return None

    if not website.startswith(("http://", "https://")):
        website = "https://" + website

    try:
        parsed = urlparse(website)
        domain = parsed.netloc.lower().strip()

        if domain.startswith("www."):
            domain = domain[4:]

        domain = domain.split(":")[0]
        return domain or None

    except Exception:
        return None


# =========================================================
# HUNTER EMAIL VERIFIER
# =========================================================

def hunter_verify_email(email):
    """
    Verify an actor-provided email with Hunter.

    outcomes:
      verified       -> Hunter returned a completed status
      invalid_input  -> missing/malformed email
      suppressed     -> Hunter privacy suppression (451)
      api_error      -> temporary/permanent API failure

    Hunter can return statuses including:
      valid, invalid, accept_all, webmail, disposable, unknown
    """

    email = clean_text(email)

    if not email or "@" not in email:
        return {
            "outcome": "invalid_input",
            "email": email,
            "score": None,
            "verification_status": None,
        }

    url = "https://api.hunter.io/v2/email-verifier"

    params = {
        "email": email,
        "api_key": HUNTER_API_KEY,
    }

    # A 202 means Hunter is still processing. Re-requesting the same
    # verification does not create another verification charge.
    for attempt in range(6):

        try:
            response = requests.get(
                url,
                params=params,
                timeout=30
            )

        except requests.RequestException as exc:
            print("Hunter verifier connection error:", email, exc)

            if attempt < 5:
                time.sleep(min(2 ** attempt, 10))
                continue

            return {
                "outcome": "api_error",
                "email": email,
                "score": None,
                "verification_status": None,
            }

        if response.status_code == 200:
            try:
                payload = response.json()
            except Exception:
                return {
                    "outcome": "api_error",
                    "email": email,
                    "score": None,
                    "verification_status": None,
                }

            data = payload.get("data") or {}
            status = clean_text(data.get("status"))
            score = data.get("score")
            verified_email = clean_text(data.get("email")) or email

            print(
                "Hunter VERIFIED:",
                verified_email,
                "status:",
                status,
                "score:",
                score
            )

            return {
                "outcome": "verified",
                "email": verified_email,
                "score": score,
                "verification_status": status,
            }

        if response.status_code == 202:
            print("Hunter verification still processing:", email)

            if attempt < 5:
                time.sleep(3)
                continue

            return {
                "outcome": "api_error",
                "email": email,
                "score": None,
                "verification_status": None,
            }

        if response.status_code == 451:
            print("Hunter SUPPRESSED:", email)
            return {
                "outcome": "suppressed",
                "email": email,
                "score": None,
                "verification_status": None,
            }

        # Hunter documents 403 for rate limiting. Keep common transient
        # server statuses here too.
        if response.status_code in (403, 429, 500, 502, 503, 504, 222):
            print(
                "Hunter verifier temporary error:",
                response.status_code,
                "attempt:",
                attempt + 1,
                email
            )

            if attempt < 5:
                time.sleep(min(2 ** attempt, 10))
                continue

            return {
                "outcome": "api_error",
                "email": email,
                "score": None,
                "verification_status": None,
            }

        print(
            "Hunter verifier error:",
            response.status_code,
            email,
            response.text[:300]
        )

        return {
            "outcome": "invalid_input",
            "email": email,
            "score": None,
            "verification_status": None,
        }

    return {
        "outcome": "api_error",
        "email": email,
        "score": None,
        "verification_status": None,
    }


# =========================================================
# HUNTER EMAIL FINDER FALLBACK
# =========================================================

def hunter_find_email(first_name, last_name, website):
    """
    Used only when Actor #2 did NOT provide an email.
    """

    domain = get_domain_from_website(website)

    if not first_name or not last_name or not domain:
        return {
            "outcome": "invalid_input",
            "email": None,
            "score": None,
            "verification_status": None,
            "domain": domain,
        }

    url = "https://api.hunter.io/v2/email-finder"

    params = {
        "domain": domain,
        "first_name": str(first_name).strip(),
        "last_name": str(last_name).strip(),
        "api_key": HUNTER_API_KEY,
    }

    for attempt in range(3):
        try:
            response = requests.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            print("Hunter Finder connection error:", first_name, last_name, exc)

            if attempt < 2:
                time.sleep(2 ** attempt)
                continue

            return {
                "outcome": "api_error",
                "email": None,
                "score": None,
                "verification_status": None,
                "domain": domain,
            }

        if response.status_code == 200:
            try:
                payload = response.json()
            except Exception:
                return {
                    "outcome": "api_error",
                    "email": None,
                    "score": None,
                    "verification_status": None,
                    "domain": domain,
                }

            data = payload.get("data") or {}
            email = clean_text(data.get("email"))
            score = data.get("score")
            verification = data.get("verification") or {}
            status = clean_text(verification.get("status"))

            if email:
                print(
                    "Hunter Finder FOUND:",
                    first_name,
                    last_name,
                    email,
                    "status:",
                    status,
                    "score:",
                    score
                )
                return {
                    "outcome": "found",
                    "email": email,
                    "score": score,
                    "verification_status": status,
                    "domain": domain,
                }

            print("Hunter Finder NO EMAIL:", first_name, last_name, domain)
            return {
                "outcome": "not_found",
                "email": None,
                "score": None,
                "verification_status": None,
                "domain": domain,
            }

        if response.status_code == 451:
            return {
                "outcome": "suppressed",
                "email": None,
                "score": None,
                "verification_status": None,
                "domain": domain,
            }

        if response.status_code in (403, 429, 500, 502, 503, 504):
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue

            return {
                "outcome": "api_error",
                "email": None,
                "score": None,
                "verification_status": None,
                "domain": domain,
            }

        return {
            "outcome": "invalid_input",
            "email": None,
            "score": None,
            "verification_status": None,
            "domain": domain,
        }

    return {
        "outcome": "api_error",
        "email": None,
        "score": None,
        "verification_status": None,
        "domain": domain,
    }


# =========================================================
# ENRICH ACTOR #2 LEADS WITH HUNTER
# =========================================================

def enrich_leads_with_hunter(df):
    """
    Priority:
      1. Actor-provided email -> Hunter Email Verifier
      2. No actor email + usable personal website -> Hunter Email Finder
      3. No email and no usable Finder inputs -> FB routing
    """

    enriched = df.copy()

    enriched["HunterScore"] = pd.NA
    enriched["HunterStatus"] = pd.NA
    enriched["HunterOutcome"] = pd.NA
    enriched["EmailSource"] = pd.NA

    for index, row in enriched.iterrows():

        actor_email = row.get("Email")
        first_name = row.get("FirstName")
        last_name = row.get("LastName")
        website = row.get("Website")

        has_email = pd.notna(actor_email) and str(actor_email).strip() != ""

        if has_email:
            result = hunter_verify_email(str(actor_email))
            enriched.at[index, "EmailSource"] = "actor"

        else:
            has_finder_inputs = (
                pd.notna(first_name)
                and pd.notna(last_name)
                and pd.notna(website)
                and str(first_name).strip() != ""
                and str(last_name).strip() != ""
                and str(website).strip() != ""
            )

            if not has_finder_inputs:
                enriched.at[index, "HunterOutcome"] = "not_found"
                enriched.at[index, "EmailSource"] = "none"
                continue

            result = hunter_find_email(
                str(first_name),
                str(last_name),
                str(website)
            )
            enriched.at[index, "EmailSource"] = "hunter_finder"

        enriched.at[index, "HunterOutcome"] = result["outcome"]

        if result.get("email"):
            enriched.at[index, "Email"] = str(result["email"]).strip().lower()

        if result.get("score") is not None:
            enriched.at[index, "HunterScore"] = result["score"]

        if result.get("verification_status") is not None:
            enriched.at[index, "HunterStatus"] = result["verification_status"]

    return enriched


# =========================================================
# UPDATE PERSISTENT EMAILLEADS.CSV ON GITHUB
# =========================================================

def update_email_leads_file(
    repo,
    new_email_leads
):
    """
    EmailLeads.csv is cumulative.

    Contains ONLY leads where Hunter found an email
    AND HunterStatus is exactly "valid".

    Existing valid leads are preserved.
    New leads are appended.
    Duplicate emails are removed.
    """

    email_path = (
        f"{GITHUB_FOLDER}/EmailLeads.csv"
    )

    if new_email_leads.empty:

        print(
            "No new email leads this run."
        )

        # If EmailLeads.csv already exists, return its current count.
        try:
            existing_file = repo.get_contents(
                email_path,
                ref=GITHUB_BRANCH
            )

            existing_text = (
                existing_file
                .decoded_content
                .decode("utf-8")
            )

            if not existing_text.strip():
                return 0

            existing_df = pd.read_csv(
                StringIO(existing_text),
                dtype="string"
            )

            return len(existing_df)

        except GithubException as exc:
            if exc.status == 404:
                return 0
            raise

    new_email_leads = (
        new_email_leads.copy()
    )

    email_columns = [
        "Bedrooms",
        "FirstName",
        "LastName",
        "Phone",
        "Address",
        "City",
        "Price",
        "Website",
        "Email",
        "HunterScore",
        "HunterStatus",
        "EmailSource",
    ]

    for column in email_columns:

        if column not in new_email_leads.columns:
            new_email_leads[column] = pd.NA

    new_email_leads = (
        new_email_leads[email_columns]
    )

    # Normalize email values before deduplication.
    new_email_leads["Email"] = (
        new_email_leads["Email"]
        .astype("string")
        .str.strip()
        .str.lower()
    )

    # Safety: EmailLeads.csv should contain ONLY
    # Hunter-verified valid emails and never blank emails.
    new_email_leads["HunterStatus"] = (
        new_email_leads["HunterStatus"]
        .astype("string")
        .str.strip()
        .str.lower()
    )

    new_email_leads = (
        new_email_leads[
            new_email_leads["Email"].notna()
            &
            (new_email_leads["Email"] != "")
            &
            (new_email_leads["HunterStatus"] == "valid")
        ]
        .copy()
    )

    try:

        existing_file = (
            repo.get_contents(
                email_path,
                ref=GITHUB_BRANCH
            )
        )

        existing_text = (
            existing_file
            .decoded_content
            .decode("utf-8")
        )

        if existing_text.strip():

            existing_df = pd.read_csv(
                StringIO(existing_text),
                dtype="string"
            )

        else:

            existing_df = pd.DataFrame(
                columns=email_columns
            )

        for column in email_columns:

            if column not in existing_df.columns:
                existing_df[column] = pd.NA

        existing_df = (
            existing_df[email_columns]
        )

        # Keep ONLY Hunter-verified valid emails from older runs too.
        existing_df["HunterStatus"] = (
            existing_df["HunterStatus"]
            .astype("string")
            .str.strip()
            .str.lower()
        )

        existing_df = (
            existing_df[
                existing_df["HunterStatus"] == "valid"
            ]
            .copy()
        )

        existing_df["Email"] = (
            existing_df["Email"]
            .astype("string")
            .str.strip()
            .str.lower()
        )

        combined = pd.concat(
            [
                existing_df,
                new_email_leads
            ],
            ignore_index=True
        )

        combined = (
            combined[
                combined["Email"].notna()
                &
                (combined["Email"] != "")
            ]
            .copy()
        )

        combined = combined.drop_duplicates(
            subset=["Email"],
            keep="first"
        )

        csv_content = (
            combined
            .to_csv(index=False)
        )

        repo.update_file(
            path=email_path,
            message="Update EmailLeads.csv",
            content=csv_content,
            sha=existing_file.sha,
            branch=GITHUB_BRANCH,
        )

        print(
            "Updated:",
            email_path,
            "total email leads:",
            len(combined)
        )

        return len(combined)

    except GithubException as exc:

        if exc.status == 404:

            new_email_leads = (
                new_email_leads
                .drop_duplicates(
                    subset=["Email"],
                    keep="first"
                )
            )

            csv_content = (
                new_email_leads
                .to_csv(index=False)
            )

            repo.create_file(
                path=email_path,
                message="Create EmailLeads.csv",
                content=csv_content,
                branch=GITHUB_BRANCH,
            )

            print(
                "Created:",
                email_path,
                "email leads:",
                len(new_email_leads)
            )

            return len(new_email_leads)

        raise

# =========================================================
# UPDATE PERSISTENT FBLEADS.CSV ON GITHUB
# =========================================================

def update_fb_leads_file(
    repo,
    new_fb_leads
):
    """
    FBleads.csv is cumulative.

    Existing rows are preserved.
    New no-email leads are appended.
    Duplicates are removed.
    """

    fb_path = (
        f"{GITHUB_FOLDER}/FBleads.csv"
    )


    # No new Facebook leads this run
    if new_fb_leads.empty:

        print(
            "No new FB leads this run."
        )

        return 0


    new_fb_leads = (
        new_fb_leads.copy()
    )


    # Keep useful fields
    fb_columns = [
        "Bedrooms",
        "FirstName",
        "LastName",
        "Phone",
        "Address",
        "City",
        "Price",
        "Website",
        "Email",
        "HunterScore",
        "HunterStatus",
        "HunterOutcome",
        "EmailSource",
    ]


    for column in fb_columns:

        if column not in new_fb_leads.columns:
            new_fb_leads[column] = pd.NA


    new_fb_leads = (
        new_fb_leads[fb_columns]
    )


    try:

        existing_file = (
            repo.get_contents(
                fb_path,
                ref=GITHUB_BRANCH
            )
        )


        existing_text = (
            existing_file
            .decoded_content
            .decode("utf-8")
        )


        if existing_text.strip():

            existing_df = pd.read_csv(
                StringIO(existing_text),
                dtype="string"
            )

        else:

            existing_df = pd.DataFrame(
                columns=fb_columns
            )


        # Make sure older file has
        # all current columns
        for column in fb_columns:

            if column not in existing_df.columns:
                existing_df[column] = pd.NA


        existing_df = (
            existing_df[fb_columns]
        )


        combined = pd.concat(
            [
                existing_df,
                new_fb_leads
            ],
            ignore_index=True
        )


        # Remove duplicate people
        combined = combined.drop_duplicates(
            subset=[
                "FirstName",
                "LastName",
                "Phone",
                "Address",
                "Website"
            ],
            keep="first"
        )


        csv_content = (
            combined
            .to_csv(index=False)
        )


        repo.update_file(
            path=fb_path,
            message="Update FBleads.csv",
            content=csv_content,
            sha=existing_file.sha,
            branch=GITHUB_BRANCH,
        )


        print(
            "Updated:",
            fb_path,
            "total leads:",
            len(combined)
        )


        return len(combined)


    except GithubException as exc:

        # File does not exist yet
        if exc.status == 404:

            csv_content = (
                new_fb_leads
                .drop_duplicates(
                    subset=[
                        "FirstName",
                        "LastName",
                        "Phone",
                        "Address",
                        "Website"
                    ],
                    keep="first"
                )
                .to_csv(index=False)
            )


            repo.create_file(
                path=fb_path,
                message="Create FBleads.csv",
                content=csv_content,
                branch=GITHUB_BRANCH,
            )


            print(
                "Created:",
                fb_path,
                "leads:",
                len(new_fb_leads)
            )


            return len(new_fb_leads)


        raise


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/")
def health():

    return {
        "status": "ok",
        "service": (
            "Apify Realtor.com Actor #2 + Hunter Verification"
        ),
        "webhook": "/webhook"
    }


# =========================================================
# APIFY WEBHOOK
# =========================================================

@app.post("/webhook")
async def apify_webhook(
    request: Request
):

    # -----------------------------------------
    # 1. RECEIVE WEBHOOK
    # -----------------------------------------

    try:
        payload = await request.json()

    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON received"
        )


    if isinstance(payload, dict):

        print(
            "Webhook payload keys:",
            list(payload.keys())
        )


    # -----------------------------------------
    # 2. FIND APIFY DATASET
    # -----------------------------------------

    dataset_id = (

        get_nested(
            payload,
            ["resource", "defaultDatasetId"]
        )

        or get_nested(
            payload,
            ["eventData", "defaultDatasetId"]
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
            ["data", "defaultDatasetId"]
        )

        or (
            payload.get("defaultDatasetId")
            if isinstance(payload, dict)
            else None
        )
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
    # 3. DOWNLOAD DATA
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
    # 4. EXTRACT + CLEAN
    # -----------------------------------------

    rows = extract_records(
        listings
    )


    if not rows:

        raise HTTPException(
            status_code=400,
            detail="No Realtor records found"
        )


    df = clean_dataframe(
        rows
    )


    if df.empty:

        raise HTTPException(
            status_code=400,
            detail=(
                "No usable records after cleaning"
            )
        )


    # =====================================================
    # 5. HUNTER PROCESSING
    #
    # Actor email exists -> Hunter Verifier.
    # No actor email + personal website -> Hunter Finder fallback.
    # =====================================================

    actor_email_count = int(df["Email"].notna().sum())
    actor_no_email_count = len(df) - actor_email_count

    print("Actor-provided emails:", actor_email_count)
    print("Actor rows without email:", actor_no_email_count)
    print("Starting Hunter verification / fallback enrichment...")

    enriched_leads = enrich_leads_with_hunter(df)

    normalized_hunter_status = (
        enriched_leads["HunterStatus"]
        .astype("string")
        .str.strip()
        .str.lower()
        .fillna("")
    )

    normalized_outcome = (
        enriched_leads["HunterOutcome"]
        .astype("string")
        .str.strip()
        .str.lower()
        .fillna("")
    )

    # Only Hunter status == valid enters EmailLeads.csv.
    new_email_leads = (
        enriched_leads[
            enriched_leads["Email"].notna()
            & (normalized_hunter_status == "valid")
        ]
        .copy()
        .reset_index(drop=True)
    )

    # Normal completed non-valid results go to FBleads.csv.
    # Privacy suppression and API failures are deliberately excluded.
    new_fb_leads = (
        enriched_leads[
            (
                normalized_outcome.isin(["not_found"])
            )
            |
            (
                normalized_outcome.isin(["verified", "found"])
                & (normalized_hunter_status != "valid")
            )
        ]
        .copy()
        .reset_index(drop=True)
    )

    hunter_valid = int((normalized_hunter_status == "valid").sum())
    hunter_non_valid = int(
        (
            normalized_outcome.isin(["verified", "found"])
            & (normalized_hunter_status != "valid")
        ).sum()
    )
    hunter_not_found = int((normalized_outcome == "not_found").sum())
    hunter_errors = int((normalized_outcome == "api_error").sum())
    hunter_suppressed = int((normalized_outcome == "suppressed").sum())

    print("Hunter valid:", hunter_valid)
    print("Hunter non-valid:", hunter_non_valid)
    print("Hunter no email:", hunter_not_found)
    print("Hunter API errors:", hunter_errors)
    print("Hunter suppressed:", hunter_suppressed)
    print("New VALID email leads:", len(new_email_leads))
    print("New FB leads:", len(new_fb_leads))


    # =====================================================
    # 8. CREATE MASTER CSV FILES
    # =====================================================

    enriched_csv = enriched_leads.to_csv(index=False)

    no_actor_email_csv = (
        enriched_leads[
            enriched_leads["EmailSource"] != "actor"
        ]
        .to_csv(index=False)
    )

    timestamp = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d_%H-%M-%S-%f"
    )

    enriched_filename = (
        f"{GITHUB_FOLDER}/"
        f"actor2_enriched_leads_{timestamp}.csv"
    )

    no_actor_email_filename = (
        f"{GITHUB_FOLDER}/"
        f"actor2_no_original_email_{timestamp}.csv"
    )


    # =====================================================
    # 9. GITHUB
    # =====================================================

    try:

        repo = github.get_repo(
            GITHUB_REPO
        )


        # -----------------------------------------
        # FULL ACTOR #2 ENRICHED SNAPSHOT
        # -----------------------------------------

        repo.create_file(
            path=enriched_filename,
            message=f"Add Actor #2 enriched leads {timestamp}",
            content=enriched_csv,
            branch=GITHUB_BRANCH,
        )

        print("Uploaded:", enriched_filename)

        # -----------------------------------------
        # ROWS THAT DID NOT START WITH AN ACTOR EMAIL
        # -----------------------------------------

        repo.create_file(
            path=no_actor_email_filename,
            message=f"Add Actor #2 no-original-email leads {timestamp}",
            content=no_actor_email_csv,
            branch=GITHUB_BRANCH,
        )

        print("Uploaded:", no_actor_email_filename)


        # -----------------------------------------
        # EMAILLEADS.CSV
        #
        # Actor email or Finder email that Hunter
        # confirmed with HunterStatus == valid.
        # -----------------------------------------

        total_email_leads = (
            update_email_leads_file(
                repo,
                new_email_leads
            )
        )


        # -----------------------------------------
        # FBLEADS.CSV
        #
        # Normal completed no-email / non-valid results
        # (accept_all, unknown, invalid, webmail,
        # disposable, etc.).
        # -----------------------------------------

        total_fb_leads = (
            update_fb_leads_file(
                repo,
                new_fb_leads
            )
        )


    except GithubException as exc:

        print(
            "GitHub error:",
            exc
        )

        raise HTTPException(
            status_code=500,
            detail=(
                f"GitHub error: {exc.data}"
            )
        )


    # =====================================================
    # 10. DONE
    # =====================================================

    return {
        "success": True,
        "dataset_id": dataset_id,
        "records_downloaded": len(listings),
        "records_after_cleaning": len(df),
        "actor_provided_emails": actor_email_count,
        "actor_rows_without_email": actor_no_email_count,
        "hunter_valid": hunter_valid,
        "hunter_non_valid": hunter_non_valid,
        "hunter_no_email": hunter_not_found,
        "hunter_api_errors": hunter_errors,
        "hunter_suppressed": hunter_suppressed,
        "new_email_leads": len(new_email_leads),
        "total_email_leads": total_email_leads,
        "email_leads_file": f"{GITHUB_FOLDER}/EmailLeads.csv",
        "new_fb_leads": len(new_fb_leads),
        "total_fb_leads": total_fb_leads,
        "fb_leads_file": f"{GITHUB_FOLDER}/FBleads.csv",
        "enriched_file": enriched_filename,
        "no_original_email_file": no_actor_email_filename,
    }
