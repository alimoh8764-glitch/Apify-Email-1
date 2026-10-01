import os
import re
import csv
import io
import threading
import time
from datetime import datetime, timezone
from typing import Any

import requests
from flask import Flask, jsonify, request
from github import Github
from openai import OpenAI


# ============================================================
# APP
# ============================================================

app = Flask(__name__)


# ============================================================
# CONFIG
# ============================================================

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get(
    "GITHUB_REPO",
    "alimoh8764-glitch/Apify-Email-1",
)
GITHUB_BRANCH = os.environ.get(
    "GITHUB_BRANCH",
    "main",
)

OPENAI_MODEL = os.environ.get(
    "OPENAI_MODEL",
    "gpt-5.6-luna",
)

BATCH_SIZE = 200

openai_client = (
    OpenAI(api_key=OPENAI_API_KEY)
    if OPENAI_API_KEY
    else None
)

github_client = (
    Github(GITHUB_TOKEN)
    if GITHUB_TOKEN
    else None
)

# In-memory status.
# This resets if Railway restarts.
jobs = {}


# ============================================================
# GENERAL HELPERS
# ============================================================

def clean_value(value: Any) -> str:
    if value is None:
        return ""

    value = str(value).strip()

    if value.lower() in {
        "none",
        "null",
        "nan",
        "n/a",
        "na",
    }:
        return ""

    return value


def valid_email(value: Any) -> bool:
    email = clean_value(value)

    if not email:
        return False

    return bool(
        re.match(
            r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
            email,
        )
    )


def get_first_name(full_name: Any) -> str:
    name = clean_value(full_name)

    if not name:
        return ""

    name = re.sub(
        r"^(mr|mrs|ms|miss|dr)\.?\s+",
        "",
        name,
        flags=re.IGNORECASE,
    )

    name = name.strip(" ,.-")

    if not name:
        return ""

    return name.split()[0]


def get_list(value: Any) -> list:
    if isinstance(value, list):
        return value

    return []


def get_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value

    return {}


# ============================================================
# CONTACT EXTRACTION
# ============================================================

def get_contact(row: dict) -> dict:
    """
    Contact priority:

    1. Agent with both name and valid email
    2. Advertiser with both name and valid email
    3. Agent email matched to advertiser email
    4. Agent name only
    5. Advertiser name only

    Never intentionally combine unrelated names/emails.
    """

    agents = get_list(row.get("agents"))
    advertisers = get_list(row.get("advertisers"))

    # --------------------------------------------------------
    # 1. COMPLETE AGENT
    # --------------------------------------------------------

    for agent in agents:
        if not isinstance(agent, dict):
            continue

        name = clean_value(agent.get("name"))
        email = clean_value(agent.get("email"))

        if name and valid_email(email):
            return {
                "name": name,
                "first_name": get_first_name(name),
                "email": email,
                "source": "agent",
            }

    # --------------------------------------------------------
    # 2. COMPLETE ADVERTISER
    # --------------------------------------------------------

    for advertiser in advertisers:
        if not isinstance(advertiser, dict):
            continue

        name = clean_value(advertiser.get("name"))
        email = clean_value(advertiser.get("email"))

        if name and valid_email(email):
            return {
                "name": name,
                "first_name": get_first_name(name),
                "email": email,
                "source": "advertiser",
            }

    # --------------------------------------------------------
    # 3. MATCH AGENT EMAIL TO ADVERTISER EMAIL
    # --------------------------------------------------------

    for agent in agents:
        if not isinstance(agent, dict):
            continue

        agent_email = clean_value(
            agent.get("email")
        )

        if not valid_email(agent_email):
            continue

        for advertiser in advertisers:
            if not isinstance(advertiser, dict):
                continue

            advertiser_name = clean_value(
                advertiser.get("name")
            )

            advertiser_email = clean_value(
                advertiser.get("email")
            )

            if (
                advertiser_name
                and valid_email(advertiser_email)
                and agent_email.lower()
                == advertiser_email.lower()
            ):
                return {
                    "name": advertiser_name,
                    "first_name": get_first_name(
                        advertiser_name
                    ),
                    "email": agent_email,
                    "source": "agent+advertiser_match",
                }

    # --------------------------------------------------------
    # 4. AGENT NAME ONLY
    # --------------------------------------------------------

    for agent in agents:
        if not isinstance(agent, dict):
            continue

        name = clean_value(
            agent.get("name")
        )

        if name:
            return {
                "name": name,
                "first_name": get_first_name(name),
                "email": "",
                "source": "agent_name_only",
            }

    # --------------------------------------------------------
    # 5. ADVERTISER NAME ONLY
    # --------------------------------------------------------

    for advertiser in advertisers:
        if not isinstance(advertiser, dict):
            continue

        name = clean_value(
            advertiser.get("name")
        )

        if name:
            return {
                "name": name,
                "first_name": get_first_name(name),
                "email": "",
                "source": "advertiser_name_only",
            }

    return {
        "name": "",
        "first_name": "",
        "email": "",
        "source": "",
    }


# ============================================================
# PROPERTY EXTRACTION
# ============================================================

def get_address(row: dict) -> str:
    address_data = get_dict(
        row.get("address")
    )

    return clean_value(
        address_data.get("street")
    )


def get_city(row: dict) -> str:
    address_data = get_dict(
        row.get("address")
    )

    return clean_value(
        address_data.get("locality")
    )


def get_description(row: dict) -> str:
    return clean_value(
        row.get("description")
    )


def format_price(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, (int, float)):
        try:
            return f"${float(value):,.0f}"
        except Exception:
            pass

    raw = clean_value(value)

    if not raw:
        return ""

    numeric = re.sub(
        r"[^\d.]",
        "",
        raw,
    )

    if not numeric:
        return raw

    try:
        return f"${float(numeric):,.0f}"
    except Exception:
        return raw


# ============================================================
# OPENAI PERSONALIZATION
# ============================================================

AI_INSTRUCTIONS = """
You write personalized first lines for cold emails to real estate agents.

You are given:
1. The property's street address
2. The listing price
3. The property's public listing description

Pick ONE genuinely interesting or unusual physical feature from the description.

Write exactly 2 short, casual sentences.

The target style is:

That wired shed on 6214 Elkington Ln caught my eye. Solid bonus for a place at that price point.

RULES:
- Sentence 1 must mention ONE specific property feature and the actual street address.
- Sentence 2 must be a short, casual reaction to that feature.
- When natural, relate the feature to the property's price point using wording like "at that price point".
- Use the actual street address provided.
- Do not use {{address}}.
- Do not state the numerical listing price in the output.
- Keep the entire response under 35 words.
- Sound like a real person who briefly looked at the listing.
- Use simple, direct language.
- Do not sound like a realtor, marketer, or copywriter.
- Do not summarize the property.
- Do not mention more than ONE feature.
- Do not invent or assume anything not stated in the description.
- Do not use exclamation marks.
- Return ONLY the two sentences.

GOOD EXAMPLES:

That wired shed on 6214 Elkington Ln caught my eye. Solid bonus for a place at that price point.

That backyard ADU at 4409 Randall Rd caught my eye. Solid bonus for a place at that price point.

That raised bar at 8 Hummingbird Ln caught my eye. Cool feature for a place at that price point.

That lighthouse guest house at 100 Nautical Ln caught my eye. Definitely not something you see every day at that price point.

That greenhouse at 4409 Randall Rd caught my eye. Nice bonus for a place at that price point.

BAD EXAMPLES:

The backyard ADU has its own bath, kitchen hookups, and newly carpeted upstairs, making it an unusually flexible setup for guests.

The oversized private office and coffered ceiling make a memorable first impression.

The included washer and dryer are a thoughtful move-in-ready touch.

The property offers a distinctive country feel.

AVOID PHRASES LIKE:
- thoughtful touch
- memorable first impression
- distinctive country feel
- especially convenient
- thoughtfully designed
- versatile layout
- ideal for
- perfect for
- great opportunity
- truly unique
- standout feature
- impressive
- stunning

Prefer unusual concrete features such as sheds, workshops, guest houses, ADUs, greenhouses, creeks, ponds, bars, unusual architecture, hobby spaces, outdoor kitchens, saunas, or other memorable property details.

If there is no genuinely interesting specific feature, return exactly:
SKIP
""".strip()


def create_personalized_line(
    description: str,
    address: str,
    price: str,
) -> str:

    description = clean_value(description)
    address = clean_value(address)
    price = clean_value(price)

    if not description or not address:
        return ""

    if not openai_client:
        print(
            "OPENAI_API_KEY missing; "
            "skipping personalization.",
            flush=True,
        )
        return ""

    description = description[:6000]

    prompt = f"""
PROPERTY ADDRESS:
{address}

LIST PRICE:
{price}

PROPERTY LISTING DESCRIPTION:
{description}

Write the personalized opening line now.
""".strip()

    max_attempts = 3

    for attempt in range(1, max_attempts + 1):
        try:
            response = openai_client.responses.create(
                model=OPENAI_MODEL,
                reasoning={"effort": "low"},
                instructions=AI_INSTRUCTIONS,
                input=prompt,
                max_output_tokens=100,
            )

            line = clean_value(
                response.output_text
            ).strip("\"'")

            if not line:
                return ""

            if line.upper() == "SKIP":
                return ""

            if address.lower() not in line.lower():
                print(
                    "AI output rejected - actual address missing:",
                    line,
                    flush=True,
                )
                return ""

            if len(line.split()) > 35:
                print(
                    "AI output rejected - over 35 words:",
                    line,
                    flush=True,
                )
                return ""

            return line

        except Exception as exc:
            print(
                f"OpenAI attempt {attempt} failed: {exc}",
                flush=True,
            )

            if attempt < max_attempts:
                time.sleep(2 * attempt)

    return ""


# ============================================================
# PROCESS ONE PROPERTY
# ============================================================

def process_property(row: dict) -> dict:
    contact = get_contact(row)

    address = get_address(row)
    city = get_city(row)

    price = format_price(
        row.get("listPrice")
    )

    description = get_description(row)

    personalized_line = (
        create_personalized_line(
            description,
            address,
            price,
        )
    )

    return {
        "first_name": contact.get(
            "first_name",
            "",
        ),
        "email": contact.get(
            "email",
            "",
        ),
        "address": address,
        "city": city,
        "price": price,
        "property_description": description,
        "personalized_line": personalized_line,
        "contact_source": contact.get(
            "source",
            "",
        ),
    }


# ============================================================
# APIFY DATASET DOWNLOAD
# ============================================================

def download_apify_dataset(
    dataset_id: str,
) -> list:

    if not APIFY_API_TOKEN:
        raise RuntimeError(
            "APIFY_API_TOKEN is missing."
        )

    if not dataset_id:
        raise RuntimeError(
            "Dataset ID is missing."
        )

    print(
        f"Downloading dataset {dataset_id}...",
        flush=True,
    )

    url = (
        "https://api.apify.com/v2/datasets/"
        f"{dataset_id}/items"
    )

    headers = {
        "Authorization":
            f"Bearer {APIFY_API_TOKEN}"
    }

    params = {
        "clean": "true",
        "format": "json",
    }

    response = requests.get(
        url,
        headers=headers,
        params=params,
        timeout=120,
    )

    response.raise_for_status()

    data = response.json()

    if not isinstance(data, list):
        raise RuntimeError(
            "Apify dataset response "
            "was not a list."
        )

    print(
        f"Downloaded {len(data)} records.",
        flush=True,
    )

    return data


# ============================================================
# BATCHING
# ============================================================

def split_batches(
    items: list,
    size: int = BATCH_SIZE,
):
    for start in range(
        0,
        len(items),
        size,
    ):
        yield items[
            start:start + size
        ]


# ============================================================
# CSV
# ============================================================

CSV_FIELDS = [
    "first_name",
    "email",
    "address",
    "city",
    "price",
    "property_description",
    "personalized_line",
    "contact_source",
]


def build_csv(results: list) -> str:
    output = io.StringIO(
        newline=""
    )

    writer = csv.DictWriter(
        output,
        fieldnames=CSV_FIELDS,
        extrasaction="ignore",
    )

    writer.writeheader()

    for result in results:
        writer.writerow(result)

    return output.getvalue()


# ============================================================
# GITHUB UPLOAD
# ============================================================

def upload_csv_to_github(
    results: list,
    run_id: str,
    event_type: str,
) -> str:

    if not GITHUB_TOKEN:
        raise RuntimeError(
            "GITHUB_TOKEN is missing."
        )

    if not GITHUB_REPO:
        raise RuntimeError(
            "GITHUB_REPO is missing."
        )

    if not github_client:
        raise RuntimeError(
            "GitHub client is not configured."
        )

    print(
        "Creating CSV and uploading "
        "to GitHub...",
        flush=True,
    )

    repo = github_client.get_repo(
        GITHUB_REPO
    )

    csv_content = build_csv(
        results
    )

    timestamp = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d_%H-%M-%S"
    )

    status_name = (
        event_type
        .replace(
            "ACTOR.RUN.",
            "",
        )
        .lower()
    )

    short_run_id = (
        run_id[:10]
        if run_id
        else "unknown"
    )

    github_path = (
        "output/"
        f"properties_{timestamp}_"
        f"{status_name}_"
        f"{short_run_id}.csv"
    )

    commit_message = (
        "Add processed property data "
        f"for Apify run {run_id}"
    )

    repo.create_file(
        github_path,
        commit_message,
        csv_content,
        branch=GITHUB_BRANCH,
    )

    print(
        f"GitHub file created: "
        f"{github_path}",
        flush=True,
    )

    return github_path


# ============================================================
# PROCESS APIFY RUN
# ============================================================

def process_apify_run(
    run_id: str,
    dataset_id: str,
    event_type: str,
):

    try:
        jobs[run_id] = {
            "status": "downloading",
            "event_type": event_type,
            "dataset_id": dataset_id,
            "processed": 0,
            "total": 0,
            "emails": 0,
            "personalized": 0,
            "github_file": None,
            "error": None,
        }

        items = download_apify_dataset(
            dataset_id
        )

        total = len(items)

        jobs[run_id]["total"] = total
        jobs[run_id]["status"] = (
            "processing"
        )

        print(
            f"Processing {total} records "
            f"in batches of {BATCH_SIZE}.",
            flush=True,
        )

        results = []

        for batch_number, batch in enumerate(
            split_batches(
                items,
                BATCH_SIZE,
            ),
            start=1,
        ):
            print(
                f"Starting batch "
                f"{batch_number} "
                f"({len(batch)} records)",
                flush=True,
            )

            for row_number, row in enumerate(
                batch,
                start=1,
            ):
                try:
                    processed = (
                        process_property(row)
                    )

                except Exception as exc:
                    print(
                        "Property processing "
                        f"failed: {exc}",
                        flush=True,
                    )

                    processed = {
                        "first_name": "",
                        "email": "",
                        "address":
                            get_address(row),
                        "city":
                            get_city(row),
                        "price":
                            format_price(
                                row.get(
                                    "listPrice"
                                )
                            ),
                        "property_description":
                            get_description(row),
                        "personalized_line": "",
                        "contact_source":
                            "processing_error",
                    }

                results.append(
                    processed
                )

                jobs[run_id][
                    "processed"
                ] = len(results)

                # Debug first few records.
                if len(results) <= 3:
                    print(
                        "DEBUG PROCESSED ROW "
                        f"{len(results)}: "
                        f"first_name="
                        f"{processed.get('first_name')!r}, "
                        f"email="
                        f"{processed.get('email')!r}, "
                        f"address="
                        f"{processed.get('address')!r}, "
                        f"city="
                        f"{processed.get('city')!r}, "
                        f"price="
                        f"{processed.get('price')!r}, "
                        f"description_chars="
                        f"{len(processed.get('property_description', ''))}, "
                        f"personalized="
                        f"{bool(processed.get('personalized_line'))}",
                        flush=True,
                    )

            print(
                f"Finished batch "
                f"{batch_number}. "
                f"{len(results)}/{total} "
                "processed.",
                flush=True,
            )

        email_count = sum(
            1
            for row in results
            if row.get("email")
        )

        personalized_count = sum(
            1
            for row in results
            if row.get(
                "personalized_line"
            )
        )

        jobs[run_id][
            "emails"
        ] = email_count

        jobs[run_id][
            "personalized"
        ] = personalized_count

        jobs[run_id]["status"] = (
            "uploading"
        )

        github_path = (
            upload_csv_to_github(
                results,
                run_id,
                event_type,
            )
        )

        jobs[run_id]["status"] = (
            "completed"
        )

        jobs[run_id][
            "github_file"
        ] = github_path

        jobs[run_id][
            "results"
        ] = results

        print(
            f"FINAL COMPLETE: "
            f"{run_id} | "
            f"{len(results)} records | "
            f"{email_count} emails | "
            f"{personalized_count} personalized",
            flush=True,
        )

    except Exception as exc:
        print(
            f"Run {run_id} FAILED: "
            f"{exc}",
            flush=True,
        )

        if run_id not in jobs:
            jobs[run_id] = {}

        jobs[run_id]["status"] = (
            "failed"
        )

        jobs[run_id]["error"] = str(
            exc
        )


# ============================================================
# HEALTH
# ============================================================

@app.route(
    "/",
    methods=["GET"],
)
def health():
    return jsonify({
        "ok": True,
        "version":
            "FINAL-NESTED-REALTOR-V2",
        "service":
            "email-personalization-ai",
        "batch_size":
            BATCH_SIZE,
        "openai_model":
            OPENAI_MODEL,
        "github_repo":
            GITHUB_REPO,
        "github_branch":
            GITHUB_BRANCH,
    })


# ============================================================
# APIFY WEBHOOK
# ============================================================

@app.route(
    "/apify-webhook",
    methods=["POST"],
)
def apify_webhook():

    payload = request.get_json(
        silent=True
    ) or {}

    event_type = clean_value(
        payload.get("eventType")
    )

    resource = get_dict(
        payload.get("resource")
    )

    run_id = clean_value(
        resource.get("id")
    )

    dataset_id = clean_value(
        resource.get(
            "defaultDatasetId"
        )
    )

    print(
        "FINAL-V2 webhook received: "
        f"{event_type}",
        flush=True,
    )

    allowed_events = {
        "ACTOR.RUN.SUCCEEDED",
        "ACTOR.RUN.ABORTED",
    }

    if event_type not in allowed_events:
        print(
            "Ignoring unsupported event: "
            f"{event_type}",
            flush=True,
        )

        return jsonify({
            "ok": True,
            "ignored": True,
            "event_type": event_type,
        }), 200

    if not run_id:
        return jsonify({
            "ok": False,
            "error":
                "resource.id missing",
        }), 400

    if not dataset_id:
        return jsonify({
            "ok": False,
            "error":
                "resource.defaultDatasetId "
                "missing",
        }), 400

    existing = jobs.get(
        run_id
    )

    if (
        existing
        and existing.get("status")
        in {
            "downloading",
            "processing",
            "uploading",
            "completed",
        }
    ):
        return jsonify({
            "ok": True,
            "duplicate": True,
            "run_id": run_id,
            "status":
                existing.get("status"),
        }), 200

    print(
        f"FINAL-V2 accepted "
        f"{event_type} "
        f"for run {run_id}. "
        f"Dataset: {dataset_id}",
        flush=True,
    )

    worker = threading.Thread(
        target=process_apify_run,
        args=(
            run_id,
            dataset_id,
            event_type,
        ),
        daemon=True,
    )

    worker.start()

    return jsonify({
        "ok": True,
        "accepted": True,
        "version":
            "FINAL-NESTED-REALTOR-V2",
        "run_id": run_id,
        "dataset_id": dataset_id,
        "event_type": event_type,
    }), 200


# ============================================================
# JOB STATUS
# ============================================================

@app.route(
    "/jobs/<run_id>",
    methods=["GET"],
)
def job_status(run_id):

    job = jobs.get(
        run_id
    )

    if not job:
        return jsonify({
            "ok": False,
            "error":
                "Job not found",
        }), 404

    safe_job = {
        key: value
        for key, value
        in job.items()
        if key != "results"
    }

    return jsonify({
        "ok": True,
        "run_id": run_id,
        **safe_job,
    })


# ============================================================
# JOB RESULTS
# ============================================================

@app.route(
    "/jobs/<run_id>/results",
    methods=["GET"],
)
def job_results(run_id):

    job = jobs.get(
        run_id
    )

    if not job:
        return jsonify({
            "ok": False,
            "error":
                "Job not found",
        }), 404

    if (
        job.get("status")
        != "completed"
    ):
        return jsonify({
            "ok": False,
            "status":
                job.get("status"),
            "error":
                "Job has not completed.",
        }), 409

    results = job.get(
        "results",
        [],
    )

    return jsonify({
        "ok": True,
        "run_id": run_id,
        "github_file":
            job.get("github_file"),
        "count":
            len(results),
        "emails":
            job.get("emails", 0),
        "personalized":
            job.get(
                "personalized",
                0,
            ),
        "results":
            results,
    })


# ============================================================
# TEST PERSONALIZATION
# ============================================================

@app.route(
    "/test-personalization",
    methods=["POST"],
)
def test_personalization():

    payload = request.get_json(
        silent=True
    ) or {}

    description = clean_value(
        payload.get("description")
    )
    address = clean_value(
        payload.get("address")
    )
    price = clean_value(
        payload.get("price")
    )

    if not description or not address:
        return jsonify({
            "ok": False,
            "error":
                "description and address are required",
        }), 400

    line = create_personalized_line(
        description,
        address,
        price,
    )

    return jsonify({
        "ok": True,
        "personalized_line":
            line,
    })


# ============================================================
# LOCAL RUN
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            8080,
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
