"""FRA AI Insights Backend - Flask API for claim analysis."""

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
CORS(app)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b").strip()

LLM_TIMEOUT_SECONDS = 30
MAX_CLAIMS_IN_PROMPT = 150

INDIVIDUAL_AREA_CEILING_HA = 4.0
PENDING_DAYS_THRESHOLD = 90

NO_ANOMALY_VALUES = {"", "none", "no anomaly", "n/a", "-"}

if fra_auth.SECRET_IS_EPHEMERAL:
    app.logger.warning(
        "FRA_JWT_SECRET not set — tokens from the dashboard will be rejected."
    )

if not GROQ_API_KEY:
    app.logger.warning(
        "GROQ_API_KEY not set — /api/analyze-claims will fail until configured."
    )


class LLMError(RuntimeError):
    """LLM error, safe to show in the dashboard."""
    pass


def token_required(view):
    """Reject requests without a valid bearer token."""
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
    """Service status and LLM configuration."""
    return jsonify(
        {
            "status": "ok",
            "llm_configured": bool(GROQ_API_KEY),
            "model": GROQ_MODEL,
        }
    )


@app.route("/api/auth/login", methods=["POST"])
def login():
    """Authenticate with email + password, return bearer token."""
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
    """Retire the current token."""
    fra_auth.revoke_token(g.official)
    return jsonify({"message": "Logged out"})


@app.route("/api/auth/me", methods=["GET"])
@token_required
def me():
    """Return the current user."""
    return jsonify({"user": g.official})


def days_since(date_str):
    """Days elapsed since YYYY-MM-DD, or None if missing."""
    try:
        filed = datetime.strptime(date_str, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    return (datetime.now() - filed).days


def hectares(claim):
    """Land area as float, or None if missing."""
    value = claim.get("land_area_ha", claim.get("area_ha"))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def recorded_anomaly(claim):
    """Anomaly note from the claim, or None if clean."""
    anomaly = str(claim.get("anomaly") or "").strip()
    return None if anomaly.lower() in NO_ANOMALY_VALUES else anomaly


def rule_based_flags(claims):
    """Fast, deterministic anomaly checks."""
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
                f"{area:g} ha exceeds the {INDIVIDUAL_AREA_CEILING_HA:g} ha ceiling"
            )

        if claim.get("lat") is None or claim.get("lon") is None:
            flags.append("No mapped location")

        if claim.get("claim_id") in duplicate_ids:
            flags.append("Duplicate claim id")

        if not flags:
            flags.append("No issues")

        flagged.append({**claim, "flags": flags})
    return flagged


def prompt_payload(flagged_claims):
    """Trimmed claims data for the LLM prompt."""
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
            f"\n\n(Showing first {len(trimmed)} of "
            f"{len(flagged_claims)} claims; {dropped} omitted.)"
        )
    return body


def build_prompt(flagged_claims):
    return f"""You are an assistant for Forest Rights Act officials.
Here is claims data with anomaly flags already computed:

{prompt_payload(flagged_claims)}

Do NOT repeat totals like claim counts or status breakdown — those are shown elsewhere. Instead:

TOP CONCERN: the single most urgent claim (ID) and exactly why.

PATTERN: one line spotting something across multiple claims.

WATCH: one claim that looks clean now but has an early warning sign.

Each section max 2 lines, under 15 words per line. No markdown, no repeated numbers."""


def groq_error_message(response):
    """Extract Groq's error message if available."""
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
    """Send flagged claims to Groq and get a summary."""
    if not GROQ_API_KEY:
        raise LLMError(
            "GROQ_API_KEY not set on the analysis service."
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
        raise LLMError("Could not reach the Groq API.") from None

    if not response.ok:
        raise LLMError(groq_error_message(response))

    try:
        summary = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise LLMError("Groq returned an unexpected response format.") from None

    summary = (summary or "").strip()
    if not summary:
        raise LLMError("Groq returned an empty summary.")
    return summary


@app.route("/api/analyze-claims", methods=["POST"])
@token_required
def analyze_claims():
    """Analyze claims: rule-based flags + LLM summary. Officials only."""
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
        return jsonify({"error": str(exc), "flagged": flagged}), 500
    except Exception:
        app.logger.exception("analyze-claims failed")
        return jsonify({"error": "Analysis failed unexpectedly."}), 500

    return jsonify({"summary": summary, "flagged": flagged})


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes"}
    app.run(debug=debug, port=5000)
