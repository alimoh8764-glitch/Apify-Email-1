import os
import re
from typing import Any

from flask import Flask, jsonify, request
from openai import OpenAI


# ============================================================
# CONFIG
# ============================================================

app = Flask(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

if not OPENAI_API_KEY:
    raise RuntimeError(
        "OPENAI_API_KEY environment variable is missing."
    )

client = OpenAI(api_key=OPENAI_API_KEY)

# You can change the model in Railway without editing the code.
OPENAI_MODEL = os.environ.get(
    "OPENAI_MODEL",
    "gpt-5.6-luna",
)


# ============================================================
# EXACT APIFY COLUMNS FROM YOUR DATA
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
# BASIC CLEANING
# ============================================================

def clean_value(value: Any) -> str:
    """Convert null/NaN-like values to a clean string."""

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
    """Basic email validation."""

    email = clean_value(email)

    if not email:
        return False

    pattern = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

    return bool(re.match(pattern, email))


# ============================================================
# NAME CLEANING
# ============================================================

def get_first_name(full_name: str) -> str:
    """
    Convert:
        John Smith
    into:
        John
    """

    full_name = clean_value(full_name)

    if not full_name:
        return ""

    # Remove common prefixes.
    full_name = re.sub(
        r"^(mr\.?|mrs\.?|ms\.?|miss|dr\.?)\s+",
        "",
        full_name,
        flags=re.IGNORECASE,
    )

    first_name = full_name.split()[0]

    # Remove odd punctuation around name.
    first_name = re.sub(
        r"[^A-Za-zÀ-ÖØ-öø-ÿ'\-]",
        "",
        first_name,
    )

    if not first_name:
        return ""

    return first_name.title()


# ============================================================
# CONTACT SELECTION
# ============================================================

def get_contact(row: dict) -> dict:
    """
    IMPORTANT:

    We NEVER blindly combine an agent email with an advertiser
    name or vice versa.

    Priority:

    1. Agent name + agent email
    2. Advertiser name + advertiser email
    3. Name only

    It is better to have a blank email than send an email using
    another person's first name.
    """

    agent_name = clean_value(row.get(AGENT_NAME))
    agent_email = clean_value(row.get(AGENT_EMAIL))

    advertiser_name = clean_value(
        row.get(ADVERTISER_NAME)
    )

    advertiser_email = clean_value(
        row.get(ADVERTISER_EMAIL)
    )

    # -----------------------------
    # Agent pair
    # -----------------------------

    if agent_name and valid_email(agent_email):

        return {
            "first_name": get_first_name(agent_name),
            "full_name": agent_name,
            "email": agent_email.lower(),
            "contact_source": "agent",
        }

    # -----------------------------
    # Advertiser pair
    # -----------------------------

    if advertiser_name and valid_email(
        advertiser_email
    ):

        return {
            "first_name": get_first_name(
                advertiser_name
            ),
            "full_name": advertiser_name,
            "email": advertiser_email.lower(),
            "contact_source": "advertiser",
        }

    # -----------------------------
    # Name only
    # -----------------------------

    if agent_name:

        return {
            "first_name": get_first_name(agent_name),
            "full_name": agent_name,
            "email": "",
            "contact_source": "agent_name_only",
        }

    if advertiser_name:

        return {
            "first_name": get_first_name(
                advertiser_name
            ),
            "full_name": advertiser_name,
            "email": "",
            "contact_source":
                "advertiser_name_only",
        }

    return {
        "first_name": "",
        "full_name": "",
        "email": "",
        "contact_source": "",
    }


# ============================================================
# ADDRESS CLEANING
# ============================================================

def clean_address(address: str) -> str:
    """
    We use address/street from Apify rather than the complete
    postal address.

    Example:

        123 North Bay, Calgary, AB E126QP

    becomes:

        123 North Bay

    Normally address/street is already the short version.
    """

    address = clean_value(address)

    if not address:
        return ""

    # If commas somehow exist in the street field,
    # only keep the street portion.
    if "," in address:
        address = address.split(",")[0].strip()

    return address


# ============================================================
# PRICE FORMATTING
# ============================================================

def format_price(price: Any) -> str:
    """
    Examples:

        425000
        -> $425,000

        1200000
        -> $1,200,000

        $725000
        -> $725,000
    """

    price = clean_value(price)

    if not price:
        return ""

    # Remove existing formatting.
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

You will receive the PUBLIC REMARKS / PROPERTY DESCRIPTION
for one real-estate listing.

Your job is to find ONE genuinely specific feature from the
description that proves someone actually paid attention to
the property.

Then write one warm, casual sentence or two about it.


GOOD FEATURES INCLUDE:

- wired shed
- detached workshop
- finished room above a garage
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
- distinctive view
- unusual outdoor feature


AVOID GENERIC FEATURES SUCH AS:

- beautiful home
- great location
- spacious property
- open floor plan
- nice kitchen
- updated home
- large bedrooms
- great opportunity
- lots of natural light
- desirable neighborhood

Generic observations do not prove the listing was actually
read.


ADDRESS RULE:

You MUST literally use:

{{address}}

Do NOT attempt to write the actual street address.

The software will replace {{address}} later.


TONE:

Sound like a real person who quickly looked through the
listing.

Warm.
Casual.
Short.
Natural.

Do not sound like marketing copy.


GOOD EXAMPLES:

That wired shed on {{address}} caught my eye. Solid bonus for
a place like that.

That finished apartment above the barn on {{address}} really
stood out. That's a pretty useful feature to have.

The private dock on {{address}} caught my attention. Definitely
a nice touch for buyers looking around there.

That four-season garden on {{address}} is a great touch. The
goldfish pond makes it even more memorable.

That rooftop deck on {{address}} caught my eye. Pretty sweet
feature for enjoying the view.


IMPORTANT:

- Maximum 35 words.
- Pick only ONE main distinctive feature.
- Never invent anything.
- Never mention the recipient's name.
- Never use an exclamation mark.
- Never say "I noticed your listing".
- Never say "I came across your listing".
- Avoid words like "impressive" and "stunning".
- Do not sound overly enthusiastic.
- Do not mention being an AI.
- Do not output quotation marks.
- Do not explain your answer.
- Output ONLY the personalized line.

If there is no genuinely specific feature in the description,
output exactly:

SKIP
"""


# ============================================================
# AI PERSONALIZATION
# ============================================================

def create_personalized_line(
    description: str,
) -> str:

    description = clean_value(description)

    if not description:
        return ""

    # Keeps token usage under control if Apify returns an
    # abnormally long description.
    description = description[:6000]

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

        # Remove accidental quotes.
        line = line.strip('"').strip("'").strip()

        # Critical validation.
        # We WANT the literal Instantly variable.
        if "{{address}}" not in line:
            print(
                "AI line rejected because "
                "{{address}} was missing:",
                line,
            )

            return ""

        # Reject excessively long output.
        if len(line.split()) > 40:
            print(
                "AI line rejected because it was too long:",
                line,
            )

            return ""

        return line

    except Exception as error:

        print(
            f"OpenAI personalization error: {error}"
        )

        return ""


# ============================================================
# PROCESS ONE APIFY RECORD
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
        create_personalized_line(description)
    )

    return {

        # ------------------------------------
        # FINAL SHEET FIELDS
        # ------------------------------------

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

        # ------------------------------------
        # Useful debugging information
        # ------------------------------------

        "contact_source":
            contact["contact_source"],
    }


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/", methods=["GET"])
def health():

    return jsonify({
        "status": "ok",
        "service":
            "email-personalization-ai",
        "model":
            OPENAI_MODEL,
    })


# ============================================================
# TEST JUST THE AI
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
        "success": True,
        "personalized_line": line,
    })


# ============================================================
# APIFY ENDPOINT
# ============================================================

@app.route("/process", methods=["POST"])
def process_payload():

    payload = request.get_json(
        silent=True
    )

    if payload is None:

        return jsonify({
            "success": False,
            "error":
                "No JSON payload received",
        }), 400

    # ----------------------------------------
    # Payload can be:
    #
    # [ {...}, {...} ]
    #
    # OR
    #
    # { "items": [ {...}, {...} ] }
    #
    # OR one record:
    #
    # { ... }
    # ----------------------------------------

    if isinstance(payload, list):

        items = payload

    elif (
        isinstance(payload, dict)
        and isinstance(
            payload.get("items"),
            list,
        )
    ):

        items = payload["items"]

    elif isinstance(payload, dict):

        items = [payload]

    else:

        return jsonify({
            "success": False,
            "error":
                "Unsupported payload format",
        }), 400

    results = []
    failed = 0

    # ----------------------------------------
    # Process every property
    # ----------------------------------------

    for row in items:

        try:

            cleaned = process_property(
                row
            )

            results.append(cleaned)

        except Exception as error:

            failed += 1

            print(
                "Failed to process row:",
                error,
            )

    # ----------------------------------------
    # Stats
    # ----------------------------------------

    with_email = sum(
        1
        for row in results
        if row["email"]
    )

    with_personalization = sum(
        1
        for row in results
        if row["personalized_line"]
    )

    return jsonify({

        "success": True,

        "stats": {
            "received":
                len(items),

            "processed":
                len(results),

            "failed":
                failed,

            "with_email":
                with_email,

            "with_personalization":
                with_personalization,
        },

        "data":
            results,
    })


# ============================================================
# RUN LOCALLY
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
