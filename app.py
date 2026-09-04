"""Forest Rights Act — Decision Support System.

An administrative view over FRA claims: state and district progress against
title recognition, extent of forest land recognised, and the anomalies holding
individual claims up. Read-only — there is no public-facing intake here.
"""

import base64
import html
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import folium
import streamlit as st
from streamlit_folium import st_folium

DATA_FILE = Path(__file__).parent / "mock_data.json"
BACKGROUND_IMAGE = Path(__file__).parent / "forest_bg.jpg"

# Fallback map view (Bastar district, Chhattisgarh) for when no claim carries
# usable coordinates to fit the view to.
DEFAULT_MAP_CENTER = [19.0744, 82.0255]
DEFAULT_MAP_ZOOM = 10

# --- Claim vocabulary -------------------------------------------------------
# The three statuses the data uses, in the administrative sense they carry: an
# Approved claim is a title distributed, Pending is still in the pipeline,
# Flagged is held for review against one of the anomalies.
TITLE_STATUS = "Approved"
PENDING_STATUS = "Pending"
FLAGGED_STATUS = "Flagged"

NO_ANOMALY = "None"
UNMAPPED_STATE = "Unmapped"

STATUS_COLORS = {
    TITLE_STATUS: "green",
    PENDING_STATUS: "orange",
    FLAGGED_STATUS: "red",
}

# Background / text pairs for the status chips. The text colour doubles as the
# map legend dot, so each one has to stay legible on a translucent panel over
# the forest photo.
STATUS_CHIPS = {
    TITLE_STATUS: ("rgba(129, 199, 132, 0.22)", "#c8e6c9"),
    PENDING_STATUS: ("rgba(255, 183, 77, 0.22)", "#ffe0b2"),
    FLAGGED_STATUS: ("rgba(239, 83, 80, 0.26)", "#ffcdd2"),
}
DEFAULT_CHIP = ("rgba(255, 255, 255, 0.16)", "#e4e7e5")

# Claims carry a district but no state, so the state rolls up through this
# lookup. A claim may also carry an explicit "state" key, which wins. Districts
# outside the lookup roll up as UNMAPPED_STATE and get called out in the panel
# rather than being silently folded into a neighbour.
FRA_DISTRICTS_BY_STATE = {
    "Chhattisgarh": (
        "Bastar", "Bijapur", "Dantewada", "Kanker", "Kondagaon", "Narayanpur",
        "Sukma", "Gariaband", "Dhamtari", "Mahasamund", "Korba", "Raigarh",
        "Jashpur", "Surguja", "Surajpur", "Balrampur", "Koriya", "Kabirdham",
        "Rajnandgaon", "Bilaspur", "Mungeli", "Balod",
    ),
    "Madhya Pradesh": (
        "Dindori", "Mandla", "Balaghat", "Anuppur", "Shahdol", "Umaria",
        "Sidhi", "Singrauli", "Betul", "Chhindwara", "Seoni", "Jhabua",
        "Alirajpur", "Barwani", "Khargone", "Burhanpur", "Harda",
    ),
    "Odisha": (
        "Koraput", "Malkangiri", "Nabarangpur", "Rayagada", "Kalahandi",
        "Kandhamal", "Gajapati", "Mayurbhanj", "Keonjhar", "Sundargarh",
        "Sambalpur", "Deogarh", "Boudh", "Nuapada",
    ),
    "Jharkhand": (
        "West Singhbhum", "East Singhbhum", "Simdega", "Gumla", "Khunti",
        "Latehar", "Lohardaga", "Palamu", "Dumka", "Pakur", "Sahibganj",
    ),
    "Maharashtra": (
        "Gadchiroli", "Gondia", "Chandrapur", "Nandurbar", "Dhule",
        "Amravati", "Yavatmal", "Nashik", "Palghar", "Thane",
    ),
    "Telangana": (
        "Bhadradri Kothagudem", "Mulugu", "Bhupalpally", "Adilabad",
        "Komaram Bheem", "Mancherial", "Nirmal", "Mahabubabad", "Nagarkurnool",
    ),
    "Andhra Pradesh": (
        "Alluri Sitharama Raju", "Parvathipuram Manyam", "Vizianagaram",
        "Srikakulam", "East Godavari", "West Godavari",
    ),
}
DISTRICT_STATE = {
    district: state
    for state, districts in FRA_DISTRICTS_BY_STATE.items()
    for district in districts
}

# Esri's satellite basemap. Served without an API key, but the attribution
# below is a condition of use, so it always rides along with the layer.
ESRI_IMAGERY_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services"
    "/World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
ESRI_IMAGERY_ATTR = (
    "Tiles &copy; Esri &mdash; Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye, "
    "Getmapping, Aerogrid, IGN, IGP, UPF, and the GIS User Community"
)
# Satellite imagery carries no place names, so labels go on as an overlay.
ESRI_LABELS_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services"
    "/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"
)
ESRI_LABELS_ATTR = "Labels &copy; Esri"

MAP_HEIGHT = 640


# `mtime` is only there as a cache key: saving an edit to mock_data.json
# invalidates the entry, so the dashboard picks the change up on the next rerun
# instead of serving the copy it read at startup.
@st.cache_data(show_spinner=False)
def load_claims(mtime):
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


# --- Aggregation ------------------------------------------------------------
# Everything the left panel reports is derived here, straight off the claims
# list. Nothing is hard-coded, so adding districts or states to mock_data.json
# is all it takes for them to show up.


def district_of(claim):
    return (claim.get("district") or "").strip() or "Unrecorded"


def state_of(claim):
    """An explicit `state` on the claim wins; otherwise infer from district."""
    state = (claim.get("state") or "").strip()
    if state:
        return state
    return DISTRICT_STATE.get(district_of(claim), UNMAPPED_STATE)


def hectares(claim):
    """Recorded extent, or None. Guards against nulls and stray strings."""
    value = claim.get("land_area_ha")
    return float(value) if isinstance(value, (int, float)) else None


def summarise(claims):
    """Roll a set of claims into the figures the panel reports."""
    counts = Counter(claim.get("status", "Unknown") for claim in claims)

    # "Recognised" is extent behind a distributed title; "under claim" is
    # everything on file, whatever stage it has reached.
    recognised = 0.0
    under_claim = 0.0
    no_extent = 0
    for claim in claims:
        extent = hectares(claim)
        if extent is None:
            no_extent += 1
            continue
        under_claim += extent
        if claim.get("status") == TITLE_STATUS:
            recognised += extent

    received = len(claims)
    titles = counts.get(TITLE_STATUS, 0)
    return {
        "received": received,
        "titles": titles,
        "pending": counts.get(PENDING_STATUS, 0),
        "flagged": counts.get(FLAGGED_STATUS, 0),
        "recognised_ha": recognised,
        "under_claim_ha": under_claim,
        "no_extent": no_extent,
        "recognition_rate": (titles / received * 100) if received else 0.0,
    }


def grouped_summary(claims, key_of):
    """summarise() per group, heaviest caseload first — how an admin scans."""
    buckets = defaultdict(list)
    for claim in claims:
        buckets[key_of(claim)].append(claim)

    rows = []
    for key, group in buckets.items():
        row = summarise(group)
        row["key"] = key
        rows.append(row)
    return sorted(rows, key=lambda row: (-row["received"], row["key"]))


def state_rows(claims):
    return grouped_summary(claims, state_of)


def district_rows(claims):
    """Keyed on (state, district) so same-named districts stay distinct."""
    return grouped_summary(claims, lambda claim: (state_of(claim), district_of(claim)))


def anomaly_rows(claims):
    """(anomaly, count) worst first, plus how many claims are clean."""
    counts = Counter((claim.get("anomaly") or NO_ANOMALY).strip() for claim in claims)
    clean = counts.pop(NO_ANOMALY, 0)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ranked, clean


def unmapped_districts(claims):
    """Districts that fell through DISTRICT_STATE, so they can be called out."""
    return sorted(
        {district_of(claim) for claim in claims if state_of(claim) == UNMAPPED_STATE}
    )


# --- Glass morphism styling -------------------------------------------------
# The forest photo is the page background; every panel above it is a
# semi-transparent pane that blurs whatever it covers. Cards are plain
# `st.container(key=...)` blocks — Streamlit turns the key into an
# `st-key-<key>` class, which is what the `st-key-glass-` selectors hook into,
# so any container keyed `glass-*` becomes a frosted pane.

# The browser cannot read a local file path out of a CSS rule, so the photo is
# inlined as a data URI. `mtime` is part of the cache key so dropping in a new
# forest_bg.jpg picks it up on the next rerun.
@st.cache_data(show_spinner=False)
def encoded_background(path_str, mtime):
    data = base64.b64encode(Path(path_str).read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{data}"


def page_background():
    """CSS `background` value for the page. Returns (value, warning_or_None).

    The photo averages out fairly dark, but a scrim still goes over it so the
    odd bright gap in the canopy cannot wash out the white text on the panes.
    """
    scrim = "linear-gradient(rgba(9, 22, 14, 0.55), rgba(9, 22, 14, 0.64))"
    try:
        uri = encoded_background(str(BACKGROUND_IMAGE), BACKGROUND_IMAGE.stat().st_mtime)
    except OSError:
        # No photo on disk — fall back to a forest gradient so the glass panes
        # still have something to sit on instead of a blank page.
        return (
            "linear-gradient(165deg, #0d2417 0%, #10321f 55%, #0b1d13 100%)",
            f"Background image not found: {BACKGROUND_IMAGE.name}",
        )
    return f'{scrim}, url("{uri}") center center / cover no-repeat fixed', None


PANEL_CSS = """
<style>
/* --- palette -------------------------------------------------------------
   Surfaces: rgba(255,255,255,0.1) panes, blur(12px), soft white borders.
   Text: white and near-white, kept crisp against the photo.
   Accent: forest #2e7d32, lifted to #a5d6a7 where it has to read as text on
   a dark pane.
   ------------------------------------------------------------------------ */

/* --- the forest photo, covering the whole page --------------------------- */
[data-testid="stAppViewContainer"] {
    background: __BACKGROUND__;
}
[data-testid="stHeader"] { background: transparent; }
[data-testid="stMainBlockContainer"] { padding-top: 2.2rem; max-width: 100%; }

/* the sidebar is unused; the decision support panel is the left column */
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] { display: none; }

/* --- the glass tray the decision support panel sits in -------------------
   Held at 0.06 rather than 0.1 because the cards inside are themselves 0.1 —
   stacking two 0.1 panes reads as one muddy 0.19 pane and the blur doubles. */
[data-testid="stColumn"]:first-of-type {
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.16);
    border-radius: 24px;
    padding: 0.6rem 0.85rem 1rem;
    backdrop-filter: blur(12px) saturate(130%);
    -webkit-backdrop-filter: blur(12px) saturate(130%);
    box-shadow: 0 10px 36px rgba(0, 0, 0, 0.26);
}

/* --- cards --------------------------------------------------------------- */
div[class*="st-key-glass-"] {
    background: rgba(255, 255, 255, 0.10);
    border: 1px solid rgba(255, 255, 255, 0.22);
    border-radius: 20px;
    padding: 1.15rem 1.25rem 1.25rem;
    backdrop-filter: blur(12px) saturate(140%);
    -webkit-backdrop-filter: blur(12px) saturate(140%);
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.28);
}
div[class*="st-key-glass-"] h3 {
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0.10em;
    text-transform: uppercase;
    color: #a5d6a7;
    padding: 0 0 0.35rem;
    margin: 0;
}

/* crisp text on the panes */
div[class*="st-key-glass-"],
div[class*="st-key-glass-"] p,
div[class*="st-key-glass-"] li,
div[class*="st-key-glass-"] label,
div[class*="st-key-glass-"] [data-testid="stMarkdownContainer"] {
    color: rgba(255, 255, 255, 0.92);
}
div[class*="st-key-glass-"] [data-testid="stCaptionContainer"],
div[class*="st-key-glass-"] [data-testid="stCaptionContainer"] p {
    color: rgba(255, 255, 255, 0.72);
}

/* --- masthead: sits straight on the photo, so it gets a shadow ----------- */
.fra-masthead h1 {
    font-size: 1.95rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    margin: 0;
    color: #ffffff;
    text-shadow: 0 2px 14px rgba(0, 0, 0, 0.55);
}
.fra-masthead p {
    margin: 0.25rem 0 0;
    color: rgba(255, 255, 255, 0.82);
    font-size: 0.9rem;
    text-shadow: 0 1px 10px rgba(0, 0, 0, 0.5);
}
.fra-masthead .fra-stamp {
    font-size: 0.76rem;
    color: rgba(255, 255, 255, 0.66);
    letter-spacing: 0.03em;
}

/* --- metrics containers -------------------------------------------------- */
div[class*="st-key-glass-"] [data-testid="stMetric"] {
    background: rgba(255, 255, 255, 0.10);
    border: 1px solid rgba(255, 255, 255, 0.20);
    border-radius: 14px;
    padding: 0.6rem 0.75rem 0.7rem;
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
}
div[class*="st-key-glass-"] [data-testid="stMetricLabel"] p {
    font-size: 0.66rem;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: rgba(255, 255, 255, 0.78);
}
div[class*="st-key-glass-"] [data-testid="stMetricValue"] {
    font-size: 1.42rem;
    color: #ffffff;
}

/* --- progress tables ----------------------------------------------------- */
.fra-table { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
.fra-table th {
    text-align: left;
    font-weight: 600;
    font-size: 0.63rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: rgba(255, 255, 255, 0.74);
    padding: 0.3rem 0.35rem;
    border-bottom: 1px solid rgba(255, 255, 255, 0.24);
    white-space: nowrap;
}
.fra-table td {
    padding: 0.42rem 0.35rem;
    color: rgba(255, 255, 255, 0.92);
    border-bottom: 1px solid rgba(255, 255, 255, 0.09);
}
.fra-table th.num, .fra-table td.num {
    text-align: right;
    font-variant-numeric: tabular-nums;
}
.fra-table tbody tr:last-child td { border-bottom: none; }
.fra-table tbody tr:hover td { background: rgba(255, 255, 255, 0.06); }
.fra-table td.lead { font-weight: 600; color: #ffffff; }
.fra-table td.titles { color: #c8e6c9; }
.fra-table td.pending { color: #ffe0b2; }
.fra-table td.flagged { color: #ffcdd2; }

/* --- anomaly breakdown --------------------------------------------------- */
.fra-anom { display: flex; flex-direction: column; gap: 0.62rem; }
.fra-anom-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 0.6rem;
    font-size: 0.78rem;
    color: rgba(255, 255, 255, 0.92);
}
.fra-anom-head b { color: #ffffff; font-variant-numeric: tabular-nums; }
.fra-anom-head .share { color: rgba(255, 255, 255, 0.68); font-size: 0.72rem; }
.fra-bar {
    height: 6px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.12);
    overflow: hidden;
    margin-top: 0.24rem;
}
.fra-bar > i {
    display: block;
    height: 100%;
    border-radius: 999px;
    background: linear-gradient(90deg, #ffb74d, #ff8a65);
}

/* --- map ----------------------------------------------------------------- */
div[class*="st-key-glass-map"] { padding: 0.7rem; }
div[class*="st-key-glass-map"] iframe {
    border-radius: 15px;
    display: block;
}
.fra-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 1rem;
    padding: 0.65rem 0.35rem 0.15rem;
    font-size: 0.78rem;
    color: rgba(255, 255, 255, 0.82);
}
.fra-legend span { display: inline-flex; align-items: center; gap: 0.4rem; }
.fra-dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.fra-chip {
    display: inline-block;
    padding: 0.12rem 0.6rem;
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    border: 1px solid rgba(255, 255, 255, 0.18);
}
</style>
"""


def glass(key):
    """A frosted-glass card. `key` is prefixed so the CSS above picks it up."""
    return st.container(key=f"glass-{key}")


def card_title(text):
    st.markdown(f"### {text}")


def count(value):
    return f"{value:,}"


def area(value):
    return f"{value:,.1f}"


def table_html(columns, rows):
    """Render a table. `columns` is [(label, css_class)]; each row matches it.

    District and state names come out of the data file, so every cell is
    escaped on the way in.
    """
    head = "".join(
        f'<th class="{cls}">{html.escape(label)}</th>' for label, cls in columns
    )
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="{cls}">{html.escape(str(value))}</td>'
            for (_, cls), value in zip(columns, row)
        )
        + "</tr>"
        for row in rows
    )
    return (
        f'<table class="fra-table"><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )


# --- Decision support panel -------------------------------------------------


def render_programme_card(claims):
    """The headline position across everything on file."""
    with glass("programme"):
        card_title("Programme overview")

        totals = summarise(claims)

        received, titles = st.columns(2)
        received.metric("Claims received", count(totals["received"]))
        titles.metric("Titles distributed", count(totals["titles"]))

        pending, flagged = st.columns(2)
        pending.metric("Claims pending", count(totals["pending"]))
        flagged.metric("Flagged for review", count(totals["flagged"]))

        extent, rate = st.columns(2)
        extent.metric("Extent recognised", f"{area(totals['recognised_ha'])} ha")
        rate.metric("Recognition rate", f"{totals['recognition_rate']:.0f}%")

        st.caption(
            f"{area(totals['under_claim_ha'])} ha under claim in total · "
            "recognised extent counts claims with a distributed title only."
        )
        if totals["no_extent"]:
            st.caption(
                f"⚠ {totals['no_extent']} claim(s) carry no recorded extent and "
                "contribute nothing to the hectare figures."
            )


def render_state_card(claims):
    with glass("state"):
        card_title("State-wise progress")

        rows = state_rows(claims)
        st.markdown(
            table_html(
                [
                    ("State", "lead"),
                    ("Claims", "num"),
                    ("Titles", "num titles"),
                    ("Pending", "num pending"),
                    ("Extent ha", "num"),
                ],
                [
                    (
                        row["key"],
                        count(row["received"]),
                        count(row["titles"]),
                        count(row["pending"]),
                        area(row["recognised_ha"]),
                    )
                    for row in rows
                ],
            ),
            unsafe_allow_html=True,
        )

        if len(rows) == 1:
            st.caption(
                f"All claims on file sit in {rows[0]['key']}; further states "
                "appear here as they are added to the data."
            )

        stray = unmapped_districts(claims)
        if stray:
            st.caption(
                "⚠ No state mapped for: "
                + ", ".join(stray)
                + " — add them to FRA_DISTRICTS_BY_STATE, or set a `state` key "
                "on those claims."
            )


def render_district_card(claims):
    with glass("district"):
        card_title("District-wise progress")

        rows = district_rows(claims)
        # The state column only earns its width once more than one state is in
        # play; with a single state it repeats the card above.
        multi_state = len({row["key"][0] for row in rows}) > 1

        columns = [("District", "lead")]
        if multi_state:
            columns.append(("State", ""))
        columns += [
            ("Claims", "num"),
            ("Titles", "num titles"),
            ("Pending", "num pending"),
            ("Flagged", "num flagged"),
            ("Extent ha", "num"),
        ]

        table_rows = []
        for row in rows:
            state, district = row["key"]
            cells = [district]
            if multi_state:
                cells.append(state)
            cells += [
                count(row["received"]),
                count(row["titles"]),
                count(row["pending"]),
                count(row["flagged"]),
                area(row["recognised_ha"]),
            ]
            table_rows.append(cells)

        st.markdown(table_html(columns, table_rows), unsafe_allow_html=True)
        st.caption(f"{len(rows)} district(s) reporting · ordered by caseload.")


def render_anomaly_card(claims):
    with glass("anomaly"):
        card_title("Anomaly breakdown")

        ranked, clean = anomaly_rows(claims)
        if not ranked:
            st.caption("No anomalies recorded against any claim on file.")
            return

        total = len(claims)
        # Bars are scaled against the worst offender rather than the whole
        # caseload, so the smaller categories stay visible.
        worst = ranked[0][1]

        bars = []
        for anomaly, hits in ranked:
            share = hits / total * 100 if total else 0
            bars.append(
                '<div><div class="fra-anom-head">'
                f"<span>{html.escape(anomaly)}</span>"
                f'<span><b>{hits}</b> <span class="share">{share:.0f}%</span></span>'
                "</div>"
                f'<div class="fra-bar"><i style="width:{hits / worst * 100:.1f}%">'
                "</i></div></div>"
            )
        st.markdown(
            f'<div class="fra-anom">{"".join(bars)}</div>', unsafe_allow_html=True
        )

        with_anomaly = sum(hits for _, hits in ranked)
        st.caption(
            f"{with_anomaly} claim(s) carry an anomaly · {clean} clean · "
            f"{len(ranked)} distinct anomaly type(s)."
        )


# --- Map --------------------------------------------------------------------


def build_map(claims):
    """Esri satellite basemap with one marker per claim.

    Returns the map and the ids of any claims that had no usable coordinates.
    """
    fra_map = folium.Map(
        location=DEFAULT_MAP_CENTER, zoom_start=DEFAULT_MAP_ZOOM, tiles=None
    )
    folium.TileLayer(
        tiles=ESRI_IMAGERY_URL,
        attr=ESRI_IMAGERY_ATTR,
        name="Esri World Imagery",
        max_zoom=19,
        control=False,
    ).add_to(fra_map)
    folium.TileLayer(
        tiles=ESRI_LABELS_URL,
        attr=ESRI_LABELS_ATTR,
        name="Place names",
        overlay=True,
        max_zoom=19,
    ).add_to(fra_map)

    skipped = []
    plotted = []
    for claim in claims:
        claim_id = claim.get("claim_id", "Unknown claim")
        lat, lon = claim.get("lat"), claim.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            skipped.append(claim_id)
            continue

        status = claim.get("status", "Unknown")
        extent = hectares(claim)
        extent_text = f"{extent} ha" if extent is not None else "Not recorded"

        # Claim records are free text, so everything here gets escaped before
        # it goes into the popup's HTML.
        def field(key, default):
            return html.escape(str(claim.get(key) or default))

        popup_html = (
            f"<b>{html.escape(str(claim_id))}</b><br>"
            f"Claimant: {field('applicant_name', 'Unknown')}<br>"
            f"District: {html.escape(district_of(claim))}, "
            f"{html.escape(state_of(claim))}<br>"
            f"Status: {html.escape(str(status))}<br>"
            f"Anomaly: {field('anomaly', NO_ANOMALY)}<br>"
            f"Extent: {html.escape(extent_text)}"
        )
        if claim.get("description"):
            popup_html += f"<br>Note: {field('description', '')}"

        folium.Marker(
            location=[lat, lon],
            popup=folium.Popup(popup_html, max_width=300),
            tooltip=f"{claim_id} — {status}",
            icon=folium.Icon(
                color=STATUS_COLORS.get(status, "blue"),
                icon="info-sign",
            ),
        ).add_to(fra_map)
        plotted.append((lat, lon))

    # Frame whatever is actually on the map rather than staying pinned to
    # Bastar, so the view still works once other districts are in the data.
    if len(set(plotted)) > 1:
        fra_map.fit_bounds(
            [
                [min(lat for lat, _ in plotted), min(lon for _, lon in plotted)],
                [max(lat for lat, _ in plotted), max(lon for _, lon in plotted)],
            ],
            padding=(30, 30),
        )

    folium.LayerControl(collapsed=True).add_to(fra_map)
    return fra_map, skipped


def render_map_card(claims):
    with glass("map"):
        fra_map, skipped = build_map(claims)
        st_folium(
            fra_map,
            use_container_width=True,
            height=MAP_HEIGHT,
            returned_objects=[],
        )

        legend = "".join(
            f'<span><i class="fra-dot" style="background:{STATUS_CHIPS[status][1]}"></i>'
            f"{status}</span>"
            for status in (TITLE_STATUS, PENDING_STATUS, FLAGGED_STATUS)
        )
        districts = len({(state_of(c), district_of(c)) for c in claims})
        states = len({state_of(c) for c in claims})
        st.markdown(
            f'<div class="fra-legend">{legend}'
            f"<span>{len(claims)} claims · {districts} district(s) · "
            f"{states} state(s)</span></div>",
            unsafe_allow_html=True,
        )

    if skipped:
        st.warning(
            f"Skipped {len(skipped)} claim(s) with missing coordinates: "
            f"{', '.join(skipped)}"
        )


# --- Page -------------------------------------------------------------------

st.set_page_config(
    page_title="FRA Decision Support System",
    layout="wide",
    initial_sidebar_state="collapsed",
)
background_value, background_warning = page_background()
st.markdown(
    PANEL_CSS.replace("__BACKGROUND__", background_value),
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="fra-masthead">'
    "<h1>Forest Rights Act — Decision Support System</h1>"
    "<p>Claim monitoring, title recognition and anomaly review · "
    "administrative view</p>"
    f'<p class="fra-stamp">Source: {html.escape(DATA_FILE.name)} · '
    f'generated {datetime.now().strftime("%d %b %Y, %H:%M")}</p></div>',
    unsafe_allow_html=True,
)
if background_warning:
    st.warning(background_warning)
st.write("")

try:
    claims = load_claims(DATA_FILE.stat().st_mtime)
except FileNotFoundError:
    st.error(f"Data file not found: {DATA_FILE}")
    st.stop()
except json.JSONDecodeError as exc:
    st.error(f"{DATA_FILE.name} is not valid JSON: {exc}")
    st.stop()

if not claims:
    st.warning(f"{DATA_FILE.name} contains no claims.")
    st.stop()

decision_panel, map_panel = st.columns([1, 1.8], gap="medium")

with decision_panel:
    render_programme_card(claims)
    render_state_card(claims)
    render_district_card(claims)
    render_anomaly_card(claims)

with map_panel:
    render_map_card(claims)
