# FNOL Claims Intake Agent

### Multi-Agent AI Workflow for Automated Auto-Insurance Claim Intake, Validation & Human Escalation

A production-oriented **First Notice of Loss (FNOL)** claims intake system built with **LangGraph, LLMs, Supabase/PostgreSQL, and email integration**.

The system converts unstructured customer claim emails into structured claim records, validates policy coverage, checks for duplicate incidents, performs weather consistency analysis, evaluates suspicious claim language, calculates a potential payout, and applies deterministic business rules to decide whether a claim can be automatically approved or requires human review.

> **Important:** This project is designed as an AI engineering prototype/reference architecture. It is not intended to independently make legally binding insurance decisions without appropriate human oversight, compliance controls, and production validation.

---

## Overview

**FNOL (First Notice of Loss)** is the initial notification an insurance company receives when a customer reports an incident.

Traditional FNOL intake often involves manually reading emails, extracting claim information, looking up policy details, checking supporting signals, and routing the claim to the appropriate team.

This project demonstrates how that workflow can be organized as a **stateful multi-agent LangGraph pipeline**, while deliberately keeping critical business decisions deterministic and auditable.

The system supports two intake modes:

* **Manual claim submission** through `process_claim()`
* **Email-based intake** through an IMAP inbox using `process_inbox()`

The architecture is intentionally hybrid:

* **LLMs** handle unstructured-language tasks.
* **Deterministic Python logic** handles business rules and validation.
* **Supabase/PostgreSQL** provides persistent claim and policy storage.
* **IMAP/SMTP** enables real email-based intake and follow-up.
* **Mock fallbacks** allow the entire workflow to run without external credentials.

---

# Key Capabilities

| Capability                      | Implementation                   |
| ------------------------------- | -------------------------------- |
| Unstructured claim extraction   | LLM                              |
| Required-field validation       | Deterministic Python             |
| Missing-information follow-up   | LLM + SMTP                       |
| Policy lookup                   | Supabase/PostgreSQL              |
| Coverage-window validation      | Deterministic date logic         |
| Duplicate incident detection    | Database query                   |
| Duplicate claim superseding     | Deterministic database logic     |
| Weather consistency check       | Weather tool abstraction         |
| Claim-language risk assessment  | LLM                              |
| Payout calculation              | Deterministic business logic     |
| Final approve/escalate decision | Deterministic rules              |
| Decision report generation      | LLM                              |
| Email-based claim intake        | IMAP                             |
| Human review queue              | Database-backed status filtering |
| Mock/offline execution          | Built-in fallbacks               |

The graph separates **language understanding** from **business-critical decision logic**, preventing an LLM from directly determining the final business outcome.

---

# Architecture

```text
                         ┌─────────────────────┐
                         │ Customer Claim Email │
                         └──────────┬──────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │   Claim Extraction  │
                         │      LLM Agent      │
                         └──────────┬──────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │ Completeness Gate   │
                         │   Deterministic     │
                         └──────────┬──────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    │                               │
              Incomplete                         Complete
                    │                               │
                    ▼                               ▼
          ┌──────────────────┐            ┌──────────────────┐
          │ Follow-up Agent  │            │  Policy Checks   │
          │       LLM        │            │  Deterministic   │
          └────────┬─────────┘            └────────┬─────────┘
                   │                               │
                   ▼                               ▼
             SMTP / Email                 ┌─────────────────┐
                                           │ Policy Coverage │
                                           │ + Duplicate     │
                                           │ + Payout Check  │
                                           └────────┬────────┘
                                                    │
                                                    ▼
                                           ┌─────────────────┐
                                           │ Weather Agent   │
                                           │ Tool / LLM      │
                                           └────────┬────────┘
                                                    │
                                                    ▼
                                           ┌─────────────────┐
                                           │ Risk Agent      │
                                           │      LLM        │
                                           └────────┬────────┘
                                                    │
                                                    ▼
                                           ┌─────────────────┐
                                           │ Decision Engine │
                                           │ Deterministic   │
                                           └────────┬────────┘
                                                    │
                                                    ▼
                                           ┌─────────────────┐
                                           │   Adjudicator   │
                                           │      LLM        │
                                           └────────┬────────┘
                                                    │
                                                    ▼
                                           ┌─────────────────┐
                                           │ Claim Decision  │
                                           │     Report      │
                                           └─────────────────┘
```

---

# LangGraph Workflow

The workflow is implemented as a stateful `ClaimState` graph.

```text
START
  │
  ▼
extract
  │
  ▼
completeness_gate
  │
  ├──────── incomplete ────────► followup ───► END
  │
  └──────── complete ──────────► policy_checks
                                      │
                                      ├── unknown policy ──► END
                                      │
                                      └── valid policy
                                             │
                                             ▼
                                      weather_agent
                                             │
                                             ▼
                                        risk_agent
                                             │
                                             ▼
                                         decision
                                             │
                                             ▼
                                        adjudicator
                                             │
                                             ▼
                                            END
```

## The graph explicitly models conditional routing rather than treating the workflow as a linear LLM chain.

# Agent Responsibilities

## 1. Claim Extraction Agent

**Type:** LLM

Converts an unstructured customer email into structured claim information.

The extraction schema includes:

* Policy number
* Incident date
* Location
* Description
* Fault claimed
* Damage type

The model is instructed to return structured JSON so downstream nodes can operate on predictable fields.

### Example

Input:

```text
Hi, my policy is POL-1001. My car was hit while parked
outside 12 MG Road, Patna on 2026-08-14.
The other driver left before I could get their details.
```

Output:

```json
{
  "policy_number": "POL-1001",
  "incident_date": "2026-08-14",
  "location": "12 MG Road, Patna",
  "description": "Car was hit while parked...",
  "fault_claimed": "other party",
  "damage_type": "collision"
}
```

---

## 2. Completeness Gate

**Type:** Deterministic Python

No LLM is used for this decision.

The system verifies the presence of four required fields:

```text
policy_number
incident_date
location
description
```

If required information is missing, the claim follows the follow-up path rather than continuing to policy validation.

---

## 3. Follow-Up Agent

**Type:** LLM + SMTP

When a claim is incomplete, the system generates a targeted follow-up email requesting only the missing information.

For example:

```text
Subject: A couple of details needed to process your claim

Hi,

Thanks for reporting this. Could you reply with the date
the incident happened and where the incident took place?

Once we have that we'll keep processing right away.

Best,
Claims Team
```

The email layer supports a dry-run mode so messages can be tested without actually sending them.

---

# Policy Validation

## Policy Lookup

Policy information is retrieved from Supabase when configured.

The policy record contains:

* Policy number
* Holder name
* Customer email
* Address
* Coverage limit
* Deductible
* Coverage start date
* Coverage end date

If Supabase is not configured, the system falls back to an in-memory representation containing sample policies.

---

## Coverage Validation

Coverage is checked using deterministic date logic:

```text
policy_start <= incident_date <= policy_end
```

No LLM is involved in this calculation.

---

## Payout Calculation

The proposed payout is calculated as:

```text
covered_amount = min(damage_estimate, coverage_limit)

payout = max(0, covered_amount - deductible)
```

This keeps the financial calculation deterministic and reproducible.

---

# Duplicate Claim Handling

The system does not simply discard duplicate submissions.

If another claim exists for the same:

```text
policy_number + incident_date
```

the new claim is retained while the previous active claim is marked:

```text
status = superseded
superseded_by = <new_claim_id>
```

The new claim becomes the active record for that incident.

Both records remain available in the database, providing a basic audit trail instead of silently deleting historical information.

---

# Weather Consistency Agent

**Type:** Tool-based validation layer

The weather component cross-checks the claimed damage type against weather conditions associated with the incident date and location.

The current notebook uses a **deterministic synthetic weather implementation** rather than a live external weather API.

The implementation is intentionally structured so it can later be replaced by a real weather provider.

---

# Risk Analysis Agent

**Type:** LLM

The risk agent analyzes claim language for suspicious phrasing or internal inconsistencies.

It produces:

```json
{
  "risk_score": 0.0,
  "notes": "..."
}
```

The score is intended to identify claims requiring additional human attention rather than make an accusation of fraud.

The current system explicitly treats the model's output as a **risk signal**, not as a definitive fraud determination.

---

# Deterministic Decision Engine

One of the most important design choices in this project is that the **final business decision is not delegated to the LLM**.

The decision engine evaluates explicit business rules.

A claim is escalated when one or more of the following conditions are met:

### 1. Coverage is invalid

```text
incident date outside policy coverage window
```

### 2. Prior claim exists

```text
same policy + same incident date
```

### 3. Payout exceeds automatic approval limit

Current threshold:

```text
$1,500
```

### 4. Weather is inconsistent

```text
weather evidence contradicts claimed damage
```

### 5. Risk score exceeds threshold

Current threshold:

```text
0.30
```

If none of these conditions are triggered:

```text
auto_approve
```

Otherwise:

```text
escalate_to_manager
```

The decision node is deliberately implemented as deterministic business logic rather than model-generated judgment.

---

# Adjudicator Agent

**Type:** LLM

The adjudicator does not decide the business outcome.

Instead, it receives the outputs of the previous stages and converts them into a concise, regulator-oriented decision report.

The report includes:

* Claim summary
* Policy information
* Incident information
* Coverage result
* Prior claim information
* Weather check
* Risk assessment
* Calculated payout
* Final decision
* Decision reasons

This separation allows the LLM to perform **explanation and synthesis** without controlling the deterministic decision rules.

---

# Persistent Data Layer

The project supports **Supabase/PostgreSQL** as its persistent backend.

Three primary tables are used:

```text
customers
    │
    └── email

policies
    │
    └── policy_number

claims
    │
    ├── claim_id
    ├── policy_number
    ├── claimant_email
    ├── incident_date
    ├── location
    ├── description
    ├── damage_type
    ├── damage_estimate
    ├── payout
    ├── status
    ├── decision
    ├── coverage_check
    ├── risk_notes
    ├── weather_check
    ├── report
    ├── is_duplicate
    ├── superseded_by
    ├── source
    └── created_at
```

The schema is included directly in the notebook and can be executed from the Supabase SQL Editor.

---

# Email-Based Intake

The project can process claims directly from an email inbox.

```text
Customer
   │
   │ Email
   ▼
IMAP Inbox
   │
   ▼
Unread Claim Emails
   │
   ▼
process_inbox()
   │
   ▼
LangGraph Pipeline
   │
   ▼
Claim Record
```

The IMAP implementation retrieves unread messages using `BODY.PEEK[]`, allowing the system to avoid marking an email as read until its processing has successfully completed.

After successful processing, the email is marked as seen.

For local development, the system automatically falls back to a mock inbox when IMAP credentials are not configured.

---

# Human-in-the-Loop Design

The system is intentionally designed around **human escalation** rather than fully autonomous claim adjudication.

Claims can enter:

```text
awaiting_info
```

when additional information is required.

Or:

```text
escalate_to_manager
```

when deterministic rules identify conditions requiring human review.

The `print_review_queue()` function surfaces active claims requiring attention.

This provides a basic foundation for integrating the workflow with a future claims-review dashboard or case-management system.

---

# Mock-First Architecture

The project can run without connecting to external systems.

This is achieved through independent fallbacks:

```text
LLM
 │
 ├── Real LLM
 └── Mock response

Database
 │
 ├── Supabase
 └── In-memory storage

Email
 │
 ├── SMTP
 └── Dry-run

Inbox
 │
 ├── IMAP
 └── Mock inbox

Weather
 │
 ├── Future live provider
 └── Synthetic implementation
```

This makes the system easier to develop, test, and demonstrate before introducing production credentials.

---

# Technology Stack

### Core

* **Python 3.10+**
* **LangGraph**
* **LLM / OpenAI-compatible API interface**

### Data

* **Supabase**
* **PostgreSQL**

### Communication

* **IMAP**
* **SMTP**
* **Gmail-compatible email configuration**

### Configuration

* **python-dotenv**

### Development

* **Jupyter Notebook**

---

# Project Structure

A recommended repository structure is:

```text
FNOL_Claims_Intake_Agent/
│
├── FNOL_Claims_Intake_Agent_LangGraph_v2.ipynb
├── FNOL_Claims_Intake_Agent_LangGraph.ipynb
│
├── .env.example
├── .gitignore
├── README.md
└── requirements.txt
```

> The current implementation is notebook-based. A natural next step would be separating the graph, database layer, configuration, email integrations, and tests into independent Python modules.

---

# Installation

Clone the repository:

```bash
git clone https://github.com/NEC0S/FNOL_Claims_Intake_Agent.git
cd FNOL_Claims_Intake_Agent
```

Create a virtual environment:

```bash
python -m venv .venv
```

Activate it.

### Windows

```powershell
.venv\Scripts\activate
```

### macOS / Linux

```bash
source .venv/bin/activate
```

Install dependencies:

```bash
pip install langgraph openai python-dotenv supabase
```

The notebook imports these core libraries directly.

---

# Environment Configuration

Create a local `.env` file:

```env
# LLM
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_API_KEY=your_api_key
LLM_MODEL=gemini-2.0-flash

# LLM execution
MOCK_LLM=false

# Supabase
SUPABASE_URL=your_supabase_project_url
SUPABASE_KEY=your_backend_key

# SMTP
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your_email
SMTP_PASSWORD=your_app_password

# Email sending
DRY_RUN_EMAIL=true

# IMAP
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USER=your_email
IMAP_PASSWORD=your_app_password
IMAP_MAILBOX=INBOX
```

**Never commit `.env` or real credentials to Git.**

Use `.env.example` for documentation and `.gitignore` for local secrets.

---

# Supabase Setup

Create a Supabase project and execute the SQL schema provided in the notebook.

The schema creates:

```text
customers
policies
claims
```

and inserts sample customer/policy records for testing.

Then configure:

```env
SUPABASE_URL=...
SUPABASE_KEY=...
```

Restart the Jupyter kernel after modifying `.env` so the updated environment variables are loaded.

---

# Running the Project

Open:

```text
FNOL_Claims_Intake_Agent_LangGraph_v2.ipynb
```

Run the notebook cells sequentially.

The graph is compiled using:

```python
graph = build_graph()
```

A claim can then be submitted through:

```python
record = process_claim(
    email_text,
    claimant_email,
    damage_estimate=2200.0
)
```

The returned record contains the processing state, decision, risk information, payout, and generated report.

---

# Example Workflow

### Customer email

```text
Hi, my policy is POL-1001. My car was hit while parked
outside 12 MG Road, Patna on 2026-08-14.
The other driver left before I could get their details.
```

### Processing

```text
Email
  ↓
Extraction
  ↓
Completeness Check
  ↓
Policy Lookup
  ↓
Coverage Validation
  ↓
Duplicate Check
  ↓
Weather Check
  ↓
Risk Analysis
  ↓
Deterministic Decision
  ↓
LLM Report Generation
```

### Output

```text
Decision:
escalate_to_manager

Reasons:
- Risk threshold exceeded
- Additional human review required

Proposed payout:
$200.00
```

The notebook includes multiple test scenarios covering complete claims, incomplete claims, high-risk claims, duplicate submissions, and inbox-based intake.

---

# Test Scenarios

The project includes five representative scenarios.

### Scenario A — Complete claim

Tests a complete claim and runs the full graph.

```text
Expected:
auto_approve / escalate_to_manager
```

depending on the configured risk and decision signals.

### Scenario B — Incomplete claim

Tests:

```text
claim → missing fields → follow-up → resume
```

The claim initially enters:

```text
awaiting_info
```

and can later be resumed with the missing information.

### Scenario C — High-risk / inconsistent claim

Tests a claim containing suspicious language and a high damage estimate.

Expected:

```text
escalate_to_manager
```

### Scenario D — Duplicate incident

Submits another claim against the same policy and incident date.

Expected behavior:

```text
Previous claim → superseded
New claim      → active
```

Both records remain available.

### Scenario E — Email inbox intake

Tests:

```python
process_inbox()
```

to retrieve and process incoming claim emails.

---

# Configuration Modes

The system can be progressively enabled.

### Fully mocked

```env
MOCK_LLM=true
DRY_RUN_EMAIL=true

SUPABASE_URL=
SUPABASE_KEY=

IMAP_USER=
IMAP_PASSWORD=
```

### Live LLM + Mock database

```env
MOCK_LLM=false

SUPABASE_URL=
SUPABASE_KEY=
```

### Live LLM + Supabase

```env
MOCK_LLM=false

SUPABASE_URL=...
SUPABASE_KEY=...
```

### Full email workflow

Add:

```env
SMTP_USER=...
SMTP_PASSWORD=...

IMAP_USER=...
IMAP_PASSWORD=...
```

and configure:

```env
DRY_RUN_EMAIL=false
```

This incremental configuration model allows individual integrations to be enabled independently.

---

# Design Principles

## 1. LLMs for Language, Code for Rules

The architecture deliberately avoids using an LLM for every task.

LLMs handle:

* Information extraction
* Natural-language follow-up generation
* Risk-language analysis
* Decision-report generation

Deterministic code handles:

* Required-field validation
* Policy lookup
* Coverage dates
* Duplicate detection
* Payout calculation
* Approval thresholds
* Final escalation logic

This improves reproducibility and makes the business logic easier to inspect and test.

---

## 2. Human-in-the-Loop

The system does not attempt to replace claims professionals.

Instead, automation handles routine processing while potentially problematic claims are routed to a manager.

---

## 3. Persistent State

Claims and policy data can persist across notebook executions when Supabase is configured.

This allows workflows such as:

```text
Initial claim
    ↓
Awaiting information
    ↓
Customer responds
    ↓
Resume existing claim
    ↓
Continue processing
```

rather than treating every interaction as an entirely new claim.

---

## 4. Auditability

The claim record stores structured outputs from multiple processing stages:

```text
decision
coverage_check
risk_notes
weather_check
report
payout
status
```

This creates a traceable representation of how the final outcome was reached.

---

# Security Considerations

This project processes potentially sensitive insurance information and credentials.

### Never commit:

```text
.env
API keys
database credentials
SMTP passwords
IMAP passwords
service-role credentials
```

Use:

```gitignore
.env
.env.*
!.env.example
```

For production deployments, credentials should be stored in a dedicated secrets-management system rather than a plaintext `.env` file. The notebook itself also recommends moving the Supabase service key into a secrets manager for production.

If a credential is ever committed or pushed accidentally, **rotate/revoke it immediately** even if the repository push is subsequently blocked.

---

# Current Limitations

This repository is a strong reference implementation, but several components are intentionally simplified.

### Weather

The current weather implementation is synthetic rather than connected to a production weather provider.

### Risk Analysis

The risk score is a language-based signal and should not be interpreted as a definitive fraud determination.

### Human Review

The review queue is currently exposed through a Python function rather than a dedicated claims dashboard.

### Deployment

The current interface is a Jupyter Notebook. A production implementation should move the workflow into a service or worker architecture.

### Scheduling

`process_inbox()` is designed to be triggered manually in the notebook. Production deployments should execute it through a scheduler or background worker.

---

# Production Roadmap

Potential next steps include:

### Architecture

* Convert notebook implementation into modular Python packages
* Add FastAPI service endpoints
* Add background workers
* Add scheduled inbox processing
* Add persistent LangGraph checkpoints

### AI / Agent Layer

* Structured-output validation
* Model fallback strategy
* Retry policies
* LLM observability
* Prompt versioning
* Evaluation datasets
* Hallucination detection
* Agent-level tracing

### Claims Intelligence

* Real weather API integration
* Document/OCR ingestion
* Image-based vehicle damage assessment
* Policy document retrieval
* Retrieval-augmented generation
* Historical claims analytics
* More sophisticated anomaly detection

### Governance

* Role-based access control
* Human approval workflow
* Full audit logs
* PII protection
* Encryption
* Secrets management
* Model monitoring
* Bias and fairness evaluation
* Compliance controls

### Platform

```text
                ┌───────────────┐
                │ Customer Email│
                └───────┬───────┘
                        │
                        ▼
                ┌───────────────┐
                │   API / Queue │
                └───────┬───────┘
                        │
                        ▼
                ┌───────────────┐
                │   LangGraph   │
                │    Runtime    │
                └───────┬───────┘
                        │
        ┌───────────────┼────────────────┐
        ▼               ▼                ▼
   LLM Services    Claims DB       External Tools
        │               │                │
        └───────────────┼────────────────┘
                        ▼
                ┌───────────────┐
                │ Decision +    │
                │ Human Review  │
                └───────────────┘
```

---

# Why This Project Matters

The goal of this project is not simply to demonstrate an LLM calling another LLM.

It demonstrates a more practical pattern for **Agentic AI systems in regulated workflows**:

> **Use AI where reasoning over unstructured information is valuable, and deterministic software where consistency, control, and auditability matter.**

The resulting architecture combines:

```text
Unstructured Data
       +
LLM Reasoning
       +
Tool / Database Access
       +
Deterministic Business Rules
       +
Persistent State
       +
Human Oversight
```

This pattern is particularly relevant to enterprise AI applications where an autonomous model should not have unrestricted control over consequential business decisions.

---

# Project Status

**Status:** Prototype / Engineering Reference Implementation

The current implementation demonstrates:

* Multi-agent LangGraph orchestration
* Stateful claim processing
* LLM-based information extraction
* Conditional graph routing
* Persistent Supabase storage
* Email-based ingestion
* Automated follow-up
* Duplicate claim handling
* Risk-language analysis
* Deterministic decision rules
* Human escalation
* Generated decision reports
* Mock-to-live integration architecture

---

# License

Add the appropriate license for your repository before public distribution.

For example:

```text
MIT License
```

if you intend to release the project under the MIT license.

---

# Author

**Abhishek Kumar**

AI / Machine Learning Engineer

This project was built as an exploration of **Agentic AI, LangGraph orchestration, LLM-powered document understanding, deterministic decision systems, and enterprise workflow automation**.
