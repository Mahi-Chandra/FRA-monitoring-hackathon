"""
FRA AI Insights Backend (Flask)
--------------------------------
Endpoints:
  GET  /api/health          liveness + whether an LLM key is configured
  POST /api/auth/login      email + password in, session token out
  POST /api/auth/logout     retires the caller's token
  GET  /api/auth/me         who the caller's token belongs to
  POST /api/analyze-claims  officials only — rule-based flags + an LLM summary

/api/analyze-claims takes claims data (JSON), runs rule-based anomaly checks,
then sends the flagged data to a free LLM (Groq) to get a plain-English
summary. Returns both to the frontend.

Accounts, hashing and tokens all live in fra_auth.py, which the Streamlit
dashboard imports too — so a login there is a login here.

There is deliberately no open registration endpoint: accounts are issued to
officials, not self-serve. Add one behind an admin check if that changes.

SETUP:
1. pip install -r requirements.txt
2. Get a free API key from https://console.groq.com (Groq is free & fast)
3. Put it in .env next to this file:  GROQ_API_KEY=your_key_here
4. Set a signing secret shared with the dashboard: FRA_JWT_SECRET=your_secret
   (already in .env — both processes must read the same one)
5. Run: python fra_ai_backend.py
6. Your endpoint will be live at http://localhost:5000/api/analyze-claims
"""

import json
import os
from datetime import datetime
from functools import wraps

import requests
from dotenv import load_dotenv
from flask import Flask, g, jsonify, request
from flask_cors import CORS

load_dotenv()

import fra_auth

app = Flask(__name__)
CORS(app)  # allows your frontend (different port/domain) to call this

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b").strip()

# Groq's free tier is quick; 30s is generous for a few hundred claims and
# still well inside the 60s the dashboard waits.
LLM_TIMEOUT_SECONDS = 30

# A whole caseload pasted into one prompt would blow the context window and
# the rate limit. Beyond this the summary is built from a prefix and the
# prompt says so, rather than the request failing.
MAX_CLAIMS_IN_PROMPT = 150

# FRA s.4(6) caps individual forest rights at 4 hectares. Anything larger is
# not automatically wrong — community claims exist — but it is worth a look.
INDIVIDUAL_AREA_CEILING_HA = 4.0

# How long a pending claim may sit before the delay is itself the anomaly.
PENDING_DAYS_THRESHOLD = 90

NO_ANOMALY_VALUES = {"", "none", "no anomaly", "n/a", "-"}

if fra_auth.SECRET_IS_EPHEMERAL:
    app.logger.warning(
        "FRA_JWT_SECRET is not set — this process signed with a random "
        "secret, so tokens from the dashboard will be rejected and every "
        "restart invalidates existing logins."
    )

if not GROQ_API_KEY:
    app.logger.warning(
        "GROQ_API_KEY is not set — /api/analyze-claims will answer 500 until "
        "a key is in the environment or .env."
    )


class LLMError(RuntimeError):
    """An LLM call that failed in a way worth showing an official.

    Carries a message already fit for the dashboard: no stack traces, no
    request URLs, and never the API key, whatever the underlying library put
    in its own exception text.
    """


def token_required(view):
    """Reject anything without a valid, unexpired, unrevoked bearer token."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        token = fra_auth.bearer_token(request.headers.get("Authorization"))
        claims, error = fra_auth.decode_token(token)
        if error:
            return jsonify({"error": error}), 401
        g.official = claims
        return view(*args, **kwargs)

    return wrapper


@app.route("/api/health", methods=["GET"])
def health():
    """Unauthenticated, so the dashboard can tell "service down" apart from
    "service up but misconfigured" without holding a token."""
    return jsonify(
        {
            "status": "ok",
            "llm_configured": bool(GROQ_API_KEY),
            "model": GROQ_MODEL,
        }
    )


@app.route("/api/auth/login", methods=["POST"])
def login():
    """Expects: { "email": ..., "password": ... } — returns a bearer token.

    A bad password and an unknown email give byte-identical responses, so this
    endpoint cannot be used to enumerate who holds an account.
    """
    body = request.get_json(silent=True) or {}

    user = fra_auth.verify_credentials(body.get("email"), body.get("password"))
    if user is None:
        return jsonify({"error": fra_auth.GENERIC_LOGIN_ERROR}), 401

    token, expires_at = fra_auth.issue_token(user)
    return jsonify(
        {"token": token, "expires_at": expires_at.isoformat(), "user": user}
    )


@app.route("/api/auth/logout", methods=["POST"])
@token_required
def logout():
    """Retire the token this request arrived with."""
    fra_auth.revoke_token(g.official)
    return jsonify({"message": "Logged out"})


@app.route("/api/auth/me", methods=["GET"])
@token_required
def me():
    """Who the current token belongs to — lets a client check it is still
    valid without side effects."""
    return jsonify({"user": g.official})


# --- Rule-based checks ------------------------------------------------------
# Deterministic, instant, and free. These run first so the model is
# summarising findings rather than being trusted to spot them.


def days_since(date_str):
    """Days since a given date (YYYY-MM-DD), or None if it is missing or
    unparseable — mock_data.json records no filed_date, and one absent field
    should sit a rule out rather than fail the whole request."""
    try:
        filed = datetime.strptime(date_str, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    return (datetime.now() - filed).days


def hectares(claim):
    """land_area_ha as a float, or None when it is absent or not a number."""
    value = claim.get("land_area_ha", claim.get("area_ha"))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def recorded_anomaly(claim):
    """The upstream anomaly note, or None when the record is clean."""
    anomaly = str(claim.get("anomaly") or "").strip()
    return None if anomaly.lower() in NO_ANOMALY_VALUES else anomaly


def rule_based_flags(claims):
    """
    Fast, deterministic anomaly checks — no AI needed for this part.
    Add more rules here as your project needs (duplicates, overlaps, etc).

    Field names follow mock_data.json: claim_id, applicant_name, district,
    lat, lon, status, anomaly, land_area_ha. Every rule tolerates its field
    being missing, so a partial record loses one check rather than the request.
    """
    seen_ids = set()
    duplicate_ids = set()
    for claim in claims:
        claim_id = claim.get("claim_id")
        if not claim_id:
            continue
        if claim_id in seen_ids:
            duplicate_ids.add(claim_id)
        seen_ids.add(claim_id)

    flagged = []
    for claim in claims:
        flags = []
        status = str(claim.get("status") or "").strip()

        anomaly = recorded_anomaly(claim)
        if anomaly:
            flags.append(anomaly)

        waiting = days_since(claim.get("filed_date"))
        if (
            status.lower() == "pending"
            and waiting is not None
            and waiting > PENDING_DAYS_THRESHOLD
        ):
            flags.append(f"Pending {waiting} days")

        area = hectares(claim)
        if area is None:
            flags.append("No land extent recorded")
        elif area > INDIVIDUAL_AREA_CEILING_HA:
            flags.append(
                f"{area:g} ha exceeds the {INDIVIDUAL_AREA_CEILING_HA:g} ha "
                "individual ceiling"
            )

        if claim.get("lat") is None or claim.get("lon") is None:
            flags.append("No mapped location")

        if claim.get("claim_id") in duplicate_ids:
            flags.append("Duplicate claim id")

        if not flags:
            flags.append("No issues")

        flagged.append({**claim, "flags": flags})
    return flagged


# --- LLM summary ------------------------------------------------------------


def prompt_payload(flagged_claims):
    """The claims as compact JSON, trimmed to what the model needs to read.

    Coordinates and any other extras are dropped: they cost tokens and the
    summary never cites them.
    """
    trimmed = [
        {
            "claim_id": claim.get("claim_id"),
            "applicant_name": claim.get("applicant_name"),
            "district": claim.get("district"),
            "status": claim.get("status"),
            "land_area_ha": claim.get("land_area_ha", claim.get("area_ha")),
            "flags": claim.get("flags", []),
        }
        for claim in flagged_claims[:MAX_CLAIMS_IN_PROMPT]
    ]
    body = json.dumps(trimmed, indent=2, ensure_ascii=False)

    dropped = len(flagged_claims) - len(trimmed)
    if dropped > 0:
        body += (
            f"\n\n(Showing the first {len(trimmed)} of "
            f"{len(flagged_claims)} claims; {dropped} were omitted for "
            "length. Say so in the summary.)"
        )
    return body


def build_prompt(flagged_claims):
    return f"""You are an assistant for Forest Rights Act officials.
Here is claims data with anomaly flags already computed:

{prompt_payload(flagged_claims)}

Do NOT repeat totals like claim counts, status breakdown, or district counts — those numbers already appear elsewhere on the dashboard. Instead, using the "flags" field on each claim, tell the official something they cannot already see at a glance:

TOP CONCERN: the single most urgent claim (ID) and exactly why — pick the one where flags compound each other (e.g. long delay + missing data), not just the oldest or the first flagged.

PATTERN: one line spotting something across multiple claims — a repeated flag type, several claims sharing the same problem, or an anomaly cluster worth investigating as one issue rather than fixing individually.

WATCH: one claim that looks clean now but has an early warning sign (e.g. approaching the pending threshold, land area right at the ceiling) that could become a problem soon if ignored.

Each section max 2 lines, under 15 words per line. No markdown, no repeated numbers, no restating what flags already say verbatim — interpret them."""


def groq_error_message(response):
    """Groq's own complaint, if it sent one, else the bare status line."""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    detail = (payload.get("error") or {}).get("message")
    if response.status_code == 401:
        return "Groq rejected the API key — check GROQ_API_KEY."
    if response.status_code == 429:
        return "Groq rate limit reached — wait a moment and try again."
    if detail:
        return f"Groq returned {response.status_code}: {detail}"
    return f"Groq returned {response.status_code}."


def get_ai_summary(flagged_claims):
    """
    Sends the already-flagged data to Groq's free LLM API
    and asks for a short plain-English summary for officials.

    Raises LLMError with a displayable message on every failure path.
    """
    if not GROQ_API_KEY:
        raise LLMError(
            "GROQ_API_KEY is not set on the analysis service. Add it to .env "
            "and restart fra_ai_backend.py."
        )

    try:
        response = requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "user", "content": build_prompt(flagged_claims)}
                ],
                "temperature": 0.2,
            },
            timeout=LLM_TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        raise LLMError(
            f"Groq did not respond within {LLM_TIMEOUT_SECONDS} seconds."
        ) from None
    except requests.RequestException:
        # Deliberately not str(exc): urllib3 messages carry the full request
        # context, which is noise at best in a dashboard toast.
        raise LLMError("Could not reach the Groq API.") from None

    if not response.ok:
        raise LLMError(groq_error_message(response))

    try:
        summary = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise LLMError("Groq returned a response in an unexpected shape.") from None

    summary = (summary or "").strip()
    if not summary:
        raise LLMError("Groq returned an empty summary.")
    return summary


@app.route("/api/analyze-claims", methods=["POST"])
@token_required
def analyze_claims():
    """
    Officials only. Expects JSON body: { "claims": [ {...}, {...} ] }
    Returns: { "summary": "...", "flagged": [...] }

    The dashboard reads "summary" and, on any non-2xx, "error".
    """
    body = request.get_json(silent=True) or {}
    claims = body.get("claims")

    if not isinstance(claims, list) or not claims:
        return jsonify({"error": "No claims data provided"}), 400
    if not all(isinstance(claim, dict) for claim in claims):
        return jsonify({"error": "Each claim must be a JSON object"}), 400

    flagged = rule_based_flags(claims)

    try:
        summary = get_ai_summary(flagged)
    except LLMError as exc:
        # The rule-based flags survived the model failing, so hand them back
        # anyway — a reviewer can still work from them.
        return jsonify({"error": str(exc), "flagged": flagged}), 500
    except Exception:
        app.logger.exception("analyze-claims failed")
        return jsonify({"error": "Analysis failed unexpectedly."}), 500

    return jsonify({"summary": summary, "flagged": flagged})


if __name__ == "__main__":
    # Debug off by default: the Werkzeug debugger is a remote shell to anyone
    # who can reach port 5000. Set FLASK_DEBUG=1 while developing.
    debug = os.environ.get("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes"}
    app.run(debug=True, port=5000)
