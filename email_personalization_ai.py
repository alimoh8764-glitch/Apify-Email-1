import os
import re
import threading
import time
from typing import Any

import requests
from flask import Flask, jsonify, request
from openai import OpenAI


# ============================================================
# CONFIG
# ============================================================

app = Flask(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN")

OPENAI_MODEL = os.environ.get(
    "OPENAI_MODEL",
    "gpt-5.6-luna",
)

BATCH_SIZE = 200

if not OPENAI_API_KEY:
    raise RuntimeError(
        "OPENAI_API_KEY environment variable is missing."
    )

if not APIFY_API_TOKEN:
    raise RuntimeError(
        "APIFY_API_TOKEN environment variable is missing."
    )

client = OpenAI(api_key=OPENAI_API_KEY)


# ============================================================
# APIFY COLUMN NAMES
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
#
# In-memory for now.
# This lets us inspect a running job from Railway.
# ============================================================

jobs = {}


# ============================================================
# CLEANING
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

    return first_name.title()


# ============================================================
# CONTACT MATCHING
# ============================================================

def get_contact(row: dict) -> dict:
    """
    Never mix an agent's email with an advertiser's name.

    Priority:

    1. Agent name + agent email
    2. Advertiser name + advertiser email
    3. Name only
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

    # Agent pair
    if agent_name and valid_email(agent_email):

        return {
            "first_name":
                get_first_name(agent_name),

            "email":
                agent_email.lower(),

            "contact_source":
                "agent",
        }

    # Advertiser pair
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

    # Name only
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
    """
    We only want the street address.

    Example:

    123 North Bay, Calgary, AB E126QP

    becomes:

    123 North Bay
    """

    address = clean_value(address)

    if not address:
        return ""

    # address/street should already normally be short,
    # but this catches full-address values.
    if "," in address:
        address = address.split(",")[0].strip()

    return address


# ============================================================
# PRICE
# ============================================================

def format_price(price: Any) -> str:
    """
    425000 -> $425,000
    1200000 -> $1,200,000
    """

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
- workshop
- barn apartment
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
- distinctive outdoor feature

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

ADDRESS:

You MUST literally write:

{{address}}

Do NOT write the real street address.

Another system will replace {{address}} later.

STYLE:

Warm.
Casual.
Natural.
Short.

Examples:

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
- Pick one main distinctive feature.
- Never invent a feature.
- Never mention the recipient's name.
- Never use an exclamation mark.
- Never say "I noticed your listing".
- Never say "I came across your listing".
- Avoid "impressive" and "stunning".
- Do not sound like marketing copy.
- Output only the personalized line.
- No quotation marks.
- No explanation.

If there is no genuinely specific feature, output exactly:

SKIP
"""


# ============================================================
# OPENAI PERSONALIZATION
# ============================================================

def create_personalized_line(
    description: str,
    max_retries: int = 3,
) -> str:

    description = clean_value(description)

    if not description:
        return ""

    # Control token usage.
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

            # AI must preserve our Instantly variable.
            if "{{address}}" not in line:

                print(
                    "Rejected AI line - "
                    "missing {{address}}:",
                    line,
                )

                return ""

            if len(line.split()) > 40:

                print(
                    "Rejected AI line - "
                    "too long:",
                    line,
                )

                return ""

            return line

        except Exception as error:

            print(
                f"OpenAI attempt "
                f"{attempt + 1} failed: "
                f"{error}"
            )

            if attempt < max_retries - 1:

                # 2 sec, then 4 sec
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
# APIFY DATASET DOWNLOAD
# ============================================================

def get_apify_dataset(
    dataset_id: str,
) -> list:
    """
    Download the completed Actor dataset.

    Railway authenticates to Apify using APIFY_API_TOKEN.
    """

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
# PROCESS COMPLETE APIFY RUN
# ============================================================

def process_apify_run(
    run_id: str,
    dataset_id: str,
):
    """
    Runs after the webhook has already returned HTTP 200.

    Dataset is processed in chunks of 200.
    """

    try:

        jobs[run_id] = {
            "status":
                "downloading_dataset",

            "dataset_id":
                dataset_id,

            "total":
                0,

            "processed":
                0,

            "batch":
                0,

            "results":
                [],
        }

        print(
            f"[{run_id}] "
            f"Downloading dataset "
            f"{dataset_id}"
        )

        items = get_apify_dataset(
            dataset_id
        )

        total = len(items)

        jobs[run_id]["total"] = total

        jobs[run_id]["status"] = (
            "processing"
        )

        print(
            f"[{run_id}] "
            f"Downloaded {total} records."
        )

        all_results = []

        # ----------------------------------------
        # 200 records per batch
        # ----------------------------------------

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
                f"({len(batch)} records)"
            )

            batch_results = []

            for row in batch:

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
                        f"Property failed: "
                        f"{error}"
                    )

            all_results.extend(
                batch_results
            )

            jobs[run_id]["processed"] = (
                len(all_results)
            )

            # Store progress.
            jobs[run_id]["results"] = (
                all_results
            )

            print(
                f"[{run_id}] "
                f"Finished batch "
                f"{batch_number}. "
                f"{len(all_results)}/"
                f"{total} processed."
            )

        # ----------------------------------------
        # Complete
        # ----------------------------------------

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

        jobs[run_id].update({
            "status":
                "completed",

            "processed":
                len(all_results),

            "with_email":
                with_email,

            "with_personalization":
                with_personalization,

            "results":
                all_results,
        })

        print(
            f"[{run_id}] COMPLETE. "
            f"{len(all_results)} records. "
            f"{with_email} emails. "
            f"{with_personalization} "
            f"personalized."
        )

    except Exception as error:

        print(
            f"[{run_id}] "
            f"JOB FAILED: {error}"
        )

        jobs[run_id] = {
            "status":
                "failed",

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
    })


# ============================================================
# APIFY SUCCESS WEBHOOK
# ============================================================

@app.route(
    "/apify-webhook",
    methods=["POST"],
)
def apify_webhook():
    """
    Apify calls this endpoint when the Actor succeeds.

    IMPORTANT:

    We acknowledge the webhook immediately.

    The 3,000-5,000 record processing job runs separately
    instead of making Apify wait for thousands of AI calls.
    """

    payload = request.get_json(
        silent=True
    ) or {}

    print(
        "Apify webhook received:",
        payload.get("eventType")
    )

    # ----------------------------------------
    # Only process successful Actor runs
    # ----------------------------------------

    event_type = payload.get(
        "eventType"
    )

    if (
        event_type
        and event_type
        != "ACTOR.RUN.SUCCEEDED"
    ):

        return jsonify({
            "success": False,
            "message":
                "Ignored non-success event.",
        }), 200

    # ----------------------------------------
    # Apify puts the completed Actor run
    # inside "resource".
    # ----------------------------------------

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

        return jsonify({
            "success": False,
            "error":
                "defaultDatasetId missing "
                "from Apify webhook.",
        }), 400

    if not run_id:

        return jsonify({
            "success": False,
            "error":
                "Actor run ID missing "
                "from webhook.",
        }), 400

    # ----------------------------------------
    # Prevent duplicate webhook processing
    # ----------------------------------------

    existing = jobs.get(run_id)

    if existing and existing.get(
        "status"
    ) in {
        "downloading_dataset",
        "processing",
        "completed",
    }:

        return jsonify({
            "success": True,
            "message":
                "Run already received.",

            "run_id":
                run_id,

            "status":
                existing.get("status"),
        }), 200

    # ----------------------------------------
    # Register job immediately
    # ----------------------------------------

    jobs[run_id] = {
        "status":
            "queued",

        "dataset_id":
            dataset_id,

        "processed":
            0,

        "results":
            [],
    }

    # ----------------------------------------
    # Start background processing
    # ----------------------------------------

    worker = threading.Thread(
        target=process_apify_run,
        args=(
            run_id,
            dataset_id,
        ),
        daemon=True,
    )

    worker.start()

    # ----------------------------------------
    # Immediately acknowledge Apify
    # ----------------------------------------

    return jsonify({
        "success":
            True,

        "message":
            "Apify run accepted for processing.",

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

    # Don't return thousands of rows from the status endpoint.
    safe_job = {
        key: value
        for key, value in job.items()
        if key != "results"
    }

    return jsonify({
        "success":
            True,

        "job":
            safe_job,
    })


# ============================================================
# GET FINAL RESULTS
# ============================================================

@app.route(
    "/jobs/<run_id>/results",
    methods=["GET"],
)
def job_results(run_id):

    job = jobs.get(run_id)

    if not job:

        return jsonify({
            "success":
                False,

            "error":
                "Job not found.",
        }), 404

    if job.get("status") != "completed":

        return jsonify({
            "success":
                False,

            "status":
                job.get("status"),

            "processed":
                job.get(
                    "processed",
                    0,
                ),

            "total":
                job.get(
                    "total",
                    0,
                ),

            "message":
                "Job has not completed yet.",
        }), 202

    return jsonify({
        "success":
            True,

        "run_id":
            run_id,

        "total":
            job.get("processed"),

        "data":
            job.get(
                "results",
                [],
            ),
    })


# ============================================================
# TEST AI ENDPOINT
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
            "success": False,
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
