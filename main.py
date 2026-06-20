import os
import asyncio
import base64
import io
import json
import re
import shlex
import httpx
from pathlib import Path
from typing import Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from supabase import create_client, Client
from fpdf import FPDF
from dotenv import load_dotenv
import google.generativeai as genai

# ===== ENV VARS =====
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# ===== INIT =====
app = FastAPI(title="HYE Ecosystem API", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"SUPABASE INIT FAILED: {e}")

# GEMINI CLIENT
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash')
else:
    model = None

# ===== TERMINAL RULES =====
LIGHT_PACKAGES = {
    'axios', 'uuid', 'clsx', 'dayjs', 'nanoid', 'lodash',
    'tailwindcss', 'postcss', 'autoprefixer', 'sass',
    'jspdf', 'jspdf-autotable', 'html2canvas', 'xlsx', 'papaparse',
    'react-icons', 'lucide-react', 'framer-motion', 'zustand',
    'prettier', 'eslint', '@types/node', 'vite'
}

HEAVY_PACKAGES = {
    'next': 400, 'nuxt': 300, 'remix': 200, 'angular': 400, '@angular': 400,
    'three': 50, 'electron': 200, 'nestjs': 100, 'tesseract.js': 50,
    'tensorflow': 500, 'puppeteer': 300, '@mui/material': 100
}

BLOCKED_COMMANDS = ["docker", "sudo", "apt-get", "pip install", "yarn add", "pnpm add", "rm -rf", "curl", "wget"]

TEMPLATES = {
    "react": {"url": "https://cdn.hye.app/templates/react-vite.zip", "size": "2MB", "desc": "React 18 + Vite + Tailwind"},
    "react-ts": {"url": "https://cdn.hye.app/templates/react-ts.zip", "size": "2.5MB", "desc": "React + TS + Tailwind"},
    "vue": {"url": "https://cdn.hye.app/templates/vue-vite.zip", "size": "1.8MB", "desc": "Vue 3 + Vite"},
    "vanilla": {"url": "https://cdn.hye.app/templates/vanilla.zip", "size": "800KB", "desc": "HTML + JS + Tailwind"},
    "nextjs": {"url": "https://cdn.hye.app/templates/next-lite.zip", "size": "8MB", "desc": "Next.js 14 Lite"},
    "express": {"url": "https://cdn.hye.app/templates/express.zip", "size": "1MB", "desc": "Express API + CORS"},
    "astro": {"url": "https://cdn.hye.app/templates/astro.zip", "size": "5MB", "desc": "Astro + MDX Blog"},
    "svelte": {"url": "https://cdn.hye.app/templates/svelte.zip", "size": "1.5MB", "desc": "Svelte + Vite"},
    "solid": {"url": "https://cdn.hye.app/templates/solid.zip", "size": "1.2MB", "desc": "SolidJS + Vite"},
    "note-app": {"url": "https://cdn.hye.app/templates/note-app.zip", "size": "3MB", "desc": "OCR Note App Clone"},
}

# ===== MODELS =====
class AIRequest(BaseModel):
    prompt: str
    mode: str = "ask"
    code: str = ""
    user_id: str = ""

class OCRRequest(BaseModel):
    image: str

class PDFTableRequest(BaseModel):
    rows: list
    filename: str = "table.pdf"

class FixRequest(BaseModel):
    code: str

class GenerateRequest(BaseModel):
    prompt: str
    type: str = "template"

# ===== 1. ROOT + HEALTH =====
@app.get("/")
def root():
    return {
        "status": "HYE API Running",
        "version": "1.1.0",
        "gemini_ready": bool(model),
        "supabase_ready": bool(supabase)
    }

@app.get("/health")
def health():
    return {
        "status": "ok",
        "gemini_key_set": bool(GEMINI_API_KEY),
        "supabase_set": bool(SUPABASE_URL)
    }

# ===== 2. AI ENDPOINT - GEMINI 2.5 =====
@app.post("/ai")
async def ai_proxy(req: AIRequest):
    if not model:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY not set on server")

    if req.mode == "build":
        system = "You are Hyecode AI. Respond with files in this EXACT format:\n\nFILE: src/App.jsx\n```jsx\n// code here\n```\n\nRULES: Start every file with FILE: path/name.ext. Wrap code in ``` blocks. No explanations outside code blocks.\n"
        full_prompt = f"{system}\n\nUser request: {req.prompt}"
    elif req.mode == "fix":
        full_prompt = f"Fix all syntax errors and ESLint issues. Return only corrected code, no explanations:\n\n{req.code}"
    else:
        full_prompt = f"{req.prompt}\n\nCurrent code context:\n{req.code}"

    try:
        response = await asyncio.to_thread(
            model.generate_content,
            full_prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.2,
                max_output_tokens=2048,
            )
        )
        return {"response": response.text.strip()}
    except Exception as e:
        print(f"GEMINI ERROR: {repr(e)}")
        if "quota" in str(e).lower():
            raise HTTPException(status_code=429, detail="Gemini quota exceeded")
        if "api_key" in str(e).lower() or "401" in str(e):
            raise HTTPException(status_code=401, detail="Gemini Error: Invalid API key")
        raise HTTPException(status_code=500, detail=f"AI Error: {str(e)}")

@app.post("/generate")
async def generate_for_editor(req: GenerateRequest):
    mode_map = {"template": "build", "help": "ask", "ask": "ask"}
    ai_req = AIRequest(prompt=req.prompt, mode=mode_map.get(req.type, "ask"))
    result = await ai_proxy(ai_req)
    return {"code": result.get("response", ""), "result": result.get("response", ""), "explanation": f"Generated via {req.type} mode"}

@app.post("/fix")
async def fix_code(req: FixRequest):
    try:
        fixed = req.code
        fixed = re.sub(r'([^=!])==([^=])', r'\1=== \2', fixed)
        fixed = re.sub(r'([^=!])!=([^=])', r'\1!== \2', fixed)
        fixed = re.sub(r'console\.log\(\)', 'console.log("")', fixed)
        return {"fixed": fixed}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ===== 3. TERMINAL WEBSOCKET - SECURED =====
@app.websocket("/ws/terminal/{user_id}")
async def terminal_ws(websocket: WebSocket, user_id: str):
    await websocket.accept()
    work_dir = Path(f"/tmp/hye/{user_id}").resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    await websocket.send_text(f"🟢 HYE Terminal v1.1\n📁 Workspace: {work_dir}\nType 'help'\n\n")
    await websocket.send_text(f"hye@{user_id}:~$ ")

    try:
        while True:
            cmd = await websocket.receive_text()
            cmd = cmd.strip()

            if not cmd:
                await websocket.send_text(f"hye@{user_id}:~$ ")
                continue

            if any(bad in cmd.lower() for bad in BLOCKED_COMMANDS):
                await websocket.send_text(f"❌ Blocked: Command not allowed\n")
                await websocket.send_text(f"hye@{user_id}:~$ ")
                continue

            if cmd == "clear":
                await websocket.send_text("\033[2J\033[H")
                await websocket.send_text(f"hye@{user_id}:~$ ")
                continue

            if cmd == "help":
                await websocket.send_text(
                    "🟢 HYE Terminal Help\n\n"
                    "✅ ALLOWED:\n hye create react\n npm run dev\n npm install axios\n\n"
                    "❌ BLOCKED:\n npm create vite\n npm install next\n\n"
                )
                await websocket.send_text(f"hye@{user_id}:~$ ")
                continue

            if cmd == "hye create" or cmd == "hye create list":
                await websocket.send_text("Available templates:\n\n")
                for name, data in TEMPLATES.items():
                    await websocket.send_text(f" {name:<12} {data['size']:<8} - {data['desc']}\n")
                await websocket.send_text(f"\nUsage: hye create react\n")
                await websocket.send_text(f"hye@{user_id}:~$ ")
                continue

            if cmd.startswith("hye create "):
                template = cmd.split()[-1]
                if template not in TEMPLATES:
                    await websocket.send_text(f"❌ Template '{template}' not found.\n")
                    await websocket.send_text(f"hye@{user_id}:~$ ")
                    continue

                url = TEMPLATES[template]["url"]
                size = TEMPLATES[template]["size"]
                await websocket.send_text(f"📦 Downloading {template} template... {size}\n")

                try:
                    async with httpx.AsyncClient() as client:
                        r = await client.get(url, timeout=30.0)
                        r.raise_for_status()
                        zip_path = work_dir / f"{template}.zip"
                        zip_path.write_bytes(r.content)

                    proc = await asyncio.create_subprocess_exec(
                        "unzip", "-q", "-o", str(zip_path), "-d", str(work_dir),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                    )
                    stdout, stderr = await proc.communicate()
                    zip_path.unlink()

                    if proc.returncode == 0:
                        await websocket.send_text(f"✅ Created {template}/\n")
                        await websocket.send_text(f"✅ Next: cd {template} && npm run dev\n")
                    else:
                        await websocket.send_text(f"❌ Unzip failed: {stderr.decode()}\n")
                except Exception as e:
                    await websocket.send_text(f"❌ Download failed: {str(e)}\n")

                await websocket.send_text(f"hye@{user_id}:~$ ")
                continue

            if "npm create" in cmd or "create-react-app" in cmd:
                await websocket.send_text("❌ Blocked: Use 'hye create <template>' instead\n")
                await websocket.send_text(f"hye@{user_id}:~$ ")
                continue

            if cmd.startswith("npm install"):
                parts = cmd.split()
                if len(parts) == 2:
                    await websocket.send_text("❌ Blocked. Install one by one: npm install axios\n")
                    await websocket.send_text(f"hye@{user_id}:~$ ")
                    continue
                pkg = parts[2].replace("-D", "").replace("--save-dev", "").strip()
                if pkg in HEAVY_PACKAGES:
                    await websocket.send_text(f"❌ {pkg} is {HEAVY_PACKAGES[pkg]}MB. Too heavy.\n")
                    await websocket.send_text(f"hye@{user_id}:~$ ")
                    continue
                elif pkg not in LIGHT_PACKAGES:
                    await websocket.send_text(f"❌ {pkg} not whitelisted.\n")
                    await websocket.send_text(f"hye@{user_id}:~$ ")
                    continue

            try:
                safe_cmd = shlex.split(cmd)
                proc = await asyncio.create_subprocess_exec(
                    *safe_cmd,
                    cwd=str(work_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT
                )
                while True:
                    line = await proc.stdout.readline()
                    if not line: break
                    await websocket.send_text(line.decode(errors='ignore'))
                await proc.wait()
            except Exception as e:
                await websocket.send_text(f"❌ Command error: {str(e)}\n")

            await websocket.send_text(f"hye@{user_id}:~$ ")

    except WebSocketDisconnect:
        print(f"User {user_id} disconnected")
    except Exception as e:
        await websocket.send_text(f"❌ Terminal error: {str(e)}\n")
        await websocket.close()

# ===== 4. HELP ENDPOINTS =====
@app.get("/help/marketplace")
def help_marketplace():
    return {
        "title": "How HYE Works",
        "sections": [
            {"heading": "3 Apps, 1 Ecosystem", "content": "HyeCodeEditor + HyeMarketplace + HyeTerminal", "icon": "📱"},
            {"heading": "hye create Command", "content": "Use 'hye create react' instead of 'npm create vite'", "icon": "🚀"}
        ]
    }

@app.get("/help/terminal")
def help_terminal():
    return {
        "title": "HYE Terminal Commands",
        "allowed": [{"cmd": "hye create react", "desc": "Download template"}],
        "blocked": [{"cmd": "npm create vite", "reason": "Use 'hye create' instead"}]
    }

# ===== 5. TEMPLATES + EXTENSIONS =====
@app.get("/templates/list")
def list_templates():
    return {"templates": [{"name": name, "size": data["size"], "desc": data["desc"]} for name, data in TEMPLATES.items()]}

@app.get("/extensions")
async def get_extensions():
    if not supabase:
        return []
    try:
        res = supabase.table("extensions").select("*").eq("featured", True).execute()
        return res.data
    except Exception:
        return []

# ===== 6. HYE SDK =====
@app.post("/sdk/vision/ocr")
async def hye_ocr(req: OCRRequest, x_hye_api_key: str = Header(None)):
    raise HTTPException(501, "OCR disabled. Use Gemini Vision API directly or HYE Pro.")

@app.post("/sdk/pdf/table")
async def hye_pdf_table(req: PDFTableRequest, x_hye_api_key: str = Header(None)):
    try:
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Arial", size=10)
        col_width = 190 / len(req.rows[0]) if req.rows and req.rows[0] else 190
        for i, row in enumerate(req.rows):
            pdf.set_font("Arial", 'B' if i == 0 else '', 10)
            for item in row:
                pdf.cell(col_width, 10, str(item), border=1)
            pdf.ln()
        pdf_bytes = pdf.output(dest='S').encode('latin-1')
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={req.filename}"}
        )
    except Exception as e:
        raise HTTPException(400, f"PDF failed: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
