from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import os
import httpx
from supabase import create_client, Client

app = FastAPI(title="HYE API", version="1.0.0")

# Allow HyeCodeEditor + HyeTerminal to connect
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Supabase client - for future: save projects, user data
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

class AIRequest(BaseModel):
    prompt: str
    mode: str = "ask" # ask, build, fix
    code: str = ""
    user_id: str = ""

@app.get("/")
def root():
    return {
        "status": "HYE API Running",
        "version": "1.0.0",
        "endpoints": ["/ai", "/ws/terminal/{user_id}", "/help/marketplace", "/templates/list"]
    }

@app.post("/ai")
async def ai_proxy(req: AIRequest):
    """HuggingFace Inference API proxy - keeps HF_TOKEN secret"""
    HF_TOKEN = os.getenv("HF_TOKEN")

    if not HF_TOKEN:
        return {"response": "Error: HF_TOKEN not set on server"}

    # Qwen2.5-Coder-7B-Instruct - best for React/JS code
    API_URL = "https://api-inference.huggingface.co/models/Qwen/Qwen2.5-Coder-7B-Instruct"

    # Build system prompt based on mode
    if req.mode == "build":
        system = "You are Hyecode AI. Respond with files in this EXACT format:\n\nFILE: src/App.jsx\n```jsx\n// code here\n```\n\nRULES: Start every file with FILE: path/name.ext. Wrap code in ``` blocks.\n"
        full_prompt = f"{system}\n\nUser request: {req.prompt}"
    elif req.mode == "fix":
        full_prompt = f"Fix all syntax errors and ESLint issues. Return only corrected code, no explanations:\n\n{req.code}"
    else: # ask mode
        full_prompt = f"{req.prompt}\n\nCurrent code context:\n{req.code}"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                API_URL,
                headers={"Authorization": f"Bearer {HF_TOKEN}"},
                json={
                    "inputs": full_prompt,
                    "parameters": {
                        "max_new_tokens": 3000,
                        "temperature": 0.7,
                        "return_full_text": False,
                        "do_sample": True
                    }
                }
            )

            if r.status_code == 503:
                return {"response": "Model is loading on HF servers. Try again in 20 seconds."}

            if r.status_code!= 200:
                return {"response": f"HF API Error {r.status_code}: {r.text}"}

            data = r.json()

            # HF returns array with generated_text
            if isinstance(data, list) and len(data) > 0:
                generated = data[0].get("generated_text", "")
                return {"response": generated.strip()}
            else:
                return {"response": str(data)}

    except Exception as e:
        return {"response": f"AI Error: {str(e)}"}

@app.websocket("/ws/terminal/{user_id}")
async def terminal_ws(websocket: WebSocket, user_id: str):
    await websocket.accept()
    work_dir = f"/tmp/hye/{user_id}"
    os.makedirs(work_dir, exist_ok=True)

    await websocket.send_text(f"hye@{user_id}:~$ ")

    try:
        while True:
            cmd = await websocket.receive_text()
            cmd = cmd.strip()

            if cmd == "clear":
                await websocket.send_text("\033[2J\033[H")
                await websocket.send_text(f"hye@{user_id}:~$ ")
                continue

            if cmd.startswith("hye create"):
                parts = cmd.split()
                template = parts[-1] if len(parts) > 2 else "react"
                await websocket.send_text(f"📦 Downloading {template} template...\n")

                # Download + unzip template
                proc = await asyncio.create_subprocess_shell(
                    f"cd {work_dir} && wget -q https://cdn.hye.app/templates/{template}.zip && unzip -q -o {template}.zip",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await proc.communicate()

                if proc.returncode == 0:
                    await websocket.send_text(f"✅ Created {template}/\n")
                    await websocket.send_text(f"✅ Run: cd {template} && npm run dev\n")
                else:
                    err = stderr.decode() or "Download failed"
                    await websocket.send_text(f"❌ Failed: {err}\n")

                await websocket.send_text(f"hye@{user_id}:~$ ")
                continue

            if "npm create" in cmd or "npx create" in cmd or "create-react-app" in cmd:
                await websocket.send_text("❌ Blocked: Use 'hye create react' instead\n")
                await websocket.send_text(f"hye@{user_id}:~$ ")
                continue

            # Block heavy installs
            blocked = ["next", "nuxt", "remix", "angular", "@angular", "three", "electron", "nestjs"]
            if "npm install" in cmd and any(b in cmd for b in blocked):
                await websocket.send_text(f"❌ Too heavy for mobile free tier. Use HYE SDK instead.\n")
                await websocket.send_text(f"hye@{user_id}:~$ ")
                continue

            # Run real command
            proc = await asyncio.create_subprocess_shell(
                cmd, cwd=work_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            output = (stdout or stderr).decode()
            if output:
                await websocket.send_text(output)
            await websocket.send_text(f"hye@{user_id}:~$ ")

    except WebSocketDisconnect:
        print(f"User {user_id} disconnected")
    except Exception as e:
        await websocket.send_text(f"Terminal error: {str(e)}\n")
        await websocket.close()

@app.get("/help/marketplace")
def help_content():
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

@app.get("/templates/list")
def list_templates():
    return {
        "templates": [
            {"name": "react", "size": "2.1MB", "desc": "Vite + React 18 + Tailwind"},
            {"name": "vue", "size": "1.8MB", "desc": "Vite + Vue 3 + Pinia"},
            {"name": "express", "size": "1.2MB", "desc": "Express API + CORS"},
            {"name": "vanilla", "size": "0.5MB", "desc": "HTML + CSS + JS"},
        ]
    }

@app.get("/health")
def health():
    return {
        "status": "ok",
        "hf_token_set": bool(os.getenv("HF_TOKEN")),
        "supabase_set": bool(SUPABASE_URL)
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)