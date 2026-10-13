
import os
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="Tekden AI")

API_KEY = os.getenv("OLLAMA_API_KEY", "").strip()
MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:20b-cloud").strip()
API_URL = os.getenv(
    "OLLAMA_BASE_URL",
    "https://ollama.com/v1/chat/completions"
).strip()

PAGE = """
<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tekden AI</title>
<style>
* { box-sizing: border-box; }
body {
  margin: 0; background: #111318; color: #f4f5f7;
  font: 16px system-ui, sans-serif;
}
header {
  padding: 20px; border-bottom: 1px solid #303440;
  font-size: 21px; font-weight: bold;
}
main {
  max-width: 850px; margin: auto; padding: 24px;
}
#messages {
  min-height: 65vh; display: flex; flex-direction: column;
  gap: 16px; padding-bottom: 20px;
}
.message {
  white-space: pre-wrap; overflow-wrap: anywhere;
  padding: 14px 17px; border-radius: 14px;
  line-height: 1.6;
}
.user { background: #29283c; align-self: flex-end; max-width: 90%; }
.assistant { background: #1c2029; align-self: flex-start; max-width: 100%; }
form { display: flex; gap: 10px; }
textarea {
  flex: 1; resize: vertical; min-height: 52px; max-height: 180px;
  background: #1c2029; color: white; border: 1px solid #393e4b;
  border-radius: 12px; padding: 14px; font: inherit;
}
button {
  background: #9184ff; color: #111318; border: 0;
  border-radius: 12px; padding: 12px 18px;
  font-weight: bold; cursor: pointer;
}
button:disabled { opacity: .5; cursor: wait; }
#status { color: #a4a9b6; font-size: 13px; padding: 10px 0; }
#new { background: #292d39; color: white; margin-bottom: 16px; }
</style>
</head>
<body>
<header>✦ Tekden AI</header>
<main>
<button id="new" type="button">＋ Yeni sohbet</button>
<div id="messages">
  <div class="message assistant">
    Merhaba! Ben Tekden AI. Bugün sana nasıl yardımcı olabilirim?
  </div>
</div>
<div id="status"></div>
<form id="form">
  <textarea id="input" placeholder="Mesajını yaz..."
    required></textarea>
  <button id="send" type="submit">Gönder</button>
</form>
</main>
<script>
const messagesEl = document.getElementById("messages");
const input = document.getElementById("input");
const form = document.getElementById("form");
const send = document.getElementById("send");
const statusEl = document.getElementById("status");
let history = [];

function addMessage(role, content) {
  const el = document.createElement("div");
  el.className = "message " + role;
  el.textContent = content;
  messagesEl.appendChild(el);
  el.scrollIntoView({ behavior: "smooth", block: "end" });
  return el;
}

document.getElementById("new").onclick = () => {
  history = [];
  messagesEl.replaceChildren();
  addMessage("assistant",
    "Yeni sohbet başladı! Sana nasıl yardımcı olabilirim?");
  statusEl.textContent = "";
  input.focus();
};

input.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

form.addEventListener("submit", async event => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text || send.disabled) return;

  addMessage("user", text);
  history.push({ role: "user", content: text });
  input.value = "";
  send.disabled = true;
  statusEl.textContent = "Yanıt hazırlanıyor...";
  const answer = addMessage("assistant", "");

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: history })
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "İstek başarısız oldu.");
    }
    answer.textContent = data.reply;
    history.push({ role: "assistant", content: data.reply });
    statusEl.textContent = "";
  } catch (error) {
    answer.textContent = "Hata: " + error.message;
    history.pop();
    statusEl.textContent = "Ayarları ve bağlantıyı kontrol et.";
  } finally {
    send.disabled = false;
    input.focus();
  }
});
</script>
</body>
</html>
"""

class ChatRequest(BaseModel):
    messages: list[dict]

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(PAGE)

@app.get("/health")
def health():
    return {"ok": True, "app": "Tekden AI"}

@app.post("/api/chat")
def chat(body: ChatRequest):
    if not API_KEY:
        raise HTTPException(
            status_code=503,
            detail="OLLAMA_API_KEY tanımlanmamış."
        )

    messages = []
    for item in body.messages[-30:]:
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and isinstance(content, str):
            messages.append({
                "role": role,
                "content": content[:12000]
            })

    try:
        response = requests.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": MODEL,
                "messages": messages,
                "stream": False
            },
            timeout=120
        )
        if response.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Ollama API hatası: HTTP {response.status_code}"
            )

        data = response.json()
        reply = data.get("choices", [{}])[0].get(
            "message", {}
        ).get("content", "")

        if not reply:
            raise HTTPException(
                status_code=502,
                detail="Model boş yanıt döndürdü."
            )

        return {"reply": reply}

    except requests.Timeout:
        raise HTTPException(
            status_code=504,
            detail="Yanıt zaman aşımına uğradı."
        )
    except requests.RequestException:
        raise HTTPException(
            status_code=502,
            detail="Ollama servisine bağlanılamadı."
        )
