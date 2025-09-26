# app.py 〈全文〉
# /twiml_stream → WebSocket → Transcribe でSTT
# 通話ごとの FINAL テキストを S3（calls/<YYYY-MM-DD>/call-<id>.json）へ保存
# /transcripts で直近メモリ保持も参照可
from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from datetime import datetime, timezone
import os, json, traceback, base64, audioop, asyncio, uuid
from typing import Dict, List

from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import TranscriptEvent

import boto3

APP_NAME = "voicebot"
APP_VERSION = "0.7.0"  # S3保存対応

SAMPLE_RATE = 8000
AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-1")
TRANSCRIBE_LANGUAGE = os.getenv("TRANSCRIBE_LANGUAGE", "ja-JP")
TRANSCRIBE_VOCAB = os.getenv("TRANSCRIBE_VOCAB", "")
S3_BUCKET = os.getenv("TRANSCRIPT_BUCKET", f"voicebot-transcripts-291234479055-{AWS_REGION}")

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
    xml=('<?xml version="1.0" encoding="UTF-8"?>'
         '<Response><Say language="ja-JP">こちらはボイスボットのテストです。10秒後に切断します。</Say>'
         '<Pause length="10"/><Hangup/></Response>')
    return Response(content=xml, media_type="text/xml")

@app.get("/twiml_stream")
@app.post("/twiml_stream")
async def twiml_stream():
    ws_url = "wss://voice.frontglass.net/stream"
    xml=('<?xml version="1.0" encoding="UTF-8"?>'
         '<Response><Say language="ja-JP">テスト</Say>'
         '<Say language="ja-JP">接続テストを開始します。</Say>'
         f'<Connect><Stream url="{ws_url}"/></Connect></Response>')
    return Response(content=xml, media_type="text/xml")

class MyTranscriptHandler(TranscriptResultStreamHandler):
    def __init__(self, output_stream, on_final):
        super().__init__(output_stream)
        self.on_final = on_final
    async def handle_transcript_event(self, ev: TranscriptEvent):
        for res in ev.transcript.results:
            if not res.alternatives: continue
            text = res.alternatives[0].transcript or ""
            if not text: continue
            if res.is_partial:
                print(f"[STT] PARTIAL: {text}", flush=True)
            else:
                print(f"[STT] FINAL  : {text}", flush=True)
                self.on_final(text)

@app.websocket("/stream")
async def stream_ws(ws: WebSocket):
    await ws.accept()
    call_id = str(uuid.uuid4())[:8]
    started_at = datetime.now(timezone.utc).isoformat()
    print(f"[WS] OPEN call={call_id} at {started_at}", flush=True)

    s3 = boto3.client("s3", region_name=AWS_REGION)
    finals: List[str] = []

    client = TranscribeStreamingClient(region=AWS_REGION)
    stream = await client.start_stream_transcription(
        language_code=TRANSCRIBE_LANGUAGE,
        media_sample_rate_hz=SAMPLE_RATE,
        media_encoding="pcm",
        vocabulary_name=TRANSCRIBE_VOCAB or None,
    )

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
                    if et in ("connected","start","stop","mark"): print(f"[WS] {et}", flush=True)
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
            traceback.print_exc()
        finally:
            try: await stream.input_stream.end_stream()
            except Exception: pass

    async def read_transcripts():
        try:
            handler = MyTranscriptHandler(stream.output_stream, on_final=lambda t: finals.append(t))
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
        # --- S3へ保存 ---
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
