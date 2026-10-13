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

@app.post("/api/image")
def image(body: ImageBody):
    if not POLLINATIONS_API_KEY:
        raise HTTPException(503, "Görsel üretimi ayarlı değil: POLLINATIONS_API_KEY eklenmeli.")
    url = "https://gen.pollinations.ai/image/" + quote(body.prompt, safe="")
    try:
        r = requests.get(url, params={"model": POLLINATIONS_IMAGE_MODEL, "nologo": "true"},
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

@app.post("/api/image")
def image(body: ImageBody):
    if not POLLINATIONS_API_KEY:
        raise HTTPException(503, "Görsel üretimi ayarlı değil: POLLINATIONS_API_KEY eklenmeli.")
    url = "https://gen.pollinations.ai/image/" + quote(body.prompt, safe="")
    try:
        r = requests.get(url, params={"model": POLLINATIONS_IMAGE_MODEL, "nologo": "true"},
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

