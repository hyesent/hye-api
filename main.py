import os
import asyncio
import base64
import io
import json
import re
import subprocess
import shlex
import zipfile
from typing import Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from supabase import create_client, Client
from fpdf import FPDF
import google.generativeai as genai

# ===== ENV VARS - SET THESE IN RENDER =====
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") # service_role key

# ===== INIT =====
app = FastAPI(title="HYE Ecosystem API", version="1.0.2")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

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

BLOCKED_COMMANDS = ["docker", "sudo", "apt-get", "pip install", "yarn add", "pnpm add"]

# ===== MODELS =====
class AIRequest(BaseModel):
    prompt: str
    mode: str = "ask" # ask, build, fix
    code: str = ""
    user_id: str = ""

class PDFTableRequest(BaseModel):
    rows: list
    filename: str = "table.pdf"

class FixRequest(BaseModel):
    code: str

class ExecRequest(BaseModel):
    cmd: str
    cwd: str = ""
    projectName: str = ""

class GenerateRequest(BaseModel):
    prompt: str
    type: str = "template" # template, help, ask

class HyeCreateRequest(BaseModel):
    type: str # react, vue, vanilla, express
    name: str # testapp

# ===== 1. ROOT + HEALTH =====
@app.get("/")
def root():
    return {
        "status": "HYE API Running",
        "version": "1.0.2",
        "endpoints": [
            "/ai", "/fix", "/exec", "/hye-create", "/ws/terminal/{user_id}",
            "/help/marketplace", "/help/terminal", "/templates/list",
            "/extensions", "/sdk/pdf/table"
        ]
    }

@app.get("/health")
def health():
    return {
        "status": "ok",
        "gemini_key_set": bool(os.getenv("GEMINI_API_KEY")),
        "supabase_set": bool(SUPABASE_URL)
    }

# ===== 2. AI ENDPOINT - GEMINI 2.5 =====
@app.post("/ai")
async def ai_proxy(req: AIRequest):
    if not model:
        return {"response": "Error: GEMINI_API_KEY not set on server"}

    if req.mode == "build":
        system = "You are Hyecode AI. Respond with files in this EXACT format:\n\nFILE: src/App.jsx\n```jsx\n// code here\n```\n\nRULES: Start every file with FILE: path/name.ext. Wrap code in ``` blocks.\n"
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
                max_output_tokens=1024,
            )
        )
        return {"response": response.text.strip()}

    except Exception as e:
        print(f"HYE AI ERROR: {repr(e)}")
        if "quota" in str(e).lower():
            return {"response": "Model quota exceeded. Try again later"}
        if "api_key" in str(e).lower() or "401" in str(e):
            return {"response": "Gemini Error: Invalid API key"}
        return {"response": f"AI Error: {str(e)}"}

# ===== 2.6. GENERATE ENDPOINT - FOR HYE EDITOR =====
@app.post("/generate")
async def generate_for_editor(req: GenerateRequest):
    mode_map = {
        "template": "build",
        "help": "ask",
        "ask": "ask"
    }

    ai_req = AIRequest(
        prompt=req.prompt,
        mode=mode_map.get(req.type, "ask"),
        code="",
        user_id=""
    )

    result = await ai_proxy(ai_req)

    return {
        "code": result.get("response", ""),
        "result": result.get("response", ""),
        "explanation": f"Generated via {req.type} mode"
    }

# ===== 2.5. FIX ENDPOINT - ADDED FOR ONLINE FIX =====
@app.post("/fix")
async def fix_code(req: FixRequest):
    try:
        fixed = req.code
        # Basic server-side fixes without AI
        fixed = re.sub(r'([^=!])==([^=])', r'\1=== \2', fixed)
        fixed = re.sub(r'([^=!])!=([^=])', r'\1!== \2', fixed)
        fixed = re.sub(r'console\.log\(\)', 'console.log("")', fixed)
        return {"fixed": fixed}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ===== 2.7. EXEC ENDPOINT - FOR HYE TERMINAL ANDROID =====
ALLOWED_CMDS = {"npm", "npx", "node", "git", "ls", "pwd", "echo", "mkdir", "cat"}
WORKSPACE = "/tmp/hye-projects"
os.makedirs(WORKSPACE, exist_ok=True)

@app.post("/exec")
async def exec_command(req: ExecRequest):
    if not req.cmd.strip():
        raise HTTPException(400, "cmd required")

    # Security: whitelist base command
    base_cmd = shlex.split(req.cmd)[0]
    if base_cmd not in ALLOWED_CMDS:
        return {
            "stdout": "",
            "stderr": f"Command '{base_cmd}' not allowed",
            "code": 1
        }

    # Build working directory
    work_dir = WORKSPACE
    if req.projectName:
        work_dir = os.path.join(WORKSPACE, req.projectName)
        os.makedirs(work_dir, exist_ok=True)

    if req.cwd:
        work_dir = os.path.join(work_dir, req.cwd.lstrip('/'))
        os.makedirs(work_dir, exist_ok=True)

    try:
        # Run with 5min timeout, 10MB output limit
        result = subprocess.run(
            req.cmd,
            shell=True,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, "CI": "true"} # npm less interactive
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "code": result.returncode,
            "cwd": work_dir
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": "Command timed out after 5 minutes",
            "code": 124
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": f"Exec failed: {str(e)}",
            "code": 1
        }

# ===== 2.8. HYE-CREATE ENDPOINT - BUILDS TEMPLATES ON THE FLY =====
@app.post("/hye-create")
async def hye_create(req: HyeCreateRequest):
    template = req.type
    project_name = req.name

    if not project_name.replace('-', '').replace('_', '').isalnum():
        raise HTTPException(400, "Project name must be alphanumeric")

    new_zip_buffer = io.BytesIO()

    try:
        with zipfile.ZipFile(new_zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:

            # Shared.gitignore
            gitignore = "node_modules\ndist\n.env\n"
            zip_file.writestr(f"{project_name}/.gitignore", gitignore)

            if template == "react":
                # React + Vite
                zip_file.writestr(f"{project_name}/package.json", json.dumps({
                    "name": project_name,
                    "private": True,
                    "version": "0.0.0",
                    "type": "module",
                    "scripts": {
                        "dev": "vite",
                        "build": "vite build",
                        "preview": "vite preview"
                    },
                    "dependencies": {
                        "react": "^18.3.1",
                        "react-dom": "^18.3.1"
                    },
                    "devDependencies": {
                        "@vitejs/plugin-react": "^4.3.2",
                        "vite": "^5.4.8"
                    }
                }, indent=2))

                zip_file.writestr(f"{project_name}/index.html", '''<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>HYE App</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>''')

                zip_file.writestr(f"{project_name}/vite.config.js", '''import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
})''')

                zip_file.writestr(f"{project_name}/src/main.jsx", '''import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)''')

                zip_file.writestr(f"{project_name}/src/App.jsx", '''function App() {
  return (
    <div style={{padding: 20, fontFamily: 'sans-serif'}}>
      <h1>Welcome to HYE 🚀</h1>
      <p>Edit <code>src/App.jsx</code> and save to reload.</p>
    </div>
  )
}

export default App''')

            elif template == "vue":
                # Vue 3 + Vite
                zip_file.writestr(f"{project_name}/package.json", json.dumps({
                    "name": project_name,
                    "private": True,
                    "version": "0.0.0",
                    "type": "module",
                    "scripts": {
                        "dev": "vite",
                        "build": "vite build",
                        "preview": "vite preview"
                    },
                    "dependencies": {
                        "vue": "^3.4.37"
                    },
                    "devDependencies": {
                        "@vitejs/plugin-vue": "^5.1.2",
                        "vite": "^5.4.8"
                    }
                }, indent=2))

                zip_file.writestr(f"{project_name}/index.html", '''<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>HYE Vue App</title>
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="/src/main.js"></script>
  </body>
</html>''')

                zip_file.writestr(f"{project_name}/vite.config.js", '''import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
})''')

                zip_file.writestr(f"{project_name}/src/main.js", '''import { createApp } from 'vue'
import App from './App.vue'

createApp(App).mount('#app')''')

                zip_file.writestr(f"{project_name}/src/App.vue", '''<template>
  <div style="padding: 20px; font-family: sans-serif;">
    <h1>Welcome to HYE Vue 🚀</h1>
    <p>Edit <code>src/App.vue</code> and save to reload.</p>
  </div>
</template>''')

            elif template == "vanilla":
                # Vanilla JS + Vite
                zip_file.writestr(f"{project_name}/package.json", json.dumps({
                    "name": project_name,
                    "private": True,
                    "version": "0.0.0",
                    "type": "module",
                    "scripts": {
                        "dev": "vite",
                        "build": "vite build",
                        "preview": "vite preview"
                    },
                    "devDependencies": {
                        "vite": "^5.4.8"
                    }
                }, indent=2))

                zip_file.writestr(f"{project_name}/index.html", '''<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>HYE Vanilla App</title>
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="/main.js"></script>
  </body>
</html>''')

                zip_file.writestr(f"{project_name}/main.js", '''document.querySelector('#app').innerHTML = `
  <div style="padding: 20px; font-family: sans-serif;">
    <h1>Welcome to HYE Vanilla 🚀</h1>
    <p>Edit <code>main.js</code> and save to reload.</p>
  </div>
`''')

                zip_file.writestr(f"{project_name}/vite.config.js", '''import { defineConfig } from 'vite'

export default defineConfig({})''')

            elif template == "express":
                # Express API
                zip_file.writestr(f"{project_name}/package.json", json.dumps({
                    "name": project_name,
                    "version": "1.0.0",
                    "main": "index.js",
                    "scripts": {
                        "start": "node index.js",
                        "dev": "node index.js"
                    },
                    "dependencies": {
                        "express": "^4.19.2",
                        "cors": "^2.8.5"
                    }
                }, indent=2))

                zip_file.writestr(f"{project_name}/index.js", '''const express = require('express');
const cors = require('cors');
const app = express();
const port = process.env.PORT || 3000;

app.use(cors());
app.use(express.json());

app.get('/', (req, res) => {
  res.json({ message: 'Welcome to HYE Express API 🚀' });
});

app.listen(port, () => {
  console.log(`Server running on port ${port}`);
});''')

            else:
                raise HTTPException(404, f"Template '{template}' not supported. Try: react, vue, vanilla, express")

        # Convert to raw base64
        new_zip_buffer.seek(0)
        zip_base64 = base64.b64encode(new_zip_buffer.read()).decode('utf-8')

        return {
            "success": True,
            "filename": f"{project_name}.zip",
            "data": zip_base64,
            "path": f"/HYE-Projects/{project_name}"
        }

    except Exception as e:
        raise HTTPException(500, f"Create failed: {str(e)}")

# ===== 3. TERMINAL WEBSOCKET =====
@app.websocket("/ws/terminal/{user_id}")
async def terminal_ws(websocket: WebSocket, user_id: str):
    await websocket.accept()
    work_dir = f"/tmp/hye/{user_id}"
    os.makedirs(work_dir, exist_ok=True)

    await websocket.send_text(f"🟢 HYE Terminal v1.0\n📁 Workspace: {work_dir}\nType 'help' or 'hye create'\n\n")
    await websocket.send_text(f"hye@{user_id}:~$ ")

    try:
        while True:
            cmd = await websocket.receive_text()
            cmd = cmd.strip()

            if cmd == "clear":
                await websocket.send_text("\033[2J\033[H")
                await websocket.send_text(f"hye@{user_id}:~$ ")
                continue

            if cmd == "help":
                await websocket.send_text(
                    "🟢 HYE Terminal Help\n\n"
                    "✅ ALLOWED:\n"
                    " hye create react - Download template\n"
                    " npm run dev - Start server\n"
                    " npm install axios - Install light packages\n"
                    " git add. && git commit -m 'msg'\n\n"
                    "❌ BLOCKED:\n"
                    " npm create vite - Use 'hye create' instead\n"
                    " npm install - Install one by one\n"
                    " npm install next - Too heavy. Use template\n\n"
                    "💡 HYE Terminal = fast commands only\n"
                )
                await websocket.send_text(f"hye@{user_id}:~$ ")
                continue

            if cmd == "hye create" or cmd == "hye create list":
                await websocket.send_text("Available templates:\n\n")
                await websocket.send_text(" react 2KB - React 18 + Vite\n")
                await websocket.send_text(" vue 2KB - Vue 3 + Vite\n")
                await websocket.send_text(" vanilla 1KB - HTML + JS + Vite\n")
                await websocket.send_text(" express 1KB - Express API + CORS\n")
                await websocket.send_text(f"\nUsage: hye create react myapp\n")
                await websocket.send_text(f"hye@{user_id}:~$ ")
                continue

            if cmd.startswith("hye create "):
                await websocket.send_text(f"❌ Use the HYE Terminal app for 'hye create'. WebSocket only supports basic commands.\n")
                await websocket.send_text(f"hye@{user_id}:~$ ")
                continue

            if "npm create" in cmd or "npx create" in cmd or "create-react-app" in cmd or "yarn create" in cmd:
                await websocket.send_text("❌ Blocked: Use 'hye create <template>' instead\n")
                await websocket.send_text("💡 Run 'hye create' to see templates\n")
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
                    size = HEAVY_PACKAGES[pkg]
                    await websocket.send_text(f"❌ {pkg} is {size}MB. Too heavy for mobile.\n")
                    await websocket.send_text("💡 Use HYE SDK: import { Hye } from '@hye/sdk'\n")
                    await websocket.send_text(f"hye@{user_id}:~$ ")
                    continue
                elif pkg not in LIGHT_PACKAGES:
                    await websocket.send_text(f"❌ {pkg} not whitelisted.\n")
                    await websocket.send_text(f"✅ Allowed: {', '.join(list(LIGHT_PACKAGES)[:6])}...\n")
                    await websocket.send_text(f"hye@{user_id}:~$ ")
                    continue
                else:
                    await websocket.send_text(f"📦 Installing {pkg}...\n")

            if any(bad in cmd for bad in BLOCKED_COMMANDS):
                await websocket.send_text(f"❌ Blocked: '{cmd}' not allowed on free tier\n")
                await websocket.send_text(f"💡 Type 'help' to see allowed commands\n")
                await websocket.send_text(f"hye@{user_id}:~$ ")
                continue

            # Run safe command
            proc = await asyncio.create_subprocess_shell(
                f"cd {work_dir} && {cmd}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            while True:
                line = await proc.stdout.readline()
                if not line: break
                await websocket.send_text(line.decode())
            await proc.wait()
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
            {
                "heading": "3 Apps, 1 Ecosystem",
                "content": "HYE splits heavy work across 3 apps:\n• HyeCodeEditor - write code\n• HyeMarketplace - install tools\n• HyeTerminal - run npm commands\n\nThis keeps each app under 10MB and fast.",
                "icon": "📱"
            },
            {
                "heading": "Why Not One App?",
                "content": "VS Code is 500MB. Phones crash. HYE gives you VS Code power in 3 light APKs that talk to each other using deep links.",
                "icon": "⚡"
            },
            {
                "heading": "hye create Command",
                "content": "Don't use 'npm create vite'. Use 'hye create react' in HYE Terminal.\n\nIt downloads pre-built templates with node_modules already installed. Saves 5 minutes + 200MB RAM.",
                "icon": "🚀"
            }
        ]
    }

@app.get("/help/terminal")
def help_terminal():
    return {
        "title": "HYE Terminal Commands",
        "allowed": [
            {"cmd": "hye create react", "desc": "Download template"},
            {"cmd": "npm run dev", "desc": "Start your app"},
            {"cmd": "npm install axios", "desc": "Install light packages"},
            {"cmd": "git add. && git commit -m 'msg'", "desc": "Save code"},
            {"cmd": "ls", "desc": "List files"}
        ],
        "blocked": [
            {"cmd": "npm create vite", "reason": "Use 'hye create' instead"},
            {"cmd": "npm install", "reason": "Install one by one"},
            {"cmd": "npm install next", "reason": "Too heavy. 400MB"}
        ],
        "tip": "HYE Terminal = fast commands. For heavy builds use HYE Cloud"
    }

# ===== 5. TEMPLATES + EXTENSIONS =====
@app.get("/templates/list")
def list_templates():
    return {
        "templates": [
            {"name": "react", "size": "2KB", "desc": "React 18 + Vite"},
            {"name": "vue", "size": "2KB", "desc": "Vue 3 + Vite"},
            {"name": "vanilla", "size": "1KB", "desc": "HTML + JS + Vite"},
            {"name": "express", "size": "1KB", "desc": "Express API + CORS"}
        ]
    }

@app.get("/extensions")
async def get_extensions():
    if not supabase:
        return []
    res = supabase.table("extensions").select("*").eq("featured", True).execute()
    return res.data

@app.post("/extensions/install")
async def install_extension(ext_id: int, user_id: str):
    if not supabase:
        return {"status": "installed", "deep_link": f"hye://editor?install={ext_id}"}
    supabase.table("user_extensions").insert({
        "user_id": user_id,
        "extension_id": ext_id
    }).execute()
    return {"status": "installed", "deep_link": f"hye://editor?install={ext_id}"}

# ===== 6. HYE SDK ENDPOINTS =====
@app.post("/sdk/pdf/table")
async def hye_pdf_table(req: PDFTableRequest, x_hye_api_key: str = Header(None)):
    # TODO: Add rate limit check
    try:
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Arial", size=10)
        col_width = 190 / len(req.rows[0]) if req.rows else 190
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
