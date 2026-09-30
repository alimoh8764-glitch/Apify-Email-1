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
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

OPENAI_MODEL = os.environ.get(
    "OPENAI_MODEL",
    "gpt-5.6-luna",
)

BATCH_SIZE = 200

openai_client = OpenAI(
    api_key=OPENAI_API_KEY
) if OPENAI_API_KEY else None

github_client = Github(
    GITHUB_TOKEN
) if GITHUB_TOKEN else None


# In-memory status only.
# Fine for testing, but not durable across Railway restarts.
jobs = {}


# ============================================================
# GENERAL CLEANING
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

    # Remove common prefixes.
    name = re.sub(
        r"^(mr|mrs|ms|miss|dr)\.?\s+",
        "",
        name,
        flags=re.IGNORECASE,
    )

    # Realtor data can occasionally contain punctuation.
    name = name.strip(" ,.-")

    if not name:
        return ""

    return name.split()[0]


# ============================================================
# NESTED REALTOR JSON HELPERS
# ============================================================

def get_list(value: Any) -> list:
    """
    Safely return a list.
    """
    if isinstance(value, list):
        return value

    return []


def get_dict(value: Any) -> dict:
    """
    Safely return a dictionary.
    """
    if isinstance(value, dict):
        return value

    return {}


# ============================================================
# CONTACT MATCHING
# ============================================================

def get_contact(row: dict) -> dict:
    """
    Get ONE correctly matched contact.

    Priority:
    1. Complete agent name + email
    2. Complete advertiser name + email
    3. Match agent email to advertiser email and use
       advertiser name
    4. Name-only agent
    5. Name-only advertiser

    Never intentionally pair unrelated names and emails.
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
    # 3. AGENT EMAIL MATCHES ADVERTISER EMAIL
    #
    # Example from your dataset:
    # agent name = null
    # agent email = allenkcurtis@gmail.com
    #
    # advertiser name = Allen Curtis
    # advertiser email = allenkcurtis@gmail.com
    #
    # That's clearly the same contact.
    # --------------------------------------------------------

    for agent in agents:
        if not isinstance(agent, dict):
            continue

        agent_email = clean_value(agent.get("email"))

        if not valid_email(agent_email):
            continue

        for advertiser in advertisers:
            if not isinstance(advertiser, dict):
                continue

            advertiser_email = clean_value(
                advertiser.get("email")
            )

            advertiser_name = clean_value(
                advertiser.get("name")
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
    # 4. NAME-ONLY AGENT
    # --------------------------------------------------------

    for agent in agents:
        if not isinstance(agent, dict):
            continue

        name = clean_value(agent.get("name"))

        if name:
            return {
                "name": name,
                "first_name": get_first_name(name),
                "email": "",
                "source": "agent_name_only",
            }

    # --------------------------------------------------------
    # 5. NAME-ONLY ADVERTISER
    # --------------------------------------------------------

    for advertiser in advertisers:
        if not isinstance(advertiser, dict):
            continue

        name = clean_value(advertiser.get("name"))

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
# PROPERTY DATA
# ============================================================

def get_address(row: dict) -> str:
    """
    Realtor JSON:
        "address": {
            "street": "143 Rosewood Ln",
            "locality": "Rutherfordton"
        }
    """

    address_data = get_dict(row.get("address"))

    return clean_value(
        address_data.get("street")
    )


def get_city(row: dict) -> str:
    address_data = get_dict(row.get("address"))

    return clean_value(
        address_data.get("locality")
    )


def get_description(row: dict) -> str:
    """
    IMPORTANT:
    Realtor scraper uses `description`,
    NOT `text`.
    """

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
You write short personalized opening lines for real estate
agent outreach.

You will receive a property's public listing description.

Your job is to identify ONE genuinely distinctive,
specific feature from that description and write a short,
natural sentence about it.

RULES:

- Use ONLY facts explicitly contained in the listing
  description.
- Never invent or assume a feature.
- Refer to the property using the literal placeholder
  {{address}}.
- NEVER insert the actual street address.
- Keep the final response under 35 words.
- Sound casual, warm, and human.
- Pick ONE distinctive detail rather than summarizing
  the whole property.
- Avoid generic compliments.
- Do not say:
  "I noticed your listing"
  "I came across your listing"
  "beautiful home"
  "great location"
  "spacious property"
  "impressive"
  "stunning"
- Do not use exclamation marks.
- Do not mention that you are an AI.
- Do not explain your reasoning.
- Return ONLY the personalized line.
- If the description contains no genuinely distinctive
  usable feature, return exactly:
  SKIP

Good style example:

That wired shed on {{address}} caught my eye. Solid bonus
for a place like that.

Another example:

The screened porches on {{address}} are a nice touch.
That kind of outdoor space gives the place some real
character.
""".strip()


def create_personalized_line(
    description: str,
) -> str:

    description = clean_value(description)

    if not description:
        return ""

    if not openai_client:
        print(
            "OPENAI_API_KEY missing; "
            "skipping personalization.",
            flush=True,
        )
        return ""

    # Prevent huge descriptions from unnecessarily
    # increasing token usage.
    description = description[:6000]

    prompt = f"""
PROPERTY LISTING DESCRIPTION:

{description}

Write the personalized opening line now.
""".strip()

    max_attempts = 3

    for attempt in range(
        1,
        max_attempts + 1,
    ):
        try:
            response = openai_client.responses.create(
                model=OPENAI_MODEL,
                reasoning={
                    "effort": "low"
                },
                instructions=AI_INSTRUCTIONS,
                input=prompt,
                max_output_tokens=100,
            )

            line = clean_value(
                response.output_text
            )

            line = line.strip(
                '"\''
            )

            if not line:
                return ""

            if line.upper() == "SKIP":
                return ""

            # Required placeholder.
            if "{{address}}" not in line:
                print(
                    "AI output rejected because "
                    "{{address}} was missing:",
                    line,
                    flush=True,
                )
                return ""

            # Extra safety limit.
            if len(line.split()) > 40:
                print(
                    "AI output rejected because "
                    "it exceeded 40 words:",
                    line,
                    flush=True,
                )
                return ""

            return line

        except Exception as exc:
            print(
                f"OpenAI attempt {attempt} failed: "
                f"{exc}",
                flush=True,
            )

            if attempt < max_attempts:
                time.sleep(
                    2 * attempt
                )

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
            description
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
# PROCESS COMPLETE APIFY RUN
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

        if total == 0:
            print(
                "Dataset contains zero records.",
                flush=True,
            )

        results = []

        batches = list(
            split_batches(
                items,
                BATCH_SIZE,
            )
        )

        for batch_number, batch in enumerate(
            batches,
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

                    results.append(
                        processed
                    )

                except Exception as exc:
                    print(
                        "Property processing "
                        f"failed: {exc}",
                        flush=True,
                    )

                    # Keep a row in the CSV so
                    # one bad property doesn't
                    # destroy the whole run.
                    results.append({
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
                    })

                jobs[run_id][
                    "processed"
                ] = len(results)

            print(
                f"Finished batch "
                f"{batch_number}. "
                f"{len(results)}/{total} "
                "processed.",
                flush=True,
            )

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

        # Store results temporarily so the
        # existing results endpoint still works.
        jobs[run_id]["results"] = results

        print(
            f"Run {run_id} completed. "
            f"{len(results)} records "
            "processed.",
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
        "service":
            "email-personalization-ai",
        "batch_size": BATCH_SIZE,
        "openai_model": OPENAI_MODEL,
        "github_repo": GITHUB_REPO,
        "github_branch": GITHUB_BRANCH,
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
        "Apify webhook received: "
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
        print(
            "Webhook missing run ID.",
            flush=True,
        )

        return jsonify({
            "ok": False,
            "error":
                "resource.id missing",
        }), 400

    if not dataset_id:
        print(
            "Webhook missing "
            "defaultDatasetId.",
            flush=True,
        )

        return jsonify({
            "ok": False,
            "error":
                "resource.defaultDatasetId "
                "missing",
        }), 400

    # Basic duplicate protection.
    # Note: this resets if Railway
    # restarts the container.
    existing = jobs.get(run_id)

    if existing and existing.get(
        "status"
    ) in {
        "downloading",
        "processing",
        "uploading",
        "completed",
    }:

        print(
            f"Run {run_id} already "
            "accepted.",
            flush=True,
        )

        return jsonify({
            "ok": True,
            "duplicate": True,
            "run_id": run_id,
            "status":
                existing.get("status"),
        }), 200

    print(
        f"Accepted {event_type} "
        f"for run {run_id}",
        flush=True,
    )

    # Respond to Apify immediately.
    # Actual work happens in the
    # background.
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

    job = jobs.get(run_id)

    if not job:
        return jsonify({
            "ok": False,
            "error": "Job not found",
        }), 404

    safe_job = {
        key: value
        for key, value in job.items()
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

    job = jobs.get(run_id)

    if not job:
        return jsonify({
            "ok": False,
            "error": "Job not found",
        }), 404

    if job.get("status") != "completed":
        return jsonify({
            "ok": False,
            "status":
                job.get("status"),
            "error":
                "Job has not completed.",
        }), 409

    return jsonify({
        "ok": True,
        "run_id": run_id,
        "github_file":
            job.get("github_file"),
        "count":
            len(
                job.get(
                    "results",
                    [],
                )
            ),
        "results":
            job.get(
                "results",
                [],
            ),
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

    if not description:
        return jsonify({
            "ok": False,
            "error":
                "description is required",
        }), 400

    line = create_personalized_line(
        description
    )

    return jsonify({
        "ok": True,
        "personalized_line": line,
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
