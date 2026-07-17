"""
Premium dark Streamlit UI for the People <-> Organization mapping.

Reads person_map.json / org_map.json produced by scripts/build_mapping.py
(from the local mapping_data/ folder, or pulled from S3 if missing).

    streamlit run lead_clean/mapping_ui.py
"""

import os
import json
import streamlit as st

HERE = os.path.dirname(os.path.abspath(__file__))
MAP_DIR = os.path.join(HERE, "mapping_data")

st.set_page_config(page_title="Leads Intelligence", page_icon="◆", layout="wide")


# ---------------------------------------------------------------- premium style

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Space+Grotesk:wght@500;600;700&display=swap');

:root{
  --bg:#08080c; --surface:rgba(255,255,255,.035); --surface2:rgba(255,255,255,.06);
  --border:rgba(255,255,255,.09); --text:#ececf5; --muted:#8a8aa3;
  --a1:#6366f1; --a2:#a855f7; --a3:#22d3ee;
}
html, body, [class*="css"], .stApp{ font-family:'Inter',sans-serif; }
.stApp{
  background:
    radial-gradient(1100px 500px at 12% -8%, rgba(99,102,241,.14), transparent 60%),
    radial-gradient(900px 500px at 100% 0%, rgba(168,85,247,.12), transparent 55%),
    #08080c;
  color:var(--text);
}
#MainMenu, footer, header{ visibility:hidden; }
.block-container{ padding-top:2.2rem; max-width:1280px; }

/* ---- hero ---- */
.hero{ margin:.2rem 0 1.6rem 0; }
.hero-badge{
  display:inline-block; font-size:.72rem; letter-spacing:.16em; font-weight:600;
  color:var(--a3); background:rgba(34,211,238,.08); border:1px solid rgba(34,211,238,.22);
  padding:.28rem .7rem; border-radius:999px; margin-bottom:.85rem;
}
.hero-title{
  font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:2.7rem; line-height:1.05;
  letter-spacing:-.02em; margin:0;
  background:linear-gradient(120deg,#fff 20%, #c7b6ff 55%, #7dd3fc 100%);
  -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;
}
.hero-sub{ color:var(--muted); font-size:1.02rem; margin-top:.5rem; font-weight:400; }

/* ---- stat cards ---- */
.stat-row{ display:grid; grid-template-columns:repeat(3,1fr); gap:16px; margin:.4rem 0 1.9rem; }
.stat-card{
  position:relative; background:var(--surface); border:1px solid var(--border);
  border-radius:18px; padding:1.15rem 1.3rem; overflow:hidden;
  transition:transform .18s ease, border-color .18s ease, box-shadow .18s ease;
}
.stat-card:hover{ transform:translateY(-3px); border-color:rgba(139,92,246,.45);
  box-shadow:0 14px 40px -18px rgba(139,92,246,.55); }
.stat-card::before{ content:""; position:absolute; inset:0 auto 0 0; width:3px;
  background:linear-gradient(180deg,var(--a1),var(--a2)); }
.stat-label{ color:var(--muted); font-size:.72rem; letter-spacing:.14em; font-weight:600; }
.stat-value{ font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:2.15rem;
  margin-top:.35rem; line-height:1;
  background:linear-gradient(120deg,#fff,#b9a7ff); -webkit-background-clip:text;
  background-clip:text; -webkit-text-fill-color:transparent; }
.stat-cap{ color:var(--muted); font-size:.78rem; margin-top:.45rem; }

/* ---- tabs ---- */
.stTabs [data-baseweb="tab-list"]{ gap:8px; background:transparent; border-bottom:1px solid var(--border); }
.stTabs [data-baseweb="tab"]{
  background:var(--surface); border:1px solid var(--border); border-radius:12px 12px 0 0;
  padding:.55rem 1.1rem; color:var(--muted); font-weight:600; font-size:.92rem;
}
.stTabs [aria-selected="true"]{
  color:#fff; background:linear-gradient(180deg,rgba(139,92,246,.22),rgba(139,92,246,.06));
  border-color:rgba(139,92,246,.5);
}
.stTabs [data-baseweb="tab-list"] button:nth-child(3) {
  margin-left: auto !important;
}

/* ---- inputs ---- */
.stTextInput input, .stSelectbox div[data-baseweb="select"]>div{
  background:var(--surface)!important; border:1px solid var(--border)!important;
  border-radius:12px!important; color:var(--text)!important;
}
.stTextInput input:focus{ border-color:var(--a2)!important; box-shadow:0 0 0 3px rgba(168,85,247,.18)!important; }
.stTextInput label, .stSelectbox label{ color:var(--muted)!important; font-weight:600; font-size:.82rem; }

/* ---- dataframe ---- */
[data-testid="stDataFrame"]{ border:1px solid var(--border); border-radius:14px; overflow:hidden; }

/* ---- section heading + captions ---- */
h3{ font-family:'Space Grotesk',sans-serif!important; font-weight:600!important; letter-spacing:-.01em; }
.stCaption, [data-testid="stCaptionContainer"]{ color:var(--muted)!important; }

/* ---- drilldown info pill ---- */
.pill{ display:inline-flex; gap:.5rem; align-items:center; background:var(--surface);
  border:1px solid var(--border); border-radius:12px; padding:.7rem 1rem; margin:.3rem 0 .9rem;
  font-size:.92rem; }
.pill b{ color:#fff; } .pill .dot{ color:var(--a2); }
</style>
""", unsafe_allow_html=True)


# ------------------------------------------------------------------- data load

org_path = os.path.join(MAP_DIR, "org_map.json")
person_path = os.path.join(MAP_DIR, "person_map.json")

@st.cache_data(show_spinner="Loading mapping…")
def load_maps(org_mtime, person_mtime):
    if not (os.path.exists(org_path) and os.path.exists(person_path)):
        try:
            import boto3
            from dotenv import load_dotenv
            load_dotenv(os.path.join(os.path.dirname(HERE), ".env"))
            os.makedirs(MAP_DIR, exist_ok=True)
            b = os.environ["LEADS_MAPPING_BUCKET"]
            s3 = boto3.client("s3", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
            s3.download_file(b, "mapping/org_map.json", org_path)
            s3.download_file(b, "mapping/person_map.json", person_path)
        except Exception as e:
            st.error(f"Could not load maps locally or from S3: {e}")
            return {}, {}
    with open(org_path, encoding="utf-8") as f:
        org_map = json.load(f)
    with open(person_path, encoding="utf-8") as f:
        person_map = json.load(f)
    return org_map, person_map


@st.cache_data(ttl=300, show_spinner=False)
def load_saved_personas():
    try:
        from dotenv import load_dotenv
        import boto3
        load_dotenv(os.path.join(os.path.dirname(HERE), ".env"))
        dynamodb = boto3.resource(
            'dynamodb',
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        )
        table = dynamodb.Table("saved_personas")
        response = table.scan()
        return response.get("Items", [])
    except Exception:
        return []


def effective_count(o):
    return max(int(o.get("employees_claimed", 0) or 0), len(o.get("people_ids", [])))


org_mtime = os.path.getmtime(org_path) if os.path.exists(org_path) else 0
person_mtime = os.path.getmtime(person_path) if os.path.exists(person_path) else 0
org_map, person_map = load_maps(org_mtime, person_mtime)

# ----------------------------------------------------------------------- hero
st.markdown("""
<div class="hero">
  <div class="hero-badge">◆ LEADS INTELLIGENCE</div>
  <div class="hero-title">People &nbsp;↔&nbsp; Organization Mapping</div>
  <div class="hero-sub">Explore every company, drill into who works there, and filter people across your dataset.</div>
</div>
""", unsafe_allow_html=True)

links = sum(len(o.get("people_ids", [])) for o in org_map.values())
st.markdown(f"""
<div class="stat-row">
  <div class="stat-card"><div class="stat-label">ORGANIZATIONS</div>
    <div class="stat-value">{len(org_map):,}</div><div class="stat-cap">unique companies</div></div>
  <div class="stat-card"><div class="stat-label">PEOPLE</div>
    <div class="stat-value">{len(person_map):,}</div><div class="stat-cap">unique individuals</div></div>
  <div class="stat-card"><div class="stat-label">PERSON – ORG LINKS</div>
    <div class="stat-value">{links:,}</div><div class="stat-cap">employment connections</div></div>
</div>
""", unsafe_allow_html=True)

tab_orgs, tab_people, tab_multiorg = st.tabs(["🏢  Organizations", "👤  People", "🔗  Multi Org"])

# ---------------------------------------------------------------- Organizations
with tab_orgs:
    # --- Box 1: Organization Profile Details (New Table) ---
    st.markdown("### 🏢 Organization Profiles (Financials & Growth)")
    q1 = st.text_input("Search organization profiles by name", key="org_q1", placeholder="Search by name…").strip().lower()
    
    rows1 = [(oid, o) for oid, o in org_map.items() if not q1 or q1 in o.get("name", "").lower()]
    rows1.sort(key=lambda kv: len(kv[1].get("people_ids", [])), reverse=True)
    st.caption(f"{len(rows1):,} organizations match profile search")

    st.dataframe(
        [{
            "Organization": o.get("name", ""),
            "Industry": o.get("industry", ""),
            "Employees": int(o.get("employees_claimed", 0)) if o.get("employees_claimed", 0) > 0 else None,
            "Revenue": int(o.get("revenue", 0)) if o.get("revenue", 0) > 0 else None,
            "Total Funding": int(o.get("total_funding", 0)) if o.get("total_funding", 0) > 0 else None,
            "Latest Funding": int(o.get("latest_funding", 0)) if o.get("latest_funding", 0) > 0 else None,
            "Founded Year": int(o.get("founded_year", 0)) if o.get("founded_year", 0) > 0 else None,
        } for oid, o in rows1[:500]],
        use_container_width=True,
        hide_index=True,
        height=300,
        column_config={
            "Employees": st.column_config.NumberColumn("Employees", format="%,d"),
            "Revenue": st.column_config.NumberColumn("Revenue", format="%,dK"),
            "Total Funding": st.column_config.NumberColumn("Total Funding", format="$%,d"),
            "Latest Funding": st.column_config.NumberColumn("Latest Funding", format="$%,d"),
            "Founded Year": st.column_config.NumberColumn("Founded Year", format="%d"),
        }
    )

    st.markdown("---")

    # --- Box 2: Organization Connection Stats (Existing Table) ---
    st.markdown("### 📊 Organization Connection Statistics")
    q2 = st.text_input("Search connection statistics by name", key="org_q2", placeholder="Search by name…").strip().lower()
    
    rows2 = [(oid, o) for oid, o in org_map.items() if not q2 or q2 in o.get("name", "").lower()]
    rows2.sort(key=lambda kv: len(kv[1].get("people_ids", [])), reverse=True)
    st.caption(f"{len(rows2):,} organizations match connection search")

    st.dataframe(
        [{
            "Organization": o.get("name", ""),
            "Industry": o.get("industry", ""),
            "Claimed employees": int(o.get("employees_claimed", 0) or 0),
            "People we have": len(o.get("people_ids", [])),
            "Effective count": effective_count(o),
        } for oid, o in rows2[:500]],
        use_container_width=True, hide_index=True, height=300,
    )

    st.markdown("---")

    # --- Box 3: Drill Down ---
    st.markdown("### Drill down — employees of an organization")
    # Union/fallback search filter for drill down:
    q_drill = q1 if q1 else q2
    drill_rows = [(oid, o) for oid, o in org_map.items() if not q_drill or q_drill in o.get("name", "").lower()]
    drill_rows.sort(key=lambda kv: len(kv[1].get("people_ids", [])), reverse=True)
    names = {f"{o.get('name','')}   ·   {len(o.get('people_ids',[]))} people": oid for oid, o in drill_rows[:500]}
    
    pick = st.selectbox("Pick an organization", ["—"] + list(names.keys()))
    if pick != "—":
        o = org_map[names[pick]]
        st.markdown(
            f"<div class='pill'><b>{o.get('name','')}</b> <span class='dot'>•</span> {o.get('industry','') or 'n/a'} "
            f"<span class='dot'>•</span> claimed <b>{int(o.get('employees_claimed',0) or 0):,}</b> "
            f"<span class='dot'>•</span> we have <b>{len(o.get('people_ids',[])):,}</b></div>",
            unsafe_allow_html=True)
        st.dataframe(
            [{
                "Name": person_map.get(pid, {}).get("name", ""),
                "Title": person_map.get(pid, {}).get("title", ""),
                "Email": person_map.get(pid, {}).get("email", ""),
                "Location": person_map.get(pid, {}).get("location", ""),
            } for pid in o.get("people_ids", [])],
            use_container_width=True, hide_index=True, height=320,
        )

# ----------------------------------------------------------------------- People
with tab_people:
    # --- Load Saved Personas ---
    personas = load_saved_personas()
    selected_rules = {}
    if personas:
        persona_display_map = {}
        for p in personas:
            p_id = p.get("Persona_ID", "")
            p_name = p.get("Name", "").strip() or f"Unnamed ({p_id})"
            persona_display_map[p_name] = p
            
        persona_names = ["— Select a Persona —"] + list(persona_display_map.keys())
        selected_persona_name = st.selectbox("🎯 Quick Filter by Saved Persona", persona_names, key="selected_persona")
        if selected_persona_name != "— Select a Persona —":
            p = persona_display_map.get(selected_persona_name, {})
            selected_rules = p.get("rules", {})
            st.info(f"Applied rules for **{selected_persona_name}**: " + ", ".join(f"*{k}* = `{v}`" for k, v in selected_rules.items() if v))
    else:
        st.info("💡 Tip: Save persona filters in the API/DynamoDB to see them here as quick filters!")

    f1, f2, f3, f4, f5 = st.columns(5)
    
    # Pre-fill inputs with selected persona rules if available
    nq = f1.text_input("Name contains", placeholder="john").strip().lower()
    
    default_title = selected_rules.get("position", "")
    tq = f2.text_input("Title contains", value=default_title, placeholder="engineer").strip().lower()
    
    default_loc = selected_rules.get("location", "")
    lq = f3.text_input("Location contains", value=default_loc, placeholder="london").strip().lower()
    
    default_ind = selected_rules.get("industry", "")
    iq = f4.text_input("Industry contains", value=default_ind, placeholder="tech").strip().lower()
    
    default_dom = selected_rules.get("domain", "")
    dq = f5.text_input("Domain contains", value=default_dom, placeholder="google.com").strip().lower()

    matches = []
    for pid, p in person_map.items():
        if nq and nq not in p.get("name", "").lower():
            continue
        if tq and tq not in p.get("title", "").lower():
            continue
        if lq and lq not in p.get("location", "").lower():
            continue
            
        p_orgs = [org_map.get(oid, {}) for oid in p.get("organization_ids", [])]
        
        if iq:
            if not any(iq in org.get("industry", "").lower() for org in p_orgs):
                continue
                
        if dq:
            email_domain = p.get("email", "").split("@")[-1].lower() if "@" in p.get("email", "") else ""
            matches_domain = any(dq in org.get("website", "").lower() or dq in org.get("domain", "").lower() for org in p_orgs) or (dq in email_domain)
            if not matches_domain:
                continue
                
        matches.append((pid, p))
        if len(matches) >= 2000:
            break
            
    st.caption(f"{len(matches):,} people match (showing up to 2000)")

    st.dataframe(
        [{
            "Name": p.get("name", ""),
            "Title": p.get("title", ""),
            "Email": p.get("email", ""),
            "Location": p.get("location", ""),
            "Organizations": ", ".join(org_map.get(o, {}).get("name", o) for o in p.get("organization_ids", [])),
        } for pid, p in matches],
        use_container_width=True, hide_index=True, height=460,
    )

# -------------------------------------------------------------------- Multi Org
with tab_multiorg:
    st.markdown("### 🔗 People Associated with Multiple Organizations")
    mq = st.text_input("Search multi-org people by name", key="multi_org_q", placeholder="e.g. john, rahul…").strip().lower()

    # Pre-filter and build list of multi-org people connections
    multi_org_people = []
    for pid, p in person_map.items():
        oids = p.get("organization_ids", [])
        if len(oids) > 1:
            name = p.get("name", "")
            if mq and mq not in name.lower():
                continue
            for oid in oids:
                org_name = org_map.get(oid, {}).get("name", oid)
                multi_org_people.append({
                    "Person ID": pid,
                    "Name": name,
                    "Organization": org_name,
                    "Title": p.get("title", ""),
                    "Email": p.get("email", ""),
                    "Location": p.get("location", ""),
                    "LinkedIn": p.get("linkedin", ""),
                })

    # Sort so that the same person (by name and ID) is grouped together
    multi_org_people.sort(key=lambda x: (x["Name"].lower(), x["Person ID"]))
    
    st.caption(f"{len(multi_org_people):,} employment connections found")

    st.dataframe(
        [{
            "Name": r["Name"],
            "Organization": r["Organization"],
            "Title": r["Title"],
            "Email": r["Email"],
            "Location": r["Location"],
            "LinkedIn": r["LinkedIn"],
        } for r in multi_org_people[:2000]],
        use_container_width=True,
        hide_index=True,
        height=500
    )
