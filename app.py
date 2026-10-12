from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import requests
import os

app = FastAPI(title="Tekden AI")

OLLAMA_URL = "https://ollama.com/api/chat"
API_KEY = os.environ.get("OLLAMA_API_KEY", "").strip()
MODEL_NAME = os.environ.get("OLLAMA_MODEL", "gpt-oss:20b-cloud").strip()

SYSTEM_PROMPT = """
Sen Tekden AI'sın.
Ahmet Kerim Kırgız tarafından geliştirilen genel amaçlı bir yapay zekâ asistanısın.
Kendini tanıtırken adının Tekden AI olduğunu ve geliştiricinin Ahmet Kerim Kırgız olduğunu söyle.
Türkçe, anlaşılır, nazik ve faydalı cevaplar ver.
İstenildiği zaman İngilizce konuş.
İstenildiği zaman başta ingilzce ve türkçe olmak üzere tüm dilleri konuş.
Kendine dil seçeneği ekle.
Bilmediğin bilgileri uydurma; emin değilsen bunu açıkça belirt.
Programlama, eğitim, bilim, yazma ve proje geliştirme konularında yardımcı ol.
Güvenli olmayan isteklerde güvenli alternatifler sun.
""".strip()


class ChatRequest(BaseModel):
    message: str


@app.get("/", response_class=HTMLResponse)
def home():
    return HTML_PAGE


@app.get("/api/health")
def health():
    configured = bool(API_KEY)
    return {
        "ok": configured,
        "provider": "Ollama Cloud",
        "model": MODEL_NAME,
        "message": (
            "Ollama Cloud bağlantısı yapılandırıldı."
            if configured
            else "Render ortam değişkenlerinde OLLAMA_API_KEY eksik."
        ),
    }


def should_search_web(message: str) -> bool:
    """Automatically search for current facts or when the user asks for sources/web."""
    q = message.casefold()
    triggers = [
        "internette ara", "internetten ara", "webde ara", "web araması", "kaynak göster",
        "kaynakları göster", "kaynakların neler", "hangi sitelere baktın", "internetten bul",
        "güncel", "şu an", "şuanda", "bugün", "dün", "son dakika", "en son", "son haberler",
        "haberleri", "haberler", "2026", "fiyatı", "fiyatları", "kaç para", "döviz", "kur kaç",
        "hava durumu", "hava nasıl", "hava kaç derece", "maç sonucu", "puan durumu", "seçim sonucu",
        "kim kazandı", "ne zaman çıkacak", "yayınlandı mı", "açık mı", "çalışıyor mu",
        "en iyi", "karşılaştır", "incele", "araştır", "resmî site", "resmi site", "linkini ver",
        "bağlantısını ver", "kaynakça", "kanıtları", "istatistikleri"
    ]
    return any(term in q for term in triggers)


@app.post("/api/chat")
def chat(data: ChatRequest):
    message = data.message.strip()
    if not message:
        return {"reply": "Bir mesaj yazıp tekrar dener misin?"}

    if not API_KEY:
        return {"error": "Sunucu ayarı eksik: Render'da OLLAMA_API_KEY ortam değişkenini ekle."}

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    sources = []
    web_search = should_search_web(message)

    try:
        if web_search:
            search_response = requests.post(
                "https://ollama.com/api/web_search",
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"query": message, "max_results": 5},
                timeout=30,
            )
            if search_response.status_code == 401:
                return {"error": "Ollama API anahtarı web araması için kabul edilmedi. Render'daki OLLAMA_API_KEY değerini kontrol et."}
            if search_response.status_code == 429:
                return {"error": "Web arama kullanım sınırına ulaşıldı. Ollama hesabındaki kullanım durumunu kontrol et."}
            search_response.raise_for_status()
            search_data = search_response.json()
            results = search_data.get("results", [])
            if not results:
                return {"error": "Bu sorgu için web arama sonucu bulunamadı. Farklı kelimelerle dene."}
            sources = [
                {"title": str(item.get("title", "Kaynak"))[:250], "url": str(item.get("url", ""))}
                for item in results
                if str(item.get("url", "")).startswith(("https://", "http://"))
            ][:5]
            search_context = "\n\n".join(
                f"Kaynak {i+1}: {item.get('title', '')}\nURL: {item.get('url', '')}\nÖzet: {item.get('content', '')}"
                for i, item in enumerate(results[:5])
            )
            messages.append({
                "role": "system",
                "content": "Kullanıcı web araması istedi. Aşağıdaki güncel arama sonuçlarını kullan. Yalnızca sonuçların desteklediği iddiaları gerçekmiş gibi sun; kaynaklarda olmayan ayrıntıları uydurma. Cevabını Türkçe ve anlaşılır ver. Arama sonuçlarının kaynakları ayrıca arayüzde bağlantı olarak gösterilecek.\n\n" + search_context,
            })

        messages.append({"role": "user", "content": message})
        response = requests.post(
            OLLAMA_URL,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL_NAME,
                "stream": False,
                "messages": messages,
                "options": {"temperature": 0.7},
            },
            timeout=180,
        )
        if response.status_code == 401:
            return {"error": "Ollama API anahtarı kabul edilmedi. Render'daki OLLAMA_API_KEY değerini kontrol et."}
        if response.status_code == 429:
            return {"error": "Ollama kullanım sınırına ulaşıldı veya kota geçici olarak doldu. Hesabındaki kullanım durumunu kontrol et."}
        if response.status_code == 404:
            return {"error": f"Model bulunamadı: {MODEL_NAME}. Render'daki OLLAMA_MODEL değerini desteklenen tam model adıyla güncelle."}
        response.raise_for_status()
        result = response.json()
        reply = result.get("message", {}).get("content", "").strip()
        if not reply:
            reply = "Şu anda yanıt oluşturamadım. Bir kez daha dener misin?"
        return {"reply": reply, "sources": sources}
    except requests.Timeout:
        return {"error": "İstek çok uzun sürdü. Lütfen tekrar dene."}
    except requests.ConnectionError:
        return {"error": "Ollama Cloud servisine bağlanamadım. Biraz sonra tekrar dene."}
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "bilinmiyor"
        return {"error": f"Ollama isteği başarısız oldu (HTTP {status}). API anahtarını ve kullanım kotanı kontrol et."}
    except (requests.RequestException, ValueError):
        return {"error": "Web araması veya Ollama yanıtı okunamadı. Biraz sonra tekrar dene."}


HTML_PAGE = r"""
<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tekden AI</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#111a2e;--panel2:#17233b;--line:#263653;--text:#edf3ff;--muted:#a8b6d1;--accent:#7c9cff;--accent2:#9d7cff}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:radial-gradient(ellipse at top,#1b2b4b 0,#0b1020 55%);color:var(--text);font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;display:flex;justify-content:center}
.app{width:min(100%,980px);height:100vh;min-height:560px;display:flex;flex-direction:column;padding:18px}
header{display:flex;align-items:center;gap:12px;padding:8px 4px 18px;border-bottom:1px solid var(--line)}
.logo{width:46px;height:46px;border-radius:15px;background:linear-gradient(135deg,var(--accent),var(--accent2));display:grid;place-items:center;font-size:23px;font-weight:800;color:#fff;box-shadow:0 8px 30px #536fe944}
.brand{flex:1}.brand h1{font-size:21px;margin:0}.brand p{margin:2px 0 0;color:var(--muted);font-size:13px}
button{font:inherit;cursor:pointer;border:1px solid var(--line);color:var(--text);background:var(--panel2);border-radius:12px;padding:10px 14px}
button:hover{filter:brightness(1.15)}button:disabled{opacity:.5;cursor:wait}
#status{font-size:12px;color:var(--muted);padding:10px 4px}
#messages{flex:1;overflow:auto;padding:14px 2px 24px;display:flex;flex-direction:column;gap:18px;scroll-behavior:smooth}
.welcome{margin:auto;text-align:center;max-width:580px;padding:26px}
.welcome .biglogo{font-size:44px}.welcome h2{font-size:28px;margin:10px 0}.welcome p{color:var(--muted)}
.suggestions{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-top:20px}
.suggestions button{font-size:13px}
.message{display:flex;gap:10px;max-width:88%;align-items:flex-start}
.message.user{align-self:flex-end;flex-direction:row-reverse}
.avatar{flex:0 0 34px;height:34px;border-radius:11px;background:var(--panel2);display:grid;place-items:center;font-size:15px}
.message.user .avatar{background:#314a83}
.bubble{padding:12px 15px;border:1px solid var(--line);background:var(--panel);border-radius:5px 16px 16px 16px;white-space:pre-wrap;overflow-wrap:anywhere}
.message.user .bubble{background:#20345b;border-radius:16px 5px 16px 16px}
.composer{border:1px solid var(--line);background:#111a2eeF;border-radius:18px;padding:10px;display:flex;gap:10px;align-items:flex-end;box-shadow:0 12px 35px #0003;flex-wrap:wrap}
.search-toggle{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:13px;padding:4px 8px;width:100%}.search-toggle input{accent-color:var(--accent);width:16px;height:16px}
.sources{margin-top:8px;padding:10px 12px;border:1px solid var(--line);border-radius:12px;background:#0d1528;font-size:13px}.sources strong{display:block;margin-bottom:6px}.sources a{display:block;color:#a9bcff;text-decoration:none;overflow-wrap:anywhere;margin:5px 0}.sources a:hover{text-decoration:underline}
textarea{flex:1;resize:none;max-height:180px;min-height:46px;border:0;outline:0;background:transparent;color:var(--text);font:inherit;padding:11px}
#send{background:linear-gradient(135deg,var(--accent),var(--accent2));border:0;font-weight:700;min-width:86px}
footer{text-align:center;color:var(--muted);font-size:11px;padding:10px 0 0}
@media(max-width:600px){.app{padding:10px;min-height:500px}.message{max-width:96%}.brand h1{font-size:19px}.welcome{padding:12px}#newChat{padding:8px;font-size:13px}}
</style>
</head>
<body>
<div class="app">
<header>
  <div class="logo">T</div>
  <div class="brand"><h1>Tekden AI</h1><p>Ahmet Kerim tarafından geliştirildi</p></div>
  <button id="newChat" type="button">＋ Yeni sohbet</button>
</header>
<div id="status">Ollama Cloud bağlantısı kontrol ediliyor…</div>
<main id="messages" aria-live="polite">
  <section class="welcome" id="welcome">
    <div class="biglogo">✦</div>
    <h2>Merhaba, ben Tekden AI.</h2>
    <p>Ahmet Kerim tarafından geliştirilen yapay zekâ asistanın. Bugün ne yapmak istiyorsun?</p>
    <div class="suggestions">
      <button type="button" data-prompt="Python öğrenmeye nereden başlamalıyım?">Python öğren</button>
      <button type="button" data-prompt="Bana yaratıcı bir proje fikri öner.">Proje fikri bul</button>
      <button type="button" data-prompt="Yapay zekâ nasıl çalışır? Basitçe anlat.">Yapay zekâyı keşfet</button>
    </div>
  </section>
</main>
<form id="form" class="composer">
  <textarea id="input" placeholder="Tekden AI'ya bir mesaj yaz…" rows="1" aria-label="Mesaj"></textarea>
  <button id="send" type="submit">Gönder ↑</button>
</form>
<footer>Tekden AI · Ollama Cloud ile çalışır · Gerektiğinde otomatik web araması</footer>
</div>
<script>
const messages = document.getElementById('messages');
const welcome = document.getElementById('welcome');
const form = document.getElementById('form');
const input = document.getElementById('input');
const send = document.getElementById('send');
const statusEl = document.getElementById('status');
let busy = false;

function addMessage(text, who) {
  if (welcome && welcome.isConnected) welcome.remove();
  const row = document.createElement('div');
  row.className = 'message ' + who;
  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = who === 'user' ? 'S' : 'T';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  row.append(avatar, bubble);
  messages.appendChild(row);
  messages.scrollTop = messages.scrollHeight;
  return bubble;
}

async function sendMessage(text) {
  text = text.trim();
  if (!text || busy) return;
  addMessage(text, 'user');
  input.value = '';
  input.style.height = '46px';
  busy = true;
  send.disabled = true;
  const waiting = addMessage('Düşünüyorum…', 'assistant');
  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : (data.error || 'Sunucu hatası oluştu.'));
    waiting.textContent = data.reply || data.error || 'Yanıt alınamadı.';
    if (data.error) waiting.textContent = '⚠️ ' + data.error;
    if (!data.error && Array.isArray(data.sources) && data.sources.length) {
      const sourceBox = document.createElement('div');
      sourceBox.className = 'sources';
      const heading = document.createElement('strong');
      heading.textContent = 'Kaynaklar';
      sourceBox.appendChild(heading);
      data.sources.forEach(source => {
        const link = document.createElement('a');
        link.href = source.url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = source.title || source.url;
        sourceBox.appendChild(link);
      });
      waiting.parentElement.appendChild(sourceBox);
    }
  } catch (err) {
    waiting.textContent = '⚠️ ' + (typeof err.message === 'string' ? err.message : 'İstek başarısız oldu.') + '\nGerekirse Render Logs bölümünü kontrol et.';
  } finally {
    busy = false;
    send.disabled = false;
    input.focus();
    messages.scrollTop = messages.scrollHeight;
  }
}

form.addEventListener('submit', event => {
  event.preventDefault();
  sendMessage(input.value);
});
input.addEventListener('input', () => {
  input.style.height = '46px';
  input.style.height = Math.min(input.scrollHeight, 180) + 'px';
});
input.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});
document.querySelectorAll('[data-prompt]').forEach(button => {
  button.addEventListener('click', () => sendMessage(button.dataset.prompt));
});
document.getElementById('newChat').addEventListener('click', () => {
  messages.innerHTML = '';
  const section = document.createElement('section');
  section.className = 'welcome';
  section.innerHTML = '<div class="biglogo">✦</div><h2>Merhaba, ben Tekden AI.</h2><p>Ahmet Kerim tarafından geliştirilen yapay zekâ asistanın. Bugün ne yapmak istiyorsun?</p>';
  messages.appendChild(section);
  input.value = '';
  input.focus();
});
fetch('/api/health').then(r => r.json()).then(data => {
  statusEl.textContent = data.ok
    ? '● Ollama Cloud bağlı · İnternet araması hazır'
    : '● Ollama Cloud ayarını kontrol et';
}).catch(() => { statusEl.textContent = 'Bağlantı durumu kontrol edilemedi.'; });
</script>
</body>
</html>
"""
