import os
import sys
import json
import logging
import requests
from datetime import datetime, timezone

# ---------- CONFIG ----------
M3U_URL        = "https://raw.githubusercontent.com/johhnypandey-afk/iptv/refs/heads/main/iptv.m3u"
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
STATE_FILE      = "state.json"
FAIL_THRESHOLD  = 2 
TIMEOUT         = 15
USER_AGENT      = "Mozilla/5.0 (compatible; M3U8-Checker/1.0)"
# ----------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def fetch_playlist(url):
    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return r.text

def parse_m3u(text):
    name = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line: continue
        if line.startswith("#EXTINF"):
            name = line.rsplit(",", 1)[-1].strip() if "," in line else "unknown"
        elif not line.startswith("#"):
            yield (name or "unknown", line)
            name = None

def check_stream(url):
    try:
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, allow_redirects=True, stream=True)
        if r.status_code != 200: return False, f"HTTP {r.status_code}"
        head = next(r.iter_content(chunk_size=2048), b"").decode("utf-8", "ignore")
        r.close()
        if "#EXTM3U" not in head and "#EXT-X-" not in head: return False, "invalid m3u8"
        return True, "OK"
    except requests.exceptions.Timeout: return False, "timeout"
    except Exception as e: return False, str(e)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f: return json.load(f)
        except: pass
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f)

def send_discord(content, embeds=None):
    payload = {"content": content}
    if embeds: payload["embeds"] = embeds
    try:
        requests.post(DISCORD_WEBHOOK, json=payload, timeout=15)
    except Exception as e:
        logging.error(f"Discord error: {e}")

def main():
    if not DISCORD_WEBHOOK:
        logging.error("DISCORD_WEBHOOK environment variable is not set.")
        sys.exit(1)

    logging.info(f"Fetching playlist: {M3U_URL}")
    try:
        text = fetch_playlist(M3U_URL)
    except Exception as e:
        logging.error(f"Cannot fetch playlist: {e}")
        send_discord(f"⚠️ **Playlist fetch failed**\n`{M3U_URL}`\nError: `{e}`")
        sys.exit(1)

    entries = list(parse_m3u(text))
    state = load_state()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    newly_down, recovered, up_count = [], [], 0

    for name, url in entries:
        ok, detail = check_stream(url)
        prev = state.get(url, {"fails": 0, "alerted": False, "name": name})

        if ok:
            up_count += 1
            if prev.get("alerted"): recovered.append((name, url))
            state[url] = {"fails": 0, "alerted": False, "name": name}
        else:
            fails = prev.get("fails", 0) + 1
            alerted = prev.get("alerted", False)
            if fails >= FAIL_THRESHOLD and not alerted:
                newly_down.append((name, url, detail, fails))
                alerted = True
            state[url] = {"fails": fails, "alerted": alerted, "name": name}

    embeds = []
    for name, url, detail, fails in newly_down:
        embeds.append({"title": f"🔴 DOWN: {name}", "description": f"Failed **{fails}** checks.\n```{detail}```", "color": 0xE74C3C, "fields": [{"name": "URL", "value": f"`{url}`"}]})
    for name, url in recovered:
        embeds.append({"title": f"🟢 RECOVERED: {name}", "description": "Stream is back online.", "color": 0x2ECC71, "fields": [{"name": "URL", "value": f"`{url}`"}]})

    if embeds:
        for i in range(0, len(embeds), 10):
            send_discord(f"**M3U8 Health Report** — {now}\nUp: `{up_count}/{len(entries)}` | Down: `{len(newly_down)}` | Recovered: `{len(recovered)}`", embeds=embeds[i:i+10])

    save_state(state)
    logging.info("Done.")

if __name__ == "__main__":
    main()
