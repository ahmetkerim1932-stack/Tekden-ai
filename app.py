import os, re, json, time, hmac, hashlib, secrets, base64
from datetime import datetime, timezone
from urllib.parse import quote
import requests
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

APP_NAME = "Tekden AI"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "").strip()
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:20b-cloud").strip()
POLLINATIONS_API_KEY = os.getenv("POLLINATIONS_API_KEY", "").strip()
POLLINATIONS_IMAGE_MODEL = os.getenv("POLLINATIONS_IMAGE_MODEL", "flux").strip()
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()
COOKIE_NAME = "tekden_session"
SESSION_DAYS = 14

app = FastAPI(title=APP_NAME)

def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL eksik. Supabase PostgreSQL bağlantısını Render Environment bölümüne ekle.")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id UUID PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL DEFAULT 'Yeni sohbet',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id BIGSERIAL PRIMARY KEY,
            chat_id UUID NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('user','assistant')),
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chats_user_updated ON chats(user_id, updated_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id, id)")

@app.on_event("startup")
def startup():
    if SESSION_SECRET and len(SESSION_SECRET) >= 32 and DATABASE_URL:
        init_db()
    else:
        # App can show setup errors rather than crashing during import/startup.
        pass

def b64e(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")

def b64d(s: str) -> str:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()

def make_session(user_id: int) -> str:
    payload = f"{user_id}:{int(time.time()) + SESSION_DAYS*86400}:{secrets.token_hex(8)}"
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return b64e(payload + ":" + sig)

def read_session(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if not token or not SESSION_SECRET or len(SESSION_SECRET) < 32:
        return None
    try:
        raw = b64d(token)
        user_id, expires, nonce, sig = raw.split(":", 3)
        payload = f"{user_id}:{expires}:{nonce}"
        expected = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected) or int(expires) < int(time.time()):
            return None
        return int(user_id)
    except Exception:
        return None

def hash_password(password: str, salt: bytes = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310000)
    return base64.urlsafe_b64encode(salt + digest).decode()

def verify_password(password: str, stored: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(stored.encode())
        salt, digest = raw[:16], raw[16:]
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310000)
        return hmac.compare_digest(candidate, digest)
    except Exception:
        return False

def current_user(request: Request):
    uid = read_session(request)
    if not uid:
        return None
    with db() as conn:
        return conn.execute("SELECT id, username FROM users WHERE id=%s", (uid,)).fetchone()

def web_search(query: str):
    if TAVILY_API_KEY:
        try:
            r = requests.post("https://api.tavily.com/search",
                json={"api_key": TAVILY_API_KEY, "query": query, "search_depth": "basic", "max_results": 5},
                timeout=20)
            r.raise_for_status()
            data = r.json()
            return [{"title": x.get("title",""), "url": x.get("url",""), "snippet": x.get("content","")} for x in data.get("results", [])]
        except Exception:
            pass
    # DuckDuckGo fallback, no API key required; availability can vary.
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
        return [{"title": x.get("title",""), "url": x.get("href",""), "snippet": x.get("body","")} for x in results]
    except Exception:
        return []

def should_search(text: str) -> bool:
    t = text.lower()
    cues = ["internette ara", "web'de ara", "webde ara", "güncel", "bugün", "son dakika",
            "en son", "kaynak göster", "kaynakları", "araştır", "2026", "şu an", "fiyatı ne",
            "latest", "current", "today", "search the web", "look up"]
    return any(x in t for x in cues) or t.strip().startswith("/search ")

def call_ollama(messages):
    if not OLLAMA_API_KEY:
        raise RuntimeError("OLLAMA_API_KEY eksik. Render Environment bölümüne Ollama Cloud API anahtarını ekle.")
    r = requests.post("https://ollama.com/api/chat",
        headers={"Authorization": f"Bearer {OLLAMA_API_KEY}", "Content-Type": "application/json"},
        json={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
        timeout=120)
    if not r.ok:
        raise RuntimeError(f"Ollama API hatası ({r.status_code}): {r.text[:500]}")
    data = r.json()
    return data.get("message", {}).get("content") or data.get("response") or "Yanıt alınamadı."

class AuthBody(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)

class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    chat_id: str | None = None
    guest_history: list[dict] = Field(default_factory=list)
    force_search: bool = False

class ImageBody(BaseModel):
    prompt: str = Field(min_length=2, max_length=2000)

@app.get("/health")
def health():
    return {"ok": True, "app": APP_NAME, "database_configured": bool(DATABASE_URL),
            "ollama_configured": bool(OLLAMA_API_KEY), "image_configured": bool(POLLINATIONS_API_KEY)}

@app.post("/api/register")
def register(body: AuthBody, response: JSONResponse):
    username = body.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise HTTPException(400, "Kullanıcı adı 3-32 karakter olmalı; harf, sayı, nokta, tire ve alt çizgi kullan.")
    if not SESSION_SECRET or len(SESSION_SECRET) < 32:
        raise HTTPException(500, "SESSION_SECRET eksik veya çok kısa.")
    try:
        with db() as conn:
            row = conn.execute("INSERT INTO users(username,password_hash) VALUES(%s,%s) RETURNING id, username",
                               (username, hash_password(body.password))).fetchone()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "Bu kullanıcı adı zaten alınmış.")
    token = make_session(row["id"])
    out = JSONResponse({"ok": True, "user": {"id": row["id"], "username": row["username"]}})
    out.set_cookie(COOKIE_NAME, token, httponly=True, secure=True, samesite="lax", max_age=SESSION_DAYS*86400)
    return out

@app.post("/api/login")
def login(body: AuthBody):
    with db() as conn:
        row = conn.execute("SELECT id, username, password_hash FROM users WHERE lower(username)=lower(%s)", (body.username.strip(),)).fetchone()
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(401, "Kullanıcı adı veya şifre hatalı.")
    out = JSONResponse({"ok": True, "user": {"id": row["id"], "username": row["username"]}})
    out.set_cookie(COOKIE_NAME, make_session(row["id"]), httponly=True, secure=True, samesite="lax", max_age=SESSION_DAYS*86400)
    return out

@app.post("/api/logout")
def logout():
    out = JSONResponse({"ok": True})
    out.delete_cookie(COOKIE_NAME, secure=True, httponly=True, samesite="lax")
    return out

@app.get("/api/me")
def me(request: Request):
    try:
        user = current_user(request)
    except Exception:
        user = None
    return {"user": user}

@app.get("/api/chats")
def list_chats(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Sohbet geçmişi için giriş yap.")
    with db() as conn:
        rows = conn.execute("SELECT id, title, created_at, updated_at FROM chats WHERE user_id=%s ORDER BY updated_at DESC LIMIT 100", (user["id"],)).fetchall()
    return {"chats": rows}

@app.get("/api/chats/{chat_id}")
def get_chat(chat_id: str, request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Giriş yap.")
    with db() as conn:
        chat = conn.execute("SELECT id, title FROM chats WHERE id=%s AND user_id=%s", (chat_id, user["id"])).fetchone()
        if not chat:
            raise HTTPException(404, "Sohbet bulunamadı.")
        messages = conn.execute("SELECT role, content FROM messages WHERE chat_id=%s ORDER BY id", (chat_id,)).fetchall()
    return {"chat": chat, "messages": messages}

@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str, request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Giriş yap.")
    with db() as conn:
        result = conn.execute("DELETE FROM chats WHERE id=%s AND user_id=%s", (chat_id, user["id"]))
        if result.rowcount == 0:
            raise HTTPException(404, "Sohbet bulunamadı.")
    return {"ok": True}

@app.post("/api/chat")
def chat(body: ChatBody, request: Request):
    user = current_user(request)
    user_message = body.message.strip()
    use_search = body.force_search or should_search(user_message)
    sources = web_search(user_message.replace("/search ", "", 1)) if use_search else []
    system = """Sen Tekden AI adlı yapay zekâ asistanısın. Kimliğin ve ürün bilgilerin:
- Tekden AI uygulaması Ahmet Kerim Kırgız tarafından tasarlanmıştır.
- Tekden AI'ın oluşturulma tarihi sorulursa 11 Ekim 2026 olarak belirt.
- “Ahmet Kerim kim?” diye sorulursa şu açıklamayı ver: “Ahmet Kerim Kırgız, 15 Temmuz Şehitler Anadolu İmam Hatip Lisesinde okuyan ve Tekden AI programını tasarlayan genç bir teknoloji meraklısıdır.”
- Kendini OpenAI tarafından oluşturulmuş bir ürün olarak tanıtma. Sen Tekden AI uygulamasının asistanısın. Uygulamanın geliştiricisi Ahmet Kerim Kırgız'dır.
- Bununla birlikte, altyapı/model sağlayıcısı doğrudan sorulursa yanlış bilgi uydurma; Tekden AI'ın bir uygulama olduğu ve yanıtlarının yapılandırılmış harici yapay zekâ servisleri tarafından üretilebildiği konusunda dürüst ol. Tekden AI uygulamasının geliştiricisi ile kullanılan model/servis sağlayıcısını birbirine karıştırma.
Türkçe dahil kullanıcının dilinde açık, doğru ve güvenli yanıt ver. Web kaynakları sağlandıysa yanıtını bu kaynaklara dayandır, kaynakların desteklemediği iddiaları kesin sunma ve yanıt sonunda kaynak URL'lerini belirt."""
    msgs = [{"role":"system","content":system}]
    if sources:
        source_text = "\n\n".join(f"[{i+1}] {x['title']}\nURL: {x['url']}\nÖzet: {x['snippet']}" for i,x in enumerate(sources))
        msgs.append({"role":"system","content":"Aşağıdaki internet arama sonuçlarını kullan. Kaynak numaralarını ve URL'leri yanıtta göster:\n"+source_text})
    if user:
        chat_id = body.chat_id
        with db() as conn:
            if chat_id:
                row = conn.execute("SELECT id FROM chats WHERE id=%s AND user_id=%s", (chat_id, user["id"])).fetchone()
                if not row:
                    raise HTTPException(404, "Sohbet bulunamadı.")
            else:
                import uuid
                chat_id = str(uuid.uuid4())
                title = user_message[:70] or "Yeni sohbet"
                conn.execute("INSERT INTO chats(id,user_id,title) VALUES(%s,%s,%s)", (chat_id, user["id"], title))
            history = conn.execute("SELECT role, content FROM messages WHERE chat_id=%s ORDER BY id DESC LIMIT 20", (chat_id,)).fetchall()
            history.reverse()
            msgs.extend({"role": x["role"], "content": x["content"]} for x in history)
            msgs.append({"role":"user","content":user_message})
            answer = call_ollama(msgs)
            conn.execute("INSERT INTO messages(chat_id,role,content) VALUES(%s,'user',%s)", (chat_id,user_message))
            conn.execute("INSERT INTO messages(chat_id,role,content) VALUES(%s,'assistant',%s)", (chat_id,answer))
            conn.execute("UPDATE chats SET updated_at=NOW() WHERE id=%s", (chat_id,))
        return {"answer": answer, "chat_id": chat_id, "sources": sources, "saved": True}
    # Guest chat is intentionally not written to the database.
    for item in body.guest_history[-12:]:
        if isinstance(item, dict) and item.get("role") in ("user","assistant") and isinstance(item.get("content"), str):
            msgs.append({"role": item["role"], "content": item["content"][:12000]})
    msgs.append({"role":"user","content":user_message})
    return {"answer": call_ollama(msgs), "chat_id": None, "sources": sources, "saved": False}


def improve_image_prompt(prompt: str) -> str:
    prompt = prompt.strip()
    return (
        "Create a polished, high-quality image matching the user's request. "
        "Use a clear composition, coherent perspective, realistic lighting, "
        "rich but natural colors, detailed textures, sharp focus, "
        "consistent anatomy and proportions, and professional visual quality. "
        "Avoid blurry details, distorted objects, duplicated elements, "
        "unintended text, watermarks, and malformed hands or faces. "
        "Follow the user's requested subject and style faithfully. "
        "User's original request: " + prompt
    )


@app.post("/api/image")
def image(body: ImageBody):
    if not POLLINATIONS_API_KEY:
        raise HTTPException(
            503,
            "Görsel üretimi ayarlı değil: POLLINATIONS_API_KEY eklenmeli."
        )

    url = "https://gen.pollinations.ai/image/" + quote(
        improve_image_prompt(body.prompt), safe=""
    )

    try:
      
        r = requests.get(
            url,
            params={
                "model": "black-forest-labs/flux.2-pro",
                "width": 1024,
                "height": 1024,
                "nologo": "true"
            },
            headers={"Authorization": f"Bearer {POLLINATIONS_API_KEY}"},
            timeout=180
        )


        if not r.ok:
            raise HTTPException(
                r.status_code,
                "Görsel servisi hata verdi: " + r.text[:250]
            )

        content_type = r.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            raise HTTPException(
                502,
                "Görsel servisi görsel yerine beklenmeyen yanıt döndürdü."
            )

        data = base64.b64encode(r.content).decode("ascii")
        return {
            "image_data": f"data:{content_type};base64,{data}",
            "model": "black-forest-labs/flux.2-pro"
        }

    except requests.RequestException as e:
        raise HTTPException(
            502,
            f"Görsel servisine bağlanılamadı: {str(e)[:200]}"
        )


            headers={"Authorization": f"Bearer {POLLINATIONS_API_KEY}"}, timeout=180)
        if not r.ok:
            raise HTTPException(r.status_code, "Görsel servisi hata verdi: " + r.text[:250])
        content_type = r.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            raise HTTPException(502, "Görsel servisi görsel yerine beklenmeyen yanıt döndürdü.")
        data = base64.b64encode(r.content).decode("ascii")
        return {"image_data": f"data:{content_type};base64,{data}", "model": POLLINATIONS_IMAGE_MODEL}
    except requests.RequestException as e:
        raise HTTPException(502, f"Görsel servisine bağlanılamadı: {str(e)[:200]}")

HTML = r"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tekden AI</title><style>
:root{color-scheme:dark;--bg:#101114;--panel:#191a20;--panel2:#22242b;--text:#f3f4f6;--muted:#9ca3af;--accent:#8b5cf6;--line:#30323a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}button,input,textarea{font:inherit}button{cursor:pointer;border:0;border-radius:10px;padding:10px 14px;background:var(--accent);color:white;font-weight:600}button.secondary{background:var(--panel2);border:1px solid var(--line)}button:disabled{opacity:.5;cursor:wait}
.layout{display:grid;grid-template-columns:270px 1fr;height:100dvh}.sidebar{background:#15161b;border-right:1px solid var(--line);padding:16px;display:flex;flex-direction:column;gap:12px;min-height:0}.brand{font-size:22px;font-weight:800;letter-spacing:-.6px}.brand span{color:#a78bfa}.sideactions{display:grid;gap:8px}.history{overflow:auto;display:grid;gap:5px}.history button{background:transparent;text-align:left;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.history button:hover{background:var(--panel2)}.userbar{margin-top:auto;border-top:1px solid var(--line);padding-top:12px}.main{display:flex;flex-direction:column;min-width:0;min-height:0}.top{padding:15px 22px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}.top small{color:var(--muted)}.messages{flex:1;overflow:auto;padding:26px max(20px,calc((100% - 820px)/2));display:flex;flex-direction:column;gap:20px}.welcome{margin:auto;text-align:center;max-width:650px}.welcome h1{font-size:clamp(28px,4vw,42px);letter-spacing:-1px;margin:0 0 8px}.welcome p{color:var(--muted)}.bubble{max-width:850px;padding:14px 16px;border-radius:16px;white-space:pre-wrap;overflow-wrap:anywhere}.bubble.user{align-self:flex-end;background:#2a2440}.bubble.assistant{align-self:flex-start;background:var(--panel);border:1px solid var(--line);width:min(100%,850px)}.bubble img{max-width:100%;border-radius:12px;margin-top:8px}.sources{font-size:13px;color:var(--muted);margin-top:10px}.sources a{color:#c4b5fd}.composerwrap{padding:12px 20px 20px}.composer{max-width:850px;margin:auto;background:var(--panel);border:1px solid #3b3d46;border-radius:18px;padding:10px;box-shadow:0 10px 40px #0003}.composer textarea{width:100%;resize:vertical;min-height:55px;max-height:180px;background:transparent;border:0;outline:0;color:var(--text);padding:8px}.tools{display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap}.toolleft{display:flex;gap:8px;align-items:center}.status{font-size:12px;color:var(--muted);padding:0 5px}.modalback{position:fixed;inset:0;background:#0009;display:none;align-items:center;justify-content:center;padding:16px}.modal{width:min(420px,100%);background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:22px}.modal h2{margin-top:0}.modal input{display:block;width:100%;padding:12px;margin:9px 0;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:10px}.modal .row{display:flex;gap:8px}.modal .row button{flex:1}.notice{color:var(--muted);font-size:12px}.mobile{display:none}@media(max-width:720px){.layout{grid-template-columns:1fr}.sidebar{display:none}.mobile{display:inline-block}.messages{padding:20px 14px}.top{padding:12px 14px}.composerwrap{padding:8px 10px 12px}}
</style></head><body>
<div class="layout"><aside class="sidebar"><div class="brand">Tekden <span>AI</span></div>
<div class="sideactions"><button id="newChat">＋ Yeni sohbet</button><button class="secondary" id="loginSide">Giriş yap / Kayıt ol</button></div>
<div><small style="color:var(--muted)">SOHBET GEÇMİŞİ</small></div><div class="history" id="history"><div class="notice">Geçmişi görmek için giriş yap.</div></div>
<div class="userbar" id="userbar"><div class="notice">Misafir modu · Sohbetler kaydedilmez.</div></div></aside>
<main class="main"><header class="top"><button class="secondary mobile" id="mobileNew">＋</button><div><strong>Tekden AI</strong><br><small>Yardımcı yapay zekân</small></div><div><button class="secondary" id="loginTop">Giriş yap</button></div></header>
<section class="messages" id="messages"><div class="welcome" id="welcome"><h1>Bugün ne yapmak istersin?</h1><p>Soru sor, internette araştırma yap veya bir görsel oluştur.</p><div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap"><button class="secondary suggestion">Bir konuyu açıkla</button><button class="secondary suggestion">Güncel haberleri araştır</button><button class="secondary suggestion">Bir görsel oluştur</button></div></div></section>
<div class="composerwrap"><div class="composer"><textarea id="prompt" placeholder="Tekden AI'a mesaj yaz..."></textarea><div class="tools"><div class="toolleft"><button class="secondary" id="searchBtn" title="İnternette ara">⌕ Web ara</button><button class="secondary" id="imageBtn" title="Görsel oluştur">▧ Görsel</button><span class="status" id="status">Hazır</span></div><button id="sendBtn">Gönder ↑</button></div></div><div class="notice" style="text-align:center;margin-top:8px">Misafir sohbetleri kaydedilmez. Oturum açarsan sohbet geçmişin hesabına kaydedilir.</div></div></main></div>
<div class="modalback" id="modalback"><div class="modal"><h2 id="modalTitle">Tekden AI'a giriş yap</h2><p class="notice">Hesabınla sohbet geçmişin saklanır.</p><input id="authUser" placeholder="Kullanıcı adı" autocomplete="username"><input id="authPass" type="password" placeholder="Şifre (en az 8 karakter)" autocomplete="current-password"><div class="row"><button id="authSubmit">Giriş yap</button><button class="secondary" id="registerBtn">Kayıt ol</button></div><p id="authError" class="notice"></p><button class="secondary" id="closeModal" style="width:100%;margin-top:8px">Kapat</button></div></div>
<script>
const $=id=>document.getElementById(id);let user=null,chatId=null,guestHistory=[],forceSearch=false;
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function setStatus(s){$('status').textContent=s}
function showModal(){ $('modalback').style.display='flex';$('authError').textContent=''; }
function closeModal(){ $('modalback').style.display='none'; }
function bubble(role,content,sources=[]){$('welcome')?.remove();const d=document.createElement('div');d.className='bubble '+role;d.textContent=content;$('messages').appendChild(d);if(sources.length){const s=document.createElement('div');s.className='sources';s.innerHTML='<b>Kaynaklar</b><br>'+sources.map(x=>`<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title||x.url)}</a>`).join('<br>');d.appendChild(s)}$('messages').scrollTop=$('messages').scrollHeight;return d}
function imageBubble(data){$('welcome')?.remove();const d=document.createElement('div');d.className='bubble assistant';d.textContent='Oluşturulan görsel';const img=document.createElement('img');img.src=data;img.alt='Yapay zekâ tarafından oluşturulan görsel';d.appendChild(img);$('messages').appendChild(d);$('messages').scrollTop=$('messages').scrollHeight}
async function api(url,opts={}){const r=await fetch(url,{...opts,headers:{'Content-Type':'application/json',...(opts.headers||{})}});let d={};try{d=await r.json()}catch{}if(!r.ok)throw new Error(d.detail||'İstek başarısız oldu.');return d}
async function refreshMe(){try{const d=await api('/api/me');user=d.user;renderUser();if(user)loadChats()}catch{user=null;renderUser()}}
function renderUser(){ $('userbar').innerHTML=user?`<div><b>${esc(user.username)}</b></div><button class="secondary" id="logoutBtn" style="width:100%;margin-top:8px">Çıkış yap</button>`:'<div class="notice">Misafir modu · Sohbetler kaydedilmez.</div>'; $('loginSide').textContent=user?'Hesap açık':'Giriş yap / Kayıt ol';$('loginTop').textContent=user?'Hesap açık':'Giriş yap';$('loginSide').onclick=user?null:showModal;$('loginTop').onclick=user?null:showModal;if(user)$('logoutBtn').onclick=async()=>{await api('/api/logout',{method:'POST'});user=null;chatId=null;guestHistory=[];$('messages').innerHTML='';newChat();renderUser();$('history').innerHTML='';}}
async function loadChats(){try{const d=await api('/api/chats');$('history').innerHTML='';d.chats.forEach(c=>{const b=document.createElement('button');b.textContent=c.title;b.title=c.title;b.onclick=()=>openChat(c.id);$('history').appendChild(b)})}catch(e){$('history').innerHTML='<div class="notice">Geçmiş yüklenemedi.</div>'}}
async function openChat(id){try{const d=await api('/api/chats/'+id);chatId=id;$('messages').innerHTML='';d.messages.forEach(m=>bubble(m.role==='user'?'user':'assistant',m.content));}catch(e){alert(e.message)}}
function newChat(){chatId=null;guestHistory=[];$('messages').innerHTML='<div class="welcome" id="welcome"><h1>Bugün ne yapmak istersin?</h1><p>Soru sor, internette araştırma yap veya bir görsel oluştur.</p></div>'; $('prompt').focus()}
async function send(){const text=$('prompt').value.trim();if(!text)return;$('prompt').value='';bubble('user',text);setStatus('Yanıt hazırlanıyor…');$('sendBtn').disabled=true;try{const d=await api('/api/chat',{method:'POST',body:JSON.stringify({message:text,chat_id:chatId,guest_history:guestHistory,force_search:forceSearch})});if(d.chat_id)chatId=d.chat_id;bubble('assistant',d.answer,d.sources||[]);if(!user){guestHistory.push({role:'user',content:text},{role:'assistant',content:d.answer});guestHistory=guestHistory.slice(-16)}if(user)loadChats();setStatus(d.sources?.length?'Kaynaklarla yanıtlandı':'Tamamlandı');}catch(e){bubble('assistant','Hata: '+e.message);setStatus('Hata')}finally{$('sendBtn').disabled=false;forceSearch=false;$('searchBtn').style.borderColor='var(--line)'}}
async function generateImage(){const prompt=$('prompt').value.trim();if(!prompt){alert('Önce oluşturmak istediğin görseli açıklayan bir metin yaz.');return}bubble('user','Görsel oluştur: '+prompt);$('prompt').value='';setStatus('Görsel oluşturuluyor…');$('imageBtn').disabled=true;try{const d=await api('/api/image',{method:'POST',body:JSON.stringify({prompt})});imageBubble(d.image_data);setStatus('Görsel hazır')}catch(e){bubble('assistant','Görsel oluşturulamadı: '+e.message);setStatus('Hata')}finally{$('imageBtn').disabled=false}}
$('sendBtn').onclick=send;$('prompt').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}});$('imageBtn').onclick=generateImage;$('searchBtn').onclick=()=>{forceSearch=!forceSearch;$('searchBtn').style.borderColor=forceSearch?'#a78bfa':'var(--line)';setStatus(forceSearch?'Web araması açık':'Otomatik arama')};$('newChat').onclick=newChat;$('mobileNew').onclick=newChat;$('loginSide').onclick=showModal;$('loginTop').onclick=showModal;$('closeModal').onclick=closeModal;$('modalback').onclick=e=>{if(e.target===$('modalback'))closeModal()};
let authMode='login';$('authSubmit').onclick=()=>auth('login');$('registerBtn').onclick=()=>auth('register');
async function auth(mode){authMode=mode;const username=$('authUser').value.trim(),password=$('authPass').value;try{const d=await api('/api/'+mode,{method:'POST',body:JSON.stringify({username,password})});user=d.user;closeModal();renderUser();newChat();loadChats()}catch(e){$('authError').textContent=e.message}}
document.querySelectorAll('.suggestion').forEach(b=>b.onclick=()=>{$('prompt').value=b.textContent==='Güncel haberleri araştır'?'Bugünkü önemli haberleri araştır ve kaynak göster.':b.textContent==='Bir görsel oluştur'?'Fütüristik, sinematik bir şehir manzarası':'Bana bu konuyu anlaşılır örneklerle açıkla';$('prompt').focus()});
refreshMe();
</script></body></html>"""

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(HTML)

