import os
import re
import time
from io import StringIO
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
BOUNCER_API_KEY = os.getenv("BOUNCER_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")


if not GITHUB_TOKEN:
    raise RuntimeError("Missing Railway variable: GITHUB_TOKEN")

if not GITHUB_REPO:
    raise RuntimeError("Missing Railway variable: GITHUB_REPO")

if not APIFY_TOKEN:
    raise RuntimeError("Missing Railway variable: APIFY_TOKEN")

if not BOUNCER_API_KEY:
    raise RuntimeError("Missing Railway variable: BOUNCER_API_KEY")

if not OPENAI_API_KEY:
    raise RuntimeError("Missing Railway variable: OPENAI_API_KEY")


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
            "PublicRemarks": clean_text(listing.get("description_text")),
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
        "PublicRemarks",
    ]

    df = pd.DataFrame(rows, columns=columns)

    if df.empty:
        return df

    df = df.dropna(how="all")

    for column in ["FirstName", "LastName", "City", "Website", "Email", "PublicRemarks"]:
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
# BOUNCER EMAIL VERIFIER
# =========================================================

def bouncer_verify_email(email):
    """Verify an actor-provided email with Bouncer's real-time API."""
    email = clean_text(email)
    if not email or "@" not in email:
        return {"outcome": "invalid_input", "email": email, "status": None, "reason": None}

    url = "https://api.usebouncer.com/v1/email/verify"
    headers = {"x-api-key": BOUNCER_API_KEY}
    params = {"email": email, "timeout": 30}

    for attempt in range(3):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=35)
        except requests.RequestException as exc:
            print("Bouncer connection error:", email, exc)
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return {"outcome": "api_error", "email": email, "status": None, "reason": None}

        if response.status_code == 200:
            try:
                payload = response.json()
            except Exception:
                return {"outcome": "api_error", "email": email, "status": None, "reason": None}

            if isinstance(payload, list):
                data = payload[0] if payload else {}
            elif isinstance(payload, dict):
                data = payload
            else:
                data = {}

            status = clean_text(data.get("status"))
            reason = clean_text(data.get("reason"))
            verified_email = clean_text(data.get("email")) or email
            print("Bouncer VERIFIED:", verified_email, "status:", status, "reason:", reason)
            return {
                "outcome": "verified",
                "email": verified_email,
                "status": status,
                "reason": reason,
            }

        if response.status_code in (408, 409, 429, 500, 502, 503, 504):
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return {"outcome": "api_error", "email": email, "status": None, "reason": None}

        print("Bouncer verifier error:", response.status_code, email, response.text[:300])
        return {"outcome": "invalid_input", "email": email, "status": None, "reason": None}

    return {"outcome": "api_error", "email": email, "status": None, "reason": None}


# =========================================================
# OPENAI PERSONALIZATION
# =========================================================

PERSONALIZATION_INSTRUCTIONS = """You write ONE short, natural personalization comment for a real estate cold email.

Read the property listing description and choose ONE concrete, specific detail that is genuinely worth commenting on. Then write a casual human reaction that shows you understood why that detail matters.

The goal is NOT to extract or restate a fact. The goal is to sound like a real person looked at the listing, noticed something specific, and had a quick sensible thought about it.

STYLE FORMULA:
specific detail noticed + natural opinion/reaction + practical reason it matters

RULES:
- Mention the actual feature, upgrade, renovation, layout, or property detail so the agent immediately knows what you noticed.
- Prefer details that give you something meaningful to say: major system replacements, renovations, useful layout features, notable outdoor features, quality materials/appliances, or other practical upgrades.
- Explain naturally why the detail is useful, convenient, valuable, or saves the next owner hassle/money/time when that conclusion is reasonable from the listing.
- Sound casual and conversational, like a person talking to another person.
- Use plain everyday language.
- Do NOT sound like marketing copy, a property brochure, a real estate analyst, or an AI.
- Do NOT simply repeat the listing fact.
- Do NOT use vague filler such as "strong selling point", "big-ticket updates have been handled", "great feature", "nice property", "impressive", "stunning", "beautiful", "gorgeous", or similar generic compliments.
- Do NOT force a joke, pun, clever line, or exaggerated enthusiasm.
- Do NOT invent facts or benefits that are not reasonably supported by the listing.
- Do NOT claim something will definitely increase value, reduce bills, prevent repairs, or produce another outcome unless the listing itself supports that claim.
- Keep the comment roughly 10-22 words.
- Do not include the agent name, property address, listing price, greeting, or the rest of the email.
- Do not ask a question.
- Do not put quotation marks around the line.
- If there is no specific detail that supports a natural, useful comment, output exactly NONE.

TONE EXAMPLES:

Listing detail: "HVAC system replaced in 2019"
Good: The HVAC replacement in 2019 was a good move, definitely saves the next owner from one of those headaches

Listing detail: "New roof installed in 2023"
Good: Getting the roof done in 2023 was smart, that's one major job the next owner won't have hanging over them

Listing detail: "Walkout basement with separate entrance"
Good: That separate basement entrance is handy, gives the next owner a lot more flexibility with how they use the space

Listing detail: "Wraparound deck"
Good: That wraparound deck is a nice touch, I can see the next owner getting a lot of use out of it

Listing detail: "Quartz countertops"
Bad: Quartz countertops are a strong selling point
Bad: The quartz countertops are stunning
Bad: Nice to see the big-ticket upgrades have been handled

Output format (strict):
LINE: <comment or NONE>
CONFIDENCE: <high/medium/low>"""

def extract_openai_output_text(payload):
    parts = []
    for item in payload.get("output") or []:
        for content in item.get("content") or []:
            if content.get("type") == "output_text" and content.get("text"):
                parts.append(content["text"])
    return "\n".join(parts).strip()

def parse_personalization_response(text):
    text = (text or "").strip()
    m = re.search(r"LINE:\s*(.+?)\s*\nCONFIDENCE:\s*(high|medium|low)\s*$", text, re.I | re.S)
    if not m:
        return {"detail": "NONE", "confidence": "low", "outcome": "parse_error"}
    detail = m.group(1).strip().strip('"').strip("'")
    confidence = m.group(2).lower()
    if detail.upper() == "NONE":
        return {"detail": "NONE", "confidence": confidence, "outcome": "no_detail"}
    if not 10 <= len(detail.split()) <= 22:
        return {"detail": "NONE", "confidence": confidence, "outcome": "parse_error"}
    return {"detail": detail, "confidence": confidence, "outcome": "found"}

def openai_extract_personalized_detail(public_remarks):
    public_remarks = clean_text(public_remarks)
    if not public_remarks:
        return {"detail": "NONE", "confidence": "low", "outcome": "invalid_input"}

    url = "https://api.openai.com/v1/responses"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": OPENAI_MODEL,
        "instructions": PERSONALIZATION_INSTRUCTIONS,
        "input": "PROPERTY LISTING DESCRIPTION:\n" + public_remarks,
        "max_output_tokens": 100,
    }

    for attempt in range(3):
        try:
            response = requests.post(url, headers=headers, json=body, timeout=45)
        except requests.RequestException as exc:
            print("OpenAI connection error:", exc)
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return {"detail": "NONE", "confidence": "low", "outcome": "api_error"}

        if response.status_code == 200:
            try:
                payload = response.json()
            except Exception:
                return {"detail": "NONE", "confidence": "low", "outcome": "api_error"}
            return parse_personalization_response(extract_openai_output_text(payload))

        if response.status_code in (408, 409, 429, 500, 502, 503, 504):
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue

        print("OpenAI error:", response.status_code, response.text[:300])
        return {"detail": "NONE", "confidence": "low", "outcome": "api_error"}

    return {"detail": "NONE", "confidence": "low", "outcome": "api_error"}

def enrich_leads_with_bouncer(df):
    enriched = df.copy()
    enriched["BouncerStatus"] = pd.NA
    enriched["BouncerReason"] = pd.NA
    enriched["BouncerOutcome"] = pd.NA
    enriched["EmailSource"] = "actor"

    for index, row in enriched.iterrows():
        email = row.get("Email")
        if pd.isna(email) or str(email).strip() == "":
            enriched.at[index, "BouncerOutcome"] = "not_found"
            enriched.at[index, "EmailSource"] = "none"
            continue
        result = bouncer_verify_email(str(email))
        enriched.at[index, "BouncerOutcome"] = result["outcome"]
        if result.get("email"):
            enriched.at[index, "Email"] = str(result["email"]).strip().lower()
        if result.get("status") is not None:
            enriched.at[index, "BouncerStatus"] = result["status"]
        if result.get("reason") is not None:
            enriched.at[index, "BouncerReason"] = result["reason"]
    return enriched

def enrich_personalization(valid_email_leads):
    enriched = valid_email_leads.copy()
    enriched["PersonalizedLine"] = pd.NA
    enriched["PersonalizationConfidence"] = pd.NA
    enriched["PersonalizationOutcome"] = pd.NA
    for index, row in enriched.iterrows():
        result = openai_extract_personalized_detail(row.get("PublicRemarks"))
        enriched.at[index, "PersonalizedLine"] = result["detail"]
        enriched.at[index, "PersonalizationConfidence"] = result["confidence"]
        enriched.at[index, "PersonalizationOutcome"] = result["outcome"]
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

    Contains ONLY actor-provided emails that Bouncer marked deliverable
    and for which OpenAI produced a usable personalization detail.

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
        "PublicRemarks",
        "BouncerStatus",
        "BouncerReason",
        "EmailSource",
        "PersonalizedLine",
        "PersonalizationConfidence",
        "PersonalizationOutcome",
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

    new_email_leads["BouncerStatus"] = (
        new_email_leads["BouncerStatus"]
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
            (new_email_leads["BouncerStatus"] == "deliverable")
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

        existing_df["BouncerStatus"] = (
            existing_df["BouncerStatus"]
            .astype("string")
            .str.strip()
            .str.lower()
        )

        existing_df = (
            existing_df[
                existing_df["BouncerStatus"] == "deliverable"
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
        "PublicRemarks",
        "BouncerStatus",
        "BouncerReason",
        "BouncerOutcome",
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
# UPDATE VALID EMAILS WITHOUT PERSONALIZATION
# =========================================================

def update_no_personalization_file(repo, leads):
    path = f"{GITHUB_FOLDER}/Emails Valid no personlised sentence.csv"
    columns = [
        "Bedrooms", "FirstName", "LastName", "Phone", "Address", "City",
        "Price", "Website", "Email", "PublicRemarks", "BouncerStatus",
        "BouncerReason", "EmailSource", "PersonalizedLine",
        "PersonalizationConfidence", "PersonalizationOutcome",
    ]
    leads = leads.copy()
    for c in columns:
        if c not in leads.columns:
            leads[c] = pd.NA
    leads = leads[columns]
    leads["Email"] = leads["Email"].astype("string").str.strip().str.lower()

    try:
        existing_file = repo.get_contents(path, ref=GITHUB_BRANCH)
        text = existing_file.decoded_content.decode("utf-8")
        existing = pd.read_csv(StringIO(text), dtype="string") if text.strip() else pd.DataFrame(columns=columns)
        for c in columns:
            if c not in existing.columns:
                existing[c] = pd.NA
        combined = pd.concat([existing[columns], leads], ignore_index=True)
        combined = combined[combined["Email"].notna() & (combined["Email"] != "")].drop_duplicates(subset=["Email"], keep="last")
        repo.update_file(path=path, message="Update valid emails without personalization", content=combined.to_csv(index=False), sha=existing_file.sha, branch=GITHUB_BRANCH)
        return len(combined)
    except GithubException as exc:
        if exc.status == 404:
            initial = leads[leads["Email"].notna() & (leads["Email"] != "")].drop_duplicates(subset=["Email"], keep="last")
            repo.create_file(path=path, message="Create valid emails without personalization", content=initial.to_csv(index=False), branch=GITHUB_BRANCH)
            return len(initial)
        raise



# =========================================================
# UPDATE ONE PERSISTENT FILE PER CATEGORY
# =========================================================

def update_category_file(repo, path, new_rows, message_label):
    """Append new rows into one stable GitHub CSV and remove duplicates.

    This prevents timestamped file spam. Emails are the preferred dedupe key;
    rows without an email are deduplicated by agent/listing identity fields.
    """
    new_rows = new_rows.copy()

    try:
        existing_file = repo.get_contents(path, ref=GITHUB_BRANCH)
        text = existing_file.decoded_content.decode("utf-8")
        existing = (
            pd.read_csv(StringIO(text), dtype="string")
            if text.strip()
            else pd.DataFrame(columns=new_rows.columns)
        )
    except GithubException as exc:
        if exc.status != 404:
            raise
        existing_file = None
        existing = pd.DataFrame(columns=new_rows.columns)

    all_columns = list(dict.fromkeys(list(existing.columns) + list(new_rows.columns)))
    for column in all_columns:
        if column not in existing.columns:
            existing[column] = pd.NA
        if column not in new_rows.columns:
            new_rows[column] = pd.NA

    combined = pd.concat(
        [existing[all_columns], new_rows[all_columns]],
        ignore_index=True,
    )

    if "Email" in combined.columns:
        combined["Email"] = (
            combined["Email"].astype("string").str.strip().str.lower()
        )

        with_email = combined[
            combined["Email"].notna() & (combined["Email"] != "")
        ].drop_duplicates(subset=["Email"], keep="last")

        without_email = combined[
            combined["Email"].isna() | (combined["Email"] == "")
        ].copy()

        identity_columns = [
            c for c in ["FirstName", "LastName", "Phone", "Address", "Website"]
            if c in without_email.columns
        ]
        if identity_columns:
            without_email = without_email.drop_duplicates(
                subset=identity_columns, keep="last"
            )
        else:
            without_email = without_email.drop_duplicates(keep="last")

        combined = pd.concat([with_email, without_email], ignore_index=True)
    else:
        combined = combined.drop_duplicates(keep="last")

    csv_content = combined.to_csv(index=False)

    if existing_file is None:
        repo.create_file(
            path=path,
            message=f"Create {message_label}",
            content=csv_content,
            branch=GITHUB_BRANCH,
        )
    else:
        repo.update_file(
            path=path,
            message=f"Update {message_label}",
            content=csv_content,
            sha=existing_file.sha,
            branch=GITHUB_BRANCH,
        )

    print("Updated category file:", path, "total rows:", len(combined))
    return len(combined)


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/")
def health():

    return {
        "status": "ok",
        "service": (
            "Apify Realtor.com Actor #2 + Bouncer + OpenAI"
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
    # 5. BOUNCER VERIFICATION + OPENAI PERSONALIZATION
    # =====================================================

    actor_email_count = int(df["Email"].notna().sum())
    actor_no_email_count = len(df) - actor_email_count

    print("Actor-provided emails:", actor_email_count)
    print("Actor rows without email:", actor_no_email_count)
    print("Starting Bouncer verification...")

    enriched_leads = enrich_leads_with_bouncer(df)

    normalized_bouncer_status = (
        enriched_leads["BouncerStatus"]
        .astype("string").str.strip().str.lower().fillna("")
    )
    normalized_outcome = (
        enriched_leads["BouncerOutcome"]
        .astype("string").str.strip().str.lower().fillna("")
    )

    bouncer_valid_leads = (
        enriched_leads[
            enriched_leads["Email"].notna()
            & (normalized_bouncer_status == "deliverable")
        ]
        .copy().reset_index(drop=True)
    )

    personalized_valid_leads = enrich_personalization(bouncer_valid_leads)
    normalized_personalization = (
        personalized_valid_leads["PersonalizationOutcome"]
        .astype("string").str.strip().str.lower().fillna("")
    )

    new_email_leads = (
        personalized_valid_leads[normalized_personalization == "found"]
        .copy().reset_index(drop=True)
    )
    no_personalization_leads = (
        personalized_valid_leads[normalized_personalization != "found"]
        .copy().reset_index(drop=True)
    )

    new_fb_leads = (
        enriched_leads[
            (normalized_outcome == "not_found")
            | ((normalized_outcome == "verified") & (normalized_bouncer_status != "deliverable"))
        ]
        .copy().reset_index(drop=True)
    )

    bouncer_deliverable = int((normalized_bouncer_status == "deliverable").sum())
    bouncer_non_deliverable = int(((normalized_outcome == "verified") & (normalized_bouncer_status != "deliverable")).sum())
    bouncer_not_found = int((normalized_outcome == "not_found").sum())
    bouncer_errors = int((normalized_outcome == "api_error").sum())

    print("Bouncer deliverable:", bouncer_deliverable)
    print("Bouncer non-deliverable:", bouncer_non_deliverable)
    print("No actor email:", bouncer_not_found)
    print("Bouncer API errors:", bouncer_errors)
    print("New personalized email leads:", len(new_email_leads))
    print("Valid emails without personalization:", len(no_personalization_leads))
    print("New FB leads:", len(new_fb_leads))

    # =====================================================
    # 8. CREATE MASTER CSV FILES
    # =====================================================

    # Keep ONE persistent file per lead category.
    # These files are updated cumulatively instead of creating timestamped copies.
    enriched_filename = f"{GITHUB_FOLDER}/actor2_enriched_leads.csv"
    no_actor_email_filename = f"{GITHUB_FOLDER}/actor2_no_original_email.csv"

    no_actor_email_leads = (
        enriched_leads[
            enriched_leads["EmailSource"] != "actor"
        ]
        .copy()
        .reset_index(drop=True)
    )


    # =====================================================
    # 9. GITHUB
    # =====================================================

    try:

        repo = github.get_repo(
            GITHUB_REPO
        )


        # -----------------------------------------
        # ONE PERSISTENT FILE PER CATEGORY
        # -----------------------------------------

        total_enriched_master = update_category_file(
            repo,
            enriched_filename,
            enriched_leads,
            "Actor #2 enriched leads",
        )

        total_no_actor_email_master = update_category_file(
            repo,
            no_actor_email_filename,
            no_actor_email_leads,
            "Actor #2 no-original-email leads",
        )


        # -----------------------------------------
        # EMAILLEADS.CSV
        #
        # Actor-provided email that Bouncer marked deliverable
        # and OpenAI successfully personalized.
        # -----------------------------------------

        total_email_leads = (
            update_email_leads_file(
                repo,
                new_email_leads
            )
        )

        total_no_personalization_leads = update_no_personalization_file(
            repo, no_personalization_leads
        )


        # -----------------------------------------
        # FBLEADS.CSV
        #
        # No actor email or Bouncer-completed non-deliverable results.
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
        "bouncer_deliverable": bouncer_deliverable,
        "bouncer_non_deliverable": bouncer_non_deliverable,
        "bouncer_api_errors": bouncer_errors,
        "new_email_leads": len(new_email_leads),
        "total_email_leads": total_email_leads,
        "email_leads_file": f"{GITHUB_FOLDER}/EmailLeads.csv",
        "valid_emails_without_personalization": len(no_personalization_leads),
        "total_valid_emails_without_personalization": total_no_personalization_leads,
        "no_personalization_file": f"{GITHUB_FOLDER}/Emails Valid no personlised sentence.csv",
        "new_fb_leads": len(new_fb_leads),
        "total_fb_leads": total_fb_leads,
        "fb_leads_file": f"{GITHUB_FOLDER}/FBleads.csv",
        "enriched_file": enriched_filename,
        "total_enriched_master": total_enriched_master,
        "no_original_email_file": no_actor_email_filename,
        "total_no_original_email_master": total_no_actor_email_master,
    }
