from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import os, sqlite3, secrets, hashlib, hmac, json, time
import requests

app = FastAPI(title="Tekden AI")
OLLAMA_URL = "https://ollama.com/api/chat"
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "").strip()
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:20b-cloud").strip()
POLLINATIONS_API_KEY = os.getenv("POLLINATIONS_API_KEY", "").strip()
POLLINATIONS_IMAGE_MODEL = os.getenv("POLLINATIONS_IMAGE_MODEL", "flux").strip() or "flux"
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip() or secrets.token_urlsafe(48)
DB_PATH = os.getenv("DATABASE_PATH", "tekden_ai.sqlite3")

SYSTEM_PROMPT = """Sen Tekden AI'sın. Ahmet Kerim tarafından geliştirilen genel amaçlı bir yapay zekâ asistanısın. Türkçe, anlaşılır, nazik ve faydalı cevaplar ver. Bilmediğin bilgileri uydurma; emin değilsen belirt. Güncel bilgi için sağlanan web arama sonuçlarını kullan ve kaynakları açıkça belirt."""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at INTEGER NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, image_url TEXT, sources_json TEXT, created_at INTEGER NOT NULL)")

init_db()


def hash_password(password: str, salt: bytes = None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240000)
    return salt.hex() + ":" + digest.hex()


def verify_password(password, stored):
    try:
        salt_hex, digest_hex = stored.split(":", 1)
        test = hash_password(password, bytes.fromhex(salt_hex)).split(":", 1)[1]
        return hmac.compare_digest(test, digest_hex)
    except Exception:
        return False


def make_session(user_id: int):
    payload = f"{user_id}:{int(time.time()) + 60*60*24*14}"
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + sig


def read_session(request: Request):
    token = request.cookies.get("tekden_session", "")
    try:
        payload, sig = token.rsplit(".", 1)
        expected = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected): return None
        uid, expiry = payload.split(":", 1)
        if int(expiry) < int(time.time()): return None
        with db() as c:
            row = c.execute("SELECT id, username FROM users WHERE id=?", (int(uid),)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def should_image(text):
    q = text.casefold()
    return any(x in q for x in ["görsel oluştur", "görsel üret", "resim oluştur", "resim çiz", "fotoğraf oluştur", "çizim oluştur", "logo oluştur", "afiş oluştur", "generate an image", "create an image", "draw me"])


def should_search(text):
    q = text.casefold()
    return any(x in q for x in ["internette ara", "internetten ara", "webde ara", "web araması", "kaynak göster", "güncel", "şu an", "bugün", "son dakika", "en son", "haberler", "fiyatı", "kaç para", "döviz", "hava durumu", "maç sonucu", "puan durumu", "araştır", "karşılaştır", "kaynakça", "linkini ver", "bağlantısını ver"])


def make_image(prompt):
    if not POLLINATIONS_API_KEY:
        return {"error": "Görsel üretimi için Render Environment bölümünde POLLINATIONS_API_KEY eksik."}
    try:
        # API expects a prompt in the path; quote it to avoid malformed URLs.
        from urllib.parse import quote
        url = "https://gen.pollinations.ai/image/" + quote(prompt[:1500], safe="")
        r = requests.get(url, params={"model": POLLINATIONS_IMAGE_MODEL, "width": 1024, "height": 1024, "nologo": "true"}, headers={"Authorization": "Bearer " + POLLINATIONS_API_KEY}, timeout=180)
        if r.status_code in (401, 403): return {"error": "Pollinations API anahtarı kabul edilmedi. Render'daki POLLINATIONS_API_KEY değerini kontrol et."}
        if r.status_code == 429: return {"error": "Görsel servisi kullanım sınırına ulaştı. Bir süre sonra tekrar dene."}
        r.raise_for_status()
        ctype = r.headers.get("content-type", "image/png")
        if not ctype.startswith("image/"):
            return {"error": "Görsel servisi beklenen görseli döndürmedi. API yanıtını kontrol etmek gerekiyor."}
        import base64
        return {"reply": "Görselin hazır.", "image": "data:" + ctype + ";base64," + base64.b64encode(r.content).decode("ascii")}
    except requests.Timeout:
        return {"error": "Görsel oluşturma zaman aşımına uğradı. Tekrar deneyebilirsin."}
    except requests.RequestException as e:
        return {"error": "Görsel servisine bağlanılamadı. API anahtarını ve Pollinations kullanım durumunu kontrol et."}


def chat_answer(text):
    if not OLLAMA_API_KEY: return {"error": "Render Environment bölümünde OLLAMA_API_KEY eksik."}
    sources = []
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if should_search(text):
        try:
            sr = requests.post("https://ollama.com/api/web_search", headers={"Authorization": f"Bearer {OLLAMA_API_KEY}", "Content-Type": "application/json"}, json={"query": text, "max_results": 5}, timeout=35)
            if sr.status_code in (401,403): return {"error": "Ollama web araması yetkilendirilmedi. API anahtarını kontrol et."}
            if sr.status_code == 429: return {"error": "Web arama kotası dolmuş olabilir. Daha sonra tekrar dene."}
            sr.raise_for_status()
            results = sr.json().get("results", [])
            sources = [{"title": str(x.get("title") or "Kaynak")[:250], "url": str(x.get("url") or "")} for x in results if str(x.get("url", "")).startswith(("https://", "http://"))][:5]
            context = "\n\n".join(f"Başlık: {x.get('title','')}\nURL: {x.get('url','')}\nÖzet: {x.get('content','')}" for x in results[:5])
            if context:
                messages.append({"role":"system", "content":"Kullanıcı güncel bilgi/web araması istiyor. Aşağıdaki sonuçlarla sınırlı kal, desteklenmeyen ayrıntıları uydurma. Kaynak bağlantıları arayüzde gösterilecek.\n" + context})
        except requests.RequestException:
            return {"error": "Web araması başarısız oldu. Ollama API anahtarını ve servis durumunu kontrol et."}
    messages.append({"role":"user", "content":text})
    try:
        r = requests.post(OLLAMA_URL, headers={"Authorization":f"Bearer {OLLAMA_API_KEY}", "Content-Type":"application/json"}, json={"model":OLLAMA_MODEL, "stream":False, "messages":messages}, timeout=180)
        if r.status_code in (401,403): return {"error":"Ollama API anahtarı kabul edilmedi."}
        if r.status_code == 429: return {"error":"Ollama kullanım kotası dolmuş olabilir. Daha sonra tekrar dene."}
        r.raise_for_status()
        reply = r.json().get("message", {}).get("content", "").strip() or "Şu anda yanıt oluşturamadım. Tekrar dener misin?"
        return {"reply":reply, "sources":sources}
    except requests.Timeout: return {"error":"Yanıt oluşturmak uzun sürdü. Tekrar dene."}
    except requests.RequestException: return {"error":"Ollama Cloud'a bağlanılamadı. API anahtarını ve model adını kontrol et."}


class AuthBody(BaseModel):
    username: str
    password: str

class ChatBody(BaseModel):
    message: str
    chat_id: str | None = None


@app.get("/", response_class=HTMLResponse)
def home(): return HTML_PAGE

@app.get("/api/health")
def health():
    return {"ok": bool(OLLAMA_API_KEY), "ollama_configured": bool(OLLAMA_API_KEY), "image_configured": bool(POLLINATIONS_API_KEY), "image_model": POLLINATIONS_IMAGE_MODEL, "database": "SQLite (Render Free'de kalıcı disk yoksa kalıcı değildir)"}

@app.get("/api/me")
def me(request: Request):
    u = read_session(request)
    return {"logged_in": bool(u), "user": {"username":u["username"]} if u else None}

@app.post("/api/register")
def register(body: AuthBody, response: Response):
    username = body.username.strip()
    password = body.password
    if len(username) < 3 or len(username) > 32 or not all(ch.isalnum() or ch in "_-" for ch in username): return JSONResponse({"error":"Kullanıcı adı 3-32 karakter olmalı; harf, rakam, _ ve - kullanılabilir."}, status_code=400)
    if len(password) < 8: return JSONResponse({"error":"Şifre en az 8 karakter olmalı."}, status_code=400)
    try:
        with db() as c:
            cur = c.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)", (username, hash_password(password), int(time.time())))
            uid = cur.lastrowid
        response.set_cookie("tekden_session", make_session(uid), httponly=True, secure=True, samesite="lax", max_age=60*60*24*14)
        return {"ok":True,"username":username}
    except sqlite3.IntegrityError: return JSONResponse({"error":"Bu kullanıcı adı zaten kullanılıyor."}, status_code=409)

@app.post("/api/login")
def login(body: AuthBody, response: Response):
    with db() as c: row = c.execute("SELECT id,username,password_hash FROM users WHERE username=?", (body.username.strip(),)).fetchone()
    if not row or not verify_password(body.password, row["password_hash"]): return JSONResponse({"error":"Kullanıcı adı veya şifre yanlış."}, status_code=401)
    response.set_cookie("tekden_session", make_session(row["id"]), httponly=True, secure=True, samesite="lax", max_age=60*60*24*14)
    return {"ok":True,"username":row["username"]}

@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("tekden_session", httponly=True, secure=True, samesite="lax")
    return {"ok":True}

@app.get("/api/chats")
def list_chats(request: Request):
    u = read_session(request)
    if not u: return JSONResponse({"error":"Sohbet geçmişi için giriş yapmalısın."}, status_code=401)
    with db() as c: rows = c.execute("SELECT id,title,updated_at FROM chats WHERE user_id=? ORDER BY updated_at DESC LIMIT 100", (u["id"],)).fetchall()
    return {"chats":[dict(x) for x in rows]}

@app.get("/api/chats/{chat_id}")
def get_chat(chat_id: str, request: Request):
    u = read_session(request)
    if not u: return JSONResponse({"error":"Giriş yapmalısın."}, status_code=401)
    with db() as c:
        chat = c.execute("SELECT id,title FROM chats WHERE id=? AND user_id=?", (chat_id,u["id"])).fetchone()
        if not chat: return JSONResponse({"error":"Sohbet bulunamadı."}, status_code=404)
        rows = c.execute("SELECT role,content,image_url,sources_json FROM messages WHERE chat_id=? ORDER BY id", (chat_id,)).fetchall()
    return {"chat":dict(chat),"messages":[{"role":r["role"],"content":r["content"],"image":r["image_url"],"sources":json.loads(r["sources_json"] or "[]")} for r in rows]}

@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str, request: Request):
    u = read_session(request)
    if not u: return JSONResponse({"error":"Giriş yapmalısın."}, status_code=401)
    with db() as c:
        c.execute("DELETE FROM messages WHERE chat_id=? AND chat_id IN (SELECT id FROM chats WHERE id=? AND user_id=?)", (chat_id,chat_id,u["id"]))
        c.execute("DELETE FROM chats WHERE id=? AND user_id=?", (chat_id,u["id"]))
    return {"ok":True}

@app.post("/api/chat")
def chat(body: ChatBody, request: Request):
    text = body.message.strip()
    if not text: return {"error":"Bir mesaj yaz."}
    u = read_session(request)
    cid = body.chat_id or secrets.token_urlsafe(12)
    result = make_image(text) if should_image(text) else chat_answer(text)
    if "error" in result: return result
    # Save only authenticated conversations. Anonymous conversations are not persisted.
    if u:
        now = int(time.time())
        title = text[:64] + ("…" if len(text) > 64 else "")
        image = result.get("image")
        sources = result.get("sources", [])
        with db() as c:
            existing = c.execute("SELECT id FROM chats WHERE id=? AND user_id=?", (cid,u["id"])).fetchone()
            if not existing:
                c.execute("INSERT INTO chats(id,user_id,title,created_at,updated_at) VALUES(?,?,?,?,?)", (cid,u["id"],title,now,now))
                c.execute("INSERT INTO messages(chat_id,role,content,image_url,sources_json,created_at) VALUES(?,?,?,?,?,?)", (cid,"user",text,None,"[]",now))
            else:
                c.execute("UPDATE chats SET updated_at=? WHERE id=? AND user_id=?", (now,cid,u["id"]))
                c.execute("INSERT INTO messages(chat_id,role,content,image_url,sources_json,created_at) VALUES(?,?,?,?,?,?)", (cid,"user",text,None,"[]",now))
            c.execute("INSERT INTO messages(chat_id,role,content,image_url,sources_json,created_at) VALUES(?,?,?,?,?,?)", (cid,"assistant",result.get("reply",""),image,json.dumps(sources,ensure_ascii=False),now))
        result["chat_id"] = cid
    else:
        result["chat_id"] = None
        result["notice"] = "Misafir sohbeti kaydedilmedi. Geçmişi saklamak için giriş yap."
    return result


HTML_PAGE = r'''<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Tekden AI</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#111a2e;--panel2:#17233b;--line:#263653;--text:#edf3ff;--muted:#a8b6d1;--accent:#829cff;--accent2:#9b7bff}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,Segoe UI,sans-serif;height:100vh}button,input,textarea{font:inherit}button{cursor:pointer;border:1px solid var(--line);border-radius:12px;background:var(--panel2);color:var(--text);padding:10px 13px}button:hover{filter:brightness(1.15)}button:disabled{opacity:.55;cursor:wait}.layout{height:100vh;display:grid;grid-template-columns:260px 1fr}.side{background:#080d19;border-right:1px solid var(--line);padding:14px;display:flex;flex-direction:column;gap:12px;min-height:0}.brand{display:flex;align-items:center;gap:10px;padding:7px}.logo{width:38px;height:38px;display:grid;place-items:center;border-radius:13px;background:linear-gradient(135deg,var(--accent),var(--accent2));font-weight:900;font-size:20px}.brand b{font-size:18px}.new{width:100%;text-align:left}.small{color:var(--muted);font-size:12px}.history{overflow:auto;display:flex;flex-direction:column;gap:5px}.history button{text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px;background:transparent}.sidebottom{margin-top:auto;display:grid;gap:8px}.main{display:flex;flex-direction:column;min-width:0;height:100vh}.top{height:68px;border-bottom:1px solid var(--line);display:flex;align-items:center;padding:0 22px;gap:12px}.top strong{font-size:17px}.top .spacer{flex:1}.content{flex:1;overflow:auto;display:flex;flex-direction:column;padding:24px max(20px,calc((100% - 850px)/2));gap:16px}.welcome{margin:auto;text-align:center;max-width:620px;padding:20px}.welcome .big{font-size:52px}.welcome h1{font-size:30px;margin:8px}.welcome p{color:var(--muted)}.cards{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-top:22px}.cards button{font-size:13px}.msg{max-width:100%;padding:15px 17px;border:1px solid var(--line);border-radius:17px;white-space:pre-wrap;overflow-wrap:anywhere}.msg.user{align-self:flex-end;background:#1a2944;max-width:88%}.msg.assistant{align-self:stretch;background:#101a2d}.msg img{max-width:min(100%,600px);display:block;border-radius:12px;margin-top:12px}.sources{display:flex;flex-direction:column;gap:5px;margin-top:10px;font-size:13px}.sources a{color:#a9bbff}.notice{color:var(--muted);font-size:12px;margin-top:7px}.composerWrap{padding:12px max(20px,calc((100% - 850px)/2)) 18px}.composer{border:1px solid #334463;background:var(--panel);border-radius:20px;padding:10px;display:flex;align-items:flex-end;gap:9px}.composer textarea{flex:1;resize:none;min-height:44px;max-height:180px;border:0;outline:0;background:transparent;color:var(--text);padding:11px}.send{background:linear-gradient(135deg,#6f8dff,#9573f7);border:0;font-weight:700}.modal{position:fixed;inset:0;background:#0009;display:none;place-items:center;padding:18px;z-index:10}.modal.open{display:grid}.box{width:min(100%,390px);background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:22px;box-shadow:0 20px 80px #0008}.box h2{margin-top:0}.box input{width:100%;margin:6px 0 12px;background:#0b1222;color:var(--text);border:1px solid var(--line);border-radius:10px;padding:12px}.row{display:flex;gap:8px}.row>*{flex:1}.hidden{display:none!important}@media(max-width:700px){.layout{grid-template-columns:1fr}.side{display:none}.top{padding:0 14px}.content{padding:16px 13px}.composerWrap{padding:8px 10px 12px}.welcome h1{font-size:25px}}
</style></head><body>
<div class="layout"><aside class="side"><div class="brand"><div class="logo">T</div><div><b>Tekden AI</b><div class="small">Akıllı sohbet asistanı</div></div></div><button class="new" onclick="newChat()">＋ Yeni sohbet</button><div class="small">GEÇMİŞ SOHBETLER</div><div id="history" class="history"><div class="small">Geçmiş için giriş yap.</div></div><div class="sidebottom"><div id="sideUser" class="small">Misafir olarak kullanıyorsun</div><button id="authButton" onclick="openAuth()">Giriş yap / Kayıt ol</button><button id="logoutButton" class="hidden" onclick="logout()">Çıkış yap</button></div></aside>
<main class="main"><header class="top"><div class="logo">T</div><strong>Tekden AI</strong><span class="small">Ara · Sohbet et · Görsel oluştur</span><span class="spacer"></span><button onclick="openAuth()" id="topAuth">Giriş yap</button></header><section id="content" class="content"><div class="welcome" id="welcome"><div class="big">✦</div><h1>Bugün ne yapmak istersin?</h1><p>Soru sor, güncel bilgileri araştır veya bir görsel tasarla.</p><div class="cards"><button onclick="suggest('2026 yılındaki yapay zekâ gelişmelerini araştır ve kaynak göster')">⌕ Güncel bilgi araştır</button><button onclick="suggest('Uzayda yüzen sevimli bir robot görseli oluştur')">▧ Görsel oluştur</button><button onclick="suggest('Bana Python öğrenmek için başlangıç planı hazırla')">⌘ Kod öğren</button></div></div></section><div class="composerWrap"><div class="composer"><textarea id="message" rows="1" placeholder="Tekden AI'a mesaj yaz…" onkeydown="keySend(event)"></textarea><button id="send" class="send" onclick="sendMessage()">Gönder ↑</button></div><div class="notice">Misafir sohbetleri kaydedilmez. Geçmişi saklamak için giriş yap. Görsel üretimi servis limitlerine tabidir.</div></div></main></div>
<div class="modal" id="authModal"><div class="box"><div class="row"><h2 id="authTitle">Giriş yap</h2><button onclick="closeAuth()" style="flex:0 0 auto">✕</button></div><label>Kullanıcı adı</label><input id="username" autocomplete="username" minlength="3" placeholder="Kullanıcı adın"><label>Şifre</label><input id="password" type="password" autocomplete="current-password" placeholder="En az 8 karakter"><div id="authError" class="small" style="color:#ff9b9b;margin-bottom:10px"></div><button id="authSubmit" class="send" style="width:100%" onclick="submitAuth()">Giriş yap</button><p class="small"><span id="switchText">Hesabın yok mu?</span> <a href="#" onclick="switchAuth(event)">Kayıt ol</a></p></div></div>
<script>
let me=null, currentChat=null, authMode='login'; const $=id=>document.getElementById(id);
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function openAuth(){ $('authModal').classList.add('open'); $('authError').textContent=''; }
function closeAuth(){ $('authModal').classList.remove('open'); }
function switchAuth(e){e.preventDefault();authMode=authMode==='login'?'register':'login';$('authTitle').textContent=authMode==='login'?'Giriş yap':'Hesap oluştur';$('authSubmit').textContent=authMode==='login'?'Giriş yap':'Kayıt ol';$('switchText').textContent=authMode==='login'?'Hesabın yok mu?':'Zaten hesabın var mı?';e.target.textContent=authMode==='login'?'Kayıt ol':'Giriş yap';$('password').autocomplete=authMode==='login'?'current-password':'new-password';}
async function api(url,opts={}){let r=await fetch(url,{credentials:'same-origin',headers:{'Content-Type':'application/json',...(opts.headers||{})},...opts});let d=await r.json().catch(()=>({error:'Sunucudan geçerli yanıt alınamadı.'}));if(!r.ok)throw new Error(d.error||'İşlem başarısız.');return d}
async function submitAuth(){ $('authError').textContent='';try{let d=await api('/api/'+authMode,{method:'POST',body:JSON.stringify({username:$('username').value.trim(),password:$('password').value})});me={username:d.username};closeAuth();await refreshUser();await loadHistory();}catch(e){$('authError').textContent=e.message}}
async function logout(){await api('/api/logout',{method:'POST'});me=null;currentChat=null;await refreshUser();$('history').innerHTML='<div class="small">Geçmiş için giriş yap.</div>';newChat();}
async function refreshUser(){let d=await api('/api/me');me=d.user;let logged=d.logged_in;$('sideUser').textContent=logged?'Giriş yapıldı: '+me.username:'Misafir olarak kullanıyorsun';$('authButton').classList.toggle('hidden',logged);$('logoutButton').classList.toggle('hidden',!logged);$('topAuth').classList.toggle('hidden',logged);}
async function loadHistory(){if(!me)return;try{let d=await api('/api/chats');$('history').innerHTML='';if(!d.chats.length){$('history').innerHTML='<div class="small">Henüz kayıtlı sohbet yok.</div>';return}d.chats.forEach(c=>{let b=document.createElement('button');b.textContent=c.title;b.title=c.title;b.onclick=()=>openChat(c.id);$('history').appendChild(b)})}catch(e){$('history').innerHTML='<div class="small">Geçmiş yüklenemedi.</div>'}}
function newChat(){currentChat=null;$('content').innerHTML='';$('content').appendChild($('welcome'));$('welcome').classList.remove('hidden');$('message').focus()}
function suggest(t){$('message').value=t;$('message').focus()}
function keySend(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMessage()}}
function addMessage(role,text,image=null,sources=[]){let w=$('welcome');if(w)w.classList.add('hidden');let div=document.createElement('div');div.className='msg '+role;div.innerHTML='<div>'+escapeHtml(text).replace(/\n/g,'<br>')+'</div>';if(image){let img=document.createElement('img');img.src=image;img.alt='Oluşturulan görsel';div.appendChild(img)}if(sources&&sources.length){let s=document.createElement('div');s.className='sources';sources.forEach(x=>{if(x.url&&/^https?:\/\//.test(x.url)){let a=document.createElement('a');a.href=x.url;a.target='_blank';a.rel='noopener noreferrer';a.textContent='↗ '+x.title;s.appendChild(a)}});div.appendChild(s)}$('content').appendChild(div);$('content').scrollTop=$('content').scrollHeight;return div}
async function sendMessage(){let text=$('message').value.trim();if(!text)return;$('message').value='';$('send').disabled=true;addMessage('user',text);let wait=addMessage('assistant','Yanıt hazırlanıyor…');try{let d=await api('/api/chat',{method:'POST',body:JSON.stringify({message:text,chat_id:currentChat})});wait.remove();if(d.error)throw new Error(d.error);if(d.chat_id)currentChat=d.chat_id;addMessage('assistant',d.reply||'İşlem tamamlandı.',d.image,d.sources||[]);if(d.notice){let n=document.createElement('div');n.className='notice';n.textContent=d.notice;$('content').appendChild(n)}if(me)await loadHistory()}catch(e){wait.textContent='Hata: '+e.message}finally{$('send').disabled=false;$('message').focus()}}
async function openChat(id){try{let d=await api('/api/chats/'+encodeURIComponent(id));currentChat=id;$('content').innerHTML='';d.messages.forEach(m=>addMessage(m.role,m.content,m.image,m.sources));}catch(e){alert(e.message)}}
refreshUser().then(()=>{if(me)loadHistory()}).catch(()=>{});
</script></body></html>'''
