import html
import json
import random
from datetime import datetime
from pathlib import Path

import folium
import streamlit as st
from streamlit_folium import st_folium

DATA_FILE = Path(__file__).parent / "mock_data.json"
USERS_FILE = Path(__file__).parent / "users.json"

# Approximate geographic centre of Bastar district, Chhattisgarh
BASTAR_CENTER = [19.0744, 82.0255]

STATUS_COLORS = {
    "Approved": "green",
    "Pending": "orange",
    "Flagged": "red",
}

MAX_OTP_ATTEMPTS = 3

# Reports filed from the dashboard are raised for review, not self-assessed.
REPORT_STATUS = "Flagged"

MAX_RECENT_REPORTS = 10

ANOMALY_OPTIONS = [
    "None",
    "Delayed (>180 days)",
    "Mismatched Land Record",
    "Duplicate Claim",
    "Missing Gram Sabha Resolution",
    "Incomplete Documentation",
    "Area Exceeds Statutory Limit (>4 ha)",
    "Overlaps Reserved Forest Compartment",
    "Coordinates Outside District Boundary",
]


@st.cache_data
def load_claims():
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


# --- Local user store -------------------------------------------------------
# Demo-only: users are kept in a plain JSON file and the OTP is shown on screen.
# Nothing here is a substitute for real authentication.


def load_users():
    if not USERS_FILE.exists():
        return {}
    try:
        with open(USERS_FILE, encoding="utf-8") as f:
            users = json.load(f)
    except json.JSONDecodeError:
        return {}
    return users if isinstance(users, dict) else {}


def save_user(user):
    users = load_users()
    users[user["phone"]] = user
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)


def normalise_phone(raw):
    """Reduce a typed number to 10 digits, or return None if it isn't one."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits if len(digits) == 10 else None


# --- Simulated OTP flow -----------------------------------------------------


def start_otp(purpose, pending_user):
    st.session_state.otp = f"{random.randint(0, 9999):04d}"
    st.session_state.otp_purpose = purpose
    st.session_state.pending_user = pending_user
    st.session_state.otp_attempts = 0


def reset_otp():
    for key in ("otp", "otp_purpose", "pending_user", "otp_attempts"):
        st.session_state.pop(key, None)


def render_otp_step(panel):
    pending = st.session_state.pending_user
    panel.write(f"Verifying **{pending['phone']}**")
    panel.info("Simulated OTP (demo only) — no SMS is sent:")
    panel.code(st.session_state.otp, language=None)

    with panel.form("otp_form"):
        entered = st.text_input("Enter the 4-digit code", max_chars=4)
        verified = st.form_submit_button("Verify")

    resend, cancel = panel.columns(2)
    if resend.button("Resend"):
        start_otp(st.session_state.otp_purpose, pending)
        st.rerun()
    if cancel.button("Cancel"):
        reset_otp()
        st.rerun()

    if not verified:
        return

    if entered.strip() != st.session_state.otp:
        st.session_state.otp_attempts += 1
        remaining = MAX_OTP_ATTEMPTS - st.session_state.otp_attempts
        if remaining <= 0:
            reset_otp()
            panel.error("Too many incorrect attempts. Please start again.")
        else:
            panel.error(f"Incorrect code. {remaining} attempt(s) left.")
        return

    if st.session_state.otp_purpose == "signup":
        save_user(pending)
        st.session_state.user = pending
    else:
        # Re-read from disk so a returning user gets their stored details.
        st.session_state.user = load_users().get(pending["phone"], pending)

    reset_otp()
    st.rerun()


# --- Sidebar ----------------------------------------------------------------


def render_signup(tab):
    with tab.form("signup_form"):
        name = st.text_input("Full name")
        phone = st.text_input("Phone number", placeholder="10-digit mobile")
        address = st.text_area("Address", height=80)
        submitted = st.form_submit_button("Send OTP")

    if not submitted:
        return

    clean_phone = normalise_phone(phone)
    if not name.strip():
        tab.error("Please enter your name.")
    elif clean_phone is None:
        tab.error("Please enter a valid 10-digit phone number.")
    elif not address.strip():
        tab.error("Please enter your address.")
    elif clean_phone in load_users():
        tab.error("That number is already registered. Use the Log in tab.")
    else:
        start_otp(
            "signup",
            {
                "name": name.strip(),
                "phone": clean_phone,
                "address": address.strip(),
                "registered_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        st.rerun()


def render_login(tab):
    with tab.form("login_form"):
        phone = st.text_input("Registered phone number")
        submitted = st.form_submit_button("Send OTP")

    if not submitted:
        return

    clean_phone = normalise_phone(phone)
    if clean_phone is None:
        tab.error("Please enter a valid 10-digit phone number.")
        return

    user = load_users().get(clean_phone)
    if user is None:
        tab.error("No account found for that number. Sign up first.")
        return

    start_otp("login", user)
    st.rerun()


# --- Filing a new report ----------------------------------------------------


def next_claim_id(claims):
    """Next free FRA-<year>-NNN id, based on what is already in the file."""
    prefix = f"FRA-{datetime.now().year}-"
    numbers = []
    for claim in claims:
        claim_id = claim.get("claim_id")
        if isinstance(claim_id, str) and claim_id.startswith(prefix):
            suffix = claim_id[len(prefix):]
            if suffix.isdigit():
                numbers.append(int(suffix))
    return f"{prefix}{max(numbers, default=0) + 1:03d}"


def append_claim(claim):
    """Read the file fresh, append, and write it back."""
    with open(DATA_FILE, encoding="utf-8") as f:
        claims = json.load(f)
    claims.append(claim)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(claims, f, indent=4, ensure_ascii=False)
        f.write("\n")
    return claims


def dummy_coordinates():
    """Scatter new reports around Bastar so each one gets its own marker."""
    return (
        round(BASTAR_CENTER[0] + random.uniform(-0.15, 0.15), 4),
        round(BASTAR_CENTER[1] + random.uniform(-0.15, 0.15), 4),
    )


def create_report(description, location, anomaly, user):
    """Validate and persist a new report. Returns (claim, error_message).

    Reports filed through the dashboard are always raised as Flagged so they
    land in the review queue — the filer does not choose a status.
    """
    if not description.strip():
        return None, "Please describe the complaint."
    if not location.strip():
        return None, "Please enter a location or district."

    lat, lon = dummy_coordinates()
    claim = {
        "claim_id": next_claim_id(load_claims()),
        "applicant_name": user["name"] if user else "Anonymous",
        # Phone is the stable identity here; names are not unique.
        "reported_by": user["phone"] if user else None,
        "district": location.strip(),
        "lat": lat,
        "lon": lon,
        "status": REPORT_STATUS,
        "anomaly": anomaly,
        "land_area_ha": None,
        "description": description.strip(),
        "reported_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        append_claim(claim)
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"Could not save the report: {exc}"

    load_claims.clear()  # otherwise the map keeps showing the cached claims
    return claim, None


@st.dialog("File a new report")
def new_report_dialog():
    user = st.session_state.get("user")

    with st.form("new_report_form"):
        description = st.text_area(
            "Complaint description",
            height=120,
            placeholder="Describe the issue being reported…",
        )
        location = st.text_input("Location / district", value="Bastar")
        anomaly = st.selectbox("Anomaly type", ANOMALY_OPTIONS)
        st.file_uploader(
            "Attach a photo (optional)",
            type=["png", "jpg", "jpeg"],
            accept_multiple_files=False,
        )
        st.caption("Demo: the image is not uploaded or saved anywhere.")
        st.info(f"Filed reports are automatically marked **{REPORT_STATUS}** for review.")
        submitted = st.form_submit_button("Submit report")

    if not submitted:
        return

    claim, error = create_report(description, location, anomaly, user)
    if error:
        st.error(error)
        return

    st.session_state.new_report_id = claim["claim_id"]
    st.rerun()


def user_reports(claims, user):
    """Reports filed by this user, newest first.

    Matched on phone number. Reports filed before `reported_by` was recorded
    fall back to a name match so early demo entries still show up.
    """
    if not user:
        return []

    mine = []
    for claim in claims:
        if not claim.get("reported_at"):
            continue  # an original claim, not something filed from the dashboard
        reporter = claim.get("reported_by")
        if reporter == user.get("phone") or (
            reporter is None and claim.get("applicant_name") == user.get("name")
        ):
            mine.append(claim)

    # Timestamps are second-resolution, so two reports filed in the same second
    # tie; claim ids increment monotonically and break the tie correctly.
    return sorted(
        mine,
        key=lambda c: (c.get("reported_at") or "", c.get("claim_id") or ""),
        reverse=True,
    )


def render_my_reports(claims, user):
    st.divider()
    st.subheader("My recent reports")

    mine = user_reports(claims, user)
    if not mine:
        st.caption("You haven't filed any reports yet — use “File New Report” above.")
        return

    st.caption(f"{len(mine)} report(s) filed by {user['name']}.")
    for claim in mine[:MAX_RECENT_REPORTS]:
        filed = (claim.get("reported_at") or "").replace("T", " ")
        status = claim.get("status", "Unknown")
        with st.expander(f"{claim.get('claim_id', 'Unknown')} — {status}  ·  {filed}"):
            st.write(f"**Status:** {status}")
            st.write(f"**Anomaly type:** {claim.get('anomaly') or 'None'}")
            st.write(f"**Location / district:** {claim.get('district') or 'Unknown'}")
            st.write(f"**Coordinates:** {claim.get('lat')}, {claim.get('lon')}")
            st.write(f"**Filed:** {filed or 'Unknown'}")
            st.write("**Description:**")
            st.write(claim.get("description") or "—")


def render_auth():
    panel = st.sidebar
    panel.header("Account")

    user = st.session_state.get("user")
    if user:
        panel.success(f"Signed in as {user['name']}")
        panel.caption(f"Phone: {user['phone']}")
        panel.caption(f"Address: {user['address']}")
        if panel.button("Log out"):
            st.session_state.pop("user", None)
            reset_otp()
            st.rerun()
        return

    if st.session_state.get("otp"):
        render_otp_step(panel)
        return

    signup_tab, login_tab = panel.tabs(["Sign up", "Log in"])
    render_signup(signup_tab)
    render_login(login_tab)


st.set_page_config(page_title="FRA Monitoring Dashboard", layout="wide")

title_col, action_col = st.columns([4, 1], vertical_alignment="bottom")
title_col.title("FRA Monitoring Dashboard")

# Filing is only available to signed-in users.
current_user = st.session_state.get("user")
if current_user:
    if action_col.button("📝 File New Report", use_container_width=True):
        new_report_dialog()
else:
    action_col.caption("Log in to file a report.")

render_auth()

if "new_report_id" in st.session_state:
    st.success(f"Report {st.session_state.pop('new_report_id')} filed and added to the map.")

try:
    claims = load_claims()
except FileNotFoundError:
    st.error(f"Data file not found: {DATA_FILE}")
    st.stop()
except json.JSONDecodeError as exc:
    st.error(f"{DATA_FILE.name} is not valid JSON: {exc}")
    st.stop()

st.caption(f"{len(claims)} claims loaded from {DATA_FILE.name}")

fra_map = folium.Map(location=BASTAR_CENTER, zoom_start=10, tiles="OpenStreetMap")

skipped = []
for claim in claims:
    claim_id = claim.get("claim_id", "Unknown claim")
    lat, lon = claim.get("lat"), claim.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        skipped.append(claim_id)
        continue

    status = claim.get("status", "Unknown")
    land_area = claim.get("land_area_ha")
    land_area_text = f"{land_area} ha" if land_area is not None else "Not recorded"

    # Reports are user-authored, so everything here gets escaped before it
    # goes into the popup's HTML.
    def field(key, default):
        return html.escape(str(claim.get(key) or default))

    popup_html = (
        f"<b>{html.escape(str(claim_id))}</b><br>"
        f"Applicant: {field('applicant_name', 'Unknown')}<br>"
        f"District: {field('district', 'Unknown')}<br>"
        f"Status: {html.escape(str(status))}<br>"
        f"Anomaly: {field('anomaly', 'None')}<br>"
        f"Land area: {html.escape(land_area_text)}"
    )
    if claim.get("description"):
        popup_html += f"<br>Report: {field('description', '')}"
    folium.Marker(
        location=[lat, lon],
        popup=folium.Popup(popup_html, max_width=300),
        tooltip=f"{claim_id} — {status}",
        icon=folium.Icon(
            color=STATUS_COLORS.get(status, "blue"),
            icon="info-sign",
        ),
    ).add_to(fra_map)

if skipped:
    st.warning(f"Skipped {len(skipped)} claim(s) with missing coordinates: {', '.join(skipped)}")

st_folium(fra_map, width=900, height=600, returned_objects=[])

if current_user:
    render_my_reports(claims, current_user)
