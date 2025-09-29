 # app.py 〈全文〉 v0.9.0
# - SMS送信の信頼性改善: 番号未取得なら保留 → /twilio/status で取得後に送信
# - 他のロジックは v0.8.9 と同等

from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect, Request, APIRouter, Form
from datetime import datetime, timezone
import os, json, traceback, base64, audioop, asyncio, uuid, time, re, urllib.parse
from typing import Dict, List, Optional, Tuple

from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import TranscriptEvent

import boto3
from twilio.rest import Client as TwilioClient

# ====== caches ======
CALL_NUMS: Dict[str, Dict[str,str]] = {}   # {callSid: {"from": "+81...", "to": "+81..."}}
PENDING_SMS: Dict[str, str] = {}           # {callSid: "body to send"}

router = APIRouter()
@router.post("/twilio/status")
async def twilio_status(
    CallSid: str = Form(default=""),
    CallStatus: str = Form(default=""),
    From: str = Form(default=""),
    To: str = Form(default="")
):
    # Status コールバックで番号をキャッシュ
    print(f"[TW] status sid={CallSid} status={CallStatus} from={From} to={To}", flush=True)
    if CallSid:
        d = CALL_NUMS.get(CallSid, {})
        if From: d["from"] = From
        if To:   d["to"]   = To
        CALL_NUMS[CallSid] = d

        # 保留SMSがあり、番号が揃ったら即送信
        if CallSid in PENDING_SMS and d.get("from") and d.get("to"):
            body = PENDING_SMS.pop(CallSid, "")
            if body:
                try_send_sms(CallSid, body)
    return {"ok": True}

APP_NAME = "voicebot"
APP_VERSION = "0.9.0"

SAMPLE_RATE = 8000
AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-1")
TRANSCRIBE_LANGUAGE = os.getenv("TRANSCRIBE_LANGUAGE", "ja-JP")
TRANSCRIBE_VOCAB = os.getenv("TRANSCRIBE_VOCAB", "")
S3_BUCKET = os.getenv("TRANSCRIPT_BUCKET", f"voicebot-transcripts-291234479055-{AWS_REGION}")

TW_SID = os.getenv("TWILIO_ACCOUNT_SID")
TW_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TW_FROM = os.getenv("TWILIO_FROM", "")  # 任意。未設定なら Call の To を使用

FALLBACK_FORCE: bool = False
LAST_WS_ERROR_AT: Optional[float] = None
WS_ERROR_WINDOW_SEC = 120
def ws_recently_failed() -> bool:
    return (LAST_WS_ERROR_AT is not None) and ((time.time() - LAST_WS_ERROR_AT) < WS_ERROR_WINDOW_SEC)

app = FastAPI(title=APP_NAME, version=APP_VERSION)
app.include_router(router)

RECENTS: List[Dict] = []
MAX_RECENTS = 50
def add_recent(call_id: str, text: str, started_at: str, finished_at: str):
    RECENTS.insert(0, {"id": call_id, "text": text, "started_at": started_at, "finished_at": finished_at})
    if len(RECENTS) > MAX_RECENTS:
        RECENTS.pop()

@app.get("/")
async def root_get():
    return {"message":"ok","app":APP_NAME,"version":APP_VERSION,
            "twilio_env":{"sid": bool(TW_SID), "token": bool(TW_TOKEN), "from": bool(TW_FROM)},
            "fallback":{"force": FALLBACK_FORCE, "recent_ws_error": ws_recently_failed()}}

@app.get("/health")
async def health_get(): return {"status":"ok"}

@app.get("/healthz")
async def healthz_get(): return {"status":"ok"}

@app.get("/version")
async def version_get():
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"app":APP_NAME,
            "deploy_stamp":{"raw":"deployed","time_utc":now_utc,"commit_sha":os.getenv("COMMIT_SHA","unknown")},
            "version": APP_VERSION}

@app.get("/admin/fallback/on")
async def fallback_on():
    global FALLBACK_FORCE
    FALLBACK_FORCE = True
    return {"fallback_force": True}

@app.get("/admin/fallback/off")
async def fallback_off():
    global FALLBACK_FORCE
    FALLBACK_FORCE = False
    return {"fallback_force": False}

@app.get("/twiml")
@app.post("/twiml")
async def twiml():
    xml=('''<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="ja-JP">こちらはボイスボットのテストです。10秒後に切断します。</Say>
  <Pause length="10"/><Hangup/>
</Response>''')
    return Response(content=xml, media_type="text/xml")

def build_reply_twiml(reply_text: str, ws_url: str) -> str:
    return (f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="ja-JP">{reply_text}</Say>
  <Connect><Stream url="{ws_url}"/></Connect>
</Response>''')

def build_fallback_twiml() -> str:
    return ('''<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="ja-JP">現在回線が混み合っています。恐れ入りますが、しばらくしてからおかけ直しください。</Say>
  <Hangup/>
</Response>''')

@app.get("/twiml_stream")
@app.post("/twiml_stream")
async def twiml_stream(req: Request):
    if FALLBACK_FORCE or ws_recently_failed():
        return Response(content=build_fallback_twiml(), media_type="text/xml")
    ws_url = "wss://voice.frontglass.net/stream"
    xml=(f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="ja-JP">お電話ありがとうございます。接続テストを開始します。ご用件をどうぞ。</Say>
  <Connect><Stream url="{ws_url}"/></Connect>
</Response>''')
    return Response(content=xml, media_type="text/xml")

# ---- 意図判定とSMS本文 ----
def classify_intent(text: str) -> str:
    t = text.lower()
    if re.search(r"予約|よやく|book|reserve", t): return "reserve"
    if re.search(r"営業時間|何時|open|close|いつまで", t): return "hours"
    if re.search(r"住所|場所|アクセス|行き方|どこ|最寄り|駐車|parking|map|地図", t): return "access"
    if re.search(r"担当|折り返し|オペレーター|人と話|転送|代表", t): return "agent"
    if re.search(r"料金|値段|価格|支払|決済|方法|メール", t): return "faq"
    return "other"

def make_reply_by_intent(text: str) -> Tuple[str, Optional[str]]:
    intent = classify_intent(text)
    if intent == "reserve": return ("ご予約の件ですね。ご希望の日時と内容をお話しください。", None)
    if intent == "hours":   return ("営業時間をご案内します。SMSのリンクをご確認ください。", "hours")
    if intent == "access":  return ("所在地をSMSでお送りします。ご確認ください。", "access")
    if intent == "agent":   return ("担当者への取次ですね。折り返し希望ならお名前と番号をどうぞ。", None)
    if intent == "faq":     return ("料金やお支払い方法はSMSのリンクをご案内します。", "faq")
    return (f"承知しました。今『{text[:30]}』と伺いました。詳しいご要件を続けてどうぞ。", None)

STORE_ADDRESS = "埼玉県上尾市菅谷3-4-1"
def sms_template(kind: str) -> str:
    if kind == "hours":
        return "営業時間のご案内です： https://example.com/hours"
    if kind == "access":
        q = urllib.parse.quote(STORE_ADDRESS)
        maps = f"https://maps.google.com/?q={q}"
        return f"店舗所在地：{STORE_ADDRESS}\n地図：{maps}"
    if kind == "faq":
        return "よくあるご質問と料金一覧： https://example.com/faq"
    return ""

def try_send_sms(call_sid: str, sms_body: str):
    if not (TW_SID and TW_TOKEN and sms_body):
        print("[SMS] skipped: env or body missing", flush=True); return
    try:
        tw = TwilioClient(TW_SID, TW_TOKEN)

        # キャッシュ優先
        cached = CALL_NUMS.get(call_sid, {})
        to_number = cached.get("from") or None
        from_number = TW_FROM or cached.get("to") or None

        # フォールバック: Call API（取得できないこともある）
        if not (to_number and from_number):
            try:
                c = tw.calls(call_sid).fetch()
                to_number = to_number or getattr(c, "from_", None)
                from_number = from_number or getattr(c, "to", None)
            except Exception as e:
                print(f"[SMS] fetch call failed: {repr(e)}", flush=True)

        if not (to_number and from_number):
            print(f"[SMS] skipped: number missing to={to_number} from={from_number}", flush=True)
            return

        print(f"[SMS] using nums to={to_number} from={from_number}", flush=True)
        tw.messages.create(to=to_number, from_=from_number, body=sms_body)
        print(f"[SMS] sent to={to_number} from={from_number} body='{sms_body}'", flush=True)
    except Exception as e:
        print(f"[SMS] failed: {repr(e)}", flush=True)
        traceback.print_exc()

# ---- WS / STT ----
class MyTranscriptHandler(TranscriptResultStreamHandler):
    def __init__(self, output_stream, on_partial, on_final):
        super().__init__(output_stream)
        self.on_partial = on_partial
        self.on_final = on_final
    async def handle_transcript_event(self, ev: TranscriptEvent):
        for res in ev.transcript.results:
            if not res.alternatives: continue
            text = (res.alternatives[0].transcript or "").strip()
            if not text: continue
            if res.is_partial: self.on_partial(text)
            else: self.on_final(text)

@app.websocket("/stream")
async def stream_ws(ws: WebSocket):
    global LAST_WS_ERROR_AT
    await ws.accept()
    call_id = str(uuid.uuid4())[:8]
    started_at = datetime.now(timezone.utc).isoformat()
    print(f"[WS] OPEN call={call_id} at {started_at}", flush=True)

    print(f"[BOOT] TW_SID={bool(TW_SID)} TW_TOKEN={bool(TW_TOKEN)} TW_FROM={bool(TW_FROM)}", flush=True)

    s3 = boto3.client("s3", region_name=AWS_REGION)
    finals: List[str] = []
    call_sid: Optional[str] = None
    replied_once = False

    client = TranscribeStreamingClient(region=AWS_REGION)
    stream = await client.start_stream_transcription(
        language_code=TRANSCRIBE_LANGUAGE,
        media_sample_rate_hz=SAMPLE_RATE,
        media_encoding="pcm",
        vocabulary_name=TRANSCRIBE_VOCAB or None,
    )

    def on_partial(t: str):
        print(f"[STT] PARTIAL: {t}", flush=True)

    async def do_reply_if_ready(text: str):
        nonlocal replied_once
        if replied_once: return
        if not (TW_SID and TW_TOKEN):
            print("[LCC] skipped: TWILIO env not set", flush=True); return
        if not call_sid:
            print("[LCC] skipped: callSid not yet known", flush=True); return
        try:
            tw = TwilioClient(TW_SID, TW_TOKEN)
            ws_url = "wss://voice.frontglass.net/stream"
            reply, sms_key = make_reply_by_intent(text)
            twml = build_reply_twiml(reply, ws_url)
            print(f"[LCC] try redirect callSid={call_sid} text='{text}'", flush=True)
            tw.calls(call_sid).update(twiml=twml)
            replied_once = True
            print(f"[LCC] replied via TwiML redirect (callSid={call_sid})", flush=True)

            # SMSが必要なら保留登録し、すぐ一度だけ送信トライ（番号が無ければ /twilio/status 待ちで後送）
            if sms_key:
                body = sms_template(sms_key)
                PENDING_SMS[call_sid] = body  # 保留に入れる
                try_send_sms(call_sid, body)   # 取れれば即送、ダメならstatusで送る
        except Exception as e:
            print(f"[LCC] reply failed: {repr(e)}", flush=True)
            traceback.print_exc()

    async def on_final_async(t: str):
        print(f"[STT] FINAL  : {t}", flush=True)
        finals.append(t)
        await do_reply_if_ready(t)

    async def pump_audio():
        nonlocal call_sid
        try:
            while True:
                t = await ws.receive_text()
                try:
                    e = json.loads(t)
                except Exception:
                    continue
                if e.get("event") != "media":
                    et = e.get("event")
                    if et in ("connected","start","stop","mark"):
                        print(f"[WS] {et}", flush=True)
                    if et == "start":
                        try:
                            print(f"[WS] start payload: {json.dumps(e.get('start',{}), ensure_ascii=False)}", flush=True)
                        except Exception:
                            pass
                        try:
                            call_sid = e.get("start", {}).get("callSid") or call_sid
                            if call_sid:
                                print(f"[WS] callSid={call_sid}", flush=True)
                        except Exception:
                            pass
                    if et == "stop": break
                    continue
                b64 = e.get("media",{}).get("payload")
                if not b64: continue
                try:
                    ulaw = base64.b64decode(b64)
                    pcm16 = audioop.ulaw2lin(ulaw, 2)
                except Exception:
                    continue
                await stream.input_stream.send_audio_event(audio_chunk=pcm16)
        except WebSocketDisconnect:
            pass
        except Exception:
            LAST_WS_ERROR_AT = time.time()
            traceback.print_exc()
        finally:
            try: await stream.input_stream.end_stream()
            except Exception: pass

    async def read_transcripts():
        try:
            handler = MyTranscriptHandler(
                stream.output_stream,
                on_partial=lambda txt: on_partial(txt),
                on_final=lambda txt: asyncio.create_task(on_final_async(txt)),
            )
            await handler.handle_events()
        except Exception:
            traceback.print_exc()

    tasks = [asyncio.create_task(pump_audio()), asyncio.create_task(read_transcripts())]
    try:
        await asyncio.gather(*tasks)
    except Exception:
        traceback.print_exc()
    finally:
        finished_at = datetime.now(timezone.utc).isoformat()
        text_joined = "".join(finals)
        add_recent(call_id, text_joined, started_at, finished_at)
        try:
            day = started_at.split("T")[0]
            key = f"calls/{day}/call-{call_id}.json"
            body = json.dumps({
                "id": call_id, "started_at": started_at, "finished_at": finished_at,
                "language": TRANSCRIBE_LANGUAGE, "text": text_joined
            }, ensure_ascii=False).encode("utf-8")
            s3.put_object(Bucket=S3_BUCKET, Key=key, Body=body, ContentType="application/json")
            print(f"[S3] put s3://{S3_BUCKET}/{key} bytes={len(body)}", flush=True)
        except Exception:
            print("[S3] put failed", flush=True)
            traceback.print_exc()

        print(f"[WS] CLOSE call={call_id} text='{text_joined}'", flush=True)
        try: await ws.close()
        except Exception: pass
