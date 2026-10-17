"""Hands-free JARVIS. Say "Hey Jarvis", then talk. Follow-ups don't need the wake word until you say
"that's all" / "stand by" or go quiet. Runs on your PC; reads and writes the same CRM the JARVIS app syncs with."""
import asyncio
import json
import os
import queue
import random
import re
import sys
import tempfile
import time
from datetime import date

import numpy as np
import sounddevice as sd

from engine import config, db, crm, sms, hunter

RATE, BLOCK = 16000, 1280  # 80 ms frames, what the wake-word model expects
END_WORDS = re.compile(r"\b(that'?s all|stand ?by|goodbye|good night|never ?mind|thanks,? jarvis|thank you,? jarvis)\b", re.I)
ACKS = ["Got you, boss.", "On it, boss.", "Got it, boss.", "You got it, boss."]
# "Got it, boss" only for commands (do something), not questions (tell me something)
ACTION_WORDS = re.compile(r"\b(add|create|put|move|log|mark|schedule|set|update|change|archive|delete|remove|text|email|send|draft|"
                          r"call|remind|book|run|import|assign|close|enroll|cancel|note|save|record|make|push|follow up|"
                          r"bump|drop|flag|tag|start|scrape|match|blast|approve|pause|launch|dial)\b", re.I)
QUESTION_START = re.compile(r"^(what|what's|whats|how|who|who's|where|when|why|which|is|are|was|were|do|does|did|"
                            r"have|has|any|tell me|give me|show me|read me|remind me what)\b", re.I)


def is_command(text):
    t = re.sub(r"^(okay|ok|so|hey|yo|alright|um|uh|please)[,\s]+", "", text.strip(), flags=re.I)
    first = " ".join(t.split()[:5])
    if re.match(r"^(can|could|would|will) you\b", t, re.I):  # "can you add ..." is a polite command
        return bool(ACTION_WORDS.search(first))
    if QUESTION_START.match(t) or t.endswith("?"):
        return False
    return bool(ACTION_WORDS.search(first))


SENTENCE = re.compile(r".+?[.!?](?:\s|$)", re.S)


# ---------------- link to the JARVIS app ----------------
class Link:
    """Shares the voice state with the JARVIS app (through the engine database the app syncs with),
    and lets the app's core start a listen on this PC's microphone."""
    def __init__(self):
        self.con = db.connect()
        self.turn = int(json.loads(db.kv_get(self.con, "voice_state", "{}") or "{}").get("turn", 0))
        cmd = json.loads(db.kv_get(self.con, "voice_cmd", "{}") or "{}")
        self.cmd_id = cmd.get("id")
        self.state, self.heard, self.reply, self.beat, self.checked = "idle", "", "", 0.0, 0.0

    def set(self, state, heard=None, reply=None, new_turn=False):
        if new_turn:
            self.turn += 1
            self.heard, self.reply = "", ""
        if heard is not None:
            self.heard = heard
        if reply is not None:
            self.reply = reply
        self.state, self.beat = state, time.time()
        try:
            from datetime import datetime, timezone
            db.kv_set(self.con, "voice_state", json.dumps({
                "state": state, "turn": self.turn, "heard": self.heard, "reply": self.reply,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}))
        except Exception:
            pass

    def tick(self):
        """Heartbeat while idle, and True when the app asked this PC to listen."""
        now = time.time()
        if now - self.beat > 10:
            self.set(self.state)
        if now - self.checked < 0.5:
            return False
        self.checked = now
        try:
            cmd = json.loads(db.kv_get(self.con, "voice_cmd", "{}") or "{}")
        except Exception:
            return False
        if cmd.get("id") and cmd.get("id") != self.cmd_id:
            self.cmd_id = cmd["id"]
            fresh = time.time() - float(cmd.get("t", 0)) < 20
            return fresh and cmd.get("action") == "listen"
        return False


# ---------------- ears ----------------
class Ears:
    def __init__(self, device=None):
        self.q = queue.Queue()
        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=BLOCK, device=device,
                                     callback=lambda indata, *_: self.q.put(indata[:, 0].copy()))
        self.floor = 200.0

    def start(self):
        self.stream.start()

    def flush(self):
        while not self.q.empty():
            self.q.get_nowait()

    def frame(self):
        f = self.q.get()
        rms = float(np.sqrt(np.mean(f.astype(np.float32) ** 2)))
        return f, rms

    def utterance(self, wait_s=7.0, silence_s=1.4, max_s=25.0):
        """Record one spoken request. Returns float32 audio, or None if nobody spoke."""
        thresh = max(self.floor * 3.0, 350.0)
        got, started, quiet, t0 = [], False, 0.0, time.time()
        while True:
            f, rms = self.frame()
            if not started:
                self.floor = 0.95 * self.floor + 0.05 * rms if rms < thresh else self.floor
                if rms > thresh:
                    started = True
                    got.append(f)
                elif time.time() - t0 > wait_s:
                    return None
                continue
            got.append(f)
            quiet = quiet + BLOCK / RATE if rms < thresh * 0.7 else 0.0
            if quiet >= silence_s or len(got) * BLOCK / RATE > max_s:
                break
        return np.concatenate(got).astype(np.float32) / 32768.0


# ---------------- speech to text ----------------
class Transcriber:
    def __init__(self, model):
        from faster_whisper import WhisperModel
        print(f"Loading speech model ({model})… first run downloads it.")
        self.m = WhisperModel(model, device="cpu", compute_type="int8")

    def __call__(self, audio):
        segs, _ = self.m.transcribe(audio, language="en", beam_size=1, vad_filter=True,
                                    initial_prompt="Jarvis, county leads, new leads, call queue, pipeline, ARV, MAO, EMD, assignment fee, wholesale, seller, cash buyer, Pinellas, Hillsborough, Pasco, Clearwater, St. Pete, Tampa, foreclosure, tax deed, buyer blast, follow-up")
        return " ".join(s.text.strip() for s in segs).strip()


# ---------------- voice ----------------
class Mouth:
    def __init__(self, vcfg):
        self.engine = vcfg.get("tts", "edge")
        self.el_voice = str(vcfg.get("elevenlabs_voice_id", "") or "")
        self.el_model = vcfg.get("elevenlabs_model", "eleven_flash_v2_5")
        self.voice = vcfg.get("edge_voice", "en-GB-RyanNeural")
        self.rate = int(vcfg.get("sapi_rate", 185))

    def say(self, text):
        text = re.sub(r"[*_#`]", "", text).strip()
        if not text:
            return
        print(f"JARVIS: {text}")
        if self.engine == "elevenlabs":
            try:
                return self._eleven(text)
            except Exception as e:
                print(f"  (ElevenLabs voice unavailable: {e}; using the backup voice)")
        if self.engine in ("edge", "elevenlabs"):
            try:
                return self._edge(text)
            except Exception as e:
                print(f"  (online voice unavailable: {e.__class__.__name__}; using Windows voice)")
        self._sapi(text)

    def say_stream(self, chunks):
        """Speak each sentence as soon as it arrives. Returns the full text spoken ('' if nothing)."""
        spoken = []
        for c in chunks:
            c = (c or "").strip()
            if c:
                self.say(c)
                spoken.append(c)
        return " ".join(spoken)

    def _eleven(self, text):
        import json as _json
        import urllib.request
        import miniaudio
        key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        if not key or not self.el_voice:
            raise RuntimeError("ELEVENLABS_API_KEY or elevenlabs_voice_id is not set")
        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{self.el_voice}?output_format=mp3_44100_128",
            data=_json.dumps({"text": text, "model_id": self.el_model}).encode(),
            headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"}, method="POST")
        try:
            audio = urllib.request.urlopen(req, timeout=30).read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'ignore')}")
        dec = miniaudio.decode(audio, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=44100)
        sd.play(np.frombuffer(dec.samples, dtype=np.int16), 44100)
        sd.wait()

    def _edge(self, text):
        import edge_tts
        import miniaudio
        path = os.path.join(tempfile.gettempdir(), "jarvis_say.mp3")
        asyncio.run(edge_tts.Communicate(text, self.voice, rate="+5%").save(path))
        dec = miniaudio.decode_file(path, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=24000)
        sd.play(np.frombuffer(dec.samples, dtype=np.int16), 24000)
        sd.wait()

    def _sapi(self, text):
        import pyttsx3
        e = pyttsx3.init()
        e.setProperty("rate", self.rate)
        for v in e.getProperty("voices"):
            if "david" in v.name.lower() or "george" in v.name.lower():
                e.setProperty("voice", v.id)
                break
        e.say(text)
        e.runAndWait()

    @staticmethod
    def chime(up=True):
        t = np.linspace(0, 0.12, int(24000 * 0.12), False)
        f1, f2 = (660, 990) if up else (990, 660)
        tone = np.concatenate([np.sin(2 * np.pi * f1 * t), np.sin(2 * np.pi * f2 * t)]) * 0.18
        sd.play((tone * 32767).astype(np.int16), 24000)
        sd.wait()


# ---------------- brain ----------------
AMT = {"type": "object", "description": "Mola's exact words for each amount you pass, keyed by field", "additionalProperties": {"type": "string"}}
DTS = {"type": "object", "description": "Mola's exact words for each date you pass, keyed by field (followUp or due)", "additionalProperties": {"type": "string"}}
DEAL_P = {k: {"type": "string"} for k in crm.DEAL_FIELDS}
TOOLS = [
    ("get_overview", "Goal progress (collected vs projected-not-earned), stage counts, call queue and overdue tasks.", {}),
    ("get_queue", "Today's call queue, highest priority first.", {}),
    ("search_deals", "Find deals by address, seller or county; optional stage. Returns ids.",
     {"query": {"type": "string"}, "stage": {"type": "string", "enum": crm.STAGES}}),
    ("create_lead", "Add a new lead. Include followUpDate (YYYY-MM-DD) when he says when to call.",
     {"address": {"type": "string"}, "followUpDate": {"type": "string"}, "amounts_said": AMT, "dates_said": DTS, "notes": {"type": "string"}, **DEAL_P}, ["address"]),
    ("update_deal", "Update fields on a deal by id (not the stage). Notes are appended.",
     {"id": {"type": "string"}, "notes": {"type": "string"}, "amounts_said": AMT, **DEAL_P}, ["id"]),
    ("move_stage", "Move a deal to a stage. Closing needs the collected fee.",
     {"id": {"type": "string"}, "stage": {"type": "string", "enum": crm.STAGES}, "actualFee": {"type": "number"}, "amounts_said": AMT}, ["id", "stage"]),
    ("log_call", "Log a call outcome; the app's rules schedule the next follow-up unless he named a date. Dead needs a reason.",
     {"dealId": {"type": "string"}, "outcome": {"type": "string", "enum": list(crm.OUTCOMES)}, "note": {"type": "string"}, "reason": {"type": "string"},
      "followUpDate": {"type": "string"}, "dates_said": DTS}, ["dealId", "outcome"]),
    ("create_task", "Create a task/reminder, optionally tied to a deal.",
     {"title": {"type": "string"}, "due": {"type": "string", "description": "YYYY-MM-DD"}, "dealId": {"type": "string"}, "dates_said": DTS}, ["title"]),
    ("list_tasks", "Open tasks.", {"includeDone": {"type": "boolean"}}),
    ("complete_task", "Mark a task done.", {"id": {"type": "string"}}, ["id"]),
    ("create_buyer", "Add a cash buyer.", {"name": {"type": "string"}, "phone": {"type": "string"}, "email": {"type": "string"}, "markets": {"type": "string"},
                                           "propertyTypes": {"type": "string"}, "maxPrice": {"type": "number"}, "notes": {"type": "string"}, "amounts_said": AMT}, ["name"]),
    ("match_buyers", "Buyers whose markets and max price fit a deal, with unknowns flagged.", {"dealId": {"type": "string"}}, ["dealId"]),
    ("analyze_deal", "MAO = ARV x purchase % - rehab - fee. Only with an ARV he gave or that's on the deal.",
     {"arv": {"type": "number"}, "sqft": {"type": "number"}, "tier": {"type": "string", "enum": ["light", "medium", "heavy"]}, "repairs": {"type": "number"},
      "maoPct": {"type": "number"}, "fee": {"type": "number"}, "asking": {"type": "number"}}, ["arv"]),
    ("hunter_summary", "The Hunter: motivated-seller records found, by county and category, tier counts, leads awaiting review, "
     "skip-trace batches waiting for results, sales in the next 7 days, and source coverage (which counties/categories actually work).", {}),
    ("hunter_leads", "The Hunter's scored motivated-seller leads with the reason for each score. Filter by county, category, tier, "
     "skip-trace status (none, exported, traced, no_result), multiple record types, or minimum score. Read-only.",
     {"county": {"type": "string"}, "category": {"type": "string", "enum": list(hunter.CATEGORIES)},
      "tiers": {"type": "array", "items": {"type": "string", "enum": ["A", "B", "C", "Review"]}},
      "skip_status": {"type": "string", "enum": ["none", "exported", "traced", "no_result"]}, "multi": {"type": "boolean"},
      "min_score": {"type": "number"}, "sort": {"type": "string", "enum": ["score", "date", "new"]}, "limit": {"type": "number"}}),
    ("new_county_leads", "New leads the county scraper found that haven't been loaded into JARVIS yet.", {"county": {"type": "string"}}),
    ("texting_status", "Texting and campaigns: sent and replies today, 7-day response rate, hot/warm/cold repliers, running campaigns with their numbers, "
     "texts queued today, and why sending is held (texting off, quiet hours, emergency stop). Also the latest seller replies.", {}),
    ("pause_campaign", "Pause a running text campaign by name, or all of them. Only pauses; launching or resuming is done in the JARVIS app.",
     {"name": {"type": "string", "description": "Campaign name, or 'all'"}}, ["name"]),
]
WRITES = {"create_lead", "update_deal", "move_stage", "log_call", "create_task", "complete_task", "create_buyer"}


def tool_specs():
    return [{"name": n, "description": d, "input_schema": {"type": "object", "properties": p, **({"required": r[0]} if r else {})}}
            for n, d, p, *r in TOOLS]


def run_tool(name, args, ctx=None):
    con = db.connect()
    if name == "get_overview":
        return crm.overview(con)
    if name == "get_queue":
        return [crm.compact(d) for d in crm.queue(con)[:15]]
    if name == "new_county_leads":
        q = "SELECT county,source_type,address,city,owner,event_date,judgment_amount FROM leads WHERE status='new'"
        rows = con.execute(q + (" AND county LIKE ?" if args.get("county") else "") + " ORDER BY event_date LIMIT 25",
                           ([f"%{args['county']}%"] if args.get("county") else [])).fetchall()
        return {"count": con.execute("SELECT COUNT(*) FROM leads WHERE status='new'").fetchone()[0], "sample": [dict(r) for r in rows]}
    if name == "texting_status":
        from engine import outreach
        oc = outreach.connect()
        ov = outreach.overview(oc, config.load())
        ov.pop("hours", None)
        return {**ov, "recent_replies": sms.overview()["recent_replies"][:5]}
    if name == "pause_campaign":
        from engine import outreach
        oc = outreach.connect()
        want = str(args.get("name") or "").strip().lower()
        hits = [r for r in oc.execute("SELECT id,name FROM campaigns WHERE status='running'")
                if want in ("all", "everything") or want in r["name"].lower()]
        if not hits:
            return {"status": "error", "summary": "No running campaign matches that."}
        for r in hits:
            outreach.set_status(oc, r["id"], "paused")
        return {"status": "ok", "summary": "Paused " + ", ".join(r["name"] for r in hits) + "."}
    if name == "hunter_summary":
        hc = hunter.connect()
        cov = hunter.coverage(hc)
        return {**hunter.summary(hc), "coverage": [{"county": m["county"], "state": m["state"],
                "working": [hunter.CATEGORIES[c] for c, v in m["categories"].items() if v in ("operational", "manual")]} for m in cov["matrix"]],
                "note": "Scores come from public records; they don't mean the owner wants to sell."}
    if name == "hunter_leads":
        hc = hunter.connect()
        f = {k: v for k, v in args.items() if v not in (None, "", [])}
        f["limit"] = min(int(f.get("limit") or 8), 15)
        r = hunter.leads(hc, f)
        return {"total": r["total"], "leads": [{"address": ", ".join(x for x in [x0.get("street"), x0.get("city")] if x), "county": x0.get("county"),
                "score": x0.get("score"), "tier": x0.get("tier"), "records": x0.get("categories"), "next_date": x0.get("next_date"),
                "why": [c["why"] for c in (x0.get("components") or {}).values() if c.get("points")][:3],
                "unknown": x0.get("missing"), "next_step": x0.get("next_action"), "contacts": x0.get("contacts")} for x0 in r["rows"]],
                "note": "Exporting to AvaData, contacting owners or promoting leads is done in the JARVIS Hunter screen with confirmation."}
    fn = getattr(crm, name)
    if name in WRITES:
        args["_ctx"] = ctx or {}
    return fn(con, **args)


class Brain:
    def __init__(self, cfg):
        from engine.ai import make_client
        self.client = make_client()
        self.model = cfg.get("anthropic_model")
        self.history = []

    def system(self):
        s = crm.settings(db.connect())
        return (f"You are JARVIS, hands-free voice partner to Ademola (Mola) Faleye, a real estate wholesaler (MolaBucks Capital Group), "
                f"focused on Florida. Goal: ${s['goal']:,.0f} in assignment fees per month. Today is {date.today():%A, %B %d, %Y}.\n"
                "Act through tools. Only say something succeeded if its tool result has status ok; repeat the result's summary. If a result has status needs_input, ask that question. "
                "If a write fails, say so plainly. Use search_deals to find ids before updates; if several deals match, ask which. "
                "Pass his exact words for amounts in amounts_said and for dates in dates_said; if he said something ambiguous like 'one-eighty', ask what he means. When he reports a call result, use log_call.\n"
                "Never invent comps, ARVs, addresses, phone numbers, or buyers. Be honest about whether the pipeline supports the goal.\n"
                "You're on a live voice call with him. Talk like a sharp human partner on the phone, not a report: usually one or two short sentences, "
                "lead with the answer, and react like a person when it fits (nice, ouch, on it). He has already heard a quick acknowledgment, so don't open with one. "
                "Call him Mola or boss now and then, not every line. Everything you write is spoken aloud: no lists, no markdown, "
                "say money naturally (one fifty K, not $150,000.00).")

    def ask(self, text):
        self.recent = (getattr(self, "recent", []) + [text])[-4:]
        self.history.append({"role": "user", "content": text})
        while len(self.history) > 30:
            self.history.pop(0)
            while self.history and not (self.history[0]["role"] == "user" and isinstance(self.history[0]["content"], str)):
                self.history.pop(0)
        for _ in range(8):
            r = self.client.messages.create(model=self.model, max_tokens=700, system=self.system(),
                                            tools=tool_specs(), messages=self.history)
            self.history.append({"role": "assistant", "content": r.content})
            if r.stop_reason != "tool_use":
                return " ".join(b.text for b in r.content if b.type == "text").strip() or "Done."
            results = []
            for b in r.content:
                if b.type != "tool_use":
                    continue
                try:
                    out = json.dumps(run_tool(b.name, dict(b.input), {"user_texts": self.recent, "request": text}), default=str)[:12000]
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
                except Exception as e:
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": f"Error: {e}", "is_error": True})
            self.history.append({"role": "user", "content": results})
        return "That took too many steps. Try asking it a simpler way."

    def ask_stream(self, text):
        """Like ask(), but yields each finished sentence as Claude writes it, so speech starts right away.
        Tool-use turns run silently (any text before the tool call is dropped)."""
        self.recent = (getattr(self, "recent", []) + [text])[-4:]
        self.history.append({"role": "user", "content": text})
        while len(self.history) > 30:
            self.history.pop(0)
            while self.history and not (self.history[0]["role"] == "user" and isinstance(self.history[0]["content"], str)):
                self.history.pop(0)
        for _ in range(8):
            buf, said_any = "", False
            pending = []  # sentences held until we know this turn isn't a tool call
            with self.client.messages.stream(model=self.model, max_tokens=700, system=self.system(),
                                             tools=tool_specs(), messages=self.history) as st:
                for ev in st:
                    if getattr(ev, "type", "") == "content_block_start" and getattr(ev.content_block, "type", "") == "tool_use":
                        pending, buf = [], ""  # a tool call is coming: this turn's words aren't the answer
                        continue
                    if getattr(ev, "type", "") != "text":
                        continue
                    buf += ev.text
                    while True:
                        m = SENTENCE.match(buf)
                        if not m:
                            break
                        sent, buf = m.group(0).strip(), buf[m.end():]
                        if sent:
                            pending.append(sent)
                    # hold back the newest sentence until the next one starts: a short "let me check" right
                    # before a tool call gets dropped instead of spoken
                    while len(pending) > 1:
                        said_any = True
                        yield pending.pop(0)
                r = st.get_final_message()
            self.history.append({"role": "assistant", "content": r.content})
            if r.stop_reason != "tool_use":
                for p in pending:
                    said_any = True
                    yield p
                if buf.strip():
                    yield buf.strip()
                return
            results = []
            for b in r.content:
                if b.type != "tool_use":
                    continue
                try:
                    out = json.dumps(run_tool(b.name, dict(b.input), {"user_texts": self.recent, "request": text}), default=str)[:12000]
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
                except Exception as e:
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": f"Error: {e}", "is_error": True})
            self.history.append({"role": "user", "content": results})
        yield "That took too many steps. Try asking it a simpler way."


def explain_api_error(e):
    """Say what actually went wrong, and print the API's own message for troubleshooting."""
    msg = str(getattr(e, "message", "") or e)
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        msg = (body.get("error") or {}).get("message") or msg
    print(f"[API error] {e.__class__.__name__}: {msg[:400]}")
    low = msg.lower()
    if "workspace" in low:
        return ("My API key isn't tied to a workspace. Create a new key inside a workspace in the Claude Console, "
                "or add your workspace I D to the dot env file as ANTHROPIC_WORKSPACE_ID, then restart me.")
    if "credit balance" in low or "billing" in low:
        return "My Anthropic API account is out of credits. Add credits under Billing in the Claude Console, then try again."
    if e.__class__.__name__ == "AuthenticationError" or "api key" in low or "x-api-key" in low:
        return "My Anthropic API key was rejected. Put a valid key in the dot env file and restart me."
    if e.__class__.__name__ in ("NotFoundError",) or "model" in low:
        return "The AI model name in my settings isn't available. Check anthropic model in config dot yaml."
    if e.__class__.__name__ in ("APIConnectionError", "APITimeoutError"):
        return "I can't reach the internet right now. Check the connection and try again."
    if e.__class__.__name__ in ("RateLimitError", "OverloadedError", "InternalServerError"):
        return "The AI service is busy. Give it a moment and try again."
    return f"I hit a problem reaching my brain: {e.__class__.__name__}. The details are in my window."


# ---------------- main loop ----------------
def main(device=None):
    cfg = config.load()
    v = cfg.get("voice", {})
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY missing in .env")
    import openwakeword
    from openwakeword.model import Model
    wake = v.get("wake_model", "hey_jarvis")
    try:
        openwakeword.utils.download_models(model_names=[wake])
    except Exception:
        pass
    oww = Model(wakeword_models=[wake], inference_framework="onnx")
    key = next(iter(oww.models))
    stt, mouth, brain, ears = Transcriber(v.get("whisper_model", "base.en")), Mouth(v), Brain(cfg), Ears(device)
    thr = float(v.get("wake_threshold", 0.5))
    ears.start()
    mouth.say("JARVIS online. Say hey Jarvis when you need me.")
    ears.flush()
    print('Listening for "Hey Jarvis"…  (Ctrl+C to quit)')
    link = Link()
    link.set("idle")
    while True:
        f, rms = ears.frame()
        ears.floor = 0.98 * ears.floor + 0.02 * rms
        asked = link.tick()
        if not asked and oww.predict(f).get(key, 0) < thr:
            continue
        oww.reset()
        if asked:
            print("(JARVIS app: listening)")
        link.set("listening", new_turn=True)
        mouth.chime(True)
        ears.flush()
        wait = 7.0
        while True:
            audio = ears.utterance(wait_s=wait)
            if audio is None:
                mouth.chime(False)
                break
            text = re.sub(r"^(hey\s+)?jarvis[,.\s]*", "", stt(audio), flags=re.I).strip()
            if not text:
                break
            print(f"YOU: {text}")
            if link.heard and link.reply:
                link.set("processing", new_turn=True)
            link.set("processing", heard=text)
            if END_WORDS.search(text) and len(text.split()) <= 5:
                link.set("speaking", reply="Standing by.")
                mouth.say("Standing by.")
                break
            if is_command(text):
                mouth.say(random.choice(ACKS))  # instant acknowledgment for commands, before any thinking
            mark = len(brain.history)
            spoken = []

            def stream():
                for sent in brain.ask_stream(text):
                    spoken.append(sent)
                    link.set("speaking", reply=" ".join(spoken))
                    yield sent
            try:
                if not mouth.say_stream(stream()):
                    link.set("speaking", reply="Done.")
                    mouth.say("Done.")
            except Exception as e:
                del brain.history[mark:]  # drop the failed turn so the next request starts clean
                msg = explain_api_error(e)
                link.set("speaking", reply=(" ".join(spoken) + " " + msg).strip())
                mouth.say(msg)
            link.set("listening")
            ears.flush()
            wait = float(v.get("followup_seconds", 6))
        link.set("idle")
        ears.flush()
        print('Listening for "Hey Jarvis"…')


def list_devices():
    print(sd.query_devices())
