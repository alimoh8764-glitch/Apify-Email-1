import csv
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_file

app = Flask(**name**)

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "").strip()

OUTPUT_DIR = Path(
os.getenv("OUTPUT_DIR", "/tmp/fb_qualifier")
)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MIN_FOLLOWERS = int(
os.getenv("MIN_FOLLOWERS", "100")
)

MAX_FOLLOWERS = int(
os.getenv("MAX_FOLLOWERS", "600")
)

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

# =========================================================

# CLEANING

# =========================================================

def clean_text(value):
if value is None:
return ""

```
text = (
    str(value)
    .replace("\ufeff", "")
    .replace("\u200b", "")
    .strip()
)

if text.lower() in {
    "nan",
    "none",
    "null",
}:
    return ""

return text
```

def clean_facebook_url(value):
text = clean_text(value)

```
if not text:
    return ""

# Handle accidental Markdown links.
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

return (
    match.group(0)
    .strip()
    .strip("\"'<>[]()")
)
```

# =========================================================

# FOLLOWER PARSING

# =========================================================

def parse_followers(value):
"""
Examples:

```
347      -> 347
"347"    -> 347
"1,234"  -> 1234
"1.2K"   -> 1200
"2M"     -> 2000000

Unknown/unparseable -> None
"""

if value is None:
    return None

if isinstance(value, bool):
    return None

if isinstance(value, (int, float)):
    try:
        if value != value:
            return None

        return int(value)

    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return None

text = (
    clean_text(value)
    .lower()
    .replace(",", "")
)

match = re.search(
    r"(\d+(?:\.\d+)?)\s*([kmb])?",
    text,
)

if not match:
    return None

try:
    number = float(
        match.group(1)
    )
except ValueError:
    return None

multiplier = {
    "k": 1_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
}.get(
    match.group(2),
    1,
)

return int(
    number * multiplier
)
```

# =========================================================

# RECORD FORMATTING

# =========================================================

def format_record(item):
followers = parse_followers(
item.get("followers")
)

```
qualified = (
    followers is not None
    and MIN_FOLLOWERS
    <= followers
    <= MAX_FOLLOWERS
)

row = {
    "Goofyurls": clean_facebook_url(
        item.get("url")
        or item.get("response_url")
    ),

    "goofyaddress": clean_text(
        item.get("address")
    ),

    # The Apify Actor gives us the Facebook
    # page/company name, not a guaranteed person's
    # first name. Do not invent one.
    "goofyfirstnames": "",

    "email": clean_text(
        item.get("email")
    ),

    "fb_followers": (
        ""
        if followers is None
        else followers
    ),

    "fb_qualification": (
        "qualified"
        if qualified
        else "rejected"
    ),

    "fb_checked_at": (
        datetime.now(
            timezone.utc
        ).isoformat()
    ),

    "facebook_name": clean_text(
        item.get("name")
    ),

    "phone": clean_text(
        item.get("phone")
    ),

    "website": clean_text(
        item.get("website")
    ),

    "page_id": clean_text(
        item.get("page_id")
    ),

    "category_name": clean_text(
        item.get("category_name")
    ),
}

return row, qualified
```

# =========================================================

# CSV

# =========================================================

def write_csv(path, rows):
temp_path = path.with_suffix(
path.suffix + ".tmp"
)

```
with temp_path.open(
    "w",
    newline="",
    encoding="utf-8",
) as file:

    writer = csv.DictWriter(
        file,
        fieldnames=OUTPUT_COLUMNS,
        extrasaction="ignore",
    )

    writer.writeheader()
    writer.writerows(rows)

os.replace(
    temp_path,
    path,
)
```

def process_items(items):
qualified_rows = []
rejected_rows = []

```
for item in items:

    if not isinstance(
        item,
        dict,
    ):
        continue

    row, qualified = (
        format_record(item)
    )

    if qualified:
        qualified_rows.append(
            row
        )
    else:
        rejected_rows.append(
            row
        )

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
    "qualified": len(
        qualified_rows
    ),
    "rejected": len(
        rejected_rows
    ),
    "min_followers": MIN_FOLLOWERS,
    "max_followers": MAX_FOLLOWERS,
}

print(
    f"[QUALIFIER] COMPLETE: {summary}",
    flush=True,
)

return summary
```

# =========================================================

# APIFY HELPERS

# =========================================================

def get_dataset_id_from_payload(
payload,
):
"""
If Apify gives us defaultDatasetId
directly in the webhook, use it.
"""

```
resource = payload.get(
    "resource"
)

candidates = []

if isinstance(
    resource,
    dict,
):
    candidates.append(
        resource.get(
            "defaultDatasetId"
        )
    )

candidates.extend([
    payload.get(
        "defaultDatasetId"
    ),
    payload.get(
        "datasetId"
    ),
])

for candidate in candidates:

    candidate = clean_text(
        candidate
    )

    if candidate:
        return candidate

return ""
```

def get_actor_run_id(payload):
"""
Extract actorRunId from Apify's webhook.

```
Supports both eventData.actorRunId
and resource.id.
"""

event_data = payload.get(
    "eventData"
)

resource = payload.get(
    "resource"
)

candidates = []

if isinstance(
    event_data,
    dict,
):
    candidates.extend([
        event_data.get(
            "actorRunId"
        ),
        event_data.get(
            "runId"
        ),
    ])

if isinstance(
    resource,
    dict,
):
    candidates.extend([
        resource.get("id"),
        resource.get(
            "actorRunId"
        ),
    ])

candidates.extend([
    payload.get(
        "actorRunId"
    ),
    payload.get(
        "runId"
    ),
])

for candidate in candidates:

    candidate = clean_text(
        candidate
    )

    if candidate:
        return candidate

return ""
```

def fetch_actor_run(run_id):
"""
Retrieve Actor run information
from Apify so we can obtain its
defaultDatasetId.
"""

```
if not APIFY_TOKEN:
    raise RuntimeError(
        "APIFY_TOKEN is not configured "
        "in Railway Variables"
    )

url = (
    "https://api.apify.com/v2/"
    f"actor-runs/{run_id}"
)

print(
    f"[APIFY] Looking up run: {run_id}",
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
    "[APIFY] Run lookup "
    f"HTTP {response.status_code}",
    flush=True,
)

response.raise_for_status()

result = response.json()

if not isinstance(
    result,
    dict,
):
    raise RuntimeError(
        "Unexpected response from "
        "Apify Actor run API"
    )

data = result.get("data")

if isinstance(
    data,
    dict,
):
    return data

return result
```

def resolve_dataset_id(payload):
"""
First try to obtain defaultDatasetId
directly from the webhook.

```
If it isn't there:

    actorRunId
         ↓
    Apify API
         ↓
    defaultDatasetId
"""

dataset_id = (
    get_dataset_id_from_payload(
        payload
    )
)

if dataset_id:

    print(
        "[APIFY] Webhook contained "
        f"dataset ID: {dataset_id}",
        flush=True,
    )

    return (
        dataset_id,
        "",
    )

run_id = get_actor_run_id(
    payload
)

if not run_id:
    raise RuntimeError(
        "Webhook did not contain "
        "defaultDatasetId or actorRunId"
    )

print(
    f"[APIFY] Run ID: {run_id}",
    flush=True,
)

run_data = fetch_actor_run(
    run_id
)

dataset_id = clean_text(
    run_data.get(
        "defaultDatasetId"
    )
)

if not dataset_id:
    raise RuntimeError(
        f"Actor run {run_id} "
        "does not contain "
        "defaultDatasetId"
    )

print(
    "[APIFY] Resolved dataset: "
    f"{dataset_id}",
    flush=True,
)

return (
    dataset_id,
    run_id,
)
```

def fetch_apify_dataset(
dataset_id,
):
"""
Download the completed Actor dataset.
"""

```
if not APIFY_TOKEN:
    raise RuntimeError(
        "APIFY_TOKEN is not configured "
        "in Railway Variables"
    )

url = (
    "https://api.apify.com/v2/"
    f"datasets/{dataset_id}/items"
)

print(
    "[APIFY] Downloading dataset: "
    f"{dataset_id}",
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
    "[APIFY] Dataset request "
    f"HTTP {response.status_code}",
    flush=True,
)

response.raise_for_status()

data = response.json()

if not isinstance(
    data,
    list,
):
    raise RuntimeError(
        "Apify dataset response "
        "was not a JSON list"
    )

print(
    "[APIFY] Records downloaded: "
    f"{len(data)}",
    flush=True,
)

return data
```

# =========================================================

# ROUTES

# =========================================================

@app.get("/")
def root():
return jsonify({
"ok": True,
"service": (
"Facebook Apify Qualifier"
),
"status": "online",
"min_followers": (
MIN_FOLLOWERS
),
"max_followers": (
MAX_FOLLOWERS
),
})

@app.get("/health")
def health():
return jsonify({
"ok": True,
"status": "online",
"apify_token_configured": bool(
APIFY_TOKEN
),
"min_followers": (
MIN_FOLLOWERS
),
"max_followers": (
MAX_FOLLOWERS
),
})

@app.post("/apify-webhook")
def apify_webhook():

```
print(
    "================================",
    flush=True,
)

print(
    "[WEBHOOK] APIFY WEBHOOK RECEIVED",
    flush=True,
)

payload = (
    request.get_json(
        silent=True
    )
    or {}
)

event_type = clean_text(
    payload.get(
        "eventType"
    )
)

print(
    "[WEBHOOK] Event type: "
    f"{event_type}",
    flush=True,
)

event_data = payload.get(
    "eventData"
)

if isinstance(
    event_data,
    dict,
):

    print(
        "[WEBHOOK] actorId: "
        f"{event_data.get('actorId')}",
        flush=True,
    )

    print(
        "[WEBHOOK] actorRunId: "
        f"{event_data.get('actorRunId')}",
        flush=True,
    )

try:

    dataset_id, run_id = (
        resolve_dataset_id(
            payload
        )
    )

    items = (
        fetch_apify_dataset(
            dataset_id
        )
    )

    summary = process_items(
        items
    )

    print(
        "[WEBHOOK] SUCCESS",
        flush=True,
    )

    print(
        "================================",
        flush=True,
    )

    return jsonify({
        "ok": True,
        "run_id": run_id,
        "dataset_id": (
            dataset_id
        ),
        **summary,
    })

except Exception as exc:

    app.logger.exception(
        "Apify webhook "
        "processing failed"
    )

    print(
        "[WEBHOOK] FAILED: "
        f"{exc}",
        flush=True,
    )

    print(
        "================================",
        flush=True,
    )

    return jsonify({
        "ok": False,
        "error": str(exc),
    }), 500
```

@app.get("/download/qualified")
def download_qualified():

```
if not QUALIFIED_FILE.exists():

    return jsonify({
        "ok": False,
        "error": (
            "No qualified CSV has "
            "been generated yet"
        ),
    }), 404

return send_file(
    QUALIFIED_FILE,
    as_attachment=True,
    download_name=(
        "diddyoil_qualified.csv"
    ),
)
```

@app.get("/download/rejected")
def download_rejected():

```
if not REJECTED_FILE.exists():

    return jsonify({
        "ok": False,
        "error": (
            "No rejected CSV has "
            "been generated yet"
        ),
    }), 404

return send_file(
    REJECTED_FILE,
    as_attachment=True,
    download_name=(
        "diddyoil_rejected.csv"
    ),
)
```

# =========================================================

# MANUAL JSON TEST

# =========================================================

@app.post("/process-json")
def process_json():

```
items = request.get_json(
    silent=True
)

if not isinstance(
    items,
    list,
):

    return jsonify({
        "ok": False,
        "error": (
            "Expected a JSON array "
            "of Actor dataset items"
        ),
    }), 400

summary = process_items(
    items
)

return jsonify({
    "ok": True,
    **summary,
})
```

# =========================================================

# LOCAL START

# =========================================================

if **name** == "**main**":

```
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
