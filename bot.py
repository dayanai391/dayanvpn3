#!/usr/bin/env python3
# DayanVPN Panel - Telegram admin bot (stdlib only)
# Env: BOT_TOKEN, ADMIN_IDS (comma-separated telegram ids),
#      SERVERS (comma-separated "domain:TAG"), GITHUB_TOKEN, GIST_ID (optional backup)

import json, os, re, time, uuid, random, string, threading, subprocess, urllib.request, urllib.parse

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
SERVERS_RAW = os.environ.get("SERVERS", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GIST_ID = os.environ.get("GIST_ID", "")
GB = 1024 ** 3

CONFIG_PATH = "/etc/xray/config.json"
HTML_DIR = "/usr/share/nginx/html"
GEN_DIR = "/etc/nginx/gen"
STATE_LOCAL = "/data/state.json"
ACCESS_LOG = "/var/log/nginx/access.log"
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ---------------- state ----------------

def default_state():
    return {"clients": [], "counters": {}, "blocked": []}

state = default_state()
state_lock = threading.Lock()

def load_state_local():
    try:
        with open(STATE_LOCAL) as f:
            return json.load(f)
    except Exception:
        return default_state()

def save_state_local():
    os.makedirs("/data", exist_ok=True)
    with open(STATE_LOCAL, "w") as f:
        json.dump(state, f)

def gist_save():
    if not (GITHUB_TOKEN and GIST_ID):
        return
    try:
        body = json.dumps({"files": {"state.json": {"content": json.dumps(state)}}}).encode()
        req = urllib.request.Request(
            f"https://api.github.com/gists/{GIST_ID}", data=body, method="PATCH",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "User-Agent": "dayanvpn"})
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:
        print("gist save failed:", e)

def gist_load():
    if not (GITHUB_TOKEN and GIST_ID):
        return None
    try:
        req = urllib.request.Request(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "User-Agent": "dayanvpn"})
        data = json.load(urllib.request.urlopen(req, timeout=20))
        return json.loads(data["files"]["state.json"]["content"])
    except Exception as e:
        print("gist load failed:", e)
        return None

def gist_create():
    global GIST_ID
    if not GITHUB_TOKEN:
        return
    try:
        body = json.dumps({
            "description": "dayanvpn-panel-state", "public": False,
            "files": {"state.json": {"content": json.dumps(default_state())}}}).encode()
        req = urllib.request.Request(
            "https://api.github.com/gists", data=body, method="POST",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "User-Agent": "dayanvpn"})
        data = json.load(urllib.request.urlopen(req, timeout=20))
        GIST_ID = data["id"]
        print("Created gist:", GIST_ID, "- set GIST_ID env to this value")
    except Exception as e:
        print("gist create failed:", e)

def persist(backup=True):
    save_state_local()
    if backup:
        gist_save()

# ---------------- telegram ----------------

def tg(method, **params):
    try:
        req = urllib.request.Request(f"{API}/{method}",
            data=urllib.parse.urlencode(params).encode(), method="POST")
        return json.load(urllib.request.urlopen(req, timeout=60))
    except Exception as e:
        print("tg error:", e)
        return {}

def send(chat, text):
    # telegram messages max 4096
    for i in range(0, len(text), 4000):
        tg("sendMessage", chat_id=chat, text=text[i:i+4000])

# ---------------- helpers ----------------

def parse_servers():
    out = []
    for part in SERVERS_RAW.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            d, t = part.rsplit(":", 1)
        else:
            d, t = part, ""
        out.append((d, t))
    return out

def new_code():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=6))

def gb_fmt(n):
    return f"{n / GB:.2f}".rstrip("0").rstrip(".")

# ---------------- xray + nginx config generation ----------------

def write_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)

def rebuild_config():
    inbounds, ports_map, xports_map = [], [], []
    clients = state["clients"]
    for i, c in enumerate(clients):
        ws_port = 21000 + i
        xh_port = 22000 + i
        code = c["code"]
        base_client = {"id": c["uuid"], "email": code, "flow": ""}
        inbounds.append({
            "listen": "127.0.0.1", "port": ws_port, "protocol": "vless",
            "settings": {"clients": [dict(base_client)], "decryption": "none"},
            "streamSettings": {"network": "ws", "wsSettings": {"path": f"/u/{code}"}},
            "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]}})
        inbounds.append({
            "listen": "127.0.0.1", "port": xh_port, "protocol": "vless",
            "settings": {"clients": [dict(base_client)], "decryption": "none"},
            "streamSettings": {"network": "xhttp", "xhttpSettings": {"path": f"/x/{code}"}},
            "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]}})
        ports_map.append(f'"/u/{code}" {ws_port};')
        xports_map.append(f'"/x/{code}" {xh_port};')

    config = {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": [{"protocol": "freedom", "tag": "direct"}]}
    write_file(CONFIG_PATH, json.dumps(config, indent=2))
    write_file(f"{GEN_DIR}/ports.map", "\n".join(ports_map) + ("\n" if ports_map else ""))
    write_file(f"{GEN_DIR}/xports.map", "\n".join(xports_map) + ("\n" if xports_map else ""))
    write_blocked_map()
    restart_xray()
    reload_nginx()

def write_blocked_map():
    lines = [f'"/u/{c}" 1;' for c in state["blocked"]] + [f'"/x/{c}" 1;' for c in state["blocked"]]
    write_file(f"{GEN_DIR}/blocked.map", "\n".join(lines) + ("\n" if lines else ""))

def restart_xray():
    subprocess.Popen(["pkill", "-f", "xray run"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def reload_nginx():
    subprocess.Popen(["nginx", "-s", "reload"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def write_sub_files():
    servers = parse_servers()
    for c in state["clients"]:
        links = []
        for d, tag in servers:
            name = f"Dayan-{c['name']}" + (f"-{tag}" if tag else "")
            links.append(
                f"vless://{c['uuid']}@{d}:443?encryption=none&security=tls&sni={d}"
                f"&type=ws&host={d}&path=%2Fu%2F{c['code']}#{name}")
            links.append(
                f"vless://{c['uuid']}@{d}:443?encryption=none&security=tls&sni={d}"
                f"&type=xhttp&path=%2Fx%2F{c['code']}&mode=auto#{name}-X")
        content = "\n".join(links).encode()
        import base64
        write_file(f"{HTML_DIR}/sub/{c['code']}", base64.b64encode(content).decode())

# ---------------- enforcement ----------------

def check_limits():
    changed = False
    now = time.time()
    with state_lock:
        for c in state["clients"]:
            used = state["counters"].get(c["code"], 0)
            expired = now > c["expires"]
            over = used >= c["gb"] * GB
            if (expired or over) and c["code"] not in state["blocked"]:
                state["blocked"].append(c["code"])
                changed = True
        if changed:
            write_blocked_map()
            reload_nginx()
            persist()
    return changed

def log_tailer():
    pos = 0
    try:
        pos = os.path.getsize(ACCESS_LOG)
    except Exception:
        pass
    rx = re.compile(r'"(?:GET|HEAD|POST|PUT) /u/([a-z0-9]{6}) HTTP/[\d.]+" (\d{3}) (\d+)')
    while True:
        try:
            with open(ACCESS_LOG) as f:
                f.seek(pos)
                for line in f:
                    m = rx.search(line)
                    if m and m.group(2) in ("101", "200"):
                        code = m.group(1)
                        with state_lock:
                            state["counters"][code] = state["counters"].get(code, 0) + int(m.group(3))
                pos = f.tell()
        except FileNotFoundError:
            pass
        time.sleep(5)

def periodic_persist():
    while True:
        time.sleep(120)
        with state_lock:
            persist()
        check_limits()

# ---------------- commands ----------------

def cmd_new(chat, args):
    if len(args) < 3:
        send(chat, "فرمت درست:\n/new نام گیگ روز\nمثال: /new reza 10 30")
        return
    name = re.sub(r"[^A-Za-z0-9_-]", "", args[0])[:16] or "user"
    try:
        gb = float(args[1]); days = int(args[2])
    except ValueError:
        send(chat, "گیگ و روز باید عدد باشن.")
        return
    c = {"code": new_code(), "name": name, "uuid": str(uuid.uuid4()),
         "gb": gb, "expires": time.time() + days * 86400, "created": time.time()}
    with state_lock:
        state["clients"].append(c)
        rebuild_config()
        write_sub_files()
        persist()
    servers = parse_servers()
    if not servers:
        send(chat, "⚠️ متغیر SERVERS خالیه! لینک ساخته شد ولی سروری توش نیست.")
        sub = ""
    else:
        sub = f"https://{servers[0][0]}/sub/{c['code']}"
    send(chat,
         f"✅ مشتری ساخته شد\n"
         f"نام: {c['name']}\n"
         f"کد: {c['code']}\n"
         f"حجم: {gb_fmt(gb)} گیگ | مدت: {days} روز\n\n"
         f"🔗 لینک اشتراک:\n{sub}\n\n"
         f"این لینک رو براش بفرست. توی v2rayNG: منو ← Online Subscription ← paste ← Update.")

def cmd_list(chat, args):
    with state_lock:
        clients = list(state["clients"])
        counters = dict(state["counters"])
        blocked = set(state["blocked"])
    if not clients:
        send(chat, "هنوز مشتری نداری. با /new شروع کن.")
        return
    lines = []
    for c in clients:
        used = counters.get(c["code"], 0)
        days_left = max(0, int((c["expires"] - time.time()) / 86400))
        st = "🚫قطع" if c["code"] in blocked else "✅"
        lines.append(
            f"{st} {c['name']} ({c['code']})\n"
            f"   حجم: {gb_fmt(used)}/{gb_fmt(c['gb'])} گیگ | {days_left} روز مانده")
    send(chat, "📋 مشتری‌ها:\n\n" + "\n".join(lines))

def cmd_del(chat, args):
    if not args:
        send(chat, "فرمت: /del کد\nمثال: /del ab3x9k")
        return
    code = args[0].strip().lower()
    with state_lock:
        before = len(state["clients"])
        state["clients"] = [c for c in state["clients"] if c["code"] != code]
        state["counters"].pop(code, None)
        if code in state["blocked"]:
            state["blocked"].remove(code)
        if len(state["clients"]) == before:
            send(chat, "کد پیدا نشد.")
            return
        rebuild_config()
        persist()
    try:
        os.remove(f"{HTML_DIR}/sub/{code}")
    except OSError:
        pass
    send(chat, f"🗑 مشتری {code} حذف شد و کانفیگش از سرور پرید.")

def cmd_topup(chat, args):
    if len(args) < 3:
        send(chat, "فرمت: /topup کد گیگ روز\nمثال: /topup ab3x9k 10 30")
        return
    code, gb_s, days_s = args[0].strip().lower(), args[1], args[2]
    try:
        add_gb = float(gb_s); add_days = int(days_s)
    except ValueError:
        send(chat, "عدد درست وارد کن.")
        return
    with state_lock:
        c = next((x for x in state["clients"] if x["code"] == code), None)
        if not c:
            send(chat, "کد پیدا نشد.")
            return
        c["gb"] += add_gb
        c["expires"] = max(c["expires"], time.time()) + add_days * 86400
        if code in state["blocked"]:
            state["blocked"].remove(code)
            write_blocked_map()
            reload_nginx()
        persist()
    send(chat, f"🔁 شارژ شد: {c['name']} ← +{gb_fmt(add_gb)} گیگ و +{add_days} روز. دوباره فعاله.")

def cmd_backup(chat, args):
    with state_lock:
        gist_save()
    send(chat, "💾 بکاپ ذخیره شد." if (GITHUB_TOKEN and GIST_ID)
         else "⚠️ GITHUB_TOKEN یا GIST_ID تنظیم نیست — بکاپ فقط محلیه.")

def handle(msg):
    m = msg.get("message", msg)
    chat = m.get("chat", {}).get("id")
    uid = m.get("from", {}).get("id")
    text = (m.get("text") or "").strip()
    if not chat or uid not in ADMIN_IDS or not text.startswith("/"):
        return
    parts = text.split()
    cmd, args = parts[0].split("@")[0].lower(), parts[1:]
    if cmd == "/new":
        cmd_new(chat, args)
    elif cmd == "/list":
        cmd_list(chat, args)
    elif cmd == "/del":
        cmd_del(chat, args)
    elif cmd == "/topup":
        cmd_topup(chat, args)
    elif cmd == "/backup":
        cmd_backup(chat, args)
    else:
        send(chat,
             "دستورات:\n"
             "/new نام گیگ روز — مشتری جدید\n"
             "/list — لیست و مصرف\n"
             "/del کد — حذف مشتری\n"
             "/topup کد گیگ روز — شارژ\n"
             "/backup — بکاپ دستی")

def poll_loop():
    offset = 0
    while True:
        try:
            data = tg("getUpdates", offset=offset, timeout=50,
                      allowed_updates=json.dumps(["message"]))
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                handle(upd)
        except Exception as e:
            print("poll error:", e)
            time.sleep(3)

# ---------------- main ----------------

def main():
    global state
    if not BOT_TOKEN or not ADMIN_IDS:
        print("BOT_TOKEN and ADMIN_IDS are required"); return
    os.makedirs(GEN_DIR, exist_ok=True)
    os.makedirs(f"{HTML_DIR}/sub", exist_ok=True)
    for f in ("ports.map", "xports.map", "blocked.map"):
        write_file(f"{GEN_DIR}/{f}", "")

    remote = gist_load()
    state = remote if remote else load_state_local()
    if GITHUB_TOKEN and not GIST_ID:
        gist_create()

    rebuild_config()
    write_sub_files()

    threading.Thread(target=log_tailer, daemon=True).start()
    threading.Thread(target=periodic_persist, daemon=True).start()
    print("Bot started.")
    poll_loop()

if __name__ == "__main__":
    main()
