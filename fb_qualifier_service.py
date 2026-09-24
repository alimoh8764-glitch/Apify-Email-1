```python
import csv
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "").strip()
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/tmp/fb_qualifier"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MIN_FOLLOWERS = int(os.getenv("MIN_FOLLOWERS", "100"))
MAX_FOLLOWERS = int(os.getenv("MAX_FOLLOWERS", "600"))

QUALIFIED_FILE = OUTPUT_DIR / "diddyoil_qualified.csv"
REJECTED_FILE = OUTPUT_DIR / "diddyoil_rejected.csv"

OUTPUT_COLUMNS = [
    "Goofyurls",
    "goofyaddress",
    "goofyfirstnames",
    "email",
    "fb_followers",
    "fb_qualification",
    "fb_checked_at",
    "facebook_name",
    "phone",
    "website",
    "page_id",
    "category_name",
]


def clean_text(value):
    if value is None:
        return ""

    text = str(value).replace("\ufeff", "").replace("\u200b", "").strip()

    if text.lower() in {"nan", "none", "null"}:
        return ""

    return text


def clean_facebook_url(value):
    text = clean_text(value)

    if not text:
        return ""

    markdown = re.search(
        r"\[[^\]]*\]\(\s*(https?://[^)\s]+)\s*\)",
        text,
        re.I,
    )

    if markdown:
        return markdown.group(1).strip()

    match = re.search(
        r"https?://[^\s\]\)]+",
        text,
        re.I,
    )

    if not match:
        return ""

    return match.group(0).strip().strip("\"'<>[]()")


def parse_followers(value):
    """
    Convert follower values such as:
        347
        "347"
        "1,234"
        "1.2K"
        "2M"

    Returns an integer or None.
    """

    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        try:
            if value != value:
                return None

            return int(value)

        except (TypeError, ValueError, OverflowError):
            return None

    text = clean_text(value).lower().replace(",", "")

    match = re.search(
        r"(\d+(?:\.\d+)?)\s*([kmb])?",
        text,
    )

    if not match:
        return None

    number = float(match.group(1))

    multiplier = {
        "k": 1_000,
        "m": 1_000_000,
        "b": 1_000_000_000,
    }.get(match.group(2), 1)

    return int(number * multiplier)


def format_record(item):
    followers = parse_followers(item.get("followers"))

    qualified = (
        followers is not None
        and MIN_FOLLOWERS <= followers <= MAX_FOLLOWERS
    )

    return {
        "Goofyurls": clean_facebook_url(
            item.get("url") or item.get("response_url")
        ),

        "goofyaddress": clean_text(item.get("address")),

        # Actor does not provide a trustworthy person's first name.
        "goofyfirstnames": "",

        "email": clean_text(item.get("email")),

        "fb_followers": (
            "" if followers is None else followers
        ),

        "fb_qualification": (
            "qualified" if qualified else "rejected"
        ),

        "fb_checked_at": datetime.now(
            timezone.utc
        ).isoformat(),

        "facebook_name": clean_text(item.get("name")),
        "phone": clean_text(item.get("phone")),
        "website": clean_text(item.get("website")),
        "page_id": clean_text(item.get("page_id")),
        "category_name": clean_text(item.get("category_name")),
    }, qualified


def write_csv(path, rows):
    temp = path.with_suffix(
        path.suffix + ".tmp"
    )

    with temp.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=OUTPUT_COLUMNS,
            extrasaction="ignore",
        )

        writer.writeheader()
        writer.writerows(rows)

    os.replace(temp, path)


def process_items(items):
    qualified_rows = []
    rejected_rows = []

    for item in items:

        if not isinstance(item, dict):
            continue

        row, qualified = format_record(item)

        if qualified:
            qualified_rows.append(row)
        else:
            rejected_rows.append(row)

    write_csv(
        QUALIFIED_FILE,
        qualified_rows,
    )

    write_csv(
        REJECTED_FILE,
        rejected_rows,
    )

    summary = {
        "received": len(items),
        "qualified": len(qualified_rows),
        "rejected": len(rejected_rows),
        "min_followers": MIN_FOLLOWERS,
        "max_followers": MAX_FOLLOWERS,
    }

    print(
        f"[QUALIFIER] Processing complete: {summary}",
        flush=True,
    )

    return summary


# ---------------------------------------------------------
# APIFY WEBHOOK HELPERS
# ---------------------------------------------------------

def get_dataset_id_from_payload(payload):
    """
    Sometimes Apify may include defaultDatasetId directly.
    If it does, use it without another API request.
    """

    resource = payload.get("resource")

    candidates = []

    if isinstance(resource, dict):
        candidates.append(
            resource.get("defaultDatasetId")
        )

    candidates.extend([
        payload.get("defaultDatasetId"),
        payload.get("datasetId"),
    ])

    for value in candidates:
        value = clean_text(value)

        if value:
            return value

    return ""


def get_actor_run_id(payload):
    """
    Extract the Actor run ID from different possible
    Apify webhook payload structures.

    Your webhook's eventData contains actorRunId.
    """

    event_data = payload.get("eventData")
    resource = payload.get("resource")

    candidates = []

    if isinstance(event_data, dict):
        candidates.extend([
            event_data.get("actorRunId"),
            event_data.get("runId"),
        ])

    if isinstance(resource, dict):
        candidates.extend([
            resource.get("id"),
            resource.get("actorRunId"),
        ])

    candidates.extend([
        payload.get("actorRunId"),
        payload.get("runId"),
    ])

    for value in candidates:
        value = clean_text(value)

        if value:
            return value

    return ""


def fetch_actor_run(run_id):
    """
    Ask Apify for the completed Actor run.

    This gives us defaultDatasetId even when the webhook
    itself doesn't contain it.
    """

    if not APIFY_TOKEN:
        raise RuntimeError(
            "APIFY_TOKEN environment variable is not configured"
        )

    url = (
        f"https://api.apify.com/v2/actor-runs/"
        f"{run_id}"
    )

    print(
        f"[APIFY] Fetching Actor run {run_id}",
        flush=True,
    )

    response = requests.get(
        url,
        params={
            "token": APIFY_TOKEN,
        },
        timeout=60,
    )

    print(
        f"[APIFY] Run lookup HTTP {response.status_code}",
        flush=True,
    )

    response.raise_for_status()

    payload = response.json()

    if not isinstance(payload, dict):
        raise RuntimeError(
            "Unexpected Actor run API response"
        )

    # Apify API normally wraps the run inside "data".
    data = payload.get("data")

    if isinstance(data, dict):
        return data

    return payload


def resolve_dataset_id(payload):
    """
    Resolve the dataset using either:

    1. defaultDatasetId directly in webhook
    OR
    2. actorRunId -> Apify run API -> defaultDatasetId
    """

    dataset_id = get_dataset_id_from_payload(
        payload
    )

    if dataset_id:

        print(
            f"[APIFY] Dataset ID supplied directly: "
            f"{dataset_id}",
            flush=True,
        )

        return dataset_id, ""


    run_id = get_actor_run_id(payload)

    if not run_id:
        raise RuntimeError(
            "Webhook contained neither "
            "defaultDatasetId nor actorRunId"
        )

    print(
        f"[APIFY] Actor run ID: {run_id}",
        flush=True,
    )

    run = fetch_actor_run(run_id)

    dataset_id = clean_text(
        run.get("defaultDatasetId")
    )

    if not dataset_id:
        raise RuntimeError(
            f"Actor run {run_id} has no defaultDatasetId"
        )

    print(
        f"[APIFY] Resolved dataset ID: {dataset_id}",
        flush=True,
    )

    return dataset_id, run_id


def fetch_apify_dataset(dataset_id):

    if not APIFY_TOKEN:
        raise RuntimeError(
            "APIFY_TOKEN environment variable is not configured"
        )

    url = (
        f"https://api.apify.com/v2/datasets/"
        f"{dataset_id}/items"
    )

    print(
        f"[APIFY] Downloading dataset {dataset_id}",
        flush=True,
    )

    response = requests.get(
        url,
        params={
            "token": APIFY_TOKEN,
            "clean": "true",
            "format": "json",
        },
        timeout=120,
    )

    print(
        f"[APIFY] Dataset HTTP {response.status_code}",
        flush=True,
    )

    response.raise_for_status()

    data = response.json()

    if not isinstance(data, list):
        raise RuntimeError(
            "Apify dataset response was not a JSON list"
        )

    print(
        f"[APIFY] Dataset contains {len(data)} records",
        flush=True,
    )

    return data


# ---------------------------------------------------------
# ROUTES
# ---------------------------------------------------------

@app.get("/")
def root():
    return jsonify({
        "ok": True,
        "service": "Facebook Apify Qualifier",
        "status": "online",
        "min_followers": MIN_FOLLOWERS,
        "max_followers": MAX_FOLLOWERS,
    })


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "apify_token_configured": bool(APIFY_TOKEN),
        "min_followers": MIN_FOLLOWERS,
        "max_followers": MAX_FOLLOWERS,
    })


@app.post("/apify-webhook")
def apify_webhook():

    print(
        "\n========== APIFY WEBHOOK RECEIVED ==========",
        flush=True,
    )

    payload = request.get_json(
        silent=True
    ) or {}

    print(
        f"[WEBHOOK] Event type: "
        f"{payload.get('eventType')}",
        flush=True,
    )

    event_data = payload.get(
        "eventData"
    )

    if isinstance(event_data, dict):

        print(
            f"[WEBHOOK] actorRunId: "
            f"{event_data.get('actorRunId')}",
            flush=True,
        )

        print(
            f"[WEBHOOK] actorId: "
            f"{event_data.get('actorId')}",
            flush=True,
        )

    try:

        dataset_id, run_id = resolve_dataset_id(
            payload
        )

        items = fetch_apify_dataset(
            dataset_id
        )

        summary = process_items(
            items
        )

        print(
            "[WEBHOOK] SUCCESS",
            flush=True,
        )

        print(
            "============================================\n",
            flush=True,
        )

        return jsonify({
            "ok": True,
            "run_id": run_id,
            "dataset_id": dataset_id,
            **summary,
        })


    except Exception as exc:

        app.logger.exception(
            "Apify webhook processing failed"
        )

        print(
            f"[WEBHOOK] FAILED: {exc}",
            flush=True,
        )

        print(
            "============================================\n",
            flush=True,
        )

        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 500


@app.get("/download/qualified")
def download_qualified():

    if not QUALIFIED_FILE.exists():

        return jsonify({
            "ok": False,
            "error": (
                "No qualified CSV has been "
                "generated yet"
            ),
        }), 404

    return send_file(
        QUALIFIED_FILE,
        as_attachment=True,
        download_name="diddyoil_qualified.csv",
    )


@app.get("/download/rejected")
def download_rejected():

    if not REJECTED_FILE.exists():

        return jsonify({
            "ok": False,
            "error": (
                "No rejected CSV has been "
                "generated yet"
            ),
        }), 404

    return send_file(
        REJECTED_FILE,
        as_attachment=True,
        download_name="diddyoil_rejected.csv",
    )


@app.post("/process-json")
def process_json():

    items = request.get_json(
        silent=True
    )

    if not isinstance(items, list):

        return jsonify({
            "ok": False,
            "error": (
                "Expected a JSON array of "
                "Actor dataset items"
            ),
        }), 400

    summary = process_items(
        items
    )

    return jsonify({
        "ok": True,
        **summary,
    })


if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "8080",
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
```
