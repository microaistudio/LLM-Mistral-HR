# Version: 1.3.3  (proxy to llama-server)
# Path: server.py
# Purpose: FastAPI gateway for DocuMind LLM — proxies to persistent llama.cpp server
# Notes:
# - GET /chat accepts both ?prompt= and legacy ?q=
# - Per-request ?max_tokens (or ?n_predict) clamped to env MODEL_TOKENS
# - Echo: reply/response, elapsed_ms, used_tokens, max_tokens_used
# - Echo: timeout_ms_used + timeout_source ("client"|"default"|"clamped")
# - ENFORCE: transport timeout = (timeout_ms_used/1000) + 0.5

import os
import json
import shlex
import subprocess
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, Query, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from time import perf_counter

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# --------------------------------------------------------------------------------------
# Configuration (env-driven)
# --------------------------------------------------------------------------------------
LLAMA_BIN = os.getenv("LLAMA_BIN", "").strip()
MODEL_PATH = os.getenv("MODEL_PATH", "").strip()

# runtime knobs
MODEL_TOKENS = int(os.getenv("MODEL_TOKENS", "64"))
MODEL_CTX = int(os.getenv("MODEL_CTX", "2048"))
MODEL_THREADS = int(os.getenv("MODEL_THREADS", "0") or "0") or None
MODEL_TEMPERATURE = float(os.getenv("MODEL_TEMPERATURE", "0.7"))
MODEL_TOP_P = float(os.getenv("MODEL_TOP_P", "0.95"))
EXTRA_ARGS = os.getenv("LLAMA_EXTRA_ARGS", "").strip()
TIMEOUT_SEC = int(os.getenv("LLAMA_TIMEOUT", "30"))  # legacy default

# proxy to persistent llama.cpp HTTP server if provided
LLAMA_SERVER_URL = os.getenv("LLAMA_SERVER_URL", "").rstrip("/")

# Gateway timeout resolver (echo + enforced upstream)
GATEWAY_DEFAULT_TIMEOUT_MS = int(os.getenv("GATEWAY_DEFAULT_TIMEOUT_MS", str(TIMEOUT_SEC * 1000)))
GATEWAY_MAX_TIMEOUT_MS     = int(os.getenv("GATEWAY_MAX_TIMEOUT_MS", "60000"))

# --------------------------------------------------------------------------------------
# App setup
# --------------------------------------------------------------------------------------
app = FastAPI(title="DocuMind LLM Gateway", version="1.3.3")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def _clamp_tokens(req_cap: Optional[int]) -> int:
    if not req_cap:
        return MODEL_TOKENS
    try:
        cap = int(req_cap)
    except Exception:
        cap = MODEL_TOKENS
    if cap < 1:
        cap = 1
    if cap > MODEL_TOKENS:
        cap = MODEL_TOKENS
    return cap

def _resolve_timeout_ms(d: Dict[str, Any]) -> tuple[int, str]:
    """
    Return (effective_timeout_ms, source).
    Precedence: timeout_ms (ms) > timeout (sec) > default.
    Clamp to [1000, GATEWAY_MAX_TIMEOUT_MS].
    Source is:
      - "client"  if provided by client and NOT reduced by clamping
      - "clamped" if provided by client and WAS reduced
      - "default" if no client override
    """
    src = "default"
    t_ms = None
    requested: Optional[int] = None

    # explicit ms
    if "timeout_ms" in d and d.get("timeout_ms") is not None:
        try:
            requested = int(d.get("timeout_ms"))
            t_ms = requested
            src = "client"
        except Exception:
            t_ms = None

    # seconds
    if t_ms is None and "timeout" in d and d.get("timeout") is not None:
        try:
            requested = int(float(d.get("timeout")) * 1000.0)
            t_ms = requested
            src = "client"
        except Exception:
            t_ms = None

    # default
    if t_ms is None:
        t_ms = GATEWAY_DEFAULT_TIMEOUT_MS
        src = "default"

    # clamp to bounds
    clamped_val = max(1000, min(int(t_ms), GATEWAY_MAX_TIMEOUT_MS))
    # Only mark "clamped" if we actually reduced a client-supplied value
    if src == "client" and requested is not None and clamped_val < requested:
        src = "clamped"
    t_ms = clamped_val
    return t_ms, src

def build_llama_cmd(prompt: str, n_predict: int) -> List[str]:
    cmd = [
        LLAMA_BIN, "-m", MODEL_PATH,
        "-p", prompt,
        "-n", str(n_predict),
        "--ctx-size", str(MODEL_CTX),
        "--temp", str(MODEL_TEMPERATURE),
        "--top-p", str(MODEL_TOP_P),
    ]
    if MODEL_THREADS and MODEL_THREADS > 0:
        cmd += ["-t", str(MODEL_THREADS)]
    if EXTRA_ARGS:
        cmd += shlex.split(EXTRA_ARGS)
    return cmd

def call_llama_subprocess(prompt: str, n_predict: int, timeout_s: float) -> str:
    if not LLAMA_BIN or not MODEL_PATH:
        raise HTTPException(500, "LLAMA_BIN/MODEL_PATH not set and no LLAMA_SERVER_URL provided.")
    try:
        proc = subprocess.run(
            build_llama_cmd(prompt, n_predict),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="LLM timed out (subprocess).")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="LLAMA_BIN not found/executable.")
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and not out:
        err = (proc.stderr or "Unknown llama.cpp error").strip()
        raise HTTPException(status_code=500, detail=f"LLM error: {err}")
    return out

def call_llama_server(prompt: str, n_predict: int, timeout_s: float) -> str:
    import urllib.request, urllib.error
    payload = {
        "prompt": prompt,
        "n_predict": n_predict,
        "temperature": MODEL_TEMPERATURE,
        "top_p": MODEL_TOP_P,
        "cache_prompt": True,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{LLAMA_SERVER_URL}/completion",
        data=data, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8")
            j = json.loads(body)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode(errors="ignore")
        except Exception:
            err_body = ""
        raise HTTPException(e.code, f"Model server HTTP error: {err_body[:200]}")
    except Exception as e:
        # Could be a timeout or connectivity; preserve legacy 502 for now
        raise HTTPException(502, f"Model server unreachable: {e}")

    for key in ("content", "response"):
        if key in j and isinstance(j[key], str):
            return j[key]
    if "choices" in j and j["choices"]:
        c = j["choices"][0]
        if "text" in c:
            return c["text"]
        if "message" in c and "content" in c["message"]:
            return c["message"]["content"]
    raise HTTPException(status_code=500, detail="Unexpected model server response.")

def call_llama(prompt: str, n_predict: int, timeout_s: float) -> str:
    if not prompt or not prompt.strip():
        raise HTTPException(status_code=422, detail="Prompt is empty.")
    prompt = prompt.strip()
    return call_llama_server(prompt, n_predict, timeout_s) if LLAMA_SERVER_URL else call_llama_subprocess(prompt, n_predict, timeout_s)

# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------
@app.get("/", response_class=JSONResponse)
def root() -> Dict[str, Any]:
    return {"service": "DocuMind LLM Gateway", "version": app.version, "status": "ok"}

@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"

# GET /chat  (accepts ?prompt= or ?q=)
@app.get("/chat")
def chat_get(
    request: Request,
    prompt: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    max_tokens: Optional[int] = Query(None),
    n_predict: Optional[int] = Query(None),
) -> JSONResponse:
    text = (prompt or q or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="Missing 'prompt' (or 'q') parameter")
    tokens_req = max_tokens or n_predict
    n_tokens = _clamp_tokens(tokens_req)

    merged = dict(request.query_params)
    t_ms, t_src = _resolve_timeout_ms(merged)
    timeout_s = (t_ms / 1000.0) + 0.5  # enforce upstream

    t0 = perf_counter()
    ans = call_llama(text, n_tokens, timeout_s)
    elapsed_ms = int((perf_counter() - t0) * 1000)
    return JSONResponse({
        "reply": ans,
        "response": ans,
        "elapsed_ms": elapsed_ms,
        "used_tokens": n_tokens,
        "max_tokens_used": n_tokens,
        "timeout_ms_used": t_ms,
        "timeout_source": t_src,
    })

# POST /chat
@app.post("/chat")
def chat_post(request: Request, payload: Dict[str, Any] = Body(...)) -> JSONResponse:
    text = ((payload or {}).get("prompt") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="Body must include 'prompt'.")
    tokens_req = (payload or {}).get("max_tokens") or (payload or {}).get("n_predict")
    n_tokens = _clamp_tokens(tokens_req)

    merged = dict(request.query_params)
    if isinstance(payload, dict):
        merged.update(payload)
    t_ms, t_src = _resolve_timeout_ms(merged)
    timeout_s = (t_ms / 1000.0) + 0.5  # enforce upstream

    t0 = perf_counter()
    ans = call_llama(text, n_tokens, timeout_s)
    elapsed_ms = int((perf_counter() - t0) * 1000)
    return JSONResponse({
        "reply": ans,
        "response": ans,
        "elapsed_ms": elapsed_ms,
        "used_tokens": n_tokens,
        "max_tokens_used": n_tokens,
        "timeout_ms_used": t_ms,
        "timeout_source": t_src,
    })

@app.get("/debug/llm")
def debug_llm() -> Dict[str, Any]:
    return {
        "mode": "server" if LLAMA_SERVER_URL else "subprocess",
        "LLAMA_SERVER_URL": LLAMA_SERVER_URL or None,
        "LLAMA_BIN": LLAMA_BIN,
        "MODEL_PATH": MODEL_PATH,
        "MODEL_TOKENS": MODEL_TOKENS,
        "MODEL_CTX": MODEL_CTX,
        "MODEL_THREADS": MODEL_THREADS,
        "temperature": MODEL_TEMPERATURE,
        "top_p": MODEL_TOP_P,
        "timeout": TIMEOUT_SEC,  # legacy
        "gateway_default_timeout_ms": GATEWAY_DEFAULT_TIMEOUT_MS,
        "gateway_max_timeout_ms": GATEWAY_MAX_TIMEOUT_MS,
    }

# --------------------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")))
