"""Claude API helper for reading messy pages and chat exports into JSON."""
import json
import os

_client = None


def make_client():
    """Anthropic client. If the key isn't tied to a workspace, set ANTHROPIC_WORKSPACE_ID in .env."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY missing in .env")
    import anthropic
    ws = (os.environ.get("ANTHROPIC_WORKSPACE_ID") or "").strip()
    return anthropic.Anthropic(default_headers={"anthropic-workspace-id": ws} if ws else None)


def _get():
    global _client
    if _client is None:
        _client = make_client()
    return _client


def extract_json(prompt, model, max_tokens=8000):
    msg = _get().messages.create(model=model, max_tokens=max_tokens,
                                 messages=[{"role": "user", "content": prompt}])
    text = "".join(getattr(b, "text", "") for b in msg.content).strip()
    return parse_json(text)


def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[4:] if text.lower().startswith("json") else text
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        return None
    s = min(starts)
    e = max(text.rfind("}"), text.rfind("]"))
    return json.loads(text[s:e + 1])


RECORD_FIELDS = {
    "case_number": "court case number", "parcel_id": "parcel / folio ID", "address": "property street address",
    "city": "property city", "zip": "property zip", "owner": "owner or defendant name",
    "judgment_amount": "judgment or opening bid amount", "assessed_value": "assessed / market value",
    "event_date": "sale, auction, or filing date (YYYY-MM-DD)", "source_type": "foreclosure, lis_pendens, tax_deed, probate, code_violation, eviction, or other",
}


def extract_records(page_text, instructions, model):
    prompt = f"""You are reading a public county records page for a real estate investor.
Task: {instructions}
Extract one record per property. Fields: {json.dumps(RECORD_FIELDS)}.
Rules: copy values exactly as shown; omit fields that are not on the page; never guess or invent addresses, names, or amounts; skip headers, navigation, and ads.
Return ONLY JSON: {{"records": [...]}}

PAGE:
{page_text[:60000]}"""
    out = extract_json(prompt, model) or {}
    recs = out.get("records", []) if isinstance(out, dict) else out
    return [r for r in recs if isinstance(r, dict)]
