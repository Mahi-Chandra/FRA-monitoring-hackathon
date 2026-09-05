"""Officials authentication for the Streamlit dashboard.

Accounts live in users.json next to mock_data.json, in the same flat-file
spirit as the rest of the project. A password is only ever held long enough to
hash it: what reaches disk is a Werkzeug scrypt digest, and nothing here logs,
returns or stores the clear text.

SETUP:
1. pip install PyJWT
2. Export a stable signing secret before starting the dashboard:
     FRA_JWT_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
   Make it at least 32 bytes; HS256 keys shorter than that are weak, and PyJWT
   raises InsecureKeyLengthWarning to say so. Without the variable set, the
   process invents its own secret at import — so every restart silently logs
   everyone out.
"""

import json
import os
import re
import secrets
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


import jwt
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).parent
USERS_FILE = BASE_DIR / "users.json"
REVOKED_FILE = BASE_DIR / "revoked_tokens.json"

# One working shift. Long enough that an official is not re-authenticating
# mid-review, short enough that a leaked token ages out the same day.
TOKEN_TTL = timedelta(hours=8)
JWT_ALGORITHM = "HS256"
MIN_PASSWORD_LENGTH = 8

OFFICIAL_ROLE = "official"
ADMIN_ROLE = "admin"
ROLES = (OFFICIAL_ROLE, ADMIN_ROLE)

# Deliberately loose: enough to catch a typo'd address, not an attempt to
# out-parse RFC 5322.
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

# Login answers identically whether or not the email is on file. To keep the
# *timing* indistinguishable too, a miss still runs one hash comparison
# against this throwaway digest before returning the same generic failure.
_DUMMY_HASH = generate_password_hash("fra-timing-equaliser")

GENERIC_LOGIN_ERROR = "Invalid email or password"

# Read .env here rather than in each entry point: the dashboard and the API
# both import this module, and only one of them was loading the file — so the
# two processes disagreed about the secret and every API call came back 401.
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:  # python-dotenv absent: real env vars still work
    pass

# An unset FRA_JWT_SECRET is survivable for a single-process demo, so this
# falls back to a random secret rather than refusing to start. Callers surface
# SECRET_IS_EPHEMERAL as a warning instead of failing hard.
_CONFIGURED_SECRET = os.environ.get("FRA_JWT_SECRET", "").strip()
SECRET_IS_EPHEMERAL = not _CONFIGURED_SECRET
JWT_SECRET = _CONFIGURED_SECRET or secrets.token_urlsafe(48)


# --- Flat-file storage ------------------------------------------------------
# users.json is a plain list of records, same shape and indentation as
# mock_data.json. Writes go through a temp file so a crash mid-save cannot
# leave a truncated account list behind. Two registrations landing in the same
# millisecond can still lose one — the accepted trade of a file over a
# database.


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _write_json(path, payload):
    """Serialise to a sibling temp file, then swap it in.

    os.replace is atomic on Windows as well as POSIX, so a reader either sees
    the old file or the complete new one, never a half-written one.
    """
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    ) as tmp:
        json.dump(payload, tmp, indent=4)
        tmp.write("\n")
        temp_path = Path(tmp.name)
    os.replace(temp_path, path)


def load_users():
    """Every account on file. Raises on a corrupt users.json rather than
    returning an empty list, which would look like "no accounts yet" and let
    registration quietly overwrite the file."""
    return _read_json(USERS_FILE, [])


def public_user(user):
    """A record safe to return, log or drop into a token — no password hash."""
    return {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"],
        "role": user["role"],
        "district": user.get("district"),
        "created_at": user["created_at"],
    }


# --- Accounts ---------------------------------------------------------------


def normalise_email(email):
    """Emails are matched case-insensitively, so they are stored folded."""
    return (email or "").strip().lower()


def validate_registration(name, email, password):
    """Every problem with a signup, as a list of messages fit to show a user."""
    errors = []
    if not (name or "").strip():
        errors.append("Name is required")
    if not EMAIL_PATTERN.match(normalise_email(email)):
        errors.append("Enter a valid email address")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        errors.append(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    return errors


def find_user(email):
    """The stored record for an email, password hash included, or None."""
    wanted = normalise_email(email)
    for user in load_users():
        if user.get("email") == wanted:
            return user
    return None


def create_user(name, email, password, role=OFFICIAL_ROLE, district=None):
    """Register an official. Returns (public_user, errors).

    Unlike login, this does say when an email is already taken: open
    registration cannot both accept a duplicate and stay silent about it. Lock
    the endpoint down and that disclosure goes with it.
    """
    errors = validate_registration(name, email, password)
    if role not in ROLES:
        errors.append(f"Role must be one of: {', '.join(ROLES)}")
    if errors:
        return None, errors

    email = normalise_email(email)
    users = load_users()
    if any(user.get("email") == email for user in users):
        return None, ["An account with that email already exists"]

    user = {
        "id": uuid.uuid4().hex,
        "name": name.strip(),
        "email": email,
        "password_hash": generate_password_hash(password),
        "role": role,
        "district": (district or "").strip() or None,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    users.append(user)
    _write_json(USERS_FILE, users)
    return public_user(user), []


def verify_credentials(email, password):
    """The account behind these credentials, or None. Never says which half
    was wrong — see GENERIC_LOGIN_ERROR."""
    user = find_user(email)
    if user is None:
        # Burn the same work an existing account would, so response time does
        # not betray which emails are registered.
        check_password_hash(_DUMMY_HASH, password or "")
        return None
    if not check_password_hash(user["password_hash"], password or ""):
        return None
    return public_user(user)


# --- Tokens -----------------------------------------------------------------


def issue_token(user):
    """Sign a session token for an account. Returns (token, expires_at)."""
    now = datetime.now(timezone.utc)
    expires_at = now + TOKEN_TTL
    payload = {
        "sub": user["id"],
        "name": user["name"],
        "email": user["email"],
        "role": user["role"],
        "district": user.get("district"),
        # A per-token id, so logout can retire this one session without
        # invalidating the official's other sign-ins.
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM), expires_at


def decode_token(token):
    """Verify a token. Returns (claims, error); error is safe to display.

    `algorithms` is pinned so a forged header cannot talk the library into
    accepting an unsigned "alg": "none" token.
    """
    if not token:
        return None, "Authentication required"
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None, "Session expired — please log in again"
    except jwt.InvalidTokenError:
        return None, "Invalid authentication token"
    if is_revoked(claims.get("jti")):
        return None, "Session has been logged out"
    return claims, None


def bearer_token(header_value):
    """The token out of an `Authorization: Bearer <token>` header, or None."""
    if not header_value:
        return None
    scheme, _, token = header_value.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


# --- Revocation -------------------------------------------------------------
# JWTs are self-contained, so a logout has to be recorded somewhere for the
# signature to stop counting. This keeps {jti: exp} on disk rather than in
# memory, so a logout survives a restart of the dashboard.


def _load_revoked():
    """Retired token ids, expired entries dropped.

    Pruning is safe because a token past its exp already fails verification on
    its own — keeping it listed would only grow the file forever.
    """
    revoked = _read_json(REVOKED_FILE, {})
    now = int(datetime.now(timezone.utc).timestamp())
    return {jti: exp for jti, exp in revoked.items() if exp > now}


def is_revoked(jti):
    return bool(jti) and jti in _load_revoked()


def revoke_token(claims):
    """Retire one session. Idempotent — logging out twice is not an error."""
    jti = claims.get("jti")
    if not jti:
        return
    revoked = _load_revoked()
    revoked[jti] = int(claims.get("exp", 0))
    _write_json(REVOKED_FILE, revoked)


# --- Demo account -----------------------------------------------------------
# One deliberately fake credential, printed on the login page so a reviewer can
# get in without an administrator issuing them an account.
#
# This is NOT a backdoor and NOT a real user's password:
#   * the account is created through create_user, exactly like any other, so
#     what lands in users.json is a scrypt digest and never the clear text;
#   * verify_credentials has no special case for it — the demo signs in through
#     the same comparison every other account goes through;
#   * it holds the plain official role, so it can never reach an admin-only
#     view, and nothing about it discloses a genuine account.
#
# Set FRA_DEMO_ACCOUNT=0 to keep it out of a deployment that is not a demo.

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "Demo@1234"  # published on purpose; fake account only
DEMO_NAME = "Demo User"
# Left without a district so the demo sees the full sample caseload — every
# record it can reach comes from mock_data.json.
DEMO_DISTRICT = None

_DEMO_OFF = {"0", "false", "no", "off"}
DEMO_ACCOUNT_ENABLED = (
    os.environ.get("FRA_DEMO_ACCOUNT", "1").strip().lower() not in _DEMO_OFF
)


def ensure_demo_user():
    """Create the demo account if it is missing. Returns its public record.

    Idempotent, and self-healing: if DEMO_PASSWORD above is edited, the stored
    hash is rewritten so the credential advertised on the login page always
    matches the one that actually works. Returns None when demo access is
    switched off, which is the signal the UI uses to hide the hint.
    """
    if not DEMO_ACCOUNT_ENABLED:
        return None

    users = load_users()
    for user in users:
        if user.get("email") == DEMO_EMAIL:
            if not check_password_hash(user["password_hash"], DEMO_PASSWORD):
                user["password_hash"] = generate_password_hash(DEMO_PASSWORD)
                _write_json(USERS_FILE, users)
            return public_user(user)

    user, _errors = create_user(
        DEMO_NAME,
        DEMO_EMAIL,
        DEMO_PASSWORD,
        role=OFFICIAL_ROLE,
        district=DEMO_DISTRICT,
    )
    return user
