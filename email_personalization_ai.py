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
