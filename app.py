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
OLLAMA_PRO_MODEL = os.getenv("OLLAMA_PRO_MODEL", "gpt-oss:120b-cloud").strip()
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "AhmetKerim.").strip()
OWNER_PASSWORD = os.getenv("OWNER_PASSWORD", "").strip()
POLLINATIONS_API_KEY = os.getenv("POLLINATIONS_API_KEY", "").strip()
POLLINATIONS_IMAGE_MODEL = os.getenv("POLLINATIONS_IMAGE_MODEL", "flux").strip()
# Hugging Face free-tier credits are limited and may change.
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_PRICE_ID_PRO = os.getenv("STRIPE_PRICE_ID_PRO", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
PUBLIC_APP_URL = os.getenv("PUBLIC_APP_URL", "").strip().rstrip("/")
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
            plan TEXT NOT NULL DEFAULT 'free',
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
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
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'free'")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chats_user_updated ON chats(user_id, updated_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id, id)")
        # Owner account is provisioned from Render environment variables, never from a hard-coded password.
        if OWNER_PASSWORD and len(OWNER_PASSWORD) >= 8 and OWNER_USERNAME:
            owner = conn.execute("SELECT id FROM users WHERE lower(username)=lower(%s)", (OWNER_USERNAME,)).fetchone()
            if owner:
                conn.execute("UPDATE users SET plan='pro', password_hash=%s WHERE id=%s", (hash_password(OWNER_PASSWORD), owner["id"]))
            else:
                conn.execute("INSERT INTO users(username,password_hash,plan) VALUES(%s,%s,'pro')", (OWNER_USERNAME, hash_password(OWNER_PASSWORD)))

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
        return conn.execute("SELECT id, username, plan FROM users WHERE id=%s", (uid,)).fetchone()

def web_search(query: str):
    """Search the web with deeper results and a resilient fallback chain."""
    query = query.strip()
    if not query:
        return []
    if TAVILY_API_KEY:
        try:
            r = requests.post(
                "https://api.tavily.com/search",
                json={"api_key": TAVILY_API_KEY, "query": query, "search_depth": "advanced",
                      "max_results": 8, "include_answer": False, "include_raw_content": False,
                      "topic": "general"},
                timeout=25,
            )
            r.raise_for_status()
            data = r.json()
            results = []
            for x in data.get("results", []):
                url = x.get("url", "")
                if url.startswith("http"):
                    results.append({"title": x.get("title", "Untitled source"), "url": url,
                                    "snippet": x.get("content", ""), "score": x.get("score")})
            if results:
                return results[:8]
        except Exception:
            pass
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=8, safesearch="moderate"))
        results = []
        for x in raw:
            url = x.get("href", x.get("url", ""))
            if url.startswith("http"):
                results.append({"title": x.get("title", "Untitled source"), "url": url,
                                "snippet": x.get("body", x.get("snippet", ""))})
        if results:
            return results[:8]
    except Exception:
        pass
    try:
        from html.parser import HTMLParser
        from urllib.parse import urlparse, parse_qs, unquote
        class DDGParser(HTMLParser):
            def __init__(self):
                super().__init__(); self.items=[]; self.current=None; self.capture=None
            def handle_starttag(self, tag, attrs):
                a=dict(attrs); cls=a.get("class", "")
                if tag == "a" and "result__a" in cls:
                    self.current={"title":"", "url":a.get("href", ""), "snippet":""}; self.capture="title"
                elif self.current is not None and tag in ("a", "div") and "result__snippet" in cls:
                    self.capture="snippet"
            def handle_data(self, data):
                if self.current is not None and self.capture in ("title", "snippet"):
                    self.current[self.capture] += data
            def handle_endtag(self, tag):
                if self.current is not None and tag == "a" and self.capture == "title":
                    self.items.append(self.current); self.current=None; self.capture=None
                elif self.current is not None and self.capture == "snippet" and tag == "div":
                    self.capture=None
        response=requests.get("https://html.duckduckgo.com/html/", params={"q":query},
                              headers={"User-Agent":"Mozilla/5.0 (compatible; TekdenAI/1.0)"}, timeout=18)
        response.raise_for_status(); parser=DDGParser(); parser.feed(response.text)
        cleaned=[]
        for item in parser.items:
            url=item.get("url", "")
            if "duckduckgo.com/l/?" in url:
                url=unquote(parse_qs(urlparse(url).query).get("uddg", [url])[0])
            if url.startswith("http") and item.get("title"):
                cleaned.append({"title":item["title"].strip(), "url":url,
                                "snippet":item.get("snippet", "").strip()})
            if len(cleaned)>=8: break
        return cleaned
    except Exception:
        return []

def should_search(text: str) -> bool:
    t = text.lower()
    cues = ["internette ara", "web'de ara", "webde ara", "güncel", "bugün", "son dakika",
            "en son", "kaynak göster", "kaynakları", "araştır", "2026", "şu an", "fiyatı ne", "internette ara", "web ara", "web araması", "internetten bul", "kaynaklarla cevapla",
            "latest", "current", "today", "search the web", "look up", "kaynaklarla araştır", "derin araştır", "detaylı araştır", "karşılaştır", "incele", "kimdir", "nedir", "ne zaman", "neden", "nasıl çalışır", "istatistik", "raporu", "bilimsel çalışma", "araştırma makalesi"]
    return any(x in t for x in cues) or t.strip().startswith("/search ")

def call_ollama(messages, model=None):
    if not OLLAMA_API_KEY:
        raise RuntimeError("OLLAMA_API_KEY eksik. Render Environment bölümüne Ollama Cloud API anahtarını ekle.")
    r = requests.post("https://ollama.com/api/chat",
        headers={"Authorization": f"Bearer {OLLAMA_API_KEY}", "Content-Type": "application/json"},
        json={"model": model or OLLAMA_MODEL, "messages": messages, "stream": False},
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

class SaveAnalysisBody(BaseModel):
    question: str = Field(min_length=1, max_length=12000)
    answer: str = Field(min_length=1, max_length=30000)
    filename: str = Field(default="", max_length=255)
    chat_id: str | None = None

@app.get("/health")
def health():
    return {"ok": True, "app": APP_NAME, "database_configured": bool(DATABASE_URL),
            "ollama_configured": bool(OLLAMA_API_KEY), "image_configured": True, "pro_model": OLLAMA_PRO_MODEL}

@app.post("/api/register")
def register(body: AuthBody, response: JSONResponse):
    username = body.username.strip()
    if OWNER_PASSWORD and username.lower() == OWNER_USERNAME.lower():
        raise HTTPException(409, "Bu kullanıcı adı ayrılmış.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise HTTPException(400, "Kullanıcı adı 3-32 karakter olmalı; harf, sayı, nokta, tire ve alt çizgi kullan.")
    if not SESSION_SECRET or len(SESSION_SECRET) < 32:
        raise HTTPException(500, "SESSION_SECRET eksik veya çok kısa.")
    try:
        with db() as conn:
            row = conn.execute("INSERT INTO users(username,password_hash) VALUES(%s,%s) RETURNING id, username, plan",
                               (username, hash_password(body.password))).fetchone()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "Bu kullanıcı adı zaten alınmış.")
    token = make_session(row["id"])
    out = JSONResponse({"ok": True, "user": {"id": row["id"], "username": row["username"], "plan": row.get("plan", "free")}})
    out.set_cookie(COOKIE_NAME, token, httponly=True, secure=True, samesite="lax", max_age=SESSION_DAYS*86400)
    return out

@app.post("/api/login")
def login(body: AuthBody):
    with db() as conn:
        row = conn.execute("SELECT id, username, password_hash, plan FROM users WHERE lower(username)=lower(%s)", (body.username.strip(),)).fetchone()
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(401, "Kullanıcı adı veya şifre hatalı.")
    out = JSONResponse({"ok": True, "user": {"id": row["id"], "username": row["username"], "plan": ("pro" if row["username"].lower()==OWNER_USERNAME.lower() and OWNER_PASSWORD else row.get("plan", "free"))}})
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
    if user and user.get("username", "").lower() == OWNER_USERNAME.lower() and OWNER_PASSWORD:
        user["plan"] = "pro"
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

@app.post("/api/save-analysis")
def save_analysis(body: SaveAnalysisBody, request: Request):
    user=current_user(request)
    if not user:
        return {"saved":False,"chat_id":None}
    import uuid
    chat_id=body.chat_id
    with db() as conn:
        if chat_id:
            row=conn.execute("SELECT id FROM chats WHERE id=%s AND user_id=%s",(chat_id,user["id"])).fetchone()
            if not row: chat_id=None
        if not chat_id:
            chat_id=str(uuid.uuid4())
            title=(body.question or ("Dosya analizi: "+body.filename))[:70] or "Dosya analizi"
            conn.execute("INSERT INTO chats(id,user_id,title) VALUES(%s,%s,%s)",(chat_id,user["id"],title))
        user_content=body.question + ("\n[Ekli dosya: "+body.filename+"]" if body.filename else "")
        conn.execute("INSERT INTO messages(chat_id,role,content) VALUES(%s,'user',%s)",(chat_id,user_content))
        conn.execute("INSERT INTO messages(chat_id,role,content) VALUES(%s,'assistant',%s)",(chat_id,body.answer))
        conn.execute("UPDATE chats SET updated_at=NOW() WHERE id=%s",(chat_id,))
    return {"saved":True,"chat_id":chat_id}

@app.post("/api/billing/checkout")
def billing_checkout(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Pro üyeliği satın almak için önce giriş yap.")
    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID_PRO:
        raise HTTPException(503, "Ödeme henüz etkinleştirilmedi. Stripe hesabı, aylık fiyat ürünü ve Render ortam değişkenleri yapılandırılmalı.")
    base = PUBLIC_APP_URL or str(request.base_url).rstrip("/")
    try:
        r = requests.post("https://api.stripe.com/v1/checkout/sessions",
            auth=(STRIPE_SECRET_KEY, ""), timeout=25,
            data={"mode":"subscription", "success_url":base+"/?payment=success",
                  "cancel_url":base+"/?payment=cancelled", "client_reference_id":str(user["id"]),
                  "line_items[0][price]":STRIPE_PRICE_ID_PRO, "line_items[0][quantity]":"1",
                  "metadata[user_id]":str(user["id"]), "subscription_data[metadata][user_id]":str(user["id"]),
                  "allow_promotion_codes":"true"})
        if not r.ok:
            raise HTTPException(502, "Ödeme sağlayıcısı oturum oluşturamadı. Stripe fiyat kimliği ve hesap ayarlarını kontrol et.")
        data=r.json()
        if not data.get("url"):
            raise HTTPException(502, "Ödeme sağlayıcısı yönlendirme adresi döndürmedi.")
        return {"url":data["url"]}
    except requests.RequestException:
        raise HTTPException(502, "Ödeme sağlayıcısına bağlanılamadı.")

@app.post("/api/billing/webhook")
async def billing_webhook(request: Request):
    # Stripe signs the raw request body. Never trust a client-supplied plan status.
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(503, "Stripe webhook secret yapılandırılmamış.")
    raw = await request.body()
    signature = request.headers.get("stripe-signature", "")
    parts = {}
    for item in signature.split(","):
        if "=" in item:
            k,v=item.split("=",1); parts.setdefault(k,[]).append(v)
    timestamp=(parts.get("t") or [""])[0]
    signatures=parts.get("v1") or []
    try:
        signed = timestamp.encode()+b"."+raw
        expected = hmac.new(STRIPE_WEBHOOK_SECRET.encode(), signed, hashlib.sha256).hexdigest()
        valid = abs(int(time.time())-int(timestamp)) < 300 and any(hmac.compare_digest(expected,x) for x in signatures)
    except Exception:
        valid=False
    if not valid:
        raise HTTPException(400, "Stripe webhook imzası doğrulanamadı.")
    try:
        event=json.loads(raw.decode("utf-8")); typ=event.get("type",""); obj=event.get("data",{}).get("object",{})
        uid=(obj.get("metadata") or {}).get("user_id") or obj.get("client_reference_id")
        if typ == "checkout.session.completed" and uid and obj.get("mode")=="subscription":
            with db() as conn:
                conn.execute("UPDATE users SET plan='pro', stripe_customer_id=%s, stripe_subscription_id=%s WHERE id=%s",
                             (obj.get("customer"), obj.get("subscription"), int(uid)))
        elif typ in ("customer.subscription.deleted", "customer.subscription.paused"):
            subid=obj.get("id")
            if subid:
                with db() as conn:
                    conn.execute("UPDATE users SET plan='free', stripe_subscription_id=NULL WHERE stripe_subscription_id=%s", (subid,))
        return {"received":True}
    except (ValueError, TypeError, KeyError):
        raise HTTPException(400, "Webhook olayı okunamadı.")

@app.get("/api/billing/status")
def billing_status(request: Request):
    user=current_user(request)
    if not user:
        return {"plan":"free", "authenticated":False}
    try:
        with db() as conn:
            row=conn.execute("SELECT plan FROM users WHERE id=%s", (user["id"],)).fetchone()
        return {"plan":(row or {}).get("plan","free"), "authenticated":True}
    except Exception:
        return {"plan":"free", "authenticated":True}

@app.post("/api/chat")
def chat(body: ChatBody, request: Request):
    user = current_user(request)
    user_message = body.message.strip()
    use_search = body.force_search or should_search(user_message)
    sources = web_search(user_message.replace("/search ", "", 1)) if use_search else []
    plan = "pro" if user and user.get("username", "").lower() == OWNER_USERNAME.lower() and OWNER_PASSWORD else (user or {}).get("plan", "free")
    if plan == "pro":
        plan_rules = """Kullanıcının planı Tekden AI Pro. Önce problemi doğru tanımla, belirsizlikleri ayır, çok adımlı görevlerde plan kur ve sonucu doğrula. Uygun olduğunda alternatifleri karşılaştır, artı/eksi ve uygulanabilir adımlar sun; web kaynakları mevcutsa kaynakları çapraz kontrol et. Dosya/görsel içeriğine sadık kal. Yanıtı doğal Türkçe ve temiz biçimlendirmeyle yaz. Kullanıcı kısa yanıt isterse kısa tut. Gerçekte çalıştırılmamış işlemleri yaptığını iddia etme. Pro modeli kullanılamazsa temel modele geçiş olabilir; bunu gerekiyorsa açıkça belirt."""
    else:
        plan_rules = """Kullanıcının planı Tekden AI Free. Doğru ve anlaşılır yanıt ver; basit soruları gereksiz uzatma. Karmaşık görevlerde temel açıklama ve ilk adımları sun. Kullanıcıyı yanıltma veya kaliteyi bilerek düşürme; gerçekten mevcut olmayan araçları ya da sınırsız kullanımı vaat etme."""
    system = """Sen Tekden AI adlı yapay zekâ asistanısın. Kimliğin ve ürün bilgilerin:
- Tekden AI uygulaması Ahmet Kerim Kırgız tarafından tasarlanmıştır.
- Tekden AI'ın oluşturulma tarihi sorulursa 11 Ekim 2026 olarak belirt.
- “Ahmet Kerim kim?” diye sorulursa şu açıklamayı ver: “Ahmet Kerim Kırgız, 15 Temmuz Şehitler Anadolu İmam Hatip Lisesinde okuyan ve Tekden AI programını tasarlayan genç bir teknoloji meraklısıdır.”
- Kendini OpenAI tarafından oluşturulmuş bir ürün olarak tanıtma. Sen Tekden AI uygulamasının asistanısın. Uygulamanın geliştiricisi Ahmet Kerim Kırgız'dır.
- Altyapı/model sağlayıcısı doğrudan sorulursa yanlış bilgi uydurma; uygulamanın geliştiricisi ile kullanılan model/servis sağlayıcısını birbirine karıştırma.

Yazım ve anlatım kuralları:
- Kullanıcının dilinde doğal, akıcı, özenli ve doğru yaz. Türkçe yanıtlarında Türkçe karakterleri doğru kullan.
- Kullanıcı yazım hatası, eksik harf, yanlış boşluk veya yaygın bir kelime hatası yaparsa bağlamdan kastını anlamaya çalış; anlam açıksa düzeltmeyi belirtmeden doğru isteğe yanıt ver. Birden fazla makul anlam varsa kısa bir soru sor.
- Gereksiz slash (/), kısa çizgi, dikey çizgi, ters tırnak, kod biçimi veya yapay ayraçlarla kelimeleri birbirine bağlama. Normal düzyazı kullan. İşaretleri yalnızca anlamlı olduklarında kullan; URL, kod, matematik veya gerekli listelerde doğru biçimi koru.
- Gereksiz İngilizce ifadeler, tekrarlayan başlıklar ve anlamsız semboller kullanma. Liste gerekiyorsa sade ve tutarlı biçimlendir.
- Bilmediğin bilgiyi uydurma. Kullanıcıya ait dosya veya görsel verildiyse onu gerçekten incelemeden içeriğini varsayma.
- Web kaynakları sağlandıysa yanıtını bu kaynaklara dayandır, kaynakların desteklemediği iddiaları kesin sunma ve yanıt sonunda kaynak URL'lerini belirt.
"""
    system += "\n\n" + plan_rules
    msgs = [{"role":"system","content":system}]
    if sources:
        source_text = "\n\n".join(f"[{i+1}] {x['title']}\nURL: {x['url']}\nÖzet: {x['snippet']}" for i,x in enumerate(sources))
        msgs.append({"role":"system","content":"Aşağıdaki internet arama sonuçlarını kanıt olarak değerlendir. Birden fazla kaynağı karşılaştır, tarih ve çelişkileri kontrol et, sonuçlarda bulunmayan ayrıntıları uydurma. Her önemli iddiayı [1], [2] gibi numaralarla kaynaklandır ve sonunda kısa kaynakça ver.\n"+source_text})
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
            if plan == "pro":
                try:
                    answer = call_ollama(msgs, OLLAMA_PRO_MODEL)
                except RuntimeError:
                    # A misconfigured/unavailable Pro model must not break chat; use the configured base model.
                    answer = call_ollama(msgs, OLLAMA_MODEL)
            else:
                answer = call_ollama(msgs, OLLAMA_MODEL)
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
    """Improve clarity and visual direction without changing the user's idea."""
    prompt = prompt.strip()
    return (
        "Create a high-quality image that faithfully follows the user's request. "
        "Prioritize a clear subject, strong composition, believable perspective, "
        "intentional framing, consistent proportions, detailed materials and textures, "
        "natural light and shadows, balanced colors, crisp focus, and professional finish. "
        "Keep the requested art style and subject; do not add unrelated objects or themes. "
        "Avoid blur, visual glitches, duplicated objects, malformed anatomy, random lettering, "
        "logos, and watermarks. Only include readable text if the user explicitly requests it. "
        "User's original request: " + prompt
    )


@app.post("/api/image")
def image(body: ImageBody):
    # Hugging Face Inference Providers currently includes a small monthly credit
    # allowance for free accounts; this is limited, not unlimited free generation.
    if not HF_TOKEN:
        raise HTTPException(
            503,
            "Görsel üretimi için Render Environment bölümüne HF_TOKEN ekle. "
            "Hugging Face ücretsiz hesabındaki aylık kullanım kredisi sınırlıdır."
        )

    enhanced_prompt = improve_image_prompt(body.prompt)
    url = "https://router.huggingface.co/hf-inference/models/stabilityai/stable-diffusion-3-medium-diffusers"
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {HF_TOKEN}", "Accept": "image/*"},
            json={"inputs": enhanced_prompt},
            timeout=180,
        )
        if not r.ok:
            if r.status_code in (401, 403):
                raise HTTPException(502, "Hugging Face anahtarı kabul edilmedi veya model erişimi yok. HF_TOKEN değerini kontrol et.")
            if r.status_code == 429:
                raise HTTPException(429, "Ücretsiz görsel üretim kotası dolmuş olabilir. Bir süre sonra yeniden dene.")
            if r.status_code == 402:
                raise HTTPException(402, "Hugging Face ücretsiz kredisi tükenmiş olabilir; hesabındaki kullanım durumunu kontrol et.")
            raise HTTPException(502, "Görsel servisi hata verdi: " + r.text[:300])

        content_type = r.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            raise HTTPException(502, "Görsel servisi görsel yerine beklenmeyen yanıt döndürdü.")
        data = base64.b64encode(r.content).decode("ascii")
        return {"image_data": f"data:{content_type};base64,{data}", "model": "stabilityai/stable-diffusion-3-medium-diffusers"}
    except requests.RequestException as e:
        raise HTTPException(502, f"Görsel servisine bağlanılamadı: {str(e)[:200]}")

HTML = r"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tekden AI</title><style>
:root{color-scheme:light;--bg:#f3f7ff;--panel:#ffffff;--panel2:#e9f0ff;--text:#172033;--muted:#5e6b82;--accent:#6842df;--line:#d5def0}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}button,input,textarea{font:inherit}button{cursor:pointer;border:0;border-radius:10px;padding:10px 14px;background:var(--accent);color:white;font-weight:600}button.secondary{background:var(--panel2);border:1px solid var(--line)}button:disabled{opacity:.5;cursor:wait}
.layout{display:grid;grid-template-columns:270px 1fr;height:100dvh}.sidebar{background:#fbfdff;border-right:1px solid var(--line);padding:16px;display:flex;flex-direction:column;gap:12px;min-height:0}.brand{font-size:22px;font-weight:800;letter-spacing:-.6px}.brand span{color:#a78bfa}.sideactions{display:grid;gap:8px}.history{overflow:auto;display:grid;gap:5px}.history button{background:transparent;text-align:left;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.history button:hover{background:var(--panel2)}.userbar{margin-top:auto;border-top:1px solid var(--line);padding-top:12px}.main{display:flex;flex-direction:column;min-width:0;min-height:0}.top{padding:15px 22px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}.top small{color:var(--muted)}.messages{flex:1;overflow:auto;padding:26px max(20px,calc((100% - 820px)/2));display:flex;flex-direction:column;gap:20px}.welcome{margin:auto;text-align:center;max-width:650px}.welcome h1{font-size:clamp(28px,4vw,42px);letter-spacing:-1px;margin:0 0 8px}.welcome p{color:var(--muted)}.bubble{max-width:850px;padding:14px 16px;border-radius:16px;white-space:pre-wrap;overflow-wrap:anywhere}.bubble.user{align-self:flex-end;background:#e8e1ff;color:#24164f}.bubble.assistant{align-self:flex-start;background:#ffffff;border:1px solid var(--line);box-shadow:0 4px 18px #536b9b0d;width:min(100%,850px)}.bubble img{max-width:100%;border-radius:12px;margin-top:8px}.sources{font-size:13px;color:var(--muted);margin-top:10px}.sources a{color:#7651e8}.composerwrap{padding:12px 20px 20px}.composer{max-width:850px;margin:auto;background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:10px;box-shadow:0 12px 38px #536b9b18}.composer textarea{width:100%;resize:vertical;min-height:55px;max-height:180px;background:transparent;border:0;outline:0;color:var(--text);padding:8px}.tools{display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap}.toolleft{display:flex;gap:8px;align-items:center}.status{font-size:12px;color:var(--muted);padding:0 5px}.attachrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:0 8px 8px}.attachlabel{display:inline-flex;gap:6px;align-items:center;background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:7px 10px;cursor:pointer}.attachname{font-size:12px;color:#7651e8;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.plans{display:grid;gap:10px}.plan{border:1px solid var(--line);border-radius:14px;padding:16px;background:#f8faff}.plan:last-child{border:2px solid #8b6cf0;background:linear-gradient(135deg,#f5f0ff,#ffffff)}.plan strong{font-size:18px}.plan p{margin:6px 0;color:var(--muted);font-size:13px}.modalback{position:fixed;inset:0;background:#0009;display:none;align-items:center;justify-content:center;padding:16px}.modal{width:min(420px,100%);background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:22px}.modal h2{margin-top:0}.modal input{display:block;width:100%;padding:12px;margin:9px 0;background:#ffffff;color:var(--text);border:1px solid var(--line);border-radius:10px}.modal .row{display:flex;gap:8px}.modal .row button{flex:1}.notice{color:var(--muted);font-size:12px}.mobile{display:none}@media(max-width:720px){.layout{grid-template-columns:1fr}.sidebar{display:none}.mobile{display:inline-block}.messages{padding:20px 14px}.top{padding:12px 14px}.composerwrap{padding:8px 10px 12px}}
</style></head><body>
<div class="layout"><aside class="sidebar"><div class="brand">Tekden <span>AI</span></div><div class="notice" style="background:#f0ebff;color:#5334bb;padding:10px 12px;border-radius:12px">Daha aydınlık, daha akıllı bir sohbet alanı</div>
<div class="sideactions"><button id="newChat">＋ Yeni sohbet</button><button class="secondary" id="loginSide">Giriş yap / Kayıt ol</button></div>
<div><small style="color:var(--muted)">SOHBET GEÇMİŞİ</small></div><div class="history" id="history"><div class="notice">Geçmişi görmek için giriş yap.</div></div>
<div class="userbar" id="userbar"><div class="notice">Misafir modu · Sohbetler kaydedilmez.</div></div></aside>
<main class="main"><header class="top"><button class="secondary mobile" id="mobileNew">＋</button><div><strong>Tekden AI</strong><br><small>Yardımcı yapay zekân</small></div><div style="display:flex;gap:8px"><button class="secondary" id="plansBtn">Planlar</button><button class="secondary" id="loginTop">Giriş yap</button></div></header>
<section class="messages" id="messages"><div class="welcome" id="welcome"><h1>Bugün ne yapmak istersin?</h1><p>Soru sor, internette araştırma yap veya bir görsel oluştur.</p><div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap"><button class="secondary suggestion">Bir konuyu açıkla</button><button class="secondary suggestion">Güncel haberleri araştır</button><button class="secondary suggestion">Bir görsel oluştur</button></div></div></section>
<div class="composerwrap"><div class="composer"><textarea id="prompt" placeholder="Tekden AI'a mesaj yaz..."></textarea><div class="attachrow"><label class="attachlabel" for="fileInput">📎 Dosya / görsel ekle</label><input id="fileInput" type="file" accept="image/*,.pdf,.txt,.md,.csv,.json,.docx" hidden><span class="attachname" id="fileName">Dosya seçilmedi</span></div><div class="tools"><div class="toolleft"><button class="secondary" id="searchBtn" title="İnternette ara">⌕ Web ara</button><button class="secondary" id="imageBtn" title="Görsel oluştur">▧ Görsel</button><span class="status" id="status">Hazır</span></div><button id="sendBtn">Gönder ↑</button></div></div><div class="notice" style="text-align:center;margin-top:8px">Misafir sohbetleri kaydedilmez. Oturum açarsan sohbet geçmişin hesabına kaydedilir.</div></div></main></div>
<div class="modalback" id="modalback"><div class="modal"><h2 id="modalTitle">Tekden AI'a giriş yap</h2><p class="notice">Hesabınla sohbet geçmişin saklanır.</p><input id="authUser" placeholder="Kullanıcı adı" autocomplete="username"><input id="authPass" type="password" placeholder="Şifre (en az 8 karakter)" autocomplete="current-password"><div class="row"><button id="authSubmit">Giriş yap</button><button class="secondary" id="registerBtn">Kayıt ol</button></div><p id="authError" class="notice"></p><button class="secondary" id="closeModal" style="width:100%;margin-top:8px">Kapat</button></div></div>
<div class="modalback" id="plansBack"><div class="modal"><h2>Tekden AI planları</h2><div class="plans"><div class="plan"><strong>Free · 0 TL/ay</strong><p>Standart sohbet modeli, gerektiğinde web araması, temel dosya ve görsel analizi. Günlük kullanım için.</p><button class="secondary" disabled>Ücretsiz plan</button></div><div class="plan"><strong>✦ Pro · 100 TL/ay</strong><p><b>Gelişmiş model denemesi</b>, daha derin akıl yürütme yönergeleri, uzun ve yapılandırılmış yanıtlar, kapsamlı araştırma isteği, daha ayrıntılı belge inceleme ve öncelikli Pro araç akışı. Harici servislerin kendi limitleri devam eder.</p><button id="buyPro">Pro'ya geç</button></div></div><p class="notice">Ödemeler Stripe üzerinden açılır. Satın alma, ödeme hesabı ve vergi/mesafeli satış yükümlülükleri doğru yapılandırılmadan etkin değildir.</p><button class="secondary" id="closePlans" style="width:100%">Kapat</button></div></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168/pdf.min.mjs" type="module"></script><script src="https://cdnjs.cloudflare.com/ajax/libs/mammoth/1.8.0/mammoth.browser.min.js"></script><script src="https://js.puter.com/v2/"></script>
<script>
const $=id=>document.getElementById(id);let user=null,chatId=null,guestHistory=[],forceSearch=false,selectedFile=null;
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function setStatus(s){$('status').textContent=s}
function showModal(){ $('modalback').style.display='flex';$('authError').textContent=''; }
function closeModal(){ $('modalback').style.display='none'; }
function bubble(role,content,sources=[]){$('welcome')?.remove();const d=document.createElement('div');d.className='bubble '+role;d.textContent=content;$('messages').appendChild(d);if(sources.length){const s=document.createElement('div');s.className='sources';s.innerHTML='<b>Kaynaklar</b><br>'+sources.map(x=>`<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title||x.url)}</a>`).join('<br>');d.appendChild(s)}$('messages').scrollTop=$('messages').scrollHeight;return d}
function imageBubble(data){$('welcome')?.remove();const d=document.createElement('div');d.className='bubble assistant';const title=document.createElement('div');title.textContent='Oluşturulan görsel';d.appendChild(title);const img=document.createElement('img');img.src=data;img.alt='Yapay zekâ tarafından oluşturulan görsel';img.loading='lazy';d.appendChild(img);const link=document.createElement('a');link.href=data;link.download='tekden-ai-gorsel.png';link.textContent='Görseli indir ↓';link.style.cssText='display:inline-block;margin-top:10px;color:#7651e8;font-weight:600';d.appendChild(link);$('messages').appendChild(d);$('messages').scrollTop=$('messages').scrollHeight}
async function api(url,opts={}){const r=await fetch(url,{...opts,headers:{'Content-Type':'application/json',...(opts.headers||{})}});let d={};try{d=await r.json()}catch{}if(!r.ok)throw new Error(d.detail||'İstek başarısız oldu.');return d}
async function refreshMe(){try{const d=await api('/api/me');user=d.user;renderUser();if(user)loadChats()}catch{user=null;renderUser()}}
function renderUser(){ $('userbar').innerHTML=user?`<div><b>${esc(user.username)}</b></div><div style="display:inline-block;margin-top:5px;padding:3px 9px;border-radius:999px;background:${user.plan==='pro'?'#e8ddff':'#e9f0ff'};color:${user.plan==='pro'?'#5a2cc4':'#43516a'};font-size:12px;font-weight:800">${user.plan==='pro'?'✦ PRO ÜYELİK':'FREE PLAN'}</div><button class="secondary" id="logoutBtn" style="width:100%;margin-top:8px">Çıkış yap</button>`:'<div class="notice">Misafir modu · Sohbetler kaydedilmez.</div>'; $('loginSide').textContent=user?'Hesap açık':'Giriş yap / Kayıt ol';$('loginTop').textContent=user?'Hesap açık':'Giriş yap';$('loginSide').onclick=user?null:showModal;$('loginTop').onclick=user?null:showModal;if(user)$('logoutBtn').onclick=async()=>{await api('/api/logout',{method:'POST'});user=null;chatId=null;guestHistory=[];$('messages').innerHTML='';newChat();renderUser();$('history').innerHTML='';}}
async function loadChats(){try{const d=await api('/api/chats');$('history').innerHTML='';d.chats.forEach(c=>{const b=document.createElement('button');b.textContent=c.title;b.title=c.title;b.onclick=()=>openChat(c.id);$('history').appendChild(b)})}catch(e){$('history').innerHTML='<div class="notice">Geçmiş yüklenemedi.</div>'}}
async function openChat(id){try{const d=await api('/api/chats/'+id);chatId=id;$('messages').innerHTML='';d.messages.forEach(m=>bubble(m.role==='user'?'user':'assistant',m.content));}catch(e){alert(e.message)}}
function newChat(){chatId=null;guestHistory=[];$('messages').innerHTML='<div class="welcome" id="welcome"><h1>Bugün ne yapmak istersin?</h1><p>Soru sor, internette araştırma yap veya bir görsel oluştur.</p></div>'; $('prompt').focus()}
function looksLikeImageRequest(text){
 const t=text.toLocaleLowerCase('tr-TR').trim();
 const cues=['görsel oluştur','görsel üret','görseli oluştur','görselini oluştur','görseli üret','resim oluştur','resmi oluştur','resmini oluştur','resmi çiz','resmini çiz','resim çiz','fotoğraf oluştur','fotoğraf üret','fotoğrafını oluştur','çizim oluştur','illüstrasyon oluştur','logo oluştur','afiş oluştur','bana çiz','çizimini yap','görsel yap','resim yap','generate an image','create an image','make a picture','draw me','görsel oluştur:','görsel:'];
 return cues.some(c=>t.includes(c)) || /\b(görseli?|resmi|resmini|fotoğrafı|fotoğrafını)\s+(oluştur|üret|çiz|yap)\b/i.test(t) || /\b(oluştur|üret|çiz|yap)\s+(bir\s+)?(görsel|resim|fotoğraf)\b/i.test(t);
}
function imagePromptFromRequest(text){
 let p=text.trim();
 p=p.replace(/^\s*(lütfen\s*)?(bana\s*)?(bir\s*)?(görsel|resim|fotoğraf)\s*(oluştur|üret|çiz|yap)\s*:?\s*/i,'');
 p=p.replace(/\s*(bir\s*)?(görseli?|resmi|resmini|fotoğrafı|fotoğrafını|çizimi)\s*(oluştur|üret|çiz|yap)\s*[.!?]*$/i,'');
 p=p.replace(/^\s*(görsel|resim|fotoğraf)\s*:\s*/i,'').trim();
 if(!p || p.length<2) p=text.trim();
 return 'Create an image that closely follows this exact user request. Preserve the requested subject, objects, setting, action, visual style, mood, colors, camera angle, and composition. Do not replace the subject, do not invent unrelated elements, and do not turn the request into a generic interpretation. If the user specifies multiple details, include all of them. Avoid unwanted text, logos, and watermarks unless explicitly requested. User request (keep its meaning faithfully): '+p;
}
async function createAndShowImage(requestText){
 if(!window.puter?.ai?.txt2img)throw new Error('Görsel servisi yüklenemedi. Sayfayı yenileyip tekrar dene.');
 const prompt=imagePromptFromRequest(requestText);
 // Gemini image generation generally follows detailed natural-language instructions better than the previous fast FLUX model.
 const img=await puter.ai.txt2img(prompt,{model:'google/gemini-3-pro-image'});
 if(!img?.src)throw new Error('Görsel servisi görsel döndürmedi.');
 imageBubble(img.src);
}
function getPuterText(result){
 if(typeof result==='string')return result;
 if(result?.message?.content){const c=result.message.content;return Array.isArray(c)?c.map(x=>x.text||'').filter(Boolean).join('\n'):String(c)}
 if(result?.text)return result.text;
 return JSON.stringify(result);
}
async function readDocument(file){
 const name=file.name.toLowerCase();
 if(name.endsWith('.txt')||name.endsWith('.md')||name.endsWith('.csv')||name.endsWith('.json'))return await file.text();
 if(name.endsWith('.docx')){
  if(!window.mammoth)throw new Error('DOCX okuyucu yüklenemedi; dosyayı TXT veya PDF olarak dışa aktarıp tekrar dene.');
  const result=await mammoth.extractRawText({arrayBuffer:await file.arrayBuffer()});return result.value;
 }
 if(name.endsWith('.pdf')){
  const pdfjs=await import('https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168/pdf.min.mjs');
  pdfjs.GlobalWorkerOptions.workerSrc='https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.4.168/pdf.worker.min.mjs';
  const pdf=await pdfjs.getDocument({data:await file.arrayBuffer()}).promise;let out='';
  for(let i=1;i<=Math.min(pdf.numPages,20);i++){const page=await pdf.getPage(i);const c=await page.getTextContent();out+='\n[Sayfa '+i+']\n'+c.items.map(x=>x.str).join(' ');if(out.length>90000)break;}
  if(!out.trim())throw new Error('PDF içinden metin çıkarılamadı. Taranmış PDF ise sayfaları görsel olarak yükle.');return out;
 }
 throw new Error('Bu dosya türü desteklenmiyor. PNG/JPG, PDF, DOCX, TXT, MD, CSV veya JSON kullan.');
}
async function analyzeAttachment(question,file){
 if(!window.puter?.ai?.chat)throw new Error('Dosya analizi servisi yüklenemedi. Sayfayı yenileyip tekrar dene.');
 const isImage=file.type.startsWith('image/');
 if(isImage){
  const prompt=(question||'Bu görseli ayrıntılı olarak analiz et.')+'\nGörselde gerçekten görülebilen bilgilere dayan. Emin olmadığın şeyleri kesinmiş gibi söyleme; metin varsa okuyabildiğin kısmı belirt.';
  const result=await puter.ai.chat(prompt,file,{model:'openai/gpt-5.4-nano'});return getPuterText(result);
 }
 const text=await readDocument(file);if(!text.trim())throw new Error('Dosyanın içinde okunabilir metin bulunamadı.');
 const prompt=(question||'Bu belgeyi analiz et ve önemli noktalarını açıkla.')+'\n\nBelge adı: '+file.name+'\nBelge içeriği (kaynağa sadık kal, eksik bilgiyi uydurma):\n'+text.slice(0,90000);
 const result=await puter.ai.chat(prompt,{model:'openai/gpt-5.4-nano'});return getPuterText(result);
}
async function send(){
 const text=$('prompt').value.trim();const file=selectedFile;if(!text&&!file)return;
 $('prompt').value='';bubble('user',(text||'Ekli dosyayı analiz et.')+(file?'\n📎 '+file.name:''));
 setStatus(file?'Dosya analiz ediliyor…':looksLikeImageRequest(text)?'İstenen görsel oluşturuluyor…':(forceSearch?'Derin web araması yapılıyor…':'Yanıt hazırlanıyor…'));$('sendBtn').disabled=true;
 try{
  if(file){const answer=await analyzeAttachment(text,file);bubble('assistant',answer);if(user){try{const saved=await api('/api/save-analysis',{method:'POST',body:JSON.stringify({question:text||'Ekli dosyayı analiz et.',answer,filename:file.name,chat_id:chatId})});if(saved.chat_id)chatId=saved.chat_id;loadChats()}catch(saveError){console.warn('Dosya analizi geçmişe kaydedilemedi',saveError)}}else guestHistory.push({role:'user',content:(text||'Ekli dosyayı analiz et.')+' [Dosya: '+file.name+']'},{role:'assistant',content:answer});selectedFile=null;$('fileInput').value='';$('fileName').textContent='Dosya seçilmedi';setStatus('Dosya analizi tamamlandı');}
  else if(looksLikeImageRequest(text)){await createAndShowImage(text);setStatus('Görsel hazır');}
  else {const d=await api('/api/chat',{method:'POST',body:JSON.stringify({message:text,chat_id:chatId,guest_history:guestHistory,force_search:forceSearch})});if(d.chat_id)chatId=d.chat_id;bubble('assistant',d.answer,d.sources||[]);if(!user){guestHistory.push({role:'user',content:text},{role:'assistant',content:d.answer});guestHistory=guestHistory.slice(-16)}if(user)loadChats();setStatus(d.sources?.length?'Kaynaklarla yanıtlandı':(forceSearch?'Kaynak bulunamadı; yanıt doğrulanamadı':'Tamamlandı'));}
 }catch(e){bubble('assistant',(file?'Dosya analiz edilemedi: ':looksLikeImageRequest(text)?'Görsel oluşturulamadı: ':'Hata: ')+e.message);setStatus('Hata')}
 finally{$('sendBtn').disabled=false;forceSearch=false;$('searchBtn').style.borderColor='var(--line)'}
}

async function generateImage(){const requestText=$('prompt').value.trim();if(!requestText){alert('Önce oluşturmak istediğin görseli ayrıntılarıyla yaz.');return}bubble('user','Görsel oluştur: '+requestText);$('prompt').value='';setStatus('İstediğin görsel oluşturuluyor…');$('imageBtn').disabled=true;try{await createAndShowImage(requestText);setStatus('Görsel hazır')}catch(e){bubble('assistant','Görsel oluşturulamadı: '+e.message);setStatus('Hata')}finally{$('imageBtn').disabled=false}}
$('sendBtn').onclick=send;$('prompt').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}});$('fileInput').onchange=e=>{selectedFile=e.target.files?.[0]||null;if(selectedFile&&selectedFile.size>12*1024*1024){alert('Dosya en fazla 12 MB olabilir.');selectedFile=null;$('fileInput').value=''}$('fileName').textContent=selectedFile?selectedFile.name:'Dosya seçilmedi';if(selectedFile)setStatus("Dosya hazır · sorunu yazıp Gönder'e bas")};$('imageBtn').onclick=generateImage;$('searchBtn').onclick=()=>{forceSearch=!forceSearch;$('searchBtn').style.borderColor=forceSearch?'#a78bfa':'var(--line)';setStatus(forceSearch?'Web araması açık':'Otomatik arama')};$('newChat').onclick=newChat;$('mobileNew').onclick=newChat;$('loginSide').onclick=showModal;$('loginTop').onclick=showModal;$('closeModal').onclick=closeModal;$('modalback').onclick=e=>{if(e.target===$('modalback'))closeModal()};
let authMode='login';$('authSubmit').onclick=()=>auth('login');$('registerBtn').onclick=()=>auth('register');
async function auth(mode){authMode=mode;const username=$('authUser').value.trim(),password=$('authPass').value;try{const d=await api('/api/'+mode,{method:'POST',body:JSON.stringify({username,password})});user=d.user;closeModal();renderUser();newChat();loadChats()}catch(e){$('authError').textContent=e.message}}
document.querySelectorAll('.suggestion').forEach(b=>b.onclick=()=>{$('prompt').value=b.textContent==='Güncel haberleri araştır'?'Bugünkü önemli haberleri araştır ve kaynak göster.':b.textContent==='Bir görsel oluştur'?'Görsel oluştur: Fütüristik, sinematik bir şehir manzarası':'Bana bu konuyu anlaşılır örneklerle açıkla';$('prompt').focus()});
$('plansBtn').onclick=()=>{$('plansBack').style.display='flex'};$('closePlans').onclick=()=>$('plansBack').style.display='none';$('plansBack').onclick=e=>{if(e.target===$('plansBack'))$('plansBack').style.display='none'};
$('buyPro').onclick=async()=>{if(!user){$('plansBack').style.display='none';showModal();$('authError').textContent='Pro üyeliği için giriş yap veya kayıt ol.';return}try{const d=await api('/api/billing/checkout',{method:'POST'});if(d.url)location.href=d.url}catch(e){alert(e.message)}};
const payState=new URLSearchParams(location.search).get('payment');if(payState==='success')setTimeout(()=>alert('Ödeme onayının hesabına yansıması webhook doğrulamasından sonra gerçekleşir.'),300);if(payState==='cancelled')setStatus('Ödeme iptal edildi');
refreshMe();
</script></body></html>"""

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(HTML)

