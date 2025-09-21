from fastapi import FastAPI, WebSocket, Response
from fastapi.responses import PlainTextResponse
import os, json, base64, asyncio, traceback
from typing import Optional

# ==== Google Cloud ====
GCP_SA_JSON = os.getenv("GCP_SA_JSON")
GOOGLE_APPLICATION_CREDENTIALS = "/tmp/gcp-sa.json"
if GCP_SA_JSON:
    with open(GOOGLE_APPLICATION_CREDENTIALS, "w", encoding="utf-8") as f:
        f.write(GCP_SA_JSON)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GOOGLE_APPLICATION_CREDENTIALS

from google.cloud import texttospeech
from google.cloud import speech_v1p1beta1 as speech

app = FastAPI()

SAMPLE_RATE = 8000            # Twilio は 8kHz μ-law
FRAME_MS    = 20              # 20ms/フレーム
FRAME_BYTES = int(SAMPLE_RATE * FRAME_MS / 1000)  # 160 bytes
VOICE_NAME  = "ja-JP-Neural2-B"
TTS_CLIENT  = texttospeech.TextToSpeechClient()

def log(msg: str):
    print(f"[app] {msg}", flush=True)

@app.get("/health")
def health():
    return {"status":"ok"}

@app.post("/twiml")
def twiml():
    host = os.getenv("PUBLIC_FQDN", "")
    if not host:
        return PlainTextResponse("<Response><Say>Config error</Say></Response>", media_type="text/xml")
    twiml_xml = f"""
<Response>
  <Connect>
    <Stream url="wss://{host}/stream"/>
  </Connect>
</Response>
""".strip()
    return Response(content=twiml_xml, media_type="text/xml")

def tts_mulaw_bytes(text: str) -> bytes:
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(language_code="ja-JP", name=VOICE_NAME)
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MULAW,
        sample_rate_hertz=SAMPLE_RATE
    )
    resp = TTS_CLIENT.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)
    return resp.audio_content

async def send_audio_over_ws(ws: WebSocket, stream_sid: str, mulaw: bytes, chunk_ms: int = FRAME_MS):
    """μ-law 生データを Twilio へ 20ms ごとに base64 で送信。※必ず streamSid を付与"""
    if not mulaw or not stream_sid:
        return
    frame = FRAME_BYTES
    pos = 0
    while pos < len(mulaw):
        payload = mulaw[pos:pos+frame]
        pos += frame
        msg = {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(payload).decode("ascii")}
        }
        await ws.send_text(json.dumps(msg))
        await asyncio.sleep(chunk_ms / 1000.0)
    # 送出完了マーク（これにも streamSid が必要）
    await ws.send_text(json.dumps({"event":"mark", "streamSid": stream_sid, "mark":{"name":"tts_end"}}))

def build_stt_streaming_config() -> speech.StreamingRecognitionConfig:
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.MULAW,
        sample_rate_hertz=SAMPLE_RATE,
        language_code="ja-JP",
        enable_automatic_punctuation=True
    )
    return speech.StreamingRecognitionConfig(
        config=config,
        interim_results=True,
        single_utterance=False
    )

class SttStream:
    """Twilio からの μ-law 8k を Google STT へストリーミング"""
    def __init__(self):
        self.client = speech.SpeechClient()
        self.requests_q: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def request_generator(self):
        yield speech.StreamingRecognizeRequest(streaming_config=build_stt_streaming_config())
        while not self.closed:
            chunk = await self.requests_q.get()
            if chunk is None:
                break
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

    async def start(self, on_transcript):
        try:
            responses = self.client.streaming_recognize(requests=self.request_generator())
            async for resp in responses:
                for result in resp.results:
                    if result.alternatives:
                        text = result.alternatives[0].transcript
                        is_final = result.is_final
                        await on_transcript(text, is_final)
        except Exception as e:
            log(f"STT error: {e}")

    async def feed(self, mulaw_bytes: bytes):
        await self.requests_q.put(mulaw_bytes)

    async def close(self):
        self.closed = True
        await self.requests_q.put(None)

@app.websocket("/stream")
async def stream(ws: WebSocket):
    await ws.accept()
    log("WebSocket /stream accepted")
    stt = SttStream()

    stream_sid: Optional[str] = None   # ★ Twilio の streamSid を保持
    stt_task: Optional[asyncio.Task] = None
    speaking = False
    started_event = asyncio.Event()    # start を受け取るまで送話しない

    async def on_transcript(text: str, is_final: bool):
        nonlocal speaking, stream_sid
        log(f"[STT]{' (final)' if is_final else ''}: {text}")
        if is_final and not speaking and stream_sid:
            reply = None
            if "名前" in text or "お名前" in text:
                reply = "はい、こちらは受付です。お名前を教えてください。"
            elif len(text.strip()) > 0:
                reply = "ありがとうございます。担当者におつなぎします。"
            if reply:
                speaking = True
                try:
                    audio = tts_mulaw_bytes(reply)
                    await send_audio_over_ws(ws, stream_sid, audio)
                finally:
                    speaking = False

    stt_task = asyncio.create_task(stt.start(on_transcript))

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            ev = msg.get("event")

            if ev == "connected":
                # ここでは streamSid は来ないことが多い。ログだけ出す。
                log("event: connected")

            elif ev == "start":
                stream_sid = msg.get("start", {}).get("streamSid")
                log(f"stream start: sid={stream_sid}")
                started_event.set()

                # 接続直後のあいさつ（★ start 後に送る）
                try:
                    greeting = "こんにちは。こちらは受付です。ご用件をどうぞ。"
                    await send_audio_over_ws(ws, stream_sid, tts_mulaw_bytes(greeting))
                except Exception as e:
                    log(f"TTS initial error: {e}")

            elif ev == "media":
                # Twilio -> μ-law base64
                payload_b64 = msg.get("media", {}).get("payload", "")
                if payload_b64:
                    await stt.feed(base64.b64decode(payload_b64))

            elif ev == "mark":
                pass

            elif ev == "stop":
                log("stream stop")
                break

            else:
                log(f"unknown event: {ev}")

    except Exception as e:
        log(f"WS loop error: {e}\n{traceback.format_exc()}")

    finally:
        await stt.close()
        if stt_task:
            await asyncio.wait([stt_task], timeout=2)
        await ws.close()
        log("WebSocket closed")
