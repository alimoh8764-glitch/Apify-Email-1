import os
import re
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna")


# ============================================================
# BASIC CLEANING
# ============================================================

def clean_value(value):
    if value is None:
        return ""

    value = str(value).strip()

    if value.lower() in {"nan", "none", "null"}:
        return ""

    return re.sub(r"\s+", " ", value)


def get_first_name(full_name):
    full_name = clean_value(full_name)

    if not full_name:
        return ""

    full_name = re.sub(
        r"^(mr\.?|mrs\.?|ms\.?|miss|dr\.?)\s+",
        "",
        full_name,
        flags=re.IGNORECASE,
    )

    return full_name.split()[0].title()


def first_value(row, *keys):
    """
    Return the first non-empty value from several possible
    Apify field names.
    """
    for key in keys:
        value = row.get(key)

        if value is not None and clean_value(value):
            return clean_value(value)

    return ""


# ============================================================
# CONTACT MATCHING
# ============================================================

def get_contact(row):
    """
    CRITICAL RULE:

    Never blindly combine an advertiser's name with an
    agent's email.

    Keep known name/email pairs together.
    """

    agent_name = first_value(
        row,
        "agents/0/agent_name",
        "agents.0.agent_name",
        "agent_name",
    )

    agent_email = first_value(
        row,
        "agents/0/email",
        "agents.0.email",
        "agent_email",
    )

    advertiser_name = first_value(
        row,
        "advertisers/0/name",
        "advertisers.0.name",
        "advertiser_name",
    )

    advertiser_email = first_value(
        row,
        "advertisers/0/email",
        "advertisers.0.email",
        "advertiser_email",
    )

    # Preferred pair
    if agent_name and agent_email:
        return {
            "first_name": get_first_name(agent_name),
            "full_name": agent_name,
            "email": agent_email,
            "contact_source": "agent",
        }

    # Second preferred pair
    if advertiser_name and advertiser_email:
        return {
            "first_name": get_first_name(advertiser_name),
            "full_name": advertiser_name,
            "email": advertiser_email,
            "contact_source": "advertiser",
        }

    # Name without verified email is safer than mismatching people.
    if agent_name:
        return {
            "first_name": get_first_name(agent_name),
            "full_name": agent_name,
            "email": "",
            "contact_source": "agent_name_only",
        }

    if advertiser_name:
        return {
            "first_name": get_first_name(advertiser_name),
            "full_name": advertiser_name,
            "email": "",
            "contact_source": "advertiser_name_only",
        }

    return {
        "first_name": "",
        "full_name": "",
        "email": "",
        "contact_source": "",
    }


# ============================================================
# PROPERTY FIELDS
# ============================================================

def get_property_address(row):
    return first_value(
        row,
        "address/street",
        "address.street",
        "street",
        "property_address",
        "address",
    )


def get_city(row):
    return first_value(
        row,
        "address/locality",
        "address.locality",
        "city",
    )


def get_price(row):
    return first_value(
        row,
        "list_price",
        "listPrice",
        "price",
    )


def get_description(row):
    return first_value(
        row,
        "text",
        "description",
        "public_remarks",
        "publicRemarks",
        "remarks",
    )


# ============================================================
# AI PERSONALIZATION
# ============================================================

PERSONALIZATION_INSTRUCTIONS = """
You write one short personalized sentence for real-estate
agent outreach.

You will receive the public listing description for ONE property.

Your job is to identify ONE concrete, distinctive feature that
proves the listing was actually looked at.

GOOD FEATURES:
- wired shed or workshop
- detached barn
- finished apartment above a garage
- unusual loft
- private dock
- screened porch
- saltwater pool
- four-season garden
- goldfish pond
- outdoor kitchen
- original stone fireplace
- wraparound porch
- rooftop deck
- unusually large workshop
- guest house
- wine cellar
- boat lift
- putting green
- distinctive architectural feature

BAD FEATURES:
- great location
- beautiful home
- spacious property
- amazing opportunity
- open floor plan
- lots of potential
- updated home
- nice kitchen
- good neighborhood

Those generic observations do NOT prove somebody actually
read the listing.

OUTPUT STYLE:

Write naturally like a person sending a quick cold email.

The sentence should normally follow this idea:

"That [specific feature] on {{address}} caught my eye. [Short,
natural positive reaction.]"

But vary the language naturally.

Examples of acceptable tone:

"That wired shed on {{address}} caught my eye. Solid bonus for
a place like that."

"That finished apartment above the barn on {{address}} really
stood out. That's a pretty useful selling point."

"The private dock on {{address}} caught my attention. Definitely
a nice feature to have for buyers looking around there."

"That four-season garden on {{address}} is a great touch. The
goldfish pond makes it even more memorable."

IMPORTANT RULES:

1. ALWAYS write {{address}} literally.
2. Do NOT replace {{address}} with the real address.
3. Do NOT mention the recipient's name.
4. Pick details ONLY from the supplied listing description.
5. Never invent a feature.
6. Do not use salesy corporate language.
7. Do not use exclamation marks.
8. Do not say "I noticed your listing."
9. Do not say "I came across your listing."
10. Do not say "impressive."
11. Keep it conversational.
12. Keep the entire output under 35 words.
13. Output ONLY the personalized line.
14. No quotation marks.
15. No explanation.
16. If the description contains no sufficiently specific feature,
    output exactly: SKIP
"""


def create_personalized_line(description):
    """
    Ask GPT-5.6 Luna to select one genuine property feature
    and turn it into a short outreach line.
    """

    description = clean_value(description)

    if not description:
        return ""

    # Avoid unnecessarily enormous listing descriptions.
    description = description[:6000]

    try:
        response = client.responses.create(
            model=MODEL,
            reasoning={
                "effort": "low"
            },
            instructions=PERSONALIZATION_INSTRUCTIONS,
            input=f"""
PROPERTY LISTING DESCRIPTION:

{description}
""",
            max_output_tokens=100,
        )

        line = clean_value(response.output_text)

        if not line:
            return ""

        if line.upper() == "SKIP":
            return ""

        # Safety check:
        # AI must preserve our Instantly variable.
        if "{{address}}" not in line:
            return ""

        # Remove accidental quotation marks.
        line = line.strip('"').strip("'").strip()

        return line

    except Exception as e:
        print(f"AI personalization error: {e}")
        return ""


# ============================================================
# PROCESS PROPERTY
# ============================================================

def process_property(row):
    contact = get_contact(row)

    address = get_property_address(row)
    city = get_city(row)
    price = get_price(row)
    description = get_description(row)

    personalized_line = create_personalized_line(description)

    return {
        "first_name": contact["first_name"],
        "full_name": contact["full_name"],
        "email": contact["email"],

        "property_address": address,
        "city": city,
        "property_price": price,

        "property_description": description,

        # Ready for Instantly:
        "personalized_line": personalized_line,

        "contact_source": contact["contact_source"],
    }


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/", methods=["GET"])
def health_check():
    return jsonify({
        "status": "ok",
        "service": "realtor-personalization-engine",
        "model": MODEL,
    })


# ============================================================
# TEST ONE DESCRIPTION
# ============================================================

@app.route("/test-personalization", methods=["POST"])
def test_personalization():
    """
    Handy while we're developing.

    POST:
    {
        "description": "Beautiful property featuring..."
    }

    Returns just the generated line.
    """

    payload = request.get_json(silent=True) or {}

    description = clean_value(payload.get("description"))

    if not description:
        return jsonify({
            "error": "description is required"
        }), 400

    line = create_personalized_line(description)

    return jsonify({
        "personalized_line": line
    })


# ============================================================
# PROCESS APIFY PAYLOAD
# ============================================================

@app.route("/process", methods=["POST"])
def process_payload():

    payload = request.get_json(silent=True)

    if payload is None:
        return jsonify({
            "error": "No JSON payload received"
        }), 400

    # Apify might send a raw list.
    if isinstance(payload, list):
        items = payload

    # Or an object containing items.
    elif isinstance(payload, dict) and isinstance(
        payload.get("items"), list
    ):
        items = payload["items"]

    # Or one property.
    elif isinstance(payload, dict):
        items = [payload]

    else:
        return jsonify({
            "error": "Unsupported payload format"
        }), 400

    cleaned_properties = []

    for item in items:
        try:
            result = process_property(item)
            cleaned_properties.append(result)

        except Exception as e:
            print(f"Property processing error: {e}")

    return jsonify({
        "success": True,
        "received": len(items),
        "processed": len(cleaned_properties),
        "data": cleaned_properties,
    })


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    port = int(os.environ.get("PORT", 8080))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )
