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
    "Goofyurls", "goofyaddress", "goofyfirstnames", "email",
    "fb_followers", "fb_qualification", "fb_checked_at",
    "facebook_name", "phone", "website", "page_id", "category_name",
]


def clean_text(value):
    if value is None:
        return ""
    text = str(value).replace("\ufeff", "").replace("\u200b", "").strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


def clean_facebook_url(value):
    text = clean_text(value)
    if not text:
        return ""
    md = re.search(r"\[[^\]]*\]\(\s*(https?://[^)\s]+)\s*\)", text, re.I)
    if md:
        return md.group(1).strip()
    match = re.search(r"https?://[^\s\]\)]+", text, re.I)
    return match.group(0).strip().strip("\"'<>[]()") if match else ""


def parse_followers(value):
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
    match = re.search(r"(\d+(?:\.\d+)?)\s*([kmb])?", text)
    if not match:
        return None
    number = float(match.group(1))
    multiplier = {"k": 1000, "m": 1000000, "b": 1000000000}.get(match.group(2), 1)
    return int(number * multiplier)


def format_record(item):
    followers = parse_followers(item.get("followers"))
    qualified = followers is not None and MIN_FOLLOWERS <= followers <= MAX_FOLLOWERS
    return {
        "Goofyurls": clean_facebook_url(item.get("url") or item.get("response_url")),
        "goofyaddress": clean_text(item.get("address")),
        "goofyfirstnames": "",
        "email": clean_text(item.get("email")),
        "fb_followers": "" if followers is None else followers,
        "fb_qualification": "qualified" if qualified else "rejected",
        "fb_checked_at": datetime.now(timezone.utc).isoformat(),
        "facebook_name": clean_text(item.get("name")),
        "phone": clean_text(item.get("phone")),
        "website": clean_text(item.get("website")),
        "page_id": clean_text(item.get("page_id")),
        "category_name": clean_text(item.get("category_name")),
    }, qualified


def write_csv(path, rows):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def process_items(items):
    qualified_rows, rejected_rows = [], []
    for item in items:
        if not isinstance(item, dict):
            continue
        row, qualified = format_record(item)
        (qualified_rows if qualified else rejected_rows).append(row)
    write_csv(QUALIFIED_FILE, qualified_rows)
    write_csv(REJECTED_FILE, rejected_rows)
    summary = {
        "received": len(items),
        "qualified": len(qualified_rows),
        "rejected": len(rejected_rows),
        "min_followers": MIN_FOLLOWERS,
        "max_followers": MAX_FOLLOWERS,
    }
    print(f"[QUALIFIER] COMPLETE: {summary}", flush=True)
    return summary


def get_dataset_id_from_payload(payload):
    resource = payload.get("resource")
    candidates = []
    if isinstance(resource, dict):
        candidates.append(resource.get("defaultDatasetId"))
    candidates += [payload.get("defaultDatasetId"), payload.get("datasetId")]
    return next((clean_text(x) for x in candidates if clean_text(x)), "")


def get_actor_run_id(payload):
    event_data = payload.get("eventData")
    resource = payload.get("resource")
    candidates = []
    if isinstance(event_data, dict):
        candidates += [event_data.get("actorRunId"), event_data.get("runId")]
    if isinstance(resource, dict):
        candidates += [resource.get("id"), resource.get("actorRunId")]
    candidates += [payload.get("actorRunId"), payload.get("runId")]
    return next((clean_text(x) for x in candidates if clean_text(x)), "")


def fetch_actor_run(run_id):
    if not APIFY_TOKEN:
        raise RuntimeError("APIFY_TOKEN is not configured in Railway Variables")
    response = requests.get(
        f"https://api.apify.com/v2/actor-runs/{run_id}",
        params={"token": APIFY_TOKEN},
        timeout=60,
    )
    print(f"[APIFY] Run lookup HTTP {response.status_code}", flush=True)
    response.raise_for_status()
    result = response.json()
    return result.get("data", result) if isinstance(result, dict) else {}


def resolve_dataset_id(payload):
    dataset_id = get_dataset_id_from_payload(payload)
    if dataset_id:
        return dataset_id, ""
    run_id = get_actor_run_id(payload)
    if not run_id:
        raise RuntimeError("Webhook did not contain defaultDatasetId or actorRunId")
    print(f"[APIFY] Run ID: {run_id}", flush=True)
    run_data = fetch_actor_run(run_id)
    dataset_id = clean_text(run_data.get("defaultDatasetId"))
    if not dataset_id:
        raise RuntimeError(f"Actor run {run_id} does not contain defaultDatasetId")
    print(f"[APIFY] Resolved dataset: {dataset_id}", flush=True)
    return dataset_id, run_id


def fetch_apify_dataset(dataset_id):
    if not APIFY_TOKEN:
        raise RuntimeError("APIFY_TOKEN is not configured in Railway Variables")
    response = requests.get(
        f"https://api.apify.com/v2/datasets/{dataset_id}/items",
        params={"token": APIFY_TOKEN, "clean": "true", "format": "json"},
        timeout=120,
    )
    print(f"[APIFY] Dataset request HTTP {response.status_code}", flush=True)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError("Apify dataset response was not a JSON list")
    print(f"[APIFY] Records downloaded: {len(data)}", flush=True)
    return data


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
        "status": "online",
        "apify_token_configured": bool(APIFY_TOKEN),
        "min_followers": MIN_FOLLOWERS,
        "max_followers": MAX_FOLLOWERS,
    })


@app.post("/apify-webhook")
def apify_webhook():
    print("[WEBHOOK] APIFY WEBHOOK RECEIVED", flush=True)
    payload = request.get_json(silent=True) or {}
    print(f"[WEBHOOK] Event type: {clean_text(payload.get('eventType'))}", flush=True)
    try:
        dataset_id, run_id = resolve_dataset_id(payload)
        items = fetch_apify_dataset(dataset_id)
        summary = process_items(items)
        print("[WEBHOOK] SUCCESS", flush=True)
        return jsonify({"ok": True, "run_id": run_id, "dataset_id": dataset_id, **summary})
    except Exception as exc:
        app.logger.exception("Apify webhook processing failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/download/qualified")
def download_qualified():
    if not QUALIFIED_FILE.exists():
        return jsonify({"ok": False, "error": "No qualified CSV has been generated yet"}), 404
    return send_file(QUALIFIED_FILE, as_attachment=True, download_name="diddyoil_qualified.csv")


@app.get("/download/rejected")
def download_rejected():
    if not REJECTED_FILE.exists():
        return jsonify({"ok": False, "error": "No rejected CSV has been generated yet"}), 404
    return send_file(REJECTED_FILE, as_attachment=True, download_name="diddyoil_rejected.csv")


@app.post("/process-json")
def process_json():
    items = request.get_json(silent=True)
    if not isinstance(items, list):
        return jsonify({"ok": False, "error": "Expected a JSON array of Actor dataset items"}), 400
    return jsonify({"ok": True, **process_items(items)})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
