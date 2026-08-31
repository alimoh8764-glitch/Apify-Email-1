import os
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request, HTTPException
from github import Github, GithubException


app = FastAPI()


# =========================================================
# RAILWAY ENVIRONMENT VARIABLES
# =========================================================

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_FOLDER = os.getenv("GITHUB_FOLDER", "data")


# Give clear errors if required variables are missing
if not GITHUB_TOKEN:
    raise RuntimeError(
        "GITHUB_TOKEN is missing. "
        "Add GITHUB_TOKEN in Railway -> Variables."
    )

if not GITHUB_REPO:
    raise RuntimeError(
        "GITHUB_REPO is missing. "
        "Add GITHUB_REPO in Railway -> Variables. "
        "Example: username/repository"
    )


# Connect to GitHub
github = Github(GITHUB_TOKEN)


# =========================================================
# CLEAN APIFY DATA
# =========================================================

def clean_data(data: Any) -> Any:
    """
    Recursively:
    - Remove None values
    - Remove empty strings
    - Remove empty lists
    - Remove empty dictionaries
    - Strip whitespace from strings
    """

    if isinstance(data, dict):
        cleaned = {}

        for key, value in data.items():
            value = clean_data(value)

            if value not in (None, "", [], {}):
                cleaned[key] = value

        return cleaned

    if isinstance(data, list):
        cleaned = [clean_data(item) for item in data]

        return [
            item
            for item in cleaned
            if item not in (None, "", [], {})
        ]

    if isinstance(data, str):
        return data.strip()

    return data


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/")
def health():
    return {
        "status": "ok",
        "service": "Apify Webhook Receiver"
    }


# =========================================================
# APIFY WEBHOOK
# =========================================================

@app.post("/webhook")
async def apify_webhook(request: Request):

    # Get JSON sent by Apify
    try:
        payload = await request.json()

    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON"
        )


    # Clean the data
    cleaned_data = clean_data(payload)


    # Generate unique filename
    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d_%H-%M-%S-%f")

    filename = (
        f"{GITHUB_FOLDER}/apify_{timestamp}.json"
    )


    # Convert cleaned data to formatted JSON
    file_content = json.dumps(
        cleaned_data,
        indent=2,
        ensure_ascii=False,
        default=str,
    )


    # Push file to GitHub
    try:

        repo = github.get_repo(GITHUB_REPO)

        repo.create_file(
            path=filename,
            message=f"Add Apify webhook data {timestamp}",
            content=file_content,
            branch=GITHUB_BRANCH,
        )


    except GithubException as exc:

        print(f"GitHub error: {exc}")

        raise HTTPException(
            status_code=500,
            detail=f"GitHub error: {exc.data}",
        )


    # Tell Apify everything worked
    return {
        "success": True,
        "file": filename,
        "repository": GITHUB_REPO,
        "records": (
            len(cleaned_data)
            if isinstance(cleaned_data, list)
            else 1
        ),
    }
