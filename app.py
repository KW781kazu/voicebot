# app.py 〈全文〉
# - Twilio <Connect><Stream> → WebSocket /stream で音声受信
# - Amazon Transcribe Streaming でリアルタイムSTT
# - FINAL を S3 に保存
# - 最初の FINAL で自動応答（<Say>）→ すぐ <Connect><Stream> で再接続
#   * TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN が未設定なら応答はスキップ
from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from datetime import datetime, timezone
import os, json, traceback, base64, audioop, asyncio, uuid
from typing import Dict, List, Optional

from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import TranscriptEvent

import boto3
from twilio.rest import Client as TwilioClient

APP_NAME = "voicebot"
APP_VERSION = "0.8.0"  # 1-turn auto-reply

SAMPLE_RATE = 8000
AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-1")
TRANSCRIBE_LANGUAGE = os.getenv("TRANSCRIBE_LANGUAGE", "ja-JP")
TRANSCRIBE_VOCAB = os.getenv("TRANSCRIBE_VOCAB", "")
S3_BUCKET = os.getenv("TRANSCRIPT_BUCKET", f"voicebot-transcripts-291234479055-{AWS_REGION}")

# Twilio 資格情報（未設定なら自動応答はスキップ）
TW_SID = os.getenv("TWILIO_ACCOUNT_SID")
TW_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

app = FastAPI(title=APP_NAME, version=APP_VERSION)

RECENTS: List[Dict] = []
MAX_RECENTS = 50

def add_recent(call_id: str, text: str, started_at: str, finished_at: str):
    RECENTS.insert(0, {"id": call_id, "text": text, "started_at": started_at, "finished_at": finished_at})
    if len(RECENTS) > MAX_RECENTS:
        RECENTS.pop()

@app.get("/")
async def root_get(): return {"message":"ok","app":APP_NAME,"version":APP_VERSION}

@app.get("/health")
async def health_get(): return {"status":"ok"}

@app.get("/version")
async def version_get():
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"app":APP_NAME,"deploy_stamp":{"raw":"deployed","time_utc":now_utc,"commit_sha":os.getenv("COMMIT_SHA","unknown")}}

@app.get("/twiml")
@app.post("/twiml")
async def twiml():
    xml=('''<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="ja-JP">こちらはボイスボットのテストです。10秒後に切断します。</Say>
  <Pause length="10"/><Hangup/>
</Response>''')
    return Response(content=xml, media_type="text/xml")

@app.get("/twiml_stream")
@app.post("/twiml_stream")
async def twiml_stream():
    ws_url = "wss://voice.frontglass.net/stream"
    xml=(f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="ja-JP">テスト</Say>
  <Say language="ja-JP">接続テストを開始します。</Say>
  <Connect><Stream url="{ws_url}"/></Connect>
</Response>''')
    return Response(content=xml, media_type="text/xml")

class MyTranscriptHandler(TranscriptResultStreamHandler):
    def __init__(self, output_stream, on_partial, on_final):
        super().__init__(output_stream)
        self.on_partial = on_partial
        self.on_final = on_final

    async def handle_transcript_event(self, ev: TranscriptEvent):
        for res in ev.transcript.results:
            if not res.alternatives: continue
            text = res.alternatives[0].transcript or ""
            if not text: continue
            if res.is_partial:
                self.on_partial(text)
            else:
                self.on_final(text)

def build_reply_twiml(reply_text: str, ws_url: str) -> str:
    # 応答 → すぐ再接続（同じWSに戻る）
    return (f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="ja-JP">{reply_text}</Say>
  <Connect><Stream url="{ws_url}"/></Connect>
</Response>''')

@app.websocket("/stream")
async def stream_ws(ws: WebSocket):
    await ws.accept()
    call_id = str(uuid.uuid4())[:8]
    started_at = datetime.now(timezone.utc).isoformat()
    print(f"[WS] OPEN call={call_id} at {started_at}", flush=True)

    s3 = boto3.client("s3", region_name=AWS_REGION)
    finals: List[str] = []
    call_sid: Optional[str] = None
    replied_once = False  # 最初のFINALだけ応答

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
        if replied_once or not TW_SID or not TW_TOKEN or not call_sid:
            return
        try:
            tw = TwilioClient(TW_SID, TW_TOKEN)
            ws_url = "wss://voice.frontglass.net/stream"
            reply = f"こちらはボイスボットです。ご用件をどうぞ。"
            # 例として簡単に：最初の発話を短く復唱
            if len(text) <= 30:
                reply = f"こちらはボイスボットです。今、「{text}」と聞こえました。ご用件をどうぞ。"
            twml = build_reply_twiml(reply, ws_url)
            # ライブ通話を即時リダイレクト
            tw.calls(call_sid).update(twiml=twml)
            replied_once = True
            print(f"[LCC] replied via TwiML redirect (callSid={call_sid})", flush=True)
        except Exception:
            print("[LCC] reply failed", flush=True)
            traceback.print_exc()

    async def on_final_async(t: str):
        print(f"[STT] FINAL  : {t}", flush=True)
        finals.append(t)
        await do_reply_if_ready(t)

    async def pump_audio():
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
                        # start イベントから callSid を取る
                        try:
                            call_info = e.get("start", {})
                            nonlocal call_sid
                            call_sid = call_info.get("callSid")
                            if call_sid:
                                print(f"[WS] callSid={call_sid}", flush=True)
                        except Exception:
                            pass
                    if et == "stop":
                        break
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
        # S3 保存
        try:
            day = started_at.split("T")[0]
            key = f"calls/{day}/call-{call_id}.json"
            body = json.dumps({
                "id": call_id,
                "started_at": started_at,
                "finished_at": finished_at,
                "language": TRANSCRIBE_LANGUAGE,
                "text": text_joined
            }, ensure_ascii=False).encode("utf-8")
            s3.put_object(Bucket=S3_BUCKET, Key=key, Body=body, ContentType="application/json")
            print(f"[S3] put s3://{S3_BUCKET}/{key} bytes={len(body)}", flush=True)
        except Exception:
            print("[S3] put failed", flush=True)
            traceback.print_exc()

        print(f"[WS] CLOSE call={call_id} text='{text_joined}'", flush=True)
        try: await ws.close()
        except Exception: pass

@app.get("/transcripts")
async def list_transcripts(limit: int = 10):
    return {"items": RECENTS[: max(1, min(limit, MAX_RECENTS))]}
