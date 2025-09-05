# Path: whatsapp_llm_gateway.py
# Version: 3.4.0-wa.llm (RAG + retries + push preview)
# WhatsApp LLM Worker: retrieve (DocuMind local) -> generate -> push via Twilio

import os, asyncio, logging
from typing import Dict, Any, Tuple, Optional, List
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="WhatsApp LLM Worker (RAG)")

# ---------------- Env ----------------
WA_LLM_TIMEOUT_MS  = int(os.getenv("WA_LLM_TIMEOUT_MS", "9000"))
WA_MAX_TOKENS      = int(os.getenv("WA_MAX_TOKENS", "48"))
WA_PERCENT_CAP     = int(os.getenv("WA_PERCENT_CAP", "35"))
WA_LANG_HINT       = os.getenv("WA_LANG_HINT", "en").lower()
WA_DEBUG           = os.getenv("WA_DEBUG", "1").lower() in {"1","true","yes","on"}

# LLM server (already working with 'prompt' schema)
LLM_API_URL        = os.getenv("LLM_API_URL", "http://127.0.0.1:8000/chat")
WA_LLM_SCHEMA      = os.getenv("WA_LLM_SCHEMA", "prompt").lower()  # we use 'prompt'

# DocuMind Ask (for retrieval)
ASK_HTTP_BASE      = os.getenv("ASK_HTTP_BASE", "http://127.0.0.1:9000").rstrip("/")
ASK_ANSWER_URL     = f"{ASK_HTTP_BASE}/api/ask/answer"   # JSON answer endpoint

# Twilio for push
TWILIO_ENABLED     = os.getenv("TWILIO_ENABLED", "true").lower() in {"1","true","yes","on"}
TWILIO_SID         = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN       = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM        = os.getenv("TWILIO_FROM", os.getenv("TWILIO_WHATSAPP_NUMBER", ""))

log = logging.getLogger("uvicorn")

# ---------------- Utilities ----------------
def _ok_twilio() -> bool:
    return TWILIO_ENABLED and TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM

def _normalize_wa(s: str) -> str:
    s = (s or "").strip().replace("whatsapp: ", "whatsapp:+")
    if s.startswith("whatsapp:"):
        left, right = s.split(":", 1); right = right.strip()
        if right and right[0].isdigit(): right = "+" + right
        s = f"{left}:{right}"
    return s

def _extract_text(data: Dict[str, Any]) -> str:
    text = (data.get("reply") or data.get("response") or data.get("text") or "").strip()
    if not text and isinstance(data.get("choices"), list) and data["choices"]:
        text = ((data["choices"][0].get("message") or {}).get("content") or "").strip()
    return text

def _is_retryable(meta: Dict[str, Any]) -> bool:
    err = (meta or {}).get("data", {}).get("error", "")
    e = err.lower()
    return (" 50" in err) or ("502" in err) or ("bad gateway" in e) or ("timeout" in e)

# ---------------- Retrieval (DocuMind local) ----------------
async def _fetch_local_context(q: str, timeout_ms: int) -> Tuple[str, List[str]]:
    """Returns (context_text, source_ids[]) or ("", [])."""
    payload = {
        "q": q,
        "lang": WA_LANG_HINT,
        "mode": "local",
        "topk": 8,
        "evidence_k": 4,
        "percent_cap": WA_PERCENT_CAP,
        "max_tokens": 96,
        "timeout_ms": max(2000, min(6000, timeout_ms - 1500)),
    }
    try:
        async with httpx.AsyncClient(timeout=(payload["timeout_ms"]/1000.0)+1.0) as cli:
            r = await cli.post(ASK_ANSWER_URL, json=payload)
            r.raise_for_status()
            j = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
            # We accept several shapes; prefer explicit fields if present
            ctx = (j.get("context") or j.get("answer") or "").strip()
            sources = j.get("sources") or j.get("evidence") or []
            # Normalize sources to a list of short IDs/labels
            if isinstance(sources, dict): sources = list(sources.values())
            sources = [str(s)[:80] for s in sources if s]
            return ctx, sources
    except Exception as e:
        if WA_DEBUG: log.warning(f"[rag.fetch] error: {e}")
        return "", []

# ---------------- LLM calls ----------------
async def _call_chat_prompt(cli: httpx.AsyncClient, url: str, prompt: str, max_tokens: int, timeout_ms: int) -> Tuple[bool, str, Dict[str, Any]]:
    body = {"prompt": prompt, "max_tokens": max_tokens, "timeout_ms": timeout_ms}
    try:
        r = await cli.post(url, json=body)
        data = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
        text = _extract_text(data)
        if r.status_code == 200 and text:
            return True, text, {"status": "200", "data": data}
        return False, "", {"status": str(r.status_code), "data": {"error": f"HTTP {r.status_code}"}}
    except httpx.HTTPError as e:
        return False, "", {"status": "ERR", "data": {"error": str(e)}}

def _build_prompt(q: str, ctx: str, sources: List[str]) -> str:
    src_line = ""
    if sources:
        src_line = "Sources: " + "; ".join(f"[{s}]" for s in sources[:6])
    policy = (
        "You are answering for WhatsApp in 3–6 short bullets.\n"
        "Use ONLY the CONTEXT below. If the answer is not in the context, say: 'Not found locally.'\n"
        "Mirror the user's language (Hindi/English). Keep it concise.\n"
        f"{src_line}\n\n"
        "CONTEXT:\n"
        f"{ctx}\n\n"
        f"QUESTION: {q}\n"
        "ANSWER:"
    )
    return policy

async def _answer_with_retry(q: str, max_tokens: int, timeout_ms: int, tries: int = 3) -> Tuple[bool, str, Dict[str, Any]]:
    # Retrieve context first (best-effort)
    ctx, sources = await _fetch_local_context(q, timeout_ms)
    prompt = _build_prompt(q, ctx, sources) if ctx else (
        "Answer briefly in 3–5 bullets. Mirror user language (Hindi/English).\n\n"
        f"Question: {q}\nAnswer:"
    )
    delay = 0.5
    async with httpx.AsyncClient(timeout=(timeout_ms/1000.0)+2.0) as cli:
        for i in range(1, tries+1):
            if WA_DEBUG: log.info(f"[llm.try{i}] schema=prompt url={LLM_API_URL}")
            ok, text, meta = await _call_chat_prompt(cli, LLM_API_URL, prompt, min(max_tokens,128), timeout_ms)
            if ok:
                if WA_DEBUG:
                    d = meta.get("data", {})
                    text_len = len(_extract_text(d))
                    log.info(f"[llm.ok] schema=prompt elapsed_ms={d.get('elapsed_ms','?')} url={LLM_API_URL} text.len={text_len}")
                return True, text, meta
            if _is_retryable(meta) and i < tries:
                if WA_DEBUG: log.warning(f"[llm.fail] schema=prompt meta={meta}; retrying in {int(delay*1000)}ms")
                await asyncio.sleep(delay); delay *= 1.6
                continue
            if WA_DEBUG: log.warning(f"[llm.fail] schema=prompt meta={meta}")
            return False, "", meta
    return False, "", {"status": "ERR", "data": {"error": "unknown"}}

# ---------------- Twilio ----------------
async def twilio_send_async(to_wa: str, text: str):
    if not _ok_twilio():
        log.warning("twilio-send skipped: missing TWILIO_* env")
        return
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
    data = {"From": TWILIO_FROM, "To": _normalize_wa(to_wa), "Body": text}
    try:
        async with httpx.AsyncClient(timeout=10.0, auth=(TWILIO_SID, TWILIO_TOKEN)) as cli:
            r = await cli.post(url, data=data)
            r.raise_for_status()
            prev = (text or "").replace("\n", " ")[:140]
            log.info(f"twilio-send ok to={data['To']} preview={prev}")
    except Exception as e:
        log.warning(f"twilio-send error: {e}")

# ---------------- Endpoints ----------------
@app.get("/twilio/health")
async def health():
    return JSONResponse({
        "ok": True,
        "twilio": bool(_ok_twilio()),
        "budget_ms": 12000,
        "timeout_ms": WA_LLM_TIMEOUT_MS,
        "llm_api_url": LLM_API_URL,
        "ask_answer_url": ASK_ANSWER_URL,
        "debug": WA_DEBUG,
        "schema": "prompt",
        "last_ok_schema": None,
    })

@app.get("/api/llm/diag")
async def diag(url: Optional[str] = None, timeout_ms: int = 3000):
    u = (url or LLM_API_URL)
    # simple ping using prompt schema
    async with httpx.AsyncClient(timeout=(timeout_ms/1000.0)+1.0) as cli:
        ok, text, meta = await _call_chat_prompt(cli, u, "Pong!", 16, timeout_ms)
    return JSONResponse({
        "url": u, "timeout_ms": timeout_ms,
        "results": [{"attempt": "prompt", "ok": ok, "text": text[:60], "meta": {"status": meta.get("status"), "data": meta.get("data")}}]
    })

@app.post("/api/wa/answer")
async def api_answer(req: Request):
    j = await req.json()
    q = (j.get("q") or "").strip()
    t = int(j.get("timeout_ms") or WA_LLM_TIMEOUT_MS)
    mx = int(j.get("max_tokens") or WA_MAX_TOKENS)
    ok, text, meta = await _answer_with_retry(q, mx, t, tries=3)
    if not ok:
        text = "LLM busy; try again."
    return JSONResponse({
        "answer": text,
        "elapsed_ms": meta.get("data", {}).get("elapsed_ms"),
        "used_tokens": meta.get("data", {}).get("used_tokens"),
        "timeout_ms_used": meta.get("data", {}).get("timeout_ms_used"),
        "max_tokens_used": meta.get("data", {}).get("max_tokens_used"),
    })

@app.post("/api/wa/push")
async def api_push(req: Request):
    j = await req.json()
    to = (j.get("to") or "").strip()
    q  = (j.get("q")  or "").strip()
    t  = int(j.get("timeout_ms") or WA_LLM_TIMEOUT_MS)
    mx = int(j.get("max_tokens") or WA_MAX_TOKENS)

    ok, text, _ = await _answer_with_retry(q, mx, t, tries=3)
    if not ok or not (text or "").strip():
        text = "LLM busy; try again."
    await twilio_send_async(to, text)
    return JSONResponse({"ok": True})
