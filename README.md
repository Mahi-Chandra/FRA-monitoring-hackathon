# FRA Decision Support System (PS-7)
AI-powered monitoring dashboard for tracking Forest Rights Act (FRA) claims, approvals, and bottleneck anomalies.

## 🌟 The Problem
Implementation of the Forest Rights Act is fragmented across states. Officials struggle to track claims, spot administrative delays, and flag land-record discrepancies manually.

## 🚀 The Solution
A lightweight, interactive WebGIS and decision-support dashboard built in Python (Streamlit) that:
1. **Visualizes District Claims:** Interactive map tracking individual and community forest rights claims.
2. **Auto-Flags Anomalies:** Instantly detects delayed claims (>180 days pending) and land area discrepancies.
3. **AI Decision-Support Panel:** Summarizes administrative bottlenecks and provides automated briefing reports for government officials.

---

## 🛠️ Tech Stack
- **Frontend & App Framework:** Streamlit (Python)
- **Mapping:** Pydeck / Streamlit Map native rendering
- **Data Manipulation:** Pandas
- **AI Summary Engine:** Rule-based heuristics + LLM prompt-ready architecture

---

## ⚙️ How to Run Locally

1. **Clone the repository:**
   ```bash
   git clone https://github.com/your-username/fra-decision-support.git
   cd fra-decision-support
   ```

2. **Install dependencies:**
   ```bash
   pip install streamlit pandas pydeck
   ```

3. **Run the Streamlit app:**
   ```bash
   streamlit run app.py
   ```

---

## 📊 Dataset Structure (`fake_data.csv`)
The application reads a CSV file containing spatial and operational data:
- `Claim_ID`: Unique identifier for each land claim
- `District`: Administrative district (e.g., Sehore, Bhopal, Betul)
- `Latitude` & `Longitude`: Geospatial coordinates for mapping
- `Claim_Size_Acres`: Area claimed by forest dwellers
- `Days_Pending`: Duration the claim has been stuck in the pipeline
- `Status`: Current state (`Approved`, `Rejected`, `Pending`)
