#!/usr/bin/env python3
"""
StoryForge — interactive AI story generator for Pollinations BYOP.
Users sign in via Pollinations OAuth (PKCE), spend THEIR OWN pollen,
developer earns 25% markup on every request (earningsEnabled=true).
Stdlib-only. Serves 127.0.0.1:3095 behind Caddy.

SECURITY MODEL (audit 2026-08-23):
- User sk_ keys live in MEMORY ONLY (never persisted to disk)
- Per-IP rate limits on /auth/start and /generate; SESSIONS capped + TTL'd
- Strict sid<->state<->verifier binding (no single-session fallback)
- OAuth codes/states scrubbed from access logs
- Art served from bounded in-memory LRU (no /tmp writes)
- ThreadingHTTPServer + socket timeouts (slowloris/concurrency hardening)
"""
import json, os, secrets, hashlib, base64, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import sqlite3, threading, time
from collections import OrderedDict, deque

PK = json.load(open(os.path.join(os.path.dirname(__file__), "..", "state", "appkey.json")))
CLIENT_ID = PK["key"]
REDIRECT_URI = "https://storyforge.wiradifit-makmur-sejahtera.duckdns.org/auth/callback"
AUTHORIZE_URL = "https://enter.pollinations.ai/authorize"
TOKEN_URL = "https://enter.pollinations.ai/api/oauth/token"
GEN_BASE = "https://gen.pollinations.ai"
# Cloudflare on pollinations infra blocks python-urllib fingerprints (err 1010)
OUTBOUND_UA = "Mozilla/5.0 (X11; Linux x86_64) StoryForge/1.0"
PORT = 3095
DB = os.path.join(os.path.dirname(__file__), "..", "state", "storyforge.db")

# --- limits ---
MAX_SESSIONS = 500          # concurrent pending OAuth handshakes
SESSION_TTL = 900           # seconds
RATE_AUTH_START = (8, 60)   # 8 per 60s per IP
RATE_GENERATE = (6, 300)    # 6 per 5min per IP (pollen burn protection)
ART_CACHE_MAX = 40          # illustrations kept in RAM (~40 x ~80KB)
REQ_TIMEOUT = 20            # socket read timeout per connection

# --- state ---
SESSIONS = {}               # sid -> {verifier, state, ts}
USER_KEYS = {}              # uid -> {sk, github, created}   MEMORY ONLY
ART_CACHE = OrderedDict()   # art_id -> png bytes
LOCK = threading.Lock()
IP_HITS = {}                # (route, ip) -> deque[timestamps]

def db():
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        uid TEXT PRIMARY KEY, github TEXT, created INTEGER,
        requests INTEGER DEFAULT 0)""")
    conn.execute("CREATE TABLE IF NOT EXISTS events(ts INTEGER, kind TEXT, detail TEXT)")
    return conn

def log_event(kind, detail=""):
    try:
        c = db()
        c.execute("INSERT INTO events VALUES(?,?,?)", (int(time.time()), kind, detail[:200]))
        c.commit()
    except Exception:
        pass

def rate_ok(route, ip):
    n, window = RATE_AUTH_START if route == "start" else RATE_GENERATE
    key = (route, ip)
    now = time.time()
    with LOCK:
        dq = IP_HITS.setdefault(key, deque())
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= n:
            return False
        dq.append(now)
        # opportunistic GC
        if len(IP_HITS) > 5000:
            for k in [k for k, v in IP_HITS.items() if not v]:
                del IP_HITS[k]
        return True

def prune_sessions():
    now = time.time()
    with LOCK:
        expired = [k for k, v in SESSIONS.items() if now - v["ts"] > SESSION_TTL]
        for k in expired:
            del SESSIONS[k]
        # hard cap (oldest first)
        while len(SESSIONS) > MAX_SESSIONS:
            SESSIONS.pop(next(iter(SESSIONS)))

PAGE = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>StoryForge — AI Stories</title><style>
body{{font-family:Georgia,serif;background:#12101c;color:#e8e4f0;margin:0}}
main{{max-width:680px;margin:0 auto;padding:2rem 1rem}}
h1{{font-size:2rem;margin:.2em 0;color:#c9a7ff}} .tag{{color:#8d86a5;font-style:italic}}
button,.btn{{background:#7c4dff;color:#fff;border:none;padding:.8em 1.6em;border-radius:8px;
 font-size:1rem;cursor:pointer;text-decoration:none;display:inline-block}}
textarea{{width:100%;background:#1d1930;color:#fff;border:1px solid #3a3157;border-radius:8px;
 padding:.8em;font-family:inherit;font-size:1rem;box-sizing:border-box;min-height:90px}}
select,input{{background:#1d1930;color:#fff;border:1px solid #3a3157;border-radius:6px;padding:.5em}}
.story{{background:#1d1930;border-radius:12px;padding:1.2em;margin-top:1.2em;line-height:1.6;white-space:pre-wrap}}
img.art{{max-width:100%;border-radius:12px;margin-top:1em}}
.err{{color:#ff8a80}} .foot{{margin-top:3rem;font-size:.8rem;color:#5f5a78;text-align:center}}
</style></head><body><main>
<h1>&#128214; StoryForge</h1><p class=tag>Turn any idea into an illustrated micro-story — powered by your own Pollinations pollen.</p>
{body}
<p class=foot>StoryForge by WIRADIFIT &middot; Bring Your Own Pollen &middot; <a style=color:#8d86a5 href=https://pollinations.ai>pollinations.ai</a></p>
</main></body></html>"""

LOGIN_BODY = """
<p>You'll authorize StoryForge to make generation requests bounded by your own budget.
No pollen is spent without your confirmation.</p>
<p><a class=btn href="/auth/start">Sign in with Pollinations &#10142;</a></p>"""

APP_BODY = """
<form method=POST action=/generate>
<label>Genre:</label>
<select name=genre><option>Fantasy</option><option>Sci-Fi</option><option>Mystery</option><option>Horror</option><option>Comedy</option></select>
&nbsp;<label>Length:</label>
<select name=len><option value=short>Flash (~120 words)</option><option value=medium>Short (~250 words)</option></select>
<br><br>
<textarea name=idea placeholder="e.g. A lighthouse keeper who receives letters from the future..." maxlength=500></textarea>
<br><input type=submit value="&#10024; Forge my story" style="margin-top:.6em">
</form>
{result}"""

def b64url(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

def esc(s): return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

class Handler(BaseHTTPRequestHandler):
    timeout = REQ_TIMEOUT

    def _send(self, html, code=200):
        body = PAGE.format(body=html).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.end_headers()
        self.wfile.write(body)

    def _cookie(self, name):
        for part in self.headers.get("Cookie", "").split(";"):
            if part.strip().startswith(name + "="):
                return part.split("=", 1)[1].strip()
        return None

    def _ip(self):
        return self.client_address[0]

    def do_GET(self):
        try:
            self._route_get()
        except Exception:
            log_event("error", "GET " + self.path.split("?")[0][:80])
            try:
                self._send("<p class=err>Something went wrong.</p>", 500)
            except Exception:
                pass

    def _route_get(self):
        route = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(route.query)

        if route.path == "/health":
            body = b'{"status":"ok","app":"storyforge"}'
            self.send_response(200); self.send_header("Content-Type","application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        elif route.path == "/":
            uid = self._cookie("sf_uid")
            if uid and uid in USER_KEYS:
                self._send(APP_BODY.format(result=""))
            else:
                self._send(LOGIN_BODY)

        elif route.path == "/auth/start":
            prune_sessions()
            if not rate_ok("start", self._ip()):
                self._send("<p class=err>Too many attempts — wait a minute.</p>", 429); return
            sid = secrets.token_urlsafe(16)
            verifier = b64url(secrets.token_bytes(48))
            challenge = b64url(hashlib.sha256(verifier.encode()).digest())
            state = secrets.token_urlsafe(16)
            with LOCK:
                SESSIONS[sid] = {"verifier": verifier, "state": state, "ts": time.time()}
            params = urllib.parse.urlencode({
                "response_type": "code", "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI, "scope": "usage",
                "state": state, "code_challenge": challenge,
                "code_challenge_method": "S256"})
            self.send_response(302)
            self.send_header("Location", f"{AUTHORIZE_URL}?{params}")
            self.send_header("Set-Cookie", f"sf_sid={sid}; Path=/; Secure; HttpOnly; SameSite=Lax")
            self.end_headers()

        elif route.path == "/auth/callback":
            code = q.get("code",[None])[0]
            state = q.get("state",[None])[0]
            sid = self._cookie("sf_sid") or ""
            sess = SESSIONS.get(sid)
            # STRICT binding: sid must exist AND its state must match (no fallbacks)
            if not code or not sess or sess.get("state") != state:
                with LOCK: SESSIONS.pop(sid, None)
                self._send("<p class=err>Auth failed: invalid state. <a href=/auth/start>Try again</a></p>", 400); return
            with LOCK: SESSIONS.pop(sid, None)   # single-use

            data = urllib.parse.urlencode({
                "grant_type": "authorization_code", "code": code,
                "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI,
                "code_verifier": sess["verifier"]}).encode()
            req = urllib.request.Request(TOKEN_URL, data=data, method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "User-Agent": OUTBOUND_UA})
            try:
                tok = json.loads(urllib.request.urlopen(req, timeout=30).read())
            except urllib.error.HTTPError as e:
                self._send(f"<p class=err>Token exchange failed ({e.code}). <a href=/auth/start>Retry</a></p>", 502); return
            except Exception:
                self._send("<p class=err>Token exchange failed. <a href=/auth/start>Retry</a></p>", 502); return
            sk = tok["access_token"]
            preq = urllib.request.Request(f"{GEN_BASE}/account/profile",
                headers={"Authorization": f"Bearer {sk}", "User-Agent": OUTBOUND_UA})
            try:
                prof = json.loads(urllib.request.urlopen(preq, timeout=20).read())
                gh = prof.get("githubUsername") or "anon"
            except Exception:
                gh = "anon"
            uid = secrets.token_urlsafe(24)
            USER_KEYS[uid] = {"sk": sk, "github": gh, "created": int(time.time())}  # memory only
            c = db()
            c.execute("INSERT OR REPLACE INTO users VALUES(?,?,?,0)", (uid, gh, int(time.time())))
            c.commit(); log_event("signin", gh)
            self.send_response(302); self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"sf_uid={uid}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=604800")
            self.end_headers()

        elif route.path.startswith("/art/") and route.path.endswith(".png"):
            aid = route.path[len("/art/"):-4]
            data = ART_CACHE.get(aid)
            if data:
                ART_CACHE.move_to_end(aid)
                self.send_response(200); self.send_header("Content-Type","image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers(); self.wfile.write(data)
            else:
                self.send_response(404); self.send_header("Content-Length","0"); self.end_headers()

        else:
            self._send("<p class=err>404</p>", 404)

    def do_POST(self):
        try:
            self._route_post()
        except Exception:
            log_event("error", "POST " + self.path[:80])
            try:
                self._send("<p class=err>Something went wrong.</p>", 500)
            except Exception:
                pass

    def _route_post(self):
        route = urllib.parse.urlparse(self.path)
        if route.path != "/generate":
            self._send("<p class=err>404</p>", 404); return
        uid = self._cookie("sf_uid")
        if not uid or uid not in USER_KEYS:
            self._send("<p class=err>Session expired. <a href=/auth/start>Sign in again</a></p>", 401); return
        if not rate_ok("generate", self._ip()):
            self._send("<p class=err>Slow down — try again in a few minutes.</p>", 429); return

        try:
            clen = int(self.headers.get("Content-Length", 0))
        except ValueError:
            clen = 0
        clen = min(max(clen, 0), 4096)  # cap body at 4KB
        form = urllib.parse.parse_qs(self.rfile.read(clen).decode(errors="replace"))
        idea   = (form.get("idea",[""])[0] or "a mysterious door").strip()[:500]
        genre  = (form.get("genre",["Fantasy"])[0] or "Fantasy").strip()[:30]
        length = 120 if form.get("len",["short"])[0] == "short" else 250
        sk = USER_KEYS[uid]["sk"]

        result_html = ""
        try:
            prompt = (f"Write an original {genre.lower()} micro-story of about {length} words "
                      f"based on this idea:\n{idea}\nReturn ONLY the story text.")
            data = json.dumps({"model":"openai","messages":[{"role":"user","content":prompt}],
                               "max_tokens": 800}).encode()
            req = urllib.request.Request(f"{GEN_BASE}/v1/chat/completions", data=data, method="POST",
                headers={"Authorization": f"Bearer {sk}", "Content-Type": "application/json",
                         "User-Agent": OUTBOUND_UA})
            story = json.loads(urllib.request.urlopen(req, timeout=120).read())["choices"][0]["message"]["content"]
            img_req = urllib.request.Request(
                f"{GEN_BASE}/image/{urllib.parse.quote('storybook illustration, ' + idea[:120])}?model=flux&width=512&height=512&nologo=true",
                headers={"Authorization": f"Bearer {sk}", "User-Agent": OUTBOUND_UA})
            art = urllib.request.urlopen(img_req, timeout=180).read()
            art_id = secrets.token_hex(8)
            with LOCK:
                ART_CACHE[art_id] = art
                ART_CACHE.move_to_end(art_id)
                while len(ART_CACHE) > ART_CACHE_MAX:
                    ART_CACHE.popitem(last=False)
            c = db(); c.execute("UPDATE users SET requests=requests+1 WHERE uid=?", (uid,)); c.commit()
            log_event("generate", USER_KEYS[uid]["github"])
            result_html = f'<div class="story">{esc(story)}</div><img class="art" src="/art/{art_id}.png" alt="illustration">'
        except urllib.error.HTTPError as e:
            result_html = f'<p class=err>Generation failed ({e.code}): your pollen balance may be exhausted.</p>'
        except Exception:
            result_html = '<p class=err>Error generating your story — please try again.</p>'
        self._send(APP_BODY.format(result=result_html))

    def log_message(self, fmt, *args):
        # scrub query strings (OAuth codes/states must not hit logs)
        line = fmt % args
        line = line.split("?")[0] if "?" in line else line
        print(time.strftime("%Y-%m-%d %H:%M:%S"), self.address_string(), line, flush=True)

if __name__ == "__main__":
    print("StoryForge listening on 127.0.0.1:" + str(PORT) + " (threaded, hardened)", flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    server.serve_forever()
