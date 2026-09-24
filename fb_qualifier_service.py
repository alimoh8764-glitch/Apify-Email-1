import base64
import csv
import io
import os
import re
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "alimoh8764-glitch/Apify-Email-1").strip()
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()
MASTER_FILE = os.getenv("MASTER_FILE", "diddy.csv").strip()
GITHUB_OUTPUT_FILE = os.getenv("GITHUB_OUTPUT_FILE", "FBRUN/qualified.csv").strip()

MIN_FOLLOWERS = int(os.getenv("MIN_FOLLOWERS", "100"))
MAX_FOLLOWERS = int(os.getenv("MAX_FOLLOWERS", "600"))
TIMEOUT = 30

OUTPUT_COLUMNS = [
    "Url",
    "Address",
    "First name",
    "Followers",
    "Email",
    "Phone",
    "Website",
    "Facebook Name",
    "Page ID",
    "Category",
    "FB Qualification",
    "FB Checked At",
]


def clean_text(value):
    if value is None:
        return ""
    text = str(value).replace("\ufeff", "").replace("\u200b", "").strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def normalize_facebook_url(value):
    """Return a stable Facebook page key for matching master rows to Apify rows."""
    text = clean_text(value)
    if not text:
        return ""

    # Handle accidental Markdown links: [label](https://facebook.com/page)
    md_match = re.search(r"\((https?://[^)]+)\)", text, flags=re.I)
    if md_match:
        text = md_match.group(1)

    url_match = re.search(r"https?://[^\s<>\]\)]+", text, flags=re.I)
    if url_match:
        text = url_match.group(0)

    text = text.strip().rstrip("/")
    text = re.sub(r"^https?://", "", text, flags=re.I)
    text = re.sub(r"^(www\.|m\.|web\.)", "", text, flags=re.I)

    # Drop query strings/fragments and normalize host.
    text = text.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    text = re.sub(r"^facebook\.com/", "", text, flags=re.I)
    text = re.sub(r"^fb\.com/", "", text, flags=re.I)

    # Page handles are case-insensitive for our matching purposes.
    return text.strip("/").lower()


def parse_followers(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)

    text = clean_text(value).lower().replace(",", "")
    if not text:
        return None

    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([kmb]?)", text)
    if not match:
        return None

    number = float(match.group(1))
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[match.group(2)]
    return int(number * multiplier)


def first_value(record, *keys):
    for key in keys:
        if key in record:
            value = clean_text(record.get(key))
            if value:
                return value
    return ""


def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "fb-qualifier-service",
    }


def github_contents_url(path):
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"


def fetch_master_csv():
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN is not configured")

    response = requests.get(
        github_contents_url(MASTER_FILE),
        headers=github_headers(),
        params={"ref": GITHUB_BRANCH},
        timeout=TIMEOUT,
    )
    print(f"[GITHUB] Master request HTTP {response.status_code}", flush=True)
    response.raise_for_status()

    payload = response.json()
    encoded = payload.get("content", "")
    if not encoded:
        raise RuntimeError(f"GitHub returned no content for {MASTER_FILE}")

    raw = base64.b64decode(encoded).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw))
    rows = list(reader)

    required = {"Url", "Address", "First name"}
    actual = set(reader.fieldnames or [])
    missing = required - actual
    if missing:
        raise RuntimeError(f"Master CSV missing columns: {', '.join(sorted(missing))}")

    master = {}
    duplicate_keys = []
    for row in rows:
        key = normalize_facebook_url(row.get("Url"))
        if not key:
            continue
        if key in master:
            duplicate_keys.append(key)
            continue
        master[key] = {
            "Url": clean_text(row.get("Url")),
            "Address": clean_text(row.get("Address")),
            "First name": clean_text(row.get("First name")),
        }

    print(
        f"[MASTER] Loaded {len(rows)} rows; {len(master)} usable unique Facebook URLs; "
        f"{len(duplicate_keys)} duplicates skipped",
        flush=True,
    )
    return master


def build_csv_bytes(rows):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def upload_csv_to_github(path, csv_bytes, commit_message):
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN is not configured")

    url = github_contents_url(path)
    existing = requests.get(
        url,
        headers=github_headers(),
        params={"ref": GITHUB_BRANCH},
        timeout=TIMEOUT,
    )

    body = {
        "message": commit_message,
        "content": base64.b64encode(csv_bytes).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }

    if existing.status_code == 200:
        body["sha"] = existing.json()["sha"]
    elif existing.status_code != 404:
        existing.raise_for_status()

    response = requests.put(url, headers=github_headers(), json=body, timeout=TIMEOUT)
    print(f"[GITHUB] Upload {path} HTTP {response.status_code}", flush=True)
    response.raise_for_status()
    return response.json()


def get_dataset_id_from_payload(payload):
    candidates = [
        payload.get("defaultDatasetId"),
        (payload.get("eventData") or {}).get("defaultDatasetId"),
        (payload.get("resource") or {}).get("defaultDatasetId"),
    ]
    return next((clean_text(x) for x in candidates if clean_text(x)), "")


def get_actor_run_id(payload):
    event_data = payload.get("eventData") or {}
    resource = payload.get("resource") or {}
    candidates = [
        event_data.get("actorRunId"),
        event_data.get("runId"),
        resource.get("actorRunId"),
        resource.get("runId"),
        resource.get("id"),
        payload.get("actorRunId"),
        payload.get("runId"),
    ]
    return next((clean_text(x) for x in candidates if clean_text(x)), "")


def fetch_actor_run(run_id):
    if not APIFY_TOKEN:
        raise RuntimeError("APIFY_TOKEN is not configured")

    response = requests.get(
        f"https://api.apify.com/v2/actor-runs/{run_id}",
        params={"token": APIFY_TOKEN},
        timeout=TIMEOUT,
    )
    print(f"[APIFY] Run request HTTP {response.status_code}", flush=True)
    response.raise_for_status()
    return response.json().get("data") or {}


def resolve_dataset_id(payload):
    direct = get_dataset_id_from_payload(payload)
    if direct:
        return direct

    run_id = get_actor_run_id(payload)
    if not run_id:
        raise RuntimeError("No Apify actor run ID found in webhook payload")

    run = fetch_actor_run(run_id)
    dataset_id = clean_text(run.get("defaultDatasetId"))
    if not dataset_id:
        raise RuntimeError(f"No defaultDatasetId found for Apify run {run_id}")
    return dataset_id


def fetch_apify_dataset(dataset_id):
    if not APIFY_TOKEN:
        raise RuntimeError("APIFY_TOKEN is not configured")

    response = requests.get(
        f"https://api.apify.com/v2/datasets/{dataset_id}/items",
        params={"token": APIFY_TOKEN, "clean": "true", "format": "json"},
        timeout=TIMEOUT,
    )
    print(f"[APIFY] Dataset request HTTP {response.status_code}", flush=True)
    response.raise_for_status()
    items = response.json()
    if not isinstance(items, list):
        raise RuntimeError("Apify dataset response was not a list")
    print(f"[APIFY] Records downloaded: {len(items)}", flush=True)
    return items


def apify_url(record):
    return first_value(
        record,
        "url",
        "facebookUrl",
        "facebook_url",
        "pageUrl",
        "page_url",
        "profileUrl",
        "profile_url",
    )


def process_items(items, run_id=""):
    master = fetch_master_csv()
    checked_at = datetime.now(timezone.utc).isoformat()

    qualified = []
    matched = 0
    unmatched = 0
    invalid_followers = 0
    out_of_range = 0
    seen_master_keys = set()

    for record in items:
        if not isinstance(record, dict):
            continue

        source_url = apify_url(record)
        key = normalize_facebook_url(source_url)
        master_row = master.get(key)

        if not key or not master_row:
            unmatched += 1
            continue

        # Prevent duplicate Apify records from duplicating the same master lead.
        if key in seen_master_keys:
            continue
        seen_master_keys.add(key)
        matched += 1

        followers = parse_followers(
            record.get("followers")
            if record.get("followers") is not None
            else record.get("followersCount", record.get("followers_count"))
        )

        if followers is None:
            invalid_followers += 1
            continue
        if not (MIN_FOLLOWERS <= followers <= MAX_FOLLOWERS):
            out_of_range += 1
            continue

        qualified.append(
            {
                "Url": master_row["Url"],
                "Address": master_row["Address"],
                "First name": master_row["First name"],
                "Followers": followers,
                "Email": first_value(record, "email", "emails"),
                "Phone": first_value(record, "phone", "phoneNumber", "phone_number"),
                "Website": first_value(record, "website", "websiteUrl", "website_url"),
                "Facebook Name": first_value(record, "name", "pageName", "page_name"),
                "Page ID": first_value(record, "page_id", "pageId", "id"),
                "Category": first_value(record, "category_name", "categoryName", "category"),
                "FB Qualification": "QUALIFIED",
                "FB Checked At": checked_at,
            }
        )

    csv_bytes = build_csv_bytes(qualified)
    run_label = run_id or "manual"
    upload_result = upload_csv_to_github(
        GITHUB_OUTPUT_FILE,
        csv_bytes,
        f"Update Facebook qualified leads ({run_label})",
    )

    summary = {
        "received": len(items),
        "master_urls": len(master),
        "matched": matched,
        "unmatched": unmatched,
        "invalid_followers": invalid_followers,
        "out_of_range": out_of_range,
        "qualified": len(qualified),
        "min_followers": MIN_FOLLOWERS,
        "max_followers": MAX_FOLLOWERS,
        "github_file": GITHUB_OUTPUT_FILE,
        "github_commit": ((upload_result.get("commit") or {}).get("sha") or "")[:12],
    }
    print(f"[QUALIFIER] COMPLETE: {summary}", flush=True)
    return summary, csv_bytes


@app.get("/")
def root():
    return jsonify(
        {
            "ok": True,
            "service": "Facebook qualifier",
            "master_file": MASTER_FILE,
            "output_file": GITHUB_OUTPUT_FILE,
            "range": f"{MIN_FOLLOWERS}-{MAX_FOLLOWERS}",
        }
    )


@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "apify_token_configured": bool(APIFY_TOKEN),
            "github_token_configured": bool(GITHUB_TOKEN),
            "github_repo": GITHUB_REPO,
            "github_branch": GITHUB_BRANCH,
        }
    )


@app.post("/apify-webhook")
def apify_webhook():
    print("[WEBHOOK] APIFY WEBHOOK RECEIVED", flush=True)
    payload = request.get_json(silent=True) or {}
    event_type = clean_text(payload.get("eventType"))
    print(f"[WEBHOOK] Event type: {event_type}", flush=True)

    try:
        dataset_id = resolve_dataset_id(payload)
        run_id = get_actor_run_id(payload)
        items = fetch_apify_dataset(dataset_id)
        summary, _ = process_items(items, run_id=run_id)
        print("[WEBHOOK] SUCCESS", flush=True)
        return jsonify({"ok": True, **summary})
    except Exception as exc:
        print(f"[WEBHOOK] ERROR: {exc}", flush=True)
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/process-json")
def process_json():
    """Manual testing route: POST a JSON list containing Apify-style records."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, list):
        return jsonify({"ok": False, "error": "Expected a JSON list"}), 400

    try:
        summary, csv_bytes = process_items(payload, run_id="manual")
        return jsonify({"ok": True, **summary})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/download/qualified")
def download_qualified():
    """Download the current qualified.csv directly from GitHub."""
    try:
        response = requests.get(
            github_contents_url(GITHUB_OUTPUT_FILE),
            headers=github_headers(),
            params={"ref": GITHUB_BRANCH},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        raw = base64.b64decode(response.json()["content"])
        return send_file(
            io.BytesIO(raw),
            mimetype="text/csv",
            as_attachment=True,
            download_name="qualified.csv",
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
