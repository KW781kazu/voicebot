# app.py 〈全文〉 v0.8.8
# - 送信先番号の取得を強化：WSイベント(connected/start)の from/to を保持し、SMS送信時に最優先で使用。
# - それでも無い場合は CallSid から fetch。既存機能はそのまま。

from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect, Request, APIRouter, Form
from datetime import datetime, timezone
import os, json, traceback, base64, audioop, asyncio, uuid, time, re, urllib.parse
from typing import Dict, List, Optional, Tuple

from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import TranscriptEvent

import boto3
from twilio.rest import Client as TwilioClient

router = APIRouter()
@router.post("/twilio/status")
async def twilio_status(
    CallSid: str = Form(default=""),
    CallStatus: str = Form(default=""),
    From: str = Form(default=""),
    To: str = Form(default="")
):
    print(f"[TW] status sid={CallSid} status={CallStatus} from={From} to={To}", flush=True)
    return {"ok": True}

APP_NAME = "voicebot"
APP_VERSION = "0.8.8"

SAMPLE_RATE = 8000
AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-1")
TRANSCRIBE_LANGUAGE = os.getenv("TRANSCRIBE_LANGUAGE", "ja-JP")
TRANSCRIBE_VOCAB = os.getenv("TRANSCRIBE_VOCAB", "")
S3_BUCKET = os.getenv("TRANSCRIPT_BUCKET", f"voicebot-transcripts-291234479055-{AWS_REGION}")

TW_SID = os.getenv("TWILIO_ACCOUNT_SID")
TW_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TW_FROM = os.getenv("TWILIO_FROM", "")  # 任意（未設定なら通話の To を使用）

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
    now_utc
