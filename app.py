import os, re, sqlite3, hashlib, hmac, secrets, time, base64
from datetime import datetime, timezone
from urllib.parse import quote
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import requestsimport os, re, json, time, hmac, hashlib, secrets, base64
from urllib.parse import quote
from datetime import datetime, timezone
from typing import Optional

import requests
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Tekden AI")
OLLAMA_URL = "https://ollama.com/api/chat"
OLLAMA_WEB_URL = "https://ollama.com/api/web_search"
API_KEY = os.environ.get("OLLAMA_API_KEY", "").strip()
MODEL_NAME = os.environ.get("OLLAMA_MODEL", "gpt-oss:20b-cloud").strip()
POLLINATIONS_API_KEY = os.environ.get("POLLINATIONS_API_KEY", "").strip()
IMAGE_MODEL = os.environ.get("POLLINATIONS_IMAGE_MODEL", "flux").strip() or "flux"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
SESSION_SECRET = os.environ.get("SESSION_SECRET", "").strip().encode("utf-8")
COOKIE_NAME = "tekden_session"
SESSION_SECONDS = 60 * 60 * 24 * 14

SYSTEM_PROMPT = """Sen Tekden AI'sın. Ahmet Kerim tarafından geliştirilen genel amaçlı bir yapay zekâ asistanısın. Türkçe, anlaşılır, nazik ve faydalı cevaplar ver. Bilmediğin bilgileri uydurma; emin değilsen belirt. Programlama, eğitim, bilim, yazma ve proje geliştirme konularında yardımcı ol. Güncel bilgi gerekiyorsa verilen web arama sonuçlarını kullan ve kaynaklarla destekle. Güvenli olmayan isteklerde güvenli alternatifler sun."""

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    chat_id: Optional[int] = None

class AuthRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)


def db_connect():
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="Veritabanı henüz ayarlanmadı. Render Environment bölümüne DATABASE_URL eklenmeli.")
    try:
        return psycopg.connect(DATABASE_URL, sslmode="require", row_factory=dict_row, connect_timeout=10)
    except Exception as exc:
        print("Database connection error:", repr(exc))
        raise HTTPException(status_code=503, detail="Veritabanına bağlanılamadı. DATABASE_URL değerini ve Supabase projesinin hazır olduğunu kontrol et.")


def init_db():
    if not DATABASE_URL:
        print("DATABASE_URL is missing; database initialization skipped")
        return
    try:
        with db_connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tekden_users (
                    id BIGSERIAL PRIMARY KEY,
                    username VARCHAR(32) UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tekden_chats (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES tekden_users(id) ON DELETE CASCADE,
                    title VARCHAR(180) NOT NULL DEFAULT 'Yeni sohbet',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tekden_messages (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL REFERENCES tekden_chats(id) ON DELETE CASCADE,
                    role VARCHAR(20) NOT NULL CHECK (role IN ('user','assistant')),
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS tekden_chats_user_updated ON tekden_chats(user_id, updated_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS tekden_messages_chat_id ON tekden_messages(chat_id, id)")
    except Exception as exc:
        print("Database initialization error:", repr(exc))

@app.on_event("startup")
def startup():
    init_db()


def password_hash(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return base64.urlsafe_b64encode(salt).decode().rstrip("=") + "$" + base64.urlsafe_b64encode(digest).decode().rstrip("=")


def password_matches(password: str, stored: str) -> bool:
    try:
        salt_s, digest_s = stored.split("$", 1)
        salt = base64.urlsafe_b64decode(salt_s + "=" * (-len(salt_s) % 4))
        expected = base64.urlsafe_b64decode(digest_s + "=" * (-len(digest_s) % 4))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def make_cookie(user_id: int) -> str:
    expires = int(time.time()) + SESSION_SECONDS
    payload = f"{user_id}:{expires}"
    sig = hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode().rstrip("=")


def read_cookie(request: Request) -> Optional[int]:
    if not SESSION_SECRET:
        return None
    token = request.cookies.get(COOKIE_NAME, "")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        user_s, expires_s, sig = raw.split(":", 2)
        payload = f"{user_s}:{expires_s}"
        expected = hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected) or int(expires_s) < int(time.time()):
            return None
        return int(user_s)
    except Exception:
        return None


def current_user_id(request: Request) -> Optional[int]:
    return read_cookie(request)


def should_generate_image(message: str) -> bool:
    q = message.casefold()
    triggers = ["görsel oluştur", "görsel üret", "resim oluştur", "resim çiz", "bir resim çiz", "fotoğraf oluştur", "fotoğraf üret", "çizim oluştur", "illüstrasyon oluştur", "afiş oluştur", "logo oluştur", "görselini yap", "resmini yap", "bana çiz", "generate an image", "create an image", "draw me", "make a picture"]
    return any(term in q for term in triggers)


def should_search_web(message: str) -> bool:
    q = message.casefold()
    triggers = ["internette ara", "internetten ara", "webde ara", "web araması", "kaynak göster", "kaynakları göster", "kaynakların neler", "hangi sitelere baktın", "internetten bul", "güncel", "şu an", "şuanda", "bugün", "dün", "son dakika", "en son", "son haberler", "haberleri", "haberler", "2026", "fiyatı", "fiyatları", "kaç para", "döviz", "kur kaç", "hava durumu", "hava nasıl", "hava kaç derece", "maç sonucu", "puan durumu", "seçim sonucu", "kim kazandı", "ne zaman çıkacak", "yayınlandı mı", "açık mı", "çalışıyor mu", "en iyi", "karşılaştır", "incele", "araştır", "resmî site", "resmi site", "linkini ver", "bağlantısını ver", "kaynakça", "kanıtları", "istatistikleri"]
    return any(term in q for term in triggers)


def generate_image(prompt: str):
    if not POLLINATIONS_API_KEY:
        return {"error": "Görsel servisi henüz yapılandırılmadı. Render Environment bölümüne POLLINATIONS_API_KEY ekle."}
    # Keep the key server-side; never return it to the browser.
    url = "https://gen.pollinations.ai/image/" + quote(prompt, safe="")
    try:
        r = requests.get(url, params={"model": IMAGE_MODEL, "width": 1024, "height": 1024, "nologo": "true"}, headers={"Authorization": f"Bearer {POLLINATIONS_API_KEY}"}, timeout=180)
        if r.status_code in (401, 403):
            return {"error": "Pollinations anahtarı kabul edilmedi veya erişim izni yok. Render'daki POLLINATIONS_API_KEY değerini kontrol et."}
        if r.status_code == 429:
            return {"error": "Görsel servisi şu anda kullanım sınırına ulaştı. Daha sonra tekrar dene."}
        r.raise_for_status()
        content_type = r.headers.get("content-type", "image/png").split(";")[0]
        if not content_type.startswith("image/"):
            return {"error": "Görsel servisi bir görsel döndürmedi. Anahtarın ve model ayarının doğru olduğunu kontrol et."}
        if len(r.content) > 12 * 1024 * 1024:
            return {"error": "Oluşturulan görsel beklenenden büyük. Lütfen tekrar dene."}
        encoded = base64.b64encode(r.content).decode("ascii")
        return {"reply": "İstediğin görsel hazır.", "image": f"data:{content_type};base64,{encoded}"}
    except requests.Timeout:
        return {"error": "Görsel oluşturmak uzun sürdü. Lütfen tekrar dene."}
    except requests.RequestException as exc:
        print("Image generation error:", repr(exc))
        return {"error": "Görsel servisine bağlanılamadı. Anahtar ve servis durumunu kontrol edip tekrar dene."}


def get_sources_and_context(message: str):
    if not should_search_web(message):
        return [], ""
    if not API_KEY:
        return [], "WEB_SEARCH_ERROR: Ollama API anahtarı ayarlanmamış."
    try:
        r = requests.post(OLLAMA_WEB_URL, headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}, json={"query": message, "max_results": 5}, timeout=35)
        if r.status_code == 401:
            return [], "WEB_SEARCH_ERROR: Ollama API anahtarı web araması için kabul edilmedi."
        if r.status_code == 429:
            return [], "WEB_SEARCH_ERROR: Web arama kullanım sınırına ulaşıldı."
        r.raise_for_status()
        results = r.json().get("results", [])
        sources = [{"title": str(item.get("title", "Kaynak"))[:250], "url": str(item.get("url", ""))} for item in results if str(item.get("url", "")).startswith(("https://", "http://"))][:5]
        context = "\n\n".join(f"Kaynak {i+1}: {item.get('title','')}\nURL: {item.get('url','')}\nÖzet: {item.get('content','')}" for i, item in enumerate(results[:5]))
        return sources, context or "Bu sorgu için arama sonucu bulunamadı."
    except Exception as exc:
        print("Web search error:", repr(exc))
        return [], "WEB_SEARCH_ERROR: Web araması şu anda tamamlanamadı."


def save_message(conn, chat_id: int, role: str, content: str):
    conn.execute("INSERT INTO tekden_messages(chat_id, role, content) VALUES (%s,%s,%s)", (chat_id, role, content))
    conn.execute("UPDATE tekden_chats SET updated_at=NOW() WHERE id=%s", (chat_id,))


def get_owned_chat(conn, chat_id: int, user_id: int):
    return conn.execute("SELECT id FROM tekden_chats WHERE id=%s AND user_id=%s", (chat_id, user_id)).fetchone()

@app.get("/", response_class=HTMLResponse)
def home():
    return HTML_PAGE

@app.get("/api/health")
def health():
    return {"ok": bool(API_KEY), "provider": "Ollama Cloud", "model": MODEL_NAME, "web_search": bool(API_KEY), "image_configured": bool(POLLINATIONS_API_KEY), "database_configured": bool(DATABASE_URL), "message": "Ollama Cloud ayarlandı." if API_KEY else "Render ortam değişkenlerinde OLLAMA_API_KEY eksik."}

@app.post("/api/register")
def register(data: AuthRequest, response: Response):
    if not SESSION_SECRET:
        raise HTTPException(status_code=503, detail="SESSION_SECRET ayarlanmamış. Render Environment bölümüne ekle.")
    username = data.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise HTTPException(status_code=400, detail="Kullanıcı adı 3-32 karakter olmalı; yalnızca harf, rakam, nokta, tire ve alt çizgi kullanabilirsin.")
    with db_connect() as conn:
        exists = conn.execute("SELECT id FROM tekden_users WHERE LOWER(username)=LOWER(%s)", (username,)).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="Bu kullanıcı adı zaten alınmış.")
        row = conn.execute("INSERT INTO tekden_users(username,password_hash) VALUES (%s,%s) RETURNING id, username", (username, password_hash(data.password))).fetchone()
    response.set_cookie(COOKIE_NAME, make_cookie(row["id"]), httponly=True, secure=True, samesite="lax", max_age=SESSION_SECONDS, path="/")
    return {"ok": True, "user": {"id": row["id"], "username": row["username"]}}

@app.post("/api/login")
def login(data: AuthRequest, response: Response):
    if not SESSION_SECRET:
        raise HTTPException(status_code=503, detail="SESSION_SECRET ayarlanmamış. Render Environment bölümüne ekle.")
    with db_connect() as conn:
        row = conn.execute("SELECT id, username, password_hash FROM tekden_users WHERE LOWER(username)=LOWER(%s)", (data.username.strip(),)).fetchone()
    if not row or not password_matches(data.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Kullanıcı adı veya şifre yanlış.")
    response.set_cookie(COOKIE_NAME, make_cookie(row["id"]), httponly=True, secure=True, samesite="lax", max_age=SESSION_SECONDS, path="/")
    return {"ok": True, "user": {"id": row["id"], "username": row["username"]}}

@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")
    return {"ok": True}

@app.get("/api/me")
def me(request: Request):
    user_id = current_user_id(request)
    if not user_id:
        return {"authenticated": False}
    with db_connect() as conn:
        row = conn.execute("SELECT id, username FROM tekden_users WHERE id=%s", (user_id,)).fetchone()
    return {"authenticated": bool(row), "user": {"id": row["id"], "username": row["username"]} if row else None}

@app.get("/api/chats")
def list_chats(request: Request):
    user_id = current_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Geçmiş sohbetler için giriş yapmalısın.")
    with db_connect() as conn:
        rows = conn.execute("SELECT id, title, created_at, updated_at FROM tekden_chats WHERE user_id=%s ORDER BY updated_at DESC LIMIT 100", (user_id,)).fetchall()
    return {"chats": [{"id": r["id"], "title": r["title"], "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat()} for r in rows]}

@app.get("/api/chats/{chat_id}")
def read_chat(chat_id: int, request: Request):
    user_id = current_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Bu sohbeti açmak için giriş yapmalısın.")
    with db_connect() as conn:
        chat = conn.execute("SELECT id, title FROM tekden_chats WHERE id=%s AND user_id=%s", (chat_id, user_id)).fetchone()
        if not chat:
            raise HTTPException(status_code=404, detail="Sohbet bulunamadı.")
        rows = conn.execute("SELECT role, content FROM tekden_messages WHERE chat_id=%s ORDER BY id", (chat_id,)).fetchall()
    messages = []
    for r in rows:
        content = r["content"]
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and parsed.get("kind") == "image":
                messages.append({"role": r["role"], "content": parsed.get("text", ""), "image": parsed.get("image")})
                continue
        except Exception:
            pass
        messages.append({"role": r["role"], "content": content})
    return {"chat": {"id": chat["id"], "title": chat["title"]}, "messages": messages}

@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: int, request: Request):
    user_id = current_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Sohbet silmek için giriş yapmalısın.")
    with db_connect() as conn:
        result = conn.execute("DELETE FROM tekden_chats WHERE id=%s AND user_id=%s", (chat_id, user_id))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Sohbet bulunamadı.")
    return {"ok": True}

@app.post("/api/chat")
def chat(data: ChatRequest, request: Request):
    message = data.message.strip()
    if not message:
        return {"reply": "Bir mesaj yazıp tekrar dener misin?"}
    user_id = current_user_id(request)
    chat_id = data.chat_id
    # Signed-in chats are persisted. Guest chats are never written to the database.
    conn = None
    if user_id:
        conn = db_connect()
        try:
            if chat_id is not None:
                if not get_owned_chat(conn, chat_id, user_id):
                    raise HTTPException(status_code=404, detail="Bu sohbet bulunamadı veya sana ait değil.")
            else:
                title = re.sub(r"\s+", " ", message)[:70] or "Yeni sohbet"
                row = conn.execute("INSERT INTO tekden_chats(user_id,title) VALUES (%s,%s) RETURNING id", (user_id, title)).fetchone()
                chat_id = row["id"]
            save_message(conn, chat_id, "user", message)
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
    try:
        if should_generate_image(message):
            result = generate_image(message)
            if conn and not result.get("error"):
                save_message(conn, chat_id, "assistant", json.dumps({"kind": "image", "text": result.get("reply", "İstediğin görsel hazır."), "image": result.get("image", "")}, ensure_ascii=False))
                conn.commit()
            result["chat_id"] = chat_id if user_id else None
            result["saved"] = bool(user_id)
            return result
        if not API_KEY:
            result = {"error": "Sunucu ayarı eksik: Render'da OLLAMA_API_KEY ortam değişkenini ekle."}
            return result
        sources, search_context = get_sources_and_context(message)
        if search_context.startswith("WEB_SEARCH_ERROR:"):
            # Don't stop chat entirely if search fails; tell the model the limitation.
            search_context = "Web araması yapılamadı: " + search_context.split(":", 1)[1].strip()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if should_search_web(message):
            messages.append({"role": "system", "content": "Kullanıcı güncel bilgi veya web araştırması istiyor. Aşağıdaki arama sonuçlarını dikkatle kullan; desteklenmeyen ayrıntıları uydurma. Kaynak bağlantıları arayüzde ayrıca gösterilecek.\n\n" + search_context})
        messages.append({"role": "user", "content": message})
        r = requests.post(OLLAMA_URL, headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}, json={"model": MODEL_NAME, "stream": False, "messages": messages, "options": {"temperature": 0.7}}, timeout=180)
        if r.status_code == 401:
            return {"error": "Ollama API anahtarı kabul edilmedi. Render'daki OLLAMA_API_KEY değerini kontrol et."}
        if r.status_code == 429:
            return {"error": "Ollama kullanım sınırına ulaşıldı veya kota geçici olarak doldu."}
        if r.status_code == 404:
            return {"error": f"Model bulunamadı: {MODEL_NAME}. OLLAMA_MODEL değerini kontrol et."}
        r.raise_for_status()
        reply = r.json().get("message", {}).get("content", "").strip() or "Şu anda yanıt oluşturamadım. Bir kez daha dener misin?"
        if conn:
            save_message(conn, chat_id, "assistant", reply)
            conn.commit()
        return {"reply": reply, "sources": sources, "chat_id": chat_id if user_id else None, "saved": bool(user_id)}
    except requests.Timeout:
        return {"error": "İstek çok uzun sürdü. Lütfen tekrar dene."}
    except requests.RequestException as exc:
        print("Chat request error:", repr(exc))
        return {"error": "Sohbet servisine bağlanılamadı. API anahtarını, modeli ve kullanım kotanı kontrol et."}
    finally:
        if conn:
            conn.close()

HTML_PAGE = r'''<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Tekden AI</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#111a2e;--panel2:#17233b;--line:#263653;--text:#edf3ff;--muted:#a8b6d1;--accent:#7c9cff;--accent2:#9d7cff}*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at top,#1b2b4b 0,#0b1020 55%);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}.layout{height:100dvh;min-height:550px;display:flex;max-width:1400px;margin:auto}.sidebar{width:265px;flex-shrink:0;background:#0c1426;border-right:1px solid var(--line);padding:14px;display:flex;flex-direction:column;gap:10px}.brand{display:flex;align-items:center;gap:10px;padding:5px 2px 14px}.logo{width:42px;height:42px;border-radius:14px;background:linear-gradient(135deg,var(--accent),var(--accent2));display:grid;place-items:center;font-size:22px;font-weight:800}.brand h1{font-size:19px;margin:0}.brand p{font-size:11px;color:var(--muted);margin:0}.btn{font:inherit;cursor:pointer;border:1px solid var(--line);color:var(--text);background:var(--panel2);border-radius:12px;padding:10px 12px;text-align:left}.btn:hover{filter:brightness(1.15)}.primary{background:linear-gradient(135deg,var(--accent),var(--accent2));border:0;font-weight:700;text-align:center}.chatlist{overflow:auto;display:flex;flex-direction:column;gap:5px}.chatitem{display:flex;gap:6px;align-items:center}.chatitem .openchat{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;border:0;background:transparent;padding:9px;text-align:left;font-size:13px}.chatitem .deletechat{padding:5px 8px;border:0;background:transparent;color:#a8b6d1}.sidebottom{margin-top:auto;display:grid;gap:8px}.small{font-size:12px;color:var(--muted)}.main{flex:1;min-width:0;display:flex;flex-direction:column;padding:15px 22px 12px}.topbar{display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--line);padding:4px 0 14px}.topbar .spacer{flex:1}#status{font-size:12px;color:var(--muted);padding:9px 2px}#messages{flex:1;overflow:auto;padding:14px 2px 24px;display:flex;flex-direction:column;gap:18px;scroll-behavior:smooth}.welcome{margin:auto;text-align:center;max-width:580px;padding:26px}.welcome .biglogo{font-size:44px}.welcome h2{font-size:28px;margin:10px 0}.welcome p{color:var(--muted)}.suggestions{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-top:20px}.suggestions .btn{font-size:13px}.message{display:flex;gap:10px;max-width:90%;align-items:flex-start}.message.user{align-self:flex-end;flex-direction:row-reverse}.avatar{flex:0 0 34px;height:34px;border-radius:11px;background:var(--panel2);display:grid;place-items:center;font-size:15px}.message.user .avatar{background:#314a83}.bubble{padding:12px 15px;border:1px solid var(--line);background:var(--panel);border-radius:5px 16px 16px 16px;white-space:pre-wrap;overflow-wrap:anywhere;min-width:0}.message.user .bubble{background:#20345b;border-radius:16px 5px 16px 16px}.bubble img{display:block;width:100%;max-width:640px;height:auto;margin-top:10px;border-radius:12px;border:1px solid var(--line)}.sources{margin-top:8px;padding:10px 12px;border:1px solid var(--line);border-radius:12px;background:#0d1528;font-size:13px}.sources strong{display:block;margin-bottom:6px}.sources a{display:block;color:#a9bcff;text-decoration:none;overflow-wrap:anywhere;margin:5px 0}.composer{border:1px solid var(--line);background:#111a2eef;border-radius:18px;padding:10px;display:flex;gap:10px;align-items:flex-end;box-shadow:0 12px 35px #0003}.composer textarea{flex:1;resize:none;max-height:180px;min-height:46px;border:0;outline:0;background:transparent;color:var(--text);font:inherit;padding:11px}.composer button{font:inherit;cursor:pointer;border:0;border-radius:12px;padding:11px 15px;background:linear-gradient(135deg,var(--accent),var(--accent2));font-weight:700;color:white}.composer button:disabled{opacity:.5}.note{text-align:center;color:var(--muted);font-size:11px;padding:9px 0 0}.guestnote{font-size:12px;color:#e6c98a;margin:0 0 8px}.modalback{position:fixed;inset:0;background:#0009;display:none;align-items:center;justify-content:center;padding:16px;z-index:10}.modal{width:min(100%,400px);background:#111a2e;border:1px solid var(--line);border-radius:18px;padding:22px;box-shadow:0 20px 70px #0008}.modal h2{margin:0 0 6px}.modal p{color:var(--muted);font-size:13px}.modal input{display:block;width:100%;margin:10px 0;padding:12px;background:#0b1020;border:1px solid var(--line);border-radius:10px;color:var(--text);font:inherit}.modalactions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.modalactions button{flex:1}.authstatus{font-size:13px;color:#ffb4b4;min-height:20px}.mobile-new{display:none}
@media(max-width:760px){.layout{min-height:0}.sidebar{width:205px;padding:9px}.main{padding:10px}.message{max-width:98%}.brand h1{font-size:16px}.brand p{display:none}.topbar .mobile-new{display:block}.sidebar .brand{padding-bottom:7px}.btn{padding:9px;font-size:13px}}
@media(max-width:540px){.sidebar{width:62px;padding:7px;align-items:stretch}.sidebar .brand{justify-content:center}.sidebar .brandtext,.sidebar .side-label,.sidebar .chatlist,.sidebar .sidebottom .small{display:none}.sidebar .logo{width:42px}.sidebar .btn{font-size:0;text-align:center;padding:10px 3px}.sidebar .btn::first-letter{font-size:17px}.sidebar #loginBtn,.sidebar #logoutBtn{font-size:0}.sidebar #loginBtn::after{content:'Giriş';font-size:11px}.sidebar #logoutBtn::after{content:'Çıkış';font-size:11px}.main{padding:8px}.topbar{padding-bottom:9px}.topbar h2{font-size:15px;margin:0}.welcome{padding:10px}.welcome h2{font-size:23px}}
</style></head><body>
<div class="layout"><aside class="sidebar"><div class="brand"><div class="logo">T</div><div class="brandtext"><h1>Tekden AI</h1><p>Ahmet Kerim tarafından geliştirildi</p></div></div><button class="btn" id="newChat">＋ Yeni sohbet</button><div class="side-label small">GEÇMİŞ SOHBETLER</div><div id="chatlist" class="chatlist"><div class="small">Geçmişi görmek için giriş yap.</div></div><div class="sidebottom"><div id="accountLabel" class="small">Oturumsuz kullanım</div><button class="btn" id="loginBtn">Giriş yap / Kayıt ol</button><button class="btn" id="logoutBtn" style="display:none">Çıkış yap</button></div></aside>
<main class="main"><div class="topbar"><button class="btn mobile-new" id="mobileNew">＋</button><div><h2 style="margin:0">Sohbet</h2><div class="small" id="modeLabel">Misafir sohbeti · kaydedilmez</div></div><div class="spacer"></div><button class="btn" id="topAuth">Giriş yap</button></div><div id="status">Bağlantılar kontrol ediliyor…</div><main id="messages" aria-live="polite"><section class="welcome" id="welcome"><div class="biglogo">✦</div><h2>Merhaba, ben Tekden AI.</h2><p>İnternette araştırabilir, sorularını yanıtlayabilir ve görsel oluşturabilirim.</p><p class="guestnote">Misafir sohbetleri kaydedilmez. Geçmişini saklamak için giriş yap.</p><div class="suggestions"><button class="btn" data-prompt="Python öğrenmeye nereden başlamalıyım?">Python öğren</button><button class="btn" data-prompt="Bana yaratıcı bir proje fikri öner.">Proje fikri bul</button><button class="btn" data-prompt="Yapay zekâ nasıl çalışır? Basitçe anlat.">Yapay zekâyı keşfet</button></div></section></main>
<form id="form" class="composer"><textarea id="input" placeholder="Tekden AI'ya bir mesaj yaz…" rows="1" aria-label="Mesaj"></textarea><button id="send" type="submit">Gönder ↑</button></form><div class="note">Tekden AI · Güncel bilgi gerektiğinde web araması · Görsel oluşturma</div></main></div>
<div class="modalback" id="authModal"><form class="modal" id="authForm"><h2 id="authTitle">Tekden AI hesabı</h2><p>Giriş yaparak sohbet geçmişini kaydedebilir ve daha sonra devam edebilirsin.</p><input id="username" autocomplete="username" placeholder="Kullanıcı adı" minlength="3" maxlength="32" required><input id="password" type="password" autocomplete="current-password" placeholder="Şifre (en az 8 karakter)" minlength="8" maxlength="128" required><div class="authstatus" id="authStatus"></div><div class="modalactions"><button class="btn primary" type="submit" id="authSubmit">Giriş yap</button><button class="btn" type="button" id="switchAuth">Hesap oluştur</button><button class="btn" type="button" id="closeAuth">Kapat</button></div></form></div>
<script>
const messagesEl=document.getElementById('messages'), input=document.getElementById('input'), sendBtn=document.getElementById('send'), statusEl=document.getElementById('status');
let busy=false, me=null, activeChatId=null, authMode='login';
function welcomeHTML(){return '<section class="welcome" id="welcome"><div class="biglogo">✦</div><h2>Merhaba, ben Tekden AI.</h2><p>İnternette araştırabilir, sorularını yanıtlayabilir ve görsel oluşturabilirim.</p><p class="guestnote">Misafir sohbetleri kaydedilmez. Geçmişini saklamak için giriş yap.</p><div class="suggestions"><button class="btn" data-prompt="Python öğrenmeye nereden başlamalıyım?">Python öğren</button><button class="btn" data-prompt="Bana yaratıcı bir proje fikri öner.">Proje fikri bul</button><button class="btn" data-prompt="Yapay zekâ nasıl çalışır? Basitçe anlat.">Yapay zekâyı keşfet</button></div></section>'}
function resetMessages(){messagesEl.innerHTML=welcomeHTML(); bindSuggestions();}
function addMessage(text,who,image=null){const w=document.getElementById('welcome');if(w)w.remove();const row=document.createElement('div');row.className='message '+(who==='user'?'user':'assistant');const avatar=document.createElement('div');avatar.className='avatar';avatar.textContent=who==='user'?'S':'T';const bubble=document.createElement('div');bubble.className='bubble';bubble.textContent=text||'';if(image){const img=document.createElement('img');img.src=image;img.alt='Oluşturulan görsel';bubble.appendChild(img)}row.append(avatar,bubble);messagesEl.appendChild(row);messagesEl.scrollTop=messagesEl.scrollHeight;return bubble}
function addSources(sources){if(!Array.isArray(sources)||!sources.length)return;const box=document.createElement('div');box.className='sources';const strong=document.createElement('strong');strong.textContent='Kaynaklar';box.appendChild(strong);sources.forEach(s=>{const a=document.createElement('a');a.href=s.url;a.target='_blank';a.rel='noopener noreferrer';a.textContent=s.title||s.url;box.appendChild(a)});messagesEl.appendChild(box)}
function safeError(data){return data?.detail||data?.error||'Bir hata oluştu.'}
async function sendMessage(text){text=text.trim();if(!text||busy)return;addMessage(text,'user');input.value='';input.style.height='46px';busy=true;sendBtn.disabled=true;const waiting=addMessage('Düşünüyorum…','assistant');try{const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text,chat_id:activeChatId})});const data=await r.json();if(!r.ok||data.error)throw new Error(safeError(data));waiting.textContent=data.reply||'Yanıt alınamadı.';if(data.image){const img=document.createElement('img');img.src=data.image;img.alt='Oluşturulan görsel';waiting.appendChild(img)}if(data.sources?.length)addSources(data.sources);if(me&&data.chat_id){activeChatId=data.chat_id;await loadChats()}}catch(e){waiting.textContent='⚠️ '+(e.message||'İstek başarısız oldu.')}finally{busy=false;sendBtn.disabled=false;input.focus();messagesEl.scrollTop=messagesEl.scrollHeight}}
document.getElementById('form').addEventListener('submit',e=>{e.preventDefault();sendMessage(input.value)});input.addEventListener('input',()=>{input.style.height='46px';input.style.height=Math.min(input.scrollHeight,180)+'px'});input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();document.getElementById('form').requestSubmit()}});
function bindSuggestions(){document.querySelectorAll('[data-prompt]').forEach(b=>b.onclick=()=>sendMessage(b.dataset.prompt))}bindSuggestions();
async function newChat(){activeChatId=null;resetMessages();input.value='';input.focus()};document.getElementById('newChat').onclick=newChat;document.getElementById('mobileNew').onclick=newChat;
function showAuth(){document.getElementById('authModal').style.display='flex';document.getElementById('authStatus').textContent='';document.getElementById('username').focus()}function closeAuth(){document.getElementById('authModal').style.display='none'}document.getElementById('loginBtn').onclick=showAuth;document.getElementById('topAuth').onclick=()=>me?newChat():showAuth();document.getElementById('closeAuth').onclick=closeAuth;document.getElementById('authModal').onclick=e=>{if(e.target.id==='authModal')closeAuth()};
function setAuthMode(mode){authMode=mode;document.getElementById('authTitle').textContent=mode==='login'?'Tekden AI hesabına giriş':'Tekden AI hesabı oluştur';document.getElementById('authSubmit').textContent=mode==='login'?'Giriş yap':'Kayıt ol';document.getElementById('switchAuth').textContent=mode==='login'?'Hesap oluştur':'Giriş yap';document.getElementById('password').autocomplete=mode==='login'?'current-password':'new-password'}document.getElementById('switchAuth').onclick=()=>setAuthMode(authMode==='login'?'register':'login');
document.getElementById('authForm').addEventListener('submit',async e=>{e.preventDefault();const status=document.getElementById('authStatus');status.textContent='İşleniyor…';const username=document.getElementById('username').value.trim(),password=document.getElementById('password').value;try{const r=await fetch('/api/'+(authMode==='login'?'login':'register'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})});const d=await r.json();if(!r.ok)throw new Error(safeError(d));me=d.user;closeAuth();document.getElementById('password').value='';updateAuthUI();await loadChats();await newChat()}catch(err){status.textContent=err.message||'İşlem başarısız.'}});
async function logout(){await fetch('/api/logout',{method:'POST'});me=null;activeChatId=null;updateAuthUI();resetMessages();document.getElementById('chatlist').innerHTML='<div class="small">Geçmişi görmek için giriş yap.</div>'}
document.getElementById('logoutBtn').onclick=logout;
function updateAuthUI(){document.getElementById('accountLabel').textContent=me?'Hesap: '+me.username:'Oturumsuz kullanım';document.getElementById('loginBtn').style.display=me?'none':'block';document.getElementById('logoutBtn').style.display=me?'block':'none';document.getElementById('topAuth').textContent=me?'＋ Yeni sohbet':'Giriş yap';document.getElementById('modeLabel').textContent=me?'Giriş yapıldı · sohbetler kaydedilir':'Misafir sohbeti · kaydedilmez'}
async function loadChats(){const list=document.getElementById('chatlist');if(!me){list.innerHTML='<div class="small">Geçmişi görmek için giriş yap.</div>';return}try{const r=await fetch('/api/chats');const d=await r.json();if(!r.ok)throw new Error(safeError(d));list.innerHTML='';if(!d.chats.length){list.innerHTML='<div class="small">Henüz kayıtlı sohbet yok.</div>';return}d.chats.forEach(c=>{const row=document.createElement('div');row.className='chatitem';const b=document.createElement('button');b.className='btn openchat';b.textContent=c.title;b.title=c.title;b.onclick=()=>openChat(c.id);const del=document.createElement('button');del.className='btn deletechat';del.textContent='×';del.title='Sohbeti sil';del.onclick=async()=>{if(!confirm('Bu sohbet silinsin mi?'))return;await fetch('/api/chats/'+c.id,{method:'DELETE'});if(activeChatId===c.id)newChat();await loadChats()};row.append(b,del);list.appendChild(row)})}catch(e){list.innerHTML='<div class="small">Geçmiş yüklenemedi.</div>'}}
async function openChat(id){try{const r=await fetch('/api/chats/'+id);const d=await r.json();if(!r.ok)throw new Error(safeError(d));activeChatId=id;messagesEl.innerHTML='';d.messages.forEach(m=>addMessage(m.content,m.role==='user'?'user':'assistant',m.image||null));if(!d.messages.length)resetMessages();messagesEl.scrollTop=messagesEl.scrollHeight}catch(e){alert(e.message||'Sohbet açılamadı.')}}
async function boot(){try{const [hr,mr]=await Promise.all([fetch('/api/health'),fetch('/api/me')]);const h=await hr.json(),m=await mr.json();me=m.authenticated?m.user:null;updateAuthUI();await loadChats();statusEl.textContent=(h.ok?'● Ollama Cloud bağlı':'● Ollama Cloud anahtarını kontrol et')+' · '+(h.image_configured?'görsel servisi hazır':'görsel servisi ayarlanmalı')+' · '+(h.database_configured?'veritabanı ayarlı':'veritabanı ayarlanmalı');if(me)document.getElementById('modeLabel').textContent='Giriş yapıldı · sohbetler kaydedilir'}catch(e){statusEl.textContent='Bağlantı durumu kontrol edilemedi.'}}boot();
</script></body></html>'''


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
                  "messages": [{"role":"system","content":"Sen Tekden AI'sın. Türkçe, anlaşılır ve nazik cevap ver. İstenildği zama tüm dilleri konuş. Geliştiricin Ahmet Kerim Kırgız."},
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
