import os
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request, HTTPException
from github import Github, GithubException


app = FastAPI()

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ["GITHUB_REPO"]  # example: username/my-data-repo
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_FOLDER = os.getenv("GITHUB_FOLDER", "data")

github = Github(GITHUB_TOKEN)


def clean_data(data: Any) -> Any:
    """
    Recursively remove:
    - None
    - empty strings
    - empty lists
    - empty dictionaries
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
        return [item for item in cleaned if item not in (None, "", [], {})]

    if isinstance(data, str):
        return data.strip()

    return data


@app.post("/webhook")
async def apify_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    cleaned_data = clean_data(payload)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S-%f")
    filename = f"{GITHUB_FOLDER}/apify_{timestamp}.json"

    file_content = json.dumps(
        cleaned_data,
        indent=2,
        ensure_ascii=False,
        default=str,
    )

    try:
        repo = github.get_repo(GITHUB_REPO)

        repo.create_file(
            path=filename,
            message=f"Add Apify webhook data {timestamp}",
            content=file_content,
            branch=GITHUB_BRANCH,
        )

    except GithubException as exc:
        raise HTTPException(
            status_code=500,
            detail=f"GitHub error: {exc.data}",
        )

    return {
        "success": True,
        "file": filename,
        "records": len(cleaned_data) if isinstance(cleaned_data, list) else 1,
    }


@app.get("/")
def health():
    return {"status": "ok"}
