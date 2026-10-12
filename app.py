import os, re, sqlite3, hashlib, hmac, secrets, time, base64
from datetime import datetime, timezone
from urllib.parse import quote
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import requests

app = FastAPI(title="Tekden AI")

DATABASE_PATH = os.getenv("DATABASE_PATH", "tekden_ai.sqlite3")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "").strip()
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:20b-cloud").strip()
POLLINATIONS_API_KEY = os.getenv("POLLINATIONS_API_KEY", "").strip()
IMAGE_MODEL = os.getenv("POLLINATIONS_IMAGE_MODEL", "flux").strip()

class Credentials(BaseModel):
    username: str
    password: str

class ChatRequest(BaseModel):
    message: str

def connect_db():
    c = sqlite3.connect(DATABASE_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with connect_db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            salt TEXT NOT NULL, password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS image_usage(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            created_at TEXT NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_usage_user_day ON image_usage(user_id, day)")

def today_utc():
    return datetime.now(timezone.utc).date().isoformat()

def pw_hash(password, salt=None):
    raw_salt = bytes.fromhex(salt) if salt else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), raw_salt, 310000)
    return digest.hex(), raw_salt.hex()

def make_session(user_id):
    if len(SESSION_SECRET) < 32:
        raise HTTPException(status_code=503, detail="Render Environment bölümüne en az 32 karakterlik SESSION_SECRET ekle.")
    payload = f"{user_id}:{int(time.time())}"
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + ":" + sig

def get_user_id(request: Request):
    token = request.cookies.get("tekden_session", "")
    try:
        uid, timestamp, sig = token.split(":", 2)
        payload = f"{uid}:{timestamp}"
        expected = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if len(SESSION_SECRET) < 32 or not hmac.compare_digest(sig, expected):
            return None
        if time.time() - int(timestamp) > 14 * 86400:
            return None
        return int(uid)
    except (ValueError, TypeError):
        return None

def usage_today(user_id):
    with connect_db() as c:
        row = c.execute("SELECT COUNT(*) n FROM image_usage WHERE user_id=? AND day=?",
                        (user_id, today_utc())).fetchone()
        return int(row["n"])

def remaining(user_id):
    return max(0, 3 - usage_today(user_id))

def add_usage(user_id):
    with connect_db() as c:
        c.execute("INSERT INTO image_usage(user_id,day,created_at) VALUES(?,?,?)",
                  (user_id, today_utc(), datetime.now(timezone.utc).isoformat()))

def set_login_cookie(response, user_id):
    response.set_cookie("tekden_session", make_session(user_id), httponly=True,
                        secure=True, samesite="lax", max_age=1209600)

init_db()

@app.post("/api/register")
def register(data: Credentials, response: Response):
    username = data.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,30}", username):
        raise HTTPException(400, "Kullanıcı adı 3-30 karakter olmalı; harf, rakam, nokta, tire ve alt çizgi kullan.")
    if len(data.password) < 10:
        raise HTTPException(400, "Parola en az 10 karakter olmalı.")
    hashed, salt = pw_hash(data.password)
    try:
        with connect_db() as c:
            cur = c.execute("INSERT INTO users(username,salt,password_hash,created_at) VALUES(?,?,?,?)",
                            (username, salt, hashed, datetime.now(timezone.utc).isoformat()))
            uid = cur.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Bu kullanıcı adı zaten kullanılıyor.")
    set_login_cookie(response, uid)
    return {"ok": True, "username": username, "images_remaining": 3}

@app.post("/api/login")
def login(data: Credentials, response: Response):
    with connect_db() as c:
        row = c.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE",
                        (data.username.strip(),)).fetchone()
    if not row:
        raise HTTPException(401, "Kullanıcı adı veya parola yanlış.")
    hashed, _ = pw_hash(data.password, row["salt"])
    if not hmac.compare_digest(hashed, row["password_hash"]):
        raise HTTPException(401, "Kullanıcı adı veya parola yanlış.")
    set_login_cookie(response, row["id"])
    return {"ok": True, "username": row["username"], "images_remaining": remaining(row["id"])}

@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("tekden_session")
    return {"ok": True}

@app.get("/api/me")
def me(request: Request):
    uid = get_user_id(request)
    if not uid:
        return {"authenticated": False}
    with connect_db() as c:
        row = c.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        return {"authenticated": False}
    return {"authenticated": True, "username": row["username"], "images_remaining": remaining(uid)}

def is_image_request(text):
    q = text.casefold()
    return any(x in q for x in [
        "görsel oluştur", "görsel üret", "resim oluştur", "resim çiz", "fotoğraf oluştur",
        "fotoğraf üret", "çizim oluştur", "illüstrasyon oluştur", "afiş oluştur",
        "logo oluştur", "görselini yap", "resmini yap", "bana çiz",
        "generate an image", "create an image", "draw me"
    ])

def generate_image(prompt, uid):
    if remaining(uid) <= 0:
        return {"error": "Günlük 3 görsel hakkını kullandın. Hakların UTC gece yarısında yenilenir."}
    if not POLLINATIONS_API_KEY:
        return {"error": "OpenAI kullanılmıyor. Görsel üretimi için Render Environment bölümüne POLLINATIONS_API_KEY eklemen gerekiyor. Bu sağlayıcının ücret/kota koşullarını kontrol et."}
    try:
        url = "https://gen.pollinations.ai/image/" + quote(prompt, safe="")
        r = requests.get(url, headers={"Authorization": f"Bearer {POLLINATIONS_API_KEY}"},
                         params={"model": IMAGE_MODEL, "width": 1024, "height": 1024}, timeout=180)
        if r.status_code == 401:
            return {"error": "Pollinations API anahtarı geçersiz."}
        if r.status_code == 402:
            return {"error": "Görsel servisi kredi/ödeme isteyebilir. Hesabındaki koşulları kontrol et."}
        if r.status_code == 429:
            return {"error": "Görsel servisi limitine ulaşıldı; sonra tekrar dene."}
        r.raise_for_status()
        ctype = r.headers.get("content-type", "").lower()
        if not ctype.startswith("image/"):
            return {"error": "Görsel servisi resim döndürmedi."}
        add_usage(uid)
        mime = "image/png" if "png" in ctype else "image/jpeg"
        image = "data:" + mime + ";base64," + base64.b64encode(r.content).decode("ascii")
        return {"reply": "Görsel hazır.", "image": image, "images_remaining": remaining(uid)}
    except requests.Timeout:
        return {"error": "Görsel oluşturma zaman aşımına uğradı."}
    except requests.RequestException:
        return {"error": "Görsel servisine bağlanılamadı."}

@app.post("/api/chat")
def chat(data: ChatRequest, request: Request):
    message = data.message.strip()
    if not message:
        return {"reply": "Bir mesaj yazıp tekrar dene."}
    uid = get_user_id(request)
    if not uid:
        return {"error": "Önce kayıt ol veya giriş yap."}
    if is_image_request(message):
        return generate_image(message, uid)
    if not OLLAMA_API_KEY:
        return {"error": "Render Environment bölümünde OLLAMA_API_KEY eksik."}
    try:
        r = requests.post("https://ollama.com/api/chat",
            headers={"Authorization": f"Bearer {OLLAMA_API_KEY}", "Content-Type": "application/json"},
            json={"model": OLLAMA_MODEL, "stream": False,
                  "messages": [{"role":"system","content":"Sen Tekden AI'sın. Türkçe, anlaşılır ve nazik cevap ver. Geliştiricin Ahmet Kerim Kırgız."},
                               {"role":"user","content":message}],
                  "options":{"temperature":0.7}}, timeout=180)
        if r.status_code == 401:
            return {"error":"Ollama API anahtarı kabul edilmedi."}
        if r.status_code == 429:
            return {"error":"Ollama kullanım kotası dolmuş olabilir."}
        if r.status_code == 404:
            return {"error":f"Model bulunamadı: {OLLAMA_MODEL}. OLLAMA_MODEL değerini kontrol et."}
        r.raise_for_status()
        answer = r.json().get("message",{}).get("content","").strip()
        return {"reply": answer or "Şu anda yanıt oluşturamadım."}
    except requests.Timeout:
        return {"error":"İstek çok uzun sürdü."}
    except requests.RequestException:
        return {"error":"Ollama Cloud bağlantısı başarısız."}

@app.get("/api/health")
def health():
    return {"ok": bool(OLLAMA_API_KEY), "provider":"Ollama Cloud",
            "model":OLLAMA_MODEL, "image_provider":"Pollinations (OpenAI dışı)"}

@app.get("/", response_class=HTMLResponse)
def home():
    return HTML


HTML = '<!doctype html>\n<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\n<title>Tekden AI</title><style>\n:root{color-scheme:dark;--bg:#0b1020;--panel:#111a2e;--line:#263653;--text:#edf3ff;--muted:#a8b6d1;--accent:#7c9cff}\n*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top,#1b2b4b,#0b1020 60%);color:var(--text);font:16px/1.5 system-ui;display:flex;justify-content:center}\n.app{width:min(100%,980px);height:100vh;min-height:540px;display:flex;flex-direction:column;padding:16px}\nheader{display:flex;align-items:center;gap:12px;border-bottom:1px solid var(--line);padding:8px 0 16px}\n.logo{width:44px;height:44px;border-radius:14px;background:linear-gradient(135deg,#7c9cff,#9d7cff);display:grid;place-items:center;font-size:24px;font-weight:800}\n.brand{flex:1}.brand h1{margin:0;font-size:21px}.brand p{margin:0;color:var(--muted);font-size:13px}\nbutton{cursor:pointer;border:1px solid var(--line);border-radius:11px;padding:9px 12px;color:var(--text);background:#17233b;font:inherit}\n#status{font-size:12px;color:var(--muted);padding:10px 0}#authPanel{background:var(--panel);border:1px solid var(--line);padding:12px;border-radius:14px}\ninput{max-width:100%;padding:10px;border-radius:9px;border:1px solid var(--line);background:var(--bg);color:var(--text);margin:4px}\n#messages{flex:1;overflow:auto;display:flex;flex-direction:column;gap:14px;padding:12px 0}\n.msg{max-width:90%;padding:12px 14px;background:var(--panel);border:1px solid var(--line);border-radius:14px;white-space:pre-wrap;overflow-wrap:anywhere}\n.msg.user{align-self:flex-end;background:#20345b}.msg img{display:block;max-width:100%;width:640px;border-radius:12px;margin-top:10px}\nform{display:flex;gap:8px;border:1px solid var(--line);border-radius:15px;padding:8px;background:var(--panel)}\ntextarea{flex:1;min-height:46px;max-height:150px;resize:vertical;background:transparent;border:0;outline:0;color:var(--text);font:inherit;padding:10px}\n#send{background:var(--accent);color:#10172b;font-weight:700}footer{text-align:center;font-size:11px;color:var(--muted);padding-top:8px}\n</style></head><body><div class="app">\n<header><div class="logo">T</div><div class="brand"><h1>Tekden AI</h1><p>Ahmet Kerim tarafından geliştirildi</p></div><button id="authToggle">Giriş / Kayıt</button></header>\n<div id="status">Hesap durumu kontrol ediliyor…</div>\n<section id="authPanel" hidden><b>Hesabın</b><div><input id="username" placeholder="Kullanıcı adı" autocomplete="username"><input id="password" type="password" placeholder="Parola (en az 10 karakter)" autocomplete="current-password"></div><button id="register">Kayıt ol</button><button id="login">Giriş yap</button><button id="logout">Çıkış</button><div id="authMessage"></div></section>\n<main id="messages"><div class="msg">Merhaba, ben Tekden AI. Sohbet etmek için giriş yap. Görsel oluşturmak için “Bir kedi resmi oluştur” gibi bir istek yaz.</div></main>\n<form id="form"><textarea id="input" placeholder="Mesaj yaz… (Enter ile gönder, Shift+Enter yeni satır)"></textarea><button id="send">Gönder</button></form>\n<footer>Tekden AI · Sohbet: Ollama Cloud · Görsel: ayrı sağlayıcı · Günlük 3 görsel hakkı</footer></div>\n<script>\nconst $=id=>document.getElementById(id), msgs=$(\'messages\');let busy=false;\nfunction add(text,who=\'assistant\',image=null){const d=document.createElement(\'div\');d.className=\'msg \'+who;d.textContent=text;if(image){const im=document.createElement(\'img\');im.src=image;im.alt=\'Oluşturulan görsel\';d.appendChild(im)}msgs.appendChild(d);msgs.scrollTop=msgs.scrollHeight;return d}\nasync function requestAuth(path){$(\'authMessage\').textContent=\'İşleniyor…\';try{const r=await fetch(path,{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({username:$(\'username\').value,password:$(\'password\').value})});const d=await r.json();if(!r.ok)throw Error(d.detail||d.error||\'İşlem başarısız\');$(\'authMessage\').textContent=\'Başarılı. Kalan görsel hakkı: \'+d.images_remaining;await refresh()}catch(e){$(\'authMessage\').textContent=e.message}}\n$(\'authToggle\').onclick=()=>{$(\'authPanel\').hidden=!$(\'authPanel\').hidden};\n$(\'register\').onclick=()=>requestAuth(\'/api/register\');$(\'login\').onclick=()=>requestAuth(\'/api/login\');\n$(\'logout\').onclick=async()=>{await fetch(\'/api/logout\',{method:\'POST\'});$(\'authMessage\').textContent=\'Çıkış yapıldı\';refresh()};\nasync function refresh(){try{const d=await (await fetch(\'/api/me\')).json();$(\'status\').textContent=d.authenticated?d.username+\' · Bugün kalan görsel hakkı: \'+d.images_remaining:\'Giriş yapılmadı · Sohbet ve görsel üretimi için giriş yap\'}catch{$(\'status\').textContent=\'Durum alınamadı\'}}\n$(\'form\').onsubmit=async e=>{e.preventDefault();const text=$(\'input\').value.trim();if(!text||busy)return;add(text,\'user\');$(\'input\').value=\'\';busy=true;$(\'send\').disabled=true;const wait=add(\'Düşünüyorum…\');try{const r=await fetch(\'/api/chat\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({message:text})});const d=await r.json();wait.remove();add(d.error?\'⚠️ \'+d.error:(d.reply||\'Yanıt yok\'), \'assistant\',d.image||null);if(d.images_remaining!==undefined)refresh()}catch{wait.textContent=\'İstek başarısız oldu.\'}finally{busy=false;$(\'send\').disabled=false;$(\'input\').focus()}};\n$(\'input\').onkeydown=e=>{if(e.key===\'Enter\'&&!e.shiftKey){e.preventDefault();$(\'form\').requestSubmit()}};\nrefresh();\n</script></body></html>'
