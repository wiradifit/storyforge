#!/usr/bin/env python3
"""
StoryForge — interactive AI story generator for Pollinations BYOP.
Users sign in via Pollinations OAuth (PKCE), spend THEIR OWN pollen,
developer earns 25% markup on every request (earningsEnabled=true).
Stdlib-only. Serves 127.0.0.1:3095 behind Caddy.
"""
import json, os, secrets, hashlib, base64, urllib.parse, urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
import sqlite3, threading, time

PK = json.load(open(os.path.join(os.path.dirname(__file__), "..", "state", "appkey.json")))
CLIENT_ID = PK["key"]
REDIRECT_URI = "https://storyforge.wiradifit-makmur-sejahtera.duckdns.org/auth/callback"
AUTHORIZE_URL = "https://enter.pollinations.ai/authorize"
TOKEN_URL = "https://enter.pollinations.ai/api/oauth/token"
GEN_BASE = "https://gen.pollinations.ai"
PORT = 3095
DB = os.path.join(os.path.dirname(__file__), "..", "state", "storyforge.db")

# --- state ---
SESSIONS = {}          # sid -> {verifier, state}
USER_KEYS = {}         # uid -> {sk, github, created}
LOCK = threading.Lock()

def db():
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        uid TEXT PRIMARY KEY, github TEXT, sk TEXT, created INTEGER,
        requests INTEGER DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS events(
        ts INTEGER, kind TEXT, detail TEXT)""")
    return conn

def log_event(kind, detail=""):
    try:
        c = db(); c.execute("INSERT INTO events VALUES(?,?,?)", (int(time.time()), kind, detail[:300])); c.commit()
    except Exception: pass

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
<textarea name=idea placeholder="e.g. A lighthouse keeper who receives letters from the future..."></textarea>
<br><input type=submit value="&#10024; Forge my story" style="margin-top:.6em">
</form>
{result}"""

def b64url(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

class Handler(BaseHTTPRequestHandler):
    def _send(self, html, code=200):
        body = PAGE.format(body=html).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cookie_uid(self):
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            if part.strip().startswith("sf_uid="):
                return part.split("=", 1)[1].strip()
        return None

    def do_GET(self):
        route = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(route.query)

        if route.path == "/health":
            body = b'{"status":"ok","app":"storyforge"}'
            self.send_response(200); self.send_header("Content-Type","application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        elif route.path == "/":
            uid = self._cookie_uid()
            if uid and uid in USER_KEYS:
                self._send(APP_BODY.format(result=""))
            else:
                self._send(LOGIN_BODY)

        elif route.path == "/auth/start":
            sid = secrets.token_urlsafe(16)
            verifier = b64url(secrets.token_bytes(48))
            challenge = b64url(hashlib.sha256(verifier.encode()).digest())
            state = secrets.token_urlsafe(16)
            with LOCK: SESSIONS[sid] = {"verifier": verifier, "state": state}
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
            code, state, sid = q.get("code",[None])[0], q.get("state",[None])[0], self._cookie_uid() or ""
            sess = SESSIONS.get(q.get("state",[None])[0] and sid or "", {})
            # match by sid cookie OR accept if single pending session
            if not sess and len(SESSIONS) == 1:
                sess = next(iter(SESSIONS.values()))
            if not code or not sess or (sess.get("state") != state):
                self._send("<p class=err>Auth failed: invalid state. <a href=/auth/start>Try again</a></p>", 400); return
            data = urllib.parse.urlencode({
                "grant_type": "authorization_code", "code": code,
                "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI,
                "code_verifier": sess["verifier"]}).encode()
            req = urllib.request.Request(TOKEN_URL, data=data, method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            try:
                tok = json.loads(urllib.request.urlopen(req, timeout=30).read())
            except urllib.error.HTTPError as e:
                self._send(f"<p class=err>Token exchange failed ({e.code}). <a href=/auth/start>Retry</a></p>", 502); return
            sk, scope = tok["access_token"], tok.get("scope","")
            # who signed in?
            preq = urllib.request.Request(f"{GEN_BASE}/account/profile", headers={"Authorization": f"Bearer {sk}"})
            try:
                prof = json.loads(urllib.request.urlopen(preq, timeout=20).read())
                gh = prof.get("githubUsername") or "anon"
            except Exception: gh = "anon"
            uid = secrets.token_urlsafe(24)
            USER_KEYS[uid] = {"sk": sk, "github": gh, "created": int(time.time())}
            c = db()
            c.execute("INSERT OR REPLACE INTO users VALUES(?,?,?,?,0)", (uid, gh, sk, int(time.time())))
            c.commit(); log_event("signin", gh)
            self.send_response(302); self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"sf_uid={uid}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=604800")
            self.end_headers()

        else:
            self._send("<p class=err>404</p>", 404)

    def do_POST(self):
        route = urllib.parse.urlparse(self.path)
        if route.path != "/generate":
            self._send("<p class=err>404</p>", 404); return
        uid = self._cookie_uid()
        if not uid or uid not in USER_KEYS:
            self._send("<p class=err>Session expired. <a href=/auth/start>Sign in again</a></p>", 401); return
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length",0))).decode())
        idea   = (form.get("idea",[""])[0] or "a mysterious door").strip()[:500]
        genre  = form.get("genre",["Fantasy"])[0]
        length = 120 if form.get("len",["short"])[0] == "short" else 250
        sk = USER_KEYS[uid]["sk"]

        result_html = ""
        try:
            prompt = (f"Write an original {genre.lower()} micro-story of about {length} words "
                      f"based on this idea:\n{idea}\nReturn ONLY the story text.")
            data = json.dumps({"model":"openai","messages":[{"role":"user","content":prompt}],
                               "max_tokens": 800}).encode()
            req = urllib.request.Request(f"{GEN_BASE}/v1/chat/completions", data=data, method="POST",
                headers={"Authorization": f"Bearer {sk}", "Content-Type": "application/json"})
            story = json.loads(urllib.request.urlopen(req, timeout=120).read())["choices"][0]["message"]["content"]
            # illustration
            img_req = urllib.request.Request(
                f"{GEN_BASE}/image/{urllib.parse.quote('storybook illustration, ' + idea[:120])}?model=flux&width=512&height=512&nologo=true",
                headers={"Authorization": f"Bearer {sk}"})
            art = urllib.request.urlopen(img_req, timeout=180).read()
            art_id = secrets.token_hex(8)
            open(f"/tmp/sf_art_{art_id}.png","wb").write(art)
            c = db(); c.execute("UPDATE users SET requests=requests+1 WHERE uid=?", (uid,)); c.commit()
            log_event("generate", USER_KEYS[uid]["github"])
            esc = story.replace("&","&amp;").replace("<","&lt;")
            result_html = f'<div class="story">{esc}</div><img class="art" src="/art/{art_id}.png" alt="illustration">'
        except urllib.error.HTTPError as e:
            result_html = f'<p class=err>Generation failed ({e.code}): your pollen balance may be exhausted.</p>'
        except Exception as e:
            result_html = f'<p class=err>Error: {type(e).__name__}</p>'
        self._send(APP_BODY.format(result=result_html))

    def do_GET_art(self):
        pass  # handled inside do_GET via route check below

    def log_message(self, fmt, *args):
        print(time.strftime("%Y-%m-%d %H:%M:%S"), self.address_string(), fmt % args, flush=True)

# serve generated artwork from /tmp
_orig_do_GET = Handler.do_GET
def do_GET_with_art(self):
    route = urllib.parse.urlparse(self.path)
    if route.path.startswith("/art/") and route.path.endswith(".png"):
        aid = route.path.split("/art/")[1].split(".")[0]
        path = f"/tmp/sf_art_{aid}.png"
        if os.path.exists(path):
            data = open(path,"rb").read()
            self.send_response(200); self.send_header("Content-Type","image/png")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
            return
        self.send_response(404); self.end_headers(); return
    _orig_do_GET(self)
Handler.do_GET = do_GET_with_art

if __name__ == "__main__":
    os.makedirs("/tmp/sf_art", exist_ok=True)
    print("StoryForge listening on 127.0.0.1:" + str(PORT), flush=True)
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
