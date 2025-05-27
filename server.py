"""
====================================================================
 FastAPI Server for Local GGUF Model Inference (Mistral-7B)
====================================================================

 Author        : MicroAI Studio - Subhash Thakkur
 File Name     : server.py
 Last Updated  : 2025-05-25
 Purpose       : Acts as an ASGI app using FastAPI to interface
                 with locally executed llama.cpp-based GGUF model.

 Functions:
 - GET  /       : Serves static HTML chat UI (basic form).
 - GET  /chat   : Accepts query parameter (?q=message) and invokes
                  `llama-run` from llama.cpp to generate model output.
 - POST /chat   : Accepts JSON payload {"message": "..."} for API-style
                  programmatic interaction.

 Dependencies:
 - FastAPI
 - Uvicorn
 - subprocess
 - aiofiles (for static file serving)

====================================================================
"""

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import subprocess
import os
import re

app = FastAPI()

# Serve static HTML frontend
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse("static/chat.html")

@app.get("/chat")
async def chat_get(q: str):
    result = call_llama(q)
    return {"response": result}

@app.post("/chat")
async def chat_post(request: Request):
    data = await request.json()
    message = data.get("message", "")
    result = call_llama(message)
    return {"response": result}

def clean_model_output(raw_output: str) -> str:
    """
    Clean the model output by removing ANSI codes, prompt formatting,
    and extracting only the assistant's response.
    """
    # Remove ANSI escape sequences
    clean = re.sub(r'\x1b\[[0-9;]*m', '', raw_output)
    
    # Remove special tokens and formatting
    clean = re.sub(r'<\|im_start\|>.*?<\|im_end\|>', '', clean, flags=re.DOTALL)
    clean = re.sub(r'<\|.*?\|>', '', clean)
    
    # Remove system/user prompts if they leaked through
    clean = re.sub(r'<s>\[INST\].*?\[/INST\]', '', clean, flags=re.DOTALL)
    clean = re.sub(r'\[INST\].*?\[/INST\]', '', clean, flags=re.DOTALL)
    
    # Remove any remaining prompt indicators
    clean = re.sub(r'(system|user|assistant):', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'Human:.*?Assistant:', '', clean, flags=re.DOTALL | re.IGNORECASE)
    
    # Clean up whitespace and newlines
    clean = re.sub(r'\n+', '\n', clean)
    clean = clean.strip()
    
    # If output is still messy, try to extract just the meaningful response
    lines = clean.split('\n')
    meaningful_lines = []
    for line in lines:
        line = line.strip()
        if line and not any(skip in line.lower() for skip in ['loading', 'model:', 'prompt:', '[end]', 'generated']):
            meaningful_lines.append(line)
    
    if meaningful_lines:
        return '\n'.join(meaningful_lines)
    
    return clean if clean else "Sorry, I couldn't generate a proper response."

def call_llama(prompt: str) -> str:
    """
    Invokes the llama.cpp binary with the local mistral GGUF model
    Returns the generated output as a string.
    """
    model_path = os.path.realpath(
        os.path.expanduser("~/llm-stack/mistral/models/mistral-7b-instruct/mistral-7b-instruct-v0.1.Q4_K_M.gguf")
    )
    llama_bin = os.path.realpath(
        os.path.expanduser("~/llm-stack/mistral/llama.cpp/build/bin/llama-run")
    )

    # Format the prompt properly for Mistral
    formatted_prompt = f"<s>[INST] {prompt.strip()} [/INST]"

    try:
        # Improved parameters for better, faster responses
        output = subprocess.check_output([
            llama_bin,
            f"file://{model_path}",
            formatted_prompt,
            "--temp", "0.7",           # Lower temperature for more focused responses
            "--top-p", "0.9",          # Nucleus sampling
            "--repeat-penalty", "1.1", # Prevent repetition
            "--ctx-size", "2048",      # Context size
            "--predict", "150",        # Limit response length
            "--threads", "4",          # Use multiple threads
            "--batch-size", "512",     # Batch size for faster processing
        ], stderr=subprocess.STDOUT, text=True, timeout=30)  # 30 second timeout
        
        return clean_model_output(output)
        
    except subprocess.TimeoutExpired:
        return "Response timed out. Please try a shorter question."
    except subprocess.CalledProcessError as e:
        error_msg = clean_model_output(e.output) if e.output else "Unknown error"
        return f"Error running model: {error_msg}"
    except FileNotFoundError as e:
        return f"Binary or model not found: {e}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"