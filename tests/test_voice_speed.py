"""Voice speed: pipelined speech, instant acknowledgments, early first sentence, prompt caching, thread-safe app link.
No microphone, speaker or network: audio output and synthesis are faked. Run: python tests/test_voice_speed.py"""
import json, sys, tempfile, threading, time, types
from pathlib import Path
from types import SimpleNamespace as NS
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
writes = []
PLAY = 0.5  # each sentence takes 0.5 s to play
class FakeOut:
    def __init__(s, **k): pass
    def __enter__(s): return s
    def __exit__(s, *a): return False
    def write(s, b): writes.append((time.time(), bytes(b))); time.sleep(PLAY)  # real playback blocks while the sound plays
sd = types.ModuleType("sounddevice"); sd.RawOutputStream = FakeOut; sd.play = lambda *a, **k: None; sd.wait = lambda: None
sys.modules["sounddevice"] = sd
from engine import db, config
tmp = Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "t.db"; config.ROOT = tmp
(tmp / "config.yaml").write_text("timezone: America/New_York\ncounties: []\n", encoding="utf-8"); (tmp / "counties.yaml").write_text("counties: []\n", encoding="utf-8")
from voice import jarvis_voice as jv

FAILS = []
def ok(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: FAILS.append(m)

m = jv.Mouth({"tts": "elevenlabs"})
SYN = 0.4  # pretend each sentence takes 0.4 s to synthesize
def fake_pcm(text):
    if text in m.cache:
        yield m.cache[text]; return
    time.sleep(SYN); yield text.encode().ljust(8, b"\0")[:8]
m.pcm = fake_pcm
m.cache["On it, boss."] = b"ACKACKAC"

def brain():  # Claude takes 1.0 s before the first sentence, then 0.1 s per sentence
    time.sleep(1.0); yield "First sentence."
    time.sleep(0.1); yield "Second sentence."
    time.sleep(0.1); yield "Third **one**."
writes.clear(); t0 = time.time()
out = m.say_stream(brain(), prefix="On it, boss.")
ok(writes[0][1] == b"ACKACKAC" and writes[0][0] - t0 < 0.15, f"the acknowledgment plays instantly, while Claude is still thinking ({writes[0][0] - t0:.2f}s)")
ok(out == "First sentence. Second sentence. Third one.", f"what's returned is the answer only, markdown stripped ({out!r})")
total = writes[-1][0] - t0
ok(total < 1.0 + SYN + 2 * PLAY + 0.3 and total < 0.5 + 1.0 + 3 * (SYN + PLAY) - 0.5, f"sentences are synthesized while earlier ones play, and Claude thinks during the acknowledgment ({total:.2f}s to the last sentence; one-after-another would be {0.5 + 1.0 + 3 * (SYN + PLAY) - PLAY:.1f}s)")

writes.clear(); t0 = time.time()
out = m.say_stream(iter(["Not cached ack."]), prefix="Got it, boss.")
ok(out == "Not cached ack.", "an acknowledgment that isn't pre-made yet still plays first and isn't counted as the answer")

def boom():
    yield "Partial answer."
    raise RuntimeError("API down")
try:
    m.say_stream(boom()); ok(False, "brain errors surface")
except RuntimeError as e:
    ok(getattr(e, "spoken", None) == "Partial answer.", "if Claude fails mid-answer, what was already said is kept and the error surfaces")

ok(not jv.FILLER.match("Bay Rentals replied and wants a call.") and jv.FILLER.match("Let me check.") and jv.FILLER.match("One sec."), "a real first sentence is spoken right away; only 'let me check' fillers are held back")

class FakeStream:
    def __init__(s, evs, final): s.evs, s.final = evs, final
    def __enter__(s): return s
    def __exit__(s, *a): return False
    def __iter__(s): return iter(s.evs)
    def get_final_message(s): return s.final
seen = []
class FakeClient:
    def __init__(s): s.messages = s
    def stream(s, **kw):
        seen.append(kw)
        txt = "Bay Rentals replied and wants a call. They suggested Tuesday afternoon. Want me to book it?"
        evs = [NS(type="text", text=w + " ") for w in txt.split(" ")]
        return FakeStream(evs, NS(stop_reason="end_turn", content=[NS(type="text", text=txt)]))
b = jv.Brain.__new__(jv.Brain); b.client = FakeClient(); b.model = "claude-sonnet-5-5"; b.history = []
b.system = lambda: "You are JARVIS."
first = next(b.ask_stream("any replies?"))
ok(first == "Bay Rentals replied and wants a call.", "the first sentence is released as soon as it's complete, not after the second")
kw = seen[-1]
ok(kw["system"][0]["cache_control"] == {"type": "ephemeral"} and kw["tools"][-1].get("cache_control") == {"type": "ephemeral"}, "system prompt and tools are prompt-cached so each reply starts sooner")
b2 = jv.Brain.__new__(jv.Brain)
cfgv = {"voice": {"model": "claude-haiku-5-5"}, "anthropic_model": "claude-sonnet-5-5"}
b2.model = (cfgv.get("voice") or {}).get("model") or cfgv.get("anthropic_model")
ok(b2.model == "claude-haiku-5-5", "voice.model in config.yaml can pick a faster model for voice only")

link = jv.Link()
errs = []
def bg():
    try:
        link.set("speaking", reply="from another thread")
    except Exception as e:
        errs.append(e)
th = threading.Thread(target=bg); th.start(); th.join()
st = json.loads(db.kv_get(db.connect(), "voice_state", "{}"))
ok(not errs and st.get("reply") == "from another thread", "the app link updates from the speaking thread (the app shows what JARVIS is saying)")
print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
