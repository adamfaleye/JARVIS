"""AI Sidekicks client assistant: Instagram DM assistant, AI-generated request drafts and a human-approved
booking workflow. Case study: AI Sidekicks' own Instagram (@aisidekicks), turning DMs into discovery-call requests.

Each client (tenant) gets its own SQLite file under data/clients/<tenant>.db, so one client's customers,
conversations and appointments never sit in the same database as JARVIS's own records or another client's.

Modules:
  store     tenant database, settings, audit log
  kb        editable knowledge base (services, prices, FAQs, policies, voice, escalation rules) + discovery checklist
  workflow  request status model; every transition is checked here (the owner's approval can't be bypassed)
  brain     conversation engine: intent and policy rules, grounded replies, price/availability/confirmation guards
  meta      official Instagram API (Instagram Login): webhook verification, signatures, dedupe, send queue
  auth      the owner's and operator's sign-in, sessions, CSRF, roles
  analytics metrics from real records only
  server    dashboard + JSON API + Meta webhook endpoint
"""
