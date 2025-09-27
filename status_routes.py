# status_routes.py
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/twilio")

@router.post("/status")
async def twilio_status(request: Request):
    try:
        form = await request.form()  # Twilioは application/x-www-form-urlencoded
        call_sid = form.get("CallSid")
        call_status = form.get("CallStatus")
        from_ = form.get("From")
        to_ = form.get("To")
        err = form.get("ErrorCode")  # 失敗時のみ付与されることが多い

        print(f"[twilio-status] sid={call_sid} status={call_status} from={from_} to={to_} err={err}", flush=True)
        return PlainTextResponse("ok", status_code=200)
    except Exception as e:
        # Twilioの再送ループを避けるため、例外時も200を返す
        print(f"[twilio-status][error] {e}", flush=True)
        return PlainTextResponse("ok", status_code=200)
