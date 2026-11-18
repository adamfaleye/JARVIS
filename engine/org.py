"""JARVIS 2.0 organization: one agent registry for both businesses.

Every agent has the same standardized identity (spec Part VIII): id, name, division, department, role,
instructions, model configuration, tools, permission level, output format. Task queue, history and
metrics come from hq_tasks/hq_events. An agent is "operational" only when it has at least one task type
with a real executor in hq.KINDS. Agents without one are listed honestly as not operational yet.

Divisions:
  molabucks  MolaBucks Capital (real estate)
  demigod    AI Sidekicks (AI automation agency)
  exec       Executive Office (serves both; every task still names the business it's for)
"""

DIVISIONS = {
    "molabucks": {"name": "MolaBucks Capital", "short": "MolaBucks", "color": "#c9a45c"},
    "demigod": {"name": "AI Sidekicks", "short": "Sidekicks", "color": "#d4d9e0"},
    "exec": {"name": "Executive Office", "short": "Executive", "color": "#4fd8ff"},
}
BUSINESSES = ("molabucks", "demigod")

# Model configuration per kind of work. The key never leaves the engine (.env).
MODELS = {"fast": "claude-haiku-5-5", "standard": "claude-opus-5-5"}

# permission: highest policy level the agent's task types may use (1 autonomous, 2 controlled, 3 needs explicit approval).
ROSTER = {
    # ---- Executive Office
    "jarvis": {"name": "JARVIS", "title": "Chief Executive Intelligence", "division": "exec", "department": "Executive Office",
               "role": "ceo", "color": "#4fd8ff", "permission": 1, "model": "standard", "tools": ["hq.plan", "hq.delegate", "hq.report"],
               "output": "plan / briefing / consolidated report",
               "mission": "Turns your goal into a plan, assigns the work, checks it, and reports honestly."},
    "chief_of_staff": {"name": "Chief of Staff", "title": "Workforce Coordination", "division": "exec", "department": "Executive Office",
                       "role": "coordination", "color": "#9fd3ff", "permission": 1, "model": "fast", "tools": ["hq.tasks", "hq.workload"],
                       "output": "workload and deadline report",
                       "mission": "Balances the workforce: assignments, workload, deadlines, cross-department coordination."},
    "finance": {"name": "Finance", "title": "Finance & Operations Analyst", "division": "exec", "department": "Executive Office",
                "role": "finance", "color": "#e8c46a", "permission": 1, "model": "fast", "tools": ["crm.read", "outreach.usage"],
                "output": "finance check (verified records only)",
                "mission": "Revenue, fees vs. goal, deadlines, costs and balances, from verified records only."},
    "research": {"name": "Research", "title": "Research & Intelligence Analyst", "division": "exec", "department": "Executive Office",
                 "role": "research", "color": "#7fe6ff", "permission": 1, "model": "standard", "tools": ["ai.draft"],
                 "output": "research brief with sources",
                 "mission": "Industry, prospect, competitor, technology and integration research."},
    # ---- MolaBucks Capital (existing, unchanged behavior)
    "hunter": {"name": "Hunter", "title": "Acquisitions Intelligence", "division": "molabucks", "department": "Acquisitions",
               "role": "acquisitions", "color": "#f3b54a", "permission": 1, "model": "fast", "tools": ["hunter.*"],
               "output": "ranked leads", "mission": "Finds, scores and organizes motivated-seller leads from public records."},
    "closer": {"name": "Closer", "title": "Dispositions & Sales", "division": "molabucks", "department": "Dispositions",
               "role": "dispositions", "color": "#46dc9c", "permission": 1, "model": "fast", "tools": ["crm.read"],
               "output": "pipeline blockers", "mission": "Moves contracted deals to buyers: deal status, buyer demand, closing dependencies."},
    "followup": {"name": "Follow-up", "title": "Pipeline Persistence", "division": "molabucks", "department": "Acquisitions",
                 "role": "followup", "color": "#a58bff", "permission": 3, "model": "fast", "tools": ["crm.read", "outreach.campaigns"],
                 "output": "due list / campaign health", "mission": "Makes sure no lead or buyer goes cold: due follow-ups, drips, reply health."},
    "frontdesk": {"name": "Front Desk", "title": "Communications Intake", "division": "molabucks", "department": "Communications",
                  "role": "intake", "color": "#ff8fb1", "permission": 1, "model": "fast", "tools": ["crm.read"],
                  "output": "triage list", "mission": "Sorts incoming texts and calls, drafts replies, escalates what matters."},
    "dispo": {"name": "Dispositions", "title": "Buyer Matching Intelligence", "division": "molabucks", "department": "Dispositions",
              "role": "buyer_matching", "color": "#3ee0c5", "permission": 1, "model": "fast", "tools": ["crm.read"],
              "output": "buyer matches", "mission": "Matches deals to buyers on real criteria and flags weak demand."},
    # ---- AI Sidekicks
    "sdr": {"name": "SDR", "title": "SDR / Appointment Setter", "division": "demigod", "department": "Sales", "role": "sdr",
            "color": "#b39bff", "permission": 3, "model": "fast", "tools": ["ai.draft"], "output": "prospect list / outreach drafts",
            "mission": "Prospect research, enrichment, outreach preparation, approved follow-up, qualification, meeting booking."},
    "ae": {"name": "Account Executive", "title": "Account Executive", "division": "demigod", "department": "Sales", "role": "ae",
           "color": "#9d86ff", "permission": 2, "model": "standard", "tools": ["ai.draft"], "output": "proposal / pricing draft",
           "mission": "Discovery and demo prep, proposals, pricing recommendations, pipeline, objections, closing support."},
    "marketing": {"name": "Marketing Director", "title": "Marketing Director", "division": "demigod", "department": "Sales",
                  "role": "marketing", "color": "#c7b8ff", "permission": 3, "model": "standard", "tools": ["ai.draft"],
                  "output": "content drafts", "mission": "LinkedIn content, demo concepts, case studies, website copy, calendar. Publishing needs approval."},
    "architect": {"name": "Solutions Architect", "title": "AI Solutions Architect", "division": "demigod", "department": "Delivery",
                  "role": "architect", "color": "#6fb7ff", "permission": 2, "model": "standard", "tools": ["ai.draft"],
                  "output": "requirements / architecture / risk notes",
                  "mission": "Requirements analysis, technical discovery, system architecture, integration plans, feasibility, risk."},
    "builder": {"name": "AI Builder", "title": "AI Builder / Developer", "division": "demigod", "department": "Delivery",
                "role": "builder", "color": "#58a6ff", "permission": 3, "model": "standard", "tools": ["ai.draft"],
                "output": "build plan / code in sandbox", "mission": "Voice agents, CRMs, automations, integrations, web apps. Production deploys need approval."},
    "qa": {"name": "QA Engineer", "title": "QA Engineer", "division": "demigod", "department": "Delivery", "role": "qa",
           "color": "#4ddbb0", "permission": 1, "model": "fast", "tools": ["ai.draft"], "output": "QA report",
           "mission": "Test calls, prompt and workflow tests, API checks, transcript review, hallucination checks, regressions."},
    "pm": {"name": "Project Manager", "title": "Project Manager", "division": "demigod", "department": "Delivery", "role": "pm",
           "color": "#7cc4ff", "permission": 2, "model": "fast", "tools": ["di.projects"], "output": "project plan / status",
           "mission": "Onboarding, requirements, plans, milestones, assignments, delivery tracking, launches."},
    "account_mgr": {"name": "Account Manager", "title": "Account Manager", "division": "demigod", "department": "Client Success",
                    "role": "account_mgr", "color": "#ffb37c", "permission": 3, "model": "fast", "tools": ["ai.draft"],
                    "output": "client report / renewal notes",
                    "mission": "Client communication, monthly reports, satisfaction, renewals, retention, upsells, referrals."},
    "support": {"name": "Support Specialist", "title": "Support Specialist", "division": "demigod", "department": "Client Success",
                "role": "support", "color": "#ff9d9d", "permission": 1, "model": "fast", "tools": ["di.tickets"],
                "output": "ticket triage", "mission": "Ticket intake, troubleshooting, error classification, escalation, resolution, change requests."},
}


def division_of(agent_id):
    a = ROSTER.get(agent_id)
    if not a:
        raise ValueError(f"Unknown agent: {agent_id}")
    return a["division"]


def check_assignment(agent_id, business):
    """An agent only takes work for its own business; the Executive Office serves both. Returns the task's business."""
    div = division_of(agent_id)
    business = business or (div if div in BUSINESSES else "molabucks")
    if business not in BUSINESSES:
        raise ValueError(f"Unknown business: {business}")
    if div != "exec" and div != business:
        raise ValueError(f"{ROSTER[agent_id]['name']} works for {DIVISIONS[div]['name']}; it can't take {DIVISIONS[business]['name']} work.")
    return business
