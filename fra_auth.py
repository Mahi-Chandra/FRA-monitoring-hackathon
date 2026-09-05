"""FRA authentication - JWT tokens, official credentials, demo accounts."""

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from dotenv import load_dotenv

load_dotenv()

ADMIN_ROLE = "admin"
OFFICIAL_ROLE = "official"

FRA_JWT_SECRET = os.environ.get("FRA_JWT_SECRET", "").strip()
SECRET_IS_EPHEMERAL = not FRA_JWT_SECRET

DEMO_ACCOUNT_ENABLED = os.environ.get("FRA_DEMO_ENABLED", "").strip().lower() in {
    "1",
    "true",
    "yes",
}
DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "demo123"

GENERIC_LOGIN_ERROR = "Invalid email or password."
GENERIC_AUTH_ERROR = "Token is invalid or expired."

DEMO_SEEDED_KEY = "_demo_seeded"
REVOKED_TOKENS_KEY = "_revoked_tokens"

VALID_ROLES = {ADMIN_ROLE, OFFICIAL_ROLE}

SCOPED_DISTRICTS = {
    "official@bhopal.gov.in": "Bhopal",
    "official@indore.gov.in": "Indore",
}

PASSWORD_HASH_ALGORITHM = "sha256"


def _hash_password(password):
    """SHA256 hash of password (not production-grade)."""
    return hashlib.sha256(password.encode()).hexdigest()


def _password_matches(stored_hash, password):
    """Compare password hash."""
    return stored_hash == _hash_password(password)


def get_demo_users():
    """List of demo accounts."""
    return [
        {
            "email": DEMO_EMAIL,
            "password_hash": _hash_password(DEMO_PASSWORD),
            "name": "Demo Official",
            "role": OFFICIAL_ROLE,
        },
    ]


def get_official_users():
    """Hardcoded officials (replace with database in production)."""
    users = [
        {
            "email": "admin@fra.gov.in",
            "password_hash": _hash_password("admin123"),
            "name": "Administrator",
            "role": ADMIN_ROLE,
        },
        {
            "email": "official@fra.gov.in",
            "password_hash": _hash_password("official123"),
            "name": "Field Officer",
            "role": OFFICIAL_ROLE,
        },
    ]
    if DEMO_ACCOUNT_ENABLED:
        users.extend(get_demo_users())
    return users


def all_users():
    """All known users."""
    return get_official_users()


def verify_credentials(email, password):
    """Check email + password. Returns user dict or None."""
    if not email or not password:
        return None

    for user in all_users():
        if user["email"].lower() == (email or "").lower():
            if _password_matches(user["password_hash"], password):
                return {
                    "email": user["email"],
                    "name": user["name"],
                    "role": user["role"],
                    "district": SCOPED_DISTRICTS.get(user["email"]),
                }
    return None


def bearer_token(auth_header):
    """Extract token from 'Bearer <token>' header, or return None."""
    if not auth_header:
        return None
    parts = (auth_header or "").split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def issue_token(user):
    """Create a signed JWT token. Returns (token, expires_at)."""
    if not FRA_JWT_SECRET:
        raise RuntimeError(
            "FRA_JWT_SECRET is not set. Tokens cannot be signed."
        )

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=8)

    payload = {
        "email": user["email"],
        "name": user["name"],
        "role": user["role"],
        "district": user.get("district"),
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": secrets.token_urlsafe(16),
    }

    token = jwt.encode(payload, FRA_JWT_SECRET, algorithm="HS256")
    return token, expires_at


def decode_token(token):
    """Verify and decode a JWT token. Returns (claims, error)."""
    if not token:
        return None, GENERIC_AUTH_ERROR

    if not FRA_JWT_SECRET:
        return None, (
            "Token verification is not available — "
            "FRA_JWT_SECRET is not set."
        )

    try:
        claims = jwt.decode(token, FRA_JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return None, "Token has expired."
    except jwt.InvalidTokenError:
        return None, GENERIC_AUTH_ERROR

    jti = claims.get("jti")
    revoked = _session_state().get(REVOKED_TOKENS_KEY, set())
    if jti in revoked:
        return None, "Token has been revoked."

    return claims, None


def revoke_token(claims):
    """Mark a token as revoked (logout)."""
    jti = claims.get("jti")
    if jti:
        state = _session_state()
        if REVOKED_TOKENS_KEY not in state:
            state[REVOKED_TOKENS_KEY] = set()
        state[REVOKED_TOKENS_KEY].add(jti)


def ensure_demo_user():
    """Create demo account on first use (if enabled)."""
    if not DEMO_ACCOUNT_ENABLED:
        return

    state = _session_state()
    if state.get(DEMO_SEEDED_KEY):
        return

    state[DEMO_SEEDED_KEY] = True


def _session_state():
    """Module-level state (revoked tokens, etc.)."""
    import streamlit as st
    return st.session_state
