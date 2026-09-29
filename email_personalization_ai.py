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
# CONFIG
# ============================================================

app = Flask(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

OPENAI_MODEL = os.environ.get(
    "OPENAI_MODEL",
    "gpt-5.6-luna",
)

BATCH_SIZE = 200


if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is missing.")

if not APIFY_API_TOKEN:
    raise RuntimeError("APIFY_API_TOKEN is missing.")

if not GITHUB_TOKEN:
    raise RuntimeError("GITHUB_TOKEN is missing.")

if not GITHUB_REPO:
    raise RuntimeError("GITHUB_REPO is missing.")


client = OpenAI(api_key=OPENAI_API_KEY)

github_client = Github(GITHUB_TOKEN)


# ============================================================
# APIFY COLUMNS
# ============================================================

AGENT_NAME = "agents/0/agent_name"
AGENT_EMAIL = "agents/0/agent_email"

ADVERTISER_NAME = "advertisers/0/name"
ADVERTISER_EMAIL = "advertisers/0/email"

PROPERTY_ADDRESS = "address/street"
PROPERTY_CITY = "address/locality"
PROPERTY_PRICE = "listPrice"
PROPERTY_DESCRIPTION = "text"


# ============================================================
# JOB STATUS
# ============================================================

jobs = {}


# ============================================================
# BASIC CLEANING
# ============================================================

def clean_value(value: Any) -> str:

    if value is None:
        return ""

    value = str(value).strip()

    if value.lower() in {
        "",
        "nan",
        "none",
        "null",
        "n/a",
        "na",
    }:
        return ""

    return re.sub(r"\s+", " ", value)


def valid_email(email: str) -> bool:

    email = clean_value(email)

    if not email:
        return False

    return bool(
        re.match(
            r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
            email,
        )
    )


# ============================================================
# FIRST NAME
# ============================================================

def get_first_name(full_name: str) -> str:

    full_name = clean_value(full_name)

    if not full_name:
        return ""

    full_name = re.sub(
        r"^(mr\.?|mrs\.?|ms\.?|miss|dr\.?)\s+",
        "",
        full_name,
        flags=re.IGNORECASE,
    )

    first_name = full_name.split()[0]

    first_name = re.sub(
        r"[^A-Za-zÀ-ÖØ-öø-ÿ'\-]",
        "",
        first_name,
    )

    if not first_name:
        return ""

    return first_name.title()


# ============================================================
# CONTACT MATCHING
# ============================================================

def get_contact(row: dict) -> dict:
    """
    Never mix agent and advertiser identities.

    Priority:

    1. Agent name + agent email
    2. Advertiser name + advertiser email
    3. Agent name only
    4. Advertiser name only
    """

    agent_name = clean_value(
        row.get(AGENT_NAME)
    )

    agent_email = clean_value(
        row.get(AGENT_EMAIL)
    )

    advertiser_name = clean_value(
        row.get(ADVERTISER_NAME)
    )

    advertiser_email = clean_value(
        row.get(ADVERTISER_EMAIL)
    )

    if agent_name and valid_email(agent_email):

        return {
            "first_name": get_first_name(agent_name),
            "email": agent_email.lower(),
            "contact_source": "agent",
        }

    if (
        advertiser_name
        and valid_email(advertiser_email)
    ):

        return {
            "first_name":
                get_first_name(advertiser_name),

            "email":
                advertiser_email.lower(),

            "contact_source":
                "advertiser",
        }

    if agent_name:

        return {
            "first_name":
                get_first_name(agent_name),

            "email":
                "",

            "contact_source":
                "agent_name_only",
        }

    if advertiser_name:

        return {
            "first_name":
                get_first_name(advertiser_name),

            "email":
                "",

            "contact_source":
                "advertiser_name_only",
        }

    return {
        "first_name": "",
        "email": "",
        "contact_source": "",
    }


# ============================================================
# ADDRESS
# ============================================================

def clean_address(address: str) -> str:

    address = clean_value(address)

    if not address:
        return ""

    if "," in address:
        address = address.split(",")[0].strip()

    return address


# ============================================================
# PRICE
# ============================================================

def format_price(price: Any) -> str:

    price = clean_value(price)

    if not price:
        return ""

    numeric = re.sub(
        r"[^0-9.]",
        "",
        price,
    )

    if not numeric:
        return ""

    try:

        number = float(numeric)

        return f"${number:,.0f}"

    except (ValueError, TypeError):

        return ""


# ============================================================
# AI PROMPT
# ============================================================

AI_INSTRUCTIONS = """
You write short personalized opening lines for real-estate
agent outreach.

You receive the public property description for ONE listing.

Find ONE genuinely distinctive property feature that proves
someone actually read the listing.

Good examples include:

- wired shed
- detached workshop
- barn apartment
- finished room above a garage
- unusual loft
- private dock
- boat lift
- four-season garden
- goldfish pond
- saltwater pool
- rooftop deck
- wraparound porch
- screened porch
- outdoor kitchen
- original fireplace
- guest house
- wine cellar
- putting green
- unusual architectural feature
- distinctive view
- unusual outdoor feature

Avoid generic observations such as:

- beautiful home
- spacious property
- great location
- nice kitchen
- open floor plan
- updated home
- large bedrooms
- lots of natural light
- desirable neighborhood

ADDRESS RULE:

You MUST literally use:

{{address}}

Do NOT write the actual address.

The outreach software will replace {{address}} later.

TONE:

Warm.
Casual.
Natural.
Short.
Human.

Good examples:

That wired shed on {{address}} caught my eye. Solid bonus for
a place like that.

That finished apartment above the barn on {{address}} really
stood out. That's a pretty useful feature to have.

The private dock on {{address}} caught my attention. Definitely
a nice touch for buyers looking around there.

That four-season garden on {{address}} is a great touch. The
goldfish pond makes it even more memorable.

RULES:

- Maximum 35 words.
- Pick only one main distinctive feature.
- Never invent anything.
- Never mention the recipient's name.
- Never use an exclamation mark.
- Never say "I noticed your listing".
- Never say "I came across your listing".
- Avoid "impressive" and "stunning".
- Do not sound like marketing copy.
- Do not mention being AI.
- Do not output quotation marks.
- Do not explain your answer.
- Output ONLY the personalized line.

If there is no genuinely specific feature, output exactly:

SKIP
"""


# ============================================================
# AI PERSONALIZATION
# ============================================================

def create_personalized_line(
    description: str,
    max_retries: int = 3,
) -> str:

    description = clean_value(description)

    if not description:
        return ""

    # Avoid wasting tokens on abnormally long descriptions.
    description = description[:6000]

    for attempt in range(max_retries):

        try:

            response = client.responses.create(
                model=OPENAI_MODEL,

                reasoning={
                    "effort": "low"
                },

                instructions=AI_INSTRUCTIONS,

                input=(
                    "PROPERTY DESCRIPTION:\n\n"
                    + description
                ),

                max_output_tokens=100,
            )

            line = clean_value(
                response.output_text
            )

            if not line:
                return ""

            if line.upper() == "SKIP":
                return ""

            line = (
                line
                .strip('"')
                .strip("'")
                .strip()
            )

            # The literal Instantly variable must survive.
            if "{{address}}" not in line:

                print(
                    "Rejected AI line because "
                    "{{address}} was missing:",
                    line,
                    flush=True,
                )

                return ""

            # Extra protection against long responses.
            if len(line.split()) > 40:

                print(
                    "Rejected AI line because "
                    "it was too long:",
                    line,
                    flush=True,
                )

                return ""

            return line

        except Exception as error:

            print(
                f"OpenAI attempt "
                f"{attempt + 1} failed: "
                f"{error}",
                flush=True,
            )

            if attempt < max_retries - 1:

                time.sleep(
                    2 ** (attempt + 1)
                )

    return ""


# ============================================================
# PROCESS ONE PROPERTY
# ============================================================

def process_property(row: dict) -> dict:

    contact = get_contact(row)

    address = clean_address(
        row.get(PROPERTY_ADDRESS)
    )

    city = clean_value(
        row.get(PROPERTY_CITY)
    )

    price = format_price(
        row.get(PROPERTY_PRICE)
    )

    description = clean_value(
        row.get(PROPERTY_DESCRIPTION)
    )

    personalized_line = (
        create_personalized_line(
            description
        )
    )

    return {
        "first_name":
            contact["first_name"],

        "email":
            contact["email"],

        "address":
            address,

        "city":
            city,

        "price":
            price,

        "property_description":
            description,

        "personalized_line":
            personalized_line,

        "contact_source":
            contact["contact_source"],
    }


# ============================================================
# DOWNLOAD APIFY DATASET
# ============================================================

def get_apify_dataset(
    dataset_id: str,
) -> list:

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

        raise ValueError(
            "Apify dataset response was not a list."
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
# BUILD CSV
# ============================================================

def build_csv(results: list) -> str:

    output = io.StringIO()

    fieldnames = [
        "first_name",
        "email",
        "address",
        "city",
        "price",
        "property_description",
        "personalized_line",
        "contact_source",
    ]

    writer = csv.DictWriter(
        output,
        fieldnames=fieldnames,
        extrasaction="ignore",
    )

    writer.writeheader()

    for row in results:
        writer.writerow(row)

    return output.getvalue()


# ============================================================
# UPLOAD CSV TO GITHUB
# ============================================================

def upload_csv_to_github(
    results: list,
    run_id: str,
    event_type: str,
) -> str:

    if not results:
        raise ValueError(
            "No processed results to upload."
        )

    repo = github_client.get_repo(
        GITHUB_REPO
    )

    csv_content = build_csv(results)

    timestamp = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d_%H-%M-%S"
    )

    status_name = (
        event_type
        .replace("ACTOR.RUN.", "")
        .lower()
    )

    # Shorten run ID in filename while keeping it identifiable.
    short_run_id = run_id[:10]

    github_path = (
        "output/"
        f"properties_{timestamp}_"
        f"{status_name}_"
        f"{short_run_id}.csv"
    )

    commit_message = (
        f"Add processed property data "
        f"for Apify run {run_id}"
    )

    repo.create_file(
        github_path,
        commit_message,
        csv_content,
        branch=GITHUB_BRANCH,
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
            "status":
                "downloading_dataset",

            "event_type":
                event_type,

            "dataset_id":
                dataset_id,

            "total":
                0,

            "processed":
                0,

            "batch":
                0,

            "github_file":
                "",
        }

        print(
            f"[{run_id}] "
            f"Event: {event_type}",
            flush=True,
        )

        print(
            f"[{run_id}] "
            f"Downloading dataset "
            f"{dataset_id}",
            flush=True,
        )

        items = get_apify_dataset(
            dataset_id
        )

        total = len(items)

        jobs[run_id]["total"] = total
        jobs[run_id]["status"] = "processing"

        print(
            f"[{run_id}] "
            f"Downloaded {total} records.",
            flush=True,
        )

        if total == 0:

            jobs[run_id]["status"] = (
                "completed_empty"
            )

            print(
                f"[{run_id}] "
                "Dataset contained 0 records.",
                flush=True,
            )

            return

        all_results = []

        # ====================================================
        # PROCESS IN BATCHES OF 200
        # ====================================================

        for batch_number, batch in enumerate(
            split_batches(
                items,
                BATCH_SIZE,
            ),
            start=1,
        ):

            jobs[run_id]["batch"] = (
                batch_number
            )

            print(
                f"[{run_id}] "
                f"Starting batch "
                f"{batch_number} "
                f"({len(batch)} records)",
                flush=True,
            )

            batch_results = []

            for row_number, row in enumerate(
                batch,
                start=1,
            ):

                try:

                    cleaned = (
                        process_property(row)
                    )

                    batch_results.append(
                        cleaned
                    )

                except Exception as error:

                    print(
                        f"[{run_id}] "
                        f"Row {row_number} "
                        f"in batch "
                        f"{batch_number} failed: "
                        f"{error}",
                        flush=True,
                    )

            all_results.extend(
                batch_results
            )

            jobs[run_id]["processed"] = (
                len(all_results)
            )

            print(
                f"[{run_id}] "
                f"Finished batch "
                f"{batch_number}. "
                f"{len(all_results)}/"
                f"{total} processed.",
                flush=True,
            )

        # ====================================================
        # STATS
        # ====================================================

        with_email = sum(
            1
            for row in all_results
            if row["email"]
        )

        with_personalization = sum(
            1
            for row in all_results
            if row["personalized_line"]
        )

        # ====================================================
        # CREATE + UPLOAD GITHUB CSV
        # ====================================================

        jobs[run_id]["status"] = (
            "uploading_to_github"
        )

        print(
            f"[{run_id}] "
            "Creating CSV and uploading "
            "to GitHub...",
            flush=True,
        )

        github_file = upload_csv_to_github(
            all_results,
            run_id,
            event_type,
        )

        jobs[run_id].update({
            "status":
                "completed",

            "processed":
                len(all_results),

            "with_email":
                with_email,

            "with_personalization":
                with_personalization,

            "github_file":
                github_file,
        })

        print(
            f"[{run_id}] COMPLETE. "
            f"{len(all_results)} records. "
            f"{with_email} emails. "
            f"{with_personalization} "
            f"personalized.",
            flush=True,
        )

        print(
            f"[{run_id}] "
            f"GitHub file created: "
            f"{github_file}",
            flush=True,
        )

    except Exception as error:

        print(
            f"[{run_id}] "
            f"JOB FAILED: "
            f"{type(error).__name__}: "
            f"{error}",
            flush=True,
        )

        jobs[run_id] = {
            "status":
                "failed",

            "event_type":
                event_type,

            "dataset_id":
                dataset_id,

            "error":
                str(error),
        }


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route(
    "/",
    methods=["GET"],
)
def health():

    return jsonify({
        "status":
            "ok",

        "service":
            "email-personalization-ai",

        "model":
            OPENAI_MODEL,

        "batch_size":
            BATCH_SIZE,

        "accepted_events": [
            "ACTOR.RUN.SUCCEEDED",
            "ACTOR.RUN.ABORTED",
        ],

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

    event_type = payload.get(
        "eventType"
    )

    print(
        "Apify webhook received:",
        event_type,
        flush=True,
    )

    # ========================================================
    # ACCEPT BOTH SUCCESS + ABORT
    # ========================================================

    allowed_events = {
        "ACTOR.RUN.SUCCEEDED",
        "ACTOR.RUN.ABORTED",
    }

    if event_type not in allowed_events:

        print(
            "Ignoring event:",
            event_type,
            flush=True,
        )

        return jsonify({
            "success":
                True,

            "message":
                "Event ignored.",

            "event_type":
                event_type,
        }), 200

    # ========================================================
    # GET RUN + DATASET
    # ========================================================

    resource = payload.get(
        "resource"
    ) or {}

    dataset_id = resource.get(
        "defaultDatasetId"
    )

    run_id = (
        resource.get("id")
        or
        payload
        .get("eventData", {})
        .get("actorRunId")
    )

    if not dataset_id:

        print(
            "Webhook missing "
            "defaultDatasetId.",
            flush=True,
        )

        return jsonify({
            "success":
                False,

            "error":
                "defaultDatasetId missing "
                "from Apify webhook.",
        }), 400

    if not run_id:

        return jsonify({
            "success":
                False,

            "error":
                "Actor run ID missing "
                "from webhook.",
        }), 400

    print(
        f"Accepted {event_type} "
        f"for run {run_id}. "
        f"Dataset: {dataset_id}",
        flush=True,
    )

    # ========================================================
    # DUPLICATE PROTECTION
    # ========================================================

    existing = jobs.get(run_id)

    if (
        existing
        and existing.get("status")
        in {
            "queued",
            "downloading_dataset",
            "processing",
            "uploading_to_github",
            "completed",
        }
    ):

        return jsonify({
            "success":
                True,

            "message":
                "Run already received.",

            "run_id":
                run_id,

            "status":
                existing.get("status"),
        }), 200

    jobs[run_id] = {
        "status":
            "queued",

        "event_type":
            event_type,

        "dataset_id":
            dataset_id,

        "processed":
            0,

        "github_file":
            "",
    }

    # ========================================================
    # BACKGROUND WORKER
    # ========================================================

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

    # Return immediately so Apify doesn't wait for OpenAI.
    return jsonify({
        "success":
            True,

        "message":
            "Apify run accepted.",

        "event_type":
            event_type,

        "run_id":
            run_id,

        "dataset_id":
            dataset_id,

        "batch_size":
            BATCH_SIZE,
    }), 200


# ============================================================
# JOB STATUS
# ============================================================

@app.route(
    "/jobs/<run_id>",
    methods=["GET"],
)
def job_status(run_id):

    job = jobs.get(run_id)

    if not job:

        return jsonify({
            "success":
                False,

            "error":
                "Job not found.",
        }), 404

    return jsonify({
        "success":
            True,

        "run_id":
            run_id,

        "job":
            job,
    })


# ============================================================
# TEST AI
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

    if not description:

        return jsonify({
            "success":
                False,

            "error":
                "description is required",
        }), 400

    line = create_personalized_line(
        description
    )

    return jsonify({
        "success":
            True,

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
        debug=False,
    )
