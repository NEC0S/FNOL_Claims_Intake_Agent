"""
FNOL Claims Intake — multi-agent backend on LangGraph
======================================================

Same problem as the original notebook (First Notice of Loss intake for an
auto insurer), rebuilt as an actual LangGraph agent graph instead of a
straight-line function pipeline.

Deliberately dropped vs. the original, per request:
  - photo / image_descriptions handling entirely
  - the "review the drafted email before sending" step (email is just sent)

Everything else keeps the same shape (hardcoded DB/date/math where a human
wouldn't want a model guessing, LLM nodes only where judgment is actually
required) but each LLM step is now its own graph node — a specialized agent
with its own system prompt and its own slice of state — instead of a bag of
`call_llm(...)` functions invoked in sequence. A final "adjudicator" node
reads every other agent's output and produces the decision + report, which
is the "multiple specialized LLMs -> one final answer" pattern you asked for.

Run modes
---------
MOCK_LLM = True (default): every agent node uses a small deterministic stand-in
so the whole graph runs with zero API key / internet. Flip to False and set
OPENAI_API_KEY (or point base_url at Gemini's OpenAI-compatible endpoint, same
as the original notebook) to go live. Every live call still has an inline
fallback to its own mock, so a bad key never crashes the graph mid-run.

Install:  pip install langgraph langchain-openai --break-system-packages
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import textwrap
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import StateGraph, END

try:
    from dotenv import load_dotenv
    load_dotenv()  # pulls in .env if present; no-op / harmless if it isn't
except ImportError:
    pass  # dotenv is optional -- env vars still work if set some other way

# ---------------------------------------------------------------------------
# Config -- all overridable via .env / real environment variables
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    return default if val is None else val.strip().lower() in ("1", "true", "yes")


MOCK_LLM = _env_bool("MOCK_LLM", True)  # flip to false + set LLM_API_KEY to go live
DRY_RUN_EMAIL = _env_bool("DRY_RUN_EMAIL", True)

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-2.0-flash")

AUTO_APPROVE_PAYOUT_LIMIT = 1500.0
RISK_SCORE_ESCALATE_THRESHOLD = 0.3
REQUIRED_CLAIM_FIELDS = ["policy_number", "incident_date", "location", "description"]

_llm_client = None
if not MOCK_LLM and LLM_API_KEY:
    from openai import OpenAI  # lazy import, only needed live
    _llm_client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)


def call_llm(system_prompt: str, user_prompt: str, mock_response: Any) -> Any:
    """One thin wrapper every agent node calls through. Mock mode (or any live
    failure) returns `mock_response` so a demo run never crashes mid-graph."""
    if MOCK_LLM or _llm_client is None:
        return mock_response
    try:
        resp = _llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw)
    except Exception as exc:
        print(f"[call_llm] live call failed ({exc!r}) -- falling back to mock.")
        return mock_response


# ---------------------------------------------------------------------------
# Synthetic policy DB + hardcoded lookups (no AI — same as original notebook)
# ---------------------------------------------------------------------------

_db = sqlite3.connect(":memory:")
_db.execute("""
    CREATE TABLE policies (
        policy_number TEXT PRIMARY KEY, holder_name TEXT, address TEXT,
        coverage_limit REAL, deductible REAL, start_date TEXT, end_date TEXT
    )
""")
_db.executemany(
    "INSERT INTO policies VALUES (?, ?, ?, ?, ?, ?, ?)",
    [
        ("POL-1001", "Ravi Sharma", "12 MG Road, Patna, Bihar", 50000.0, 2000.0, "2025-01-01", "2026-12-31"),
        ("POL-1002", "Ananya Gupta", "45 Park Street, Kolkata, WB", 30000.0, 1500.0, "2025-06-01", "2026-05-31"),
        ("POL-1003", "Farid Khan", "9 Residency Road, Bengaluru, KA", 75000.0, 3000.0, "2024-01-01", "2025-12-31"),
        ("POL-1004", "Meera Nair", "22 Marine Drive, Mumbai, MH", 40000.0, 2500.0, "2025-03-15", "2027-03-14"),
    ],
)
_db.commit()

claims_log: Dict[str, Dict[str, Any]] = {}  # audit trail + review queue


def lookup_policy(policy_number: str) -> Optional[Dict[str, Any]]:
    row = _db.execute(
        "SELECT policy_number, holder_name, address, coverage_limit, deductible, "
        "start_date, end_date FROM policies WHERE policy_number = ?",
        (policy_number,),
    ).fetchone()
    if row is None:
        return None
    keys = ["policy_number", "holder_name", "address", "coverage_limit", "deductible", "start_date", "end_date"]
    return dict(zip(keys, row))


def check_coverage_window(incident_date_str: str, policy: Dict[str, Any]) -> Dict[str, Any]:
    incident_dt = datetime.strptime(incident_date_str, "%Y-%m-%d").date()
    start_dt = datetime.strptime(policy["start_date"], "%Y-%m-%d").date()
    end_dt = datetime.strptime(policy["end_date"], "%Y-%m-%d").date()
    in_window = start_dt <= incident_dt <= end_dt
    return {
        "in_window": in_window,
        "reason": (
            f"Incident date {incident_date_str} falls within active coverage "
            f"({policy['start_date']} to {policy['end_date']})."
            if in_window else
            f"Incident date {incident_date_str} is OUTSIDE the coverage window "
            f"({policy['start_date']} to {policy['end_date']})."
        ),
    }


def check_duplicate_claim(policy_number: str, incident_date_str: str, exclude_claim_id: str) -> bool:
    return any(
        cid != exclude_claim_id
        and c["claim"].get("policy_number") == policy_number
        and c["claim"].get("incident_date") == incident_date_str
        for cid, c in claims_log.items()
        if c.get("claim")
    )


def check_weather(location: str, incident_date_str: str, claimed_damage_type: str) -> Dict[str, Any]:
    """Deterministic synthetic stand-in for a real weather API call."""
    day = int(incident_date_str[-2:])
    condition = "clear" if day % 2 == 0 else "storm_with_hail"
    matches = True if claimed_damage_type != "hail" else (condition == "storm_with_hail")
    return {"condition": condition, "source": "MOCK weather", "matches_claim": matches}


def calculate_payout(damage_estimate: float, policy: Dict[str, Any]) -> float:
    covered = min(damage_estimate, policy["coverage_limit"])
    return max(0.0, covered - policy["deductible"])


def send_email(to_addr: str, body_with_subject: str) -> None:
    subject_line, _, body = body_with_subject.partition("\n\n")
    subject = subject_line.replace("Subject:", "").strip()
    if DRY_RUN_EMAIL:
        print(f"--- [DRY RUN] would email {to_addr} ---\nSubject: {subject}\n{body}\n--- end email ---")
        return
    raise NotImplementedError("Wire up real SMTP here if you turn DRY_RUN_EMAIL off.")


# ---------------------------------------------------------------------------
# Shared graph state
# ---------------------------------------------------------------------------

class ClaimState(TypedDict, total=False):
    claim_id: str
    raw_email_text: str
    claimant_email: str
    damage_estimate: float

    claim: Dict[str, Any]
    missing_fields: List[str]
    followup_email: str

    policy: Optional[Dict[str, Any]]
    coverage_check: Dict[str, Any]
    is_duplicate: bool
    weather_check: Dict[str, Any]
    risk_notes: Dict[str, Any]
    payout: float

    decision: Dict[str, Any]
    report: str
    status: str


# ---------------------------------------------------------------------------
# Agent 1 — Extraction (LLM node: free text -> structured JSON)
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM_PROMPT = """You are a claims intake parser for an auto insurer.
Read the claimant's free-text email and return ONLY a JSON object with exactly
these keys: policy_number, incident_date (YYYY-MM-DD or null), location,
description, fault_claimed, damage_type. No prose, no markdown fences."""


def _mock_extract(email_text: str) -> Dict[str, Any]:
    policy_match = re.search(r"POL-\d+", email_text)
    date_match = re.search(r"\d{4}-\d{2}-\d{2}", email_text)
    location_match = re.search(
        r"(?:at|near|outside)\s+([A-Za-z0-9,.\s]+?)(?:\.|,\s*on|\son\s\d{4}|$)", email_text
    )
    return {
        "policy_number": policy_match.group(0) if policy_match else None,
        "incident_date": date_match.group(0) if date_match else None,
        "location": location_match.group(1).strip() if location_match else None,
        "description": email_text.strip(),
        "fault_claimed": "other party" if re.search(r"other (driver|party)", email_text, re.I) else "unclear",
        "damage_type": (
            "hail" if re.search(r"hail", email_text, re.I)
            else "collision" if re.search(r"hit|collision|crash", email_text, re.I)
            else "unspecified"
        ),
    }


def extraction_agent(state: ClaimState) -> Dict[str, Any]:
    extracted = call_llm(
        EXTRACTION_SYSTEM_PROMPT,
        state["raw_email_text"],
        mock_response=_mock_extract(state["raw_email_text"]),
    )
    claim = dict(state.get("claim", {}))
    claim.update({k: v for k, v in extracted.items() if v not in (None, "")})
    return {"claim": claim}


# ---------------------------------------------------------------------------
# Completeness gate — hardcoded, no AI
# ---------------------------------------------------------------------------

def completeness_gate(state: ClaimState) -> Dict[str, Any]:
    missing = [f for f in REQUIRED_CLAIM_FIELDS if not state["claim"].get(f)]
    return {"missing_fields": missing}


def route_after_completeness(state: ClaimState) -> str:
    return "incomplete" if state["missing_fields"] else "complete"


# ---------------------------------------------------------------------------
# Agent 2 — Follow-up drafter (LLM node, only runs on the incomplete branch)
# ---------------------------------------------------------------------------

FOLLOWUP_SYSTEM_PROMPT = """You are a claims intake assistant. Write a short,
warm, specific email asking the claimant for exactly the missing information
listed. Do not invent facts about their claim. Return plain text starting
with a 'Subject:' line."""

_FIELD_LABELS = {
    "policy_number": "your policy number",
    "incident_date": "the date the incident happened",
    "location": "where the incident took place",
    "description": "a short description of what happened",
}


def _mock_followup(missing: List[str]) -> str:
    asks = "; ".join(_FIELD_LABELS.get(f, f) for f in missing)
    return (
        "Subject: A couple of details needed to process your claim\n\n"
        f"Hi,\n\nThanks for reporting this. Could you reply with {asks}?\n\n"
        "Once we have that we'll keep processing right away.\n\nBest,\nClaims Team"
    )


def followup_agent(state: ClaimState) -> Dict[str, Any]:
    missing = state["missing_fields"]
    body = call_llm(
        FOLLOWUP_SYSTEM_PROMPT,
        f"Missing fields: {missing}\nKnown so far: {state['claim']}",
        mock_response=_mock_followup(missing),
    )
    send_email(state["claimant_email"], body)
    return {"followup_email": body, "status": "awaiting_info"}


# ---------------------------------------------------------------------------
# Hardcoded checks node — policy / coverage / duplicate / payout (no AI)
# ---------------------------------------------------------------------------

def policy_checks_node(state: ClaimState) -> Dict[str, Any]:
    claim = state["claim"]
    policy = lookup_policy(claim["policy_number"])
    if policy is None:
        return {"policy": None, "status": "rejected_unknown_policy"}

    coverage_check = check_coverage_window(claim["incident_date"], policy)
    is_duplicate = check_duplicate_claim(claim["policy_number"], claim["incident_date"], state["claim_id"])
    payout = calculate_payout(state.get("damage_estimate", 0.0), policy)
    return {
        "policy": policy,
        "coverage_check": coverage_check,
        "is_duplicate": is_duplicate,
        "payout": payout,
    }


def route_after_policy_checks(state: ClaimState) -> str:
    return "no_policy" if state.get("policy") is None else "ok"


# ---------------------------------------------------------------------------
# Agent 3 — Weather-consistency agent (tool-calling LLM node)
# ---------------------------------------------------------------------------

def weather_agent(state: ClaimState) -> Dict[str, Any]:
    """A 'specialized LLM' here would call check_weather as a tool then reason
    about whether the result is consistent with the claim; the tool call
    itself is deterministic so the mock IS the real logic path."""
    claim = state["claim"]
    location = claim.get("location") or state["policy"]["address"]
    weather_check = check_weather(location, claim["incident_date"], claim.get("damage_type", "unspecified"))
    return {"weather_check": weather_check}


# ---------------------------------------------------------------------------
# Agent 4 — Suspicious-language / fraud-risk agent (LLM node)
# ---------------------------------------------------------------------------

RISK_SYSTEM_PROMPT = """You are a claims fraud-language reviewer. Read the
claim description and return ONLY a JSON object:
{"risk_score": <0.0-1.0>, "notes": "<short reason>"}.
Higher score = more suspicious phrasing or internal inconsistency. Do not
accuse anyone of fraud outright -- just flag language patterns worth a human
look."""

_SUSPICIOUS_PHRASES = ["cash only", "no police report", "totaled it myself", "pay me directly", "don't tell my insurer"]


def _mock_risk(description: str) -> Dict[str, Any]:
    hits = [p for p in _SUSPICIOUS_PHRASES if p in description.lower()]
    score = min(1.0, 0.15 * len(hits) + (0.2 if len(description.split()) < 6 else 0))
    return {
        "risk_score": round(score, 2),
        "notes": f"Flagged phrases: {hits}" if hits else "No obvious red-flag phrasing detected.",
    }


def risk_language_agent(state: ClaimState) -> Dict[str, Any]:
    description = state["claim"].get("description", "")
    risk_notes = call_llm(RISK_SYSTEM_PROMPT, f"Claim description:\n{description}", mock_response=_mock_risk(description))
    return {"risk_notes": risk_notes}


# ---------------------------------------------------------------------------
# Decision node — hardcoded thresholds, combines the three specialist outputs
# ---------------------------------------------------------------------------

def decision_node(state: ClaimState) -> Dict[str, Any]:
    payout = state["payout"]
    weather_check = state["weather_check"]
    risk_notes = state["risk_notes"]
    coverage_check = state["coverage_check"]

    reasons: List[str] = []
    escalate = False

    if not coverage_check["in_window"]:
        escalate = True
        reasons.append(coverage_check["reason"])
    if state["is_duplicate"]:
        escalate = True
        reasons.append("Possible duplicate claim already on file for this policy/date.")
    if payout > AUTO_APPROVE_PAYOUT_LIMIT:
        escalate = True
        reasons.append(f"Payout ${payout:,.2f} exceeds auto-approve limit of ${AUTO_APPROVE_PAYOUT_LIMIT:,.2f}.")
    if weather_check.get("matches_claim") is False:
        escalate = True
        reasons.append("Claimed damage is inconsistent with historical weather data for that date/location.")
    if risk_notes.get("risk_score", 0) >= RISK_SCORE_ESCALATE_THRESHOLD:
        escalate = True
        reasons.append(f"Language risk score {risk_notes.get('risk_score')} meets/exceeds threshold {RISK_SCORE_ESCALATE_THRESHOLD}.")

    if not reasons:
        reasons.append("In coverage window; not a duplicate; payout within limit; weather consistent; language risk low.")

    decision = {"decision": "escalate_to_manager" if escalate else "auto_approve", "reasons": reasons}
    status = decision["decision"]
    return {"decision": decision, "status": status}


# ---------------------------------------------------------------------------
# Agent 5 — Adjudicator / report writer (LLM node — reads every agent's output)
# ---------------------------------------------------------------------------

REPORT_SYSTEM_PROMPT = """You are writing a regulator-ready claims decision
report. Summarise the claim, every check performed, its data source, and the
final decision with explicit reasons. Be factual and concise -- do not add
checks that weren't performed."""


def _mock_report(state: ClaimState) -> str:
    c, p = state["claim"], state["policy"]
    return textwrap.dedent(f"""
        CLAIM DECISION REPORT
        Claim ID: {state['claim_id']}
        Generated: {datetime.now(timezone.utc).isoformat()}

        Policy: {c.get('policy_number')} ({p['holder_name']})
        Incident: {c.get('incident_date')} at {c.get('location')}
        Description: {c.get('description')}

        Coverage check: {state['coverage_check']['reason']}
        Duplicate check: {'FLAGGED as possible duplicate' if state['is_duplicate'] else 'no duplicate found'}
        Weather cross-check: {state['weather_check']['condition']} (source: {state['weather_check']['source']}, matches claim: {state['weather_check']['matches_claim']})
        Language risk score: {state['risk_notes']['risk_score']} -- {state['risk_notes']['notes']}
        Calculated payout: ${state['payout']:,.2f}

        DECISION: {state['decision']['decision'].upper()}
        Reason(s): {'; '.join(state['decision']['reasons'])}
    """).strip()


def adjudicator_agent(state: ClaimState) -> Dict[str, Any]:
    bundle = {k: state[k] for k in (
        "claim_id", "claim", "policy", "coverage_check", "is_duplicate",
        "weather_check", "risk_notes", "payout", "decision",
    )}
    report = call_llm(REPORT_SYSTEM_PROMPT, json.dumps(bundle, default=str), mock_response=_mock_report(state))
    return {"report": report}


# ---------------------------------------------------------------------------
# Build the graph
# ---------------------------------------------------------------------------

def build_graph():
    g = StateGraph(ClaimState)

    g.add_node("extract", extraction_agent)
    g.add_node("completeness_gate", completeness_gate)
    g.add_node("followup", followup_agent)
    g.add_node("policy_checks", policy_checks_node)
    g.add_node("weather_agent", weather_agent)
    g.add_node("risk_agent", risk_language_agent)
    g.add_node("decision", decision_node)
    g.add_node("adjudicator", adjudicator_agent)

    g.set_entry_point("extract")
    g.add_edge("extract", "completeness_gate")

    g.add_conditional_edges(
        "completeness_gate",
        route_after_completeness,
        {"incomplete": "followup", "complete": "policy_checks"},
    )
    g.add_edge("followup", END)

    g.add_conditional_edges(
        "policy_checks",
        route_after_policy_checks,
        {"no_policy": END, "ok": "weather_agent"},
    )

    # weather + risk are independent specialist agents; run risk right after
    # weather (LangGraph also supports true fan-out via multiple edges from
    # one node if you want them literally parallel — see note below).
    g.add_edge("weather_agent", "risk_agent")
    g.add_edge("risk_agent", "decision")
    g.add_edge("decision", "adjudicator")
    g.add_edge("adjudicator", END)

    return g.compile()


graph = build_graph()


# ---------------------------------------------------------------------------
# Orchestration helpers (thin wrappers around graph.invoke)
# ---------------------------------------------------------------------------

def process_claim(
    raw_email_text: str,
    claimant_email: str,
    damage_estimate: float = 0.0,
    claim_id: Optional[str] = None,
) -> Dict[str, Any]:
    claim_id = claim_id or f"CLM-{uuid.uuid4().hex[:8].upper()}"
    state: ClaimState = {
        "claim_id": claim_id,
        "raw_email_text": raw_email_text,
        "claimant_email": claimant_email,
        "damage_estimate": damage_estimate,
        "claim": {},
        "status": "new",
    }
    result = graph.invoke(state)
    claims_log[claim_id] = result
    return result


def resume_claim(claim_id: str, followup_email_text: str, damage_estimate: Optional[float] = None) -> Dict[str, Any]:
    prior = claims_log[claim_id]
    state: ClaimState = {
        "claim_id": claim_id,
        "raw_email_text": followup_email_text,
        "claimant_email": prior["claimant_email"],
        "damage_estimate": damage_estimate if damage_estimate is not None else prior.get("damage_estimate", 0.0),
        "claim": dict(prior.get("claim", {})),
        "status": "new",
    }
    result = graph.invoke(state)
    claims_log[claim_id] = result
    return result


def print_review_queue() -> None:
    queue = {cid: c for cid, c in claims_log.items() if c.get("status") in ("awaiting_info", "escalate_to_manager")}
    if not queue:
        print("Review queue is empty.")
        return
    for cid, c in queue.items():
        print(f"- {cid}: status={c['status']}  policy={c.get('claim', {}).get('policy_number')}")


# ---------------------------------------------------------------------------
# Test scenarios (mirrors the original notebook, minus photo fields)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== Scenario A: clean, complete claim (expect auto_approve) ===")
    email_a = (
        "Hi, my policy is POL-1001. My car was hit while parked outside 12 MG Road, "
        "Patna on 2026-08-14. The other driver left before I could get their details."
    )
    record_a = process_claim(email_a, "ravi.sharma@example.com", damage_estimate=2200.0)
    print("STATUS:", record_a["status"])
    print(record_a["report"], "\n")

    print("=== Scenario B: incomplete claim, then resumed ===")
    email_b_incomplete = "Hi, policy POL-1002, someone hit my car."
    record_b = process_claim(email_b_incomplete, "ananya.gupta@example.com")
    print("STATUS AFTER FIRST PASS:", record_b["status"])
    print("MISSING FIELDS:", record_b["missing_fields"])

    email_b_followup = "It happened on 2026-03-15 at 45 Park Street, Kolkata."
    record_b = resume_claim(record_b["claim_id"], email_b_followup, damage_estimate=900.0)
    print("STATUS AFTER RESUME:", record_b["status"])
    print(record_b["report"], "\n")

    print("=== Scenario C: inconsistent / high-value claim (expect escalate) ===")
    email_c = (
        "Policy POL-1004. Hail storm damaged my car badly near 22 Marine Drive, Mumbai "
        "on 2026-08-12, cash only please, no police report needed, totaled it myself basically."
    )
    record_c = process_claim(email_c, "meera.nair@example.com", damage_estimate=30000.0)
    print("STATUS:", record_c["status"])
    print("RISK NOTES:", record_c["risk_notes"])
    print("WEATHER CHECK:", record_c["weather_check"])
    print(record_c["report"], "\n")

    print("=== Manager review queue ===")
    print_review_queue()
