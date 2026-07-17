# Lead Management System (Monorepo)

Welcome to the **Lead Management System** repository. This project is structured as a monorepo consisting of two major, self-contained subsystems that work together to discover, enrich, validate, and search B2B leads.

---

## 📂 Repository Architecture

```text
lead-management-system/ (Git Repository Root)
├── README.md                      <-- This navigation guide
│
├── lead_management_system/
│   ├── lead_enrichment_system/    <-- Part 1: FastAPI Gateway, DynamoDB database, & Streamlit Dashboard
│   │   ├── README.md              <-- Technical Deep-Dive for Part 1
│   │   ├── lead_clean/            <-- ETL clean orchestrator, models & schemas
│   │   └── requirements.txt
│   │
│   └── scrape_and_validate_kit/   <-- Part 2: Local lead discovery LangGraph agent & phone validator
│       ├── README.md              <-- Technical Deep-Dive for Part 2
│       ├── lead_gen/              <-- LangGraph scraping runner & agents
│       ├── lead_val/              <-- Local CSV phone and domain tags validator
│       └── requirements.txt
```

---

## ⚡ Subsystem Overviews

### 🏢 1. Lead Enrichment System (`/lead_management_system/lead_enrichment_system`)
A cloud-native, crash-proof ETL platform that turns raw incoming lead CSV sheets into a **deduplicated, AI-enriched, instantly searchable lead database** — and serves it to any client UI through a FastAPI gateway.

*   **Key Features:** Auto-detects custom column names, streams raw audit replicas, creates permanent user identity hashes, runs a round-robin multi-model Gemini router for cost-friendly enrichment, and backs data in DynamoDB across 5 purpose-built tables.
*   **UI Components:** Includes a premium Streamlit dashboard showcasing company financial profiles, employee mappings, relationship statistics, and saved persona filters.
*   **Learn More:** Read the [Lead Enrichment System README](lead_management_system/lead_enrichment_system/README.md).

```mermaid
graph TD
    %% ETL Pipeline
    CSV[Incoming Raw CSVs] -->|Upload| S3_Raw[(AWS S3: raw/)]
    S3_Raw -->|Trigger Run| Orchestrator[clean_orchestrator.py]
    
    Orchestrator -->|Part 1: Audit Copy| DDB_Raw[(DynamoDB: raw_data_leads)]
    Orchestrator -->|Part 2: Standardize & Fingerprint| Dedup{Seen Before?}
    
    Dedup -->|Yes: Track Duplicate| DDB_Dup[(DynamoDB: duplicate_raw_leads)]
    Dedup -->|No: New Contact| Scrape[Website Scraper]
    
    Scrape -->|Cache Text| DDB_Context[(DynamoDB: cleaned_leads_scrape_context)]
    Scrape -->|Enrich| Router[smart_llm_router.py]
    
    Router -->|Gemini / Gemma / Vertex Rotation| AI[Gemini API]
    AI -->|Snap to Config Taxonomy| DDB_Clean[(DynamoDB: cleaned_data_leads)]
    
    %% Backup
    Orchestrator -.->|State Backup/Restore| S3_State[(AWS S3: state/)]

    %% FastAPI Gateway
    DDB_Clean <-->|Query leads| PersonaAPI[persona_api.py - FastAPI Gateway]
    DDB_Saved[(DynamoDB: saved_personas)] <-->|Manage Rules| PersonaAPI
    
    %% Streamlit UI & Mapping
    Mapping_ETL[build_mapping.py] -->|Compile Maps| Local_Maps[(Local JSON Maps)]
    S3_Map[(AWS S3: mapping/)] <-->|Backup/Sync| Mapping_ETL
    
    Local_Maps -->|Render Tables| Streamlit[mapping_ui.py - Streamlit UI]
    DDB_Saved -.->|Load Quick Filters| Streamlit

    style Streamlit fill:#a855f7,stroke:#fff,stroke-width:2px,color:#fff
    style PersonaAPI fill:#6366f1,stroke:#fff,stroke-width:2px,color:#fff
    style Orchestrator fill:#22d3ee,stroke:#fff,stroke-width:2px,color:#fff
```

---

### 🛠️ 2. Scrape & Validate Kit (`/lead_management_system/scrape_and_validate_kit`)
A local-first, clone-and-run search tool to discover business leads from raw sources (SerpAPI Google Maps / JustDial) and run offline validations.

*   **Key Features:** A stateful LangGraph agentic discovery pipeline, browser-based web scraper (Playwright-ready), offline phone number parser, and an AI taxonomy validator.
*   **Taxonomy Sync:** Shares config files (snaps categories dynamically to `domains_subdomains.json`) and environment defaults with the main Enrichment system.
*   **Learn More:** Read the [Scrape & Validate Kit README](lead_management_system/scrape_and_validate_kit/README.md).

```mermaid
graph TD
    %% Row 1: Plan & Discover
    Start([Start Plan]) --> Research[domain_research: Target list from domains.json]
    Research --> Discovery[lead_discovery: SerpAPI/JustDial]
    Discovery --> Dedup{dedup_filter: Dup?}
    
    Dedup -->|Yes| Discovery
    Dedup -->|No| Verify[biz_verify: Check active status]

    %% Row 2: Scraping & Intelligence
    Verify --> Contacts[contact_discovery: Scrape websites]
    Contacts --> Intel[decision_maker_intel: Match titles via Apollo/Hunter]
    Intel --> PhoneVal[contact_verify: Basic formatting]
    PhoneVal --> Enrich[enrichment: Gemini model rotation]

    %% Row 3: QA Gate & Save
    Enrich --> QAGate{qa_gate: Targets met?}
    QAGate -->|No: Need more leads| Discovery
    QAGate -->|Yes| Writer[csv_writer: Append CSV]

    %% Connection to validation
    Writer -.->|Generated Raw CSV| RawCSV[Messy Lead CSV]
    
    subgraph Lead_Val_System ["2. Offline Lead Validation Subsystem"]
        RawCSV --> Detector[column_detector: Auto-detect name/phone headers]
        Detector --> OfflinePhone[phone_validator: Validate numbers via offline library]
        OfflinePhone --> Classifier[category_classifier: Taxonomy-bound LLM classification]
        Classifier --> CleanCSV[Validated & Enriched CSV Output]
    end

    %% Styles
    style Start fill:#22d3ee,stroke:#fff,stroke-width:1px,color:#fff
    style Writer fill:#22d3ee,stroke:#fff,stroke-width:1px,color:#fff
    style RawCSV fill:#a855f7,stroke:#fff,stroke-width:1px,color:#fff
    style CleanCSV fill:#a855f7,stroke:#fff,stroke-width:1px,color:#fff
```

---

## ⚙️ Shared Setup & Environment Fallback

Both projects can be configured independently or run using shared resources. 

*   **Credentials Fallback:** Any environment variable not explicitly defined in `scrape_and_validate_kit/.env` automatically falls back to read from `lead_enrichment_system/.env` when run within this workspace, preventing double configuration.
*   **Configuration rules:** Copy `.env.example` to `.env` in both folders and input your Gemini AI Studio keys, AWS access keys, and optionally SerpAPI/Apollo keys.

---

For deep setup instructions, API contracts, deployment configurations (Modal/cron support), and database models, click through to the respective subfolders.
