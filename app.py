from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
import os
from pathlib import Path
from datetime import datetime

app = FastAPI(title="voicebot")

DEPLOY_STAMP_PATH = Path(__file__).parent / "DEPLOY_STAMP.txt"

def _read_deploy_stamp():
    try:
        txt = DEPLOY_STAMP_PATH.read_text(encoding="utf-8").strip()
        parts = txt.split()
        stamp = {"raw": txt, "time_utc": None, "commit_sha": None}
        if len(parts) >= 2:
            stamp["time_utc"] = parts[1]
        if len(parts) >= 3:
            stamp["commit_sha"] = parts[2]
        return stamp
    except FileNotFoundError:
        return {"raw": None, "time_utc": None, "commit_sha": None}
    except Exception as e:
        return {"raw": f"error: {e}", "time_utc": None, "commit_sha": None}

@app.get("/", response_class=PlainTextResponse)
def root():
    return "voicebot is running"

@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat() + "Z"}

@app.get("/version")
def version():
    stamp = _read_deploy_stamp()
    env = {
        "image_tag": os.getenv("IMAGE_TAG"),
        "aws_region": os.getenv("AWS_REGION"),
    }
    return JSONResponse({"app": "voicebot", "deploy_stamp": stamp, "env": env})
