import os
import sys
import json
import logging
import requests
import subprocess
import zipfile
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

# ---------- CONFIG ----------
M3U_URL        = "https://raw.githubusercontent.com/johhnypandey-afk/iptv/refs/heads/main/iptv.m3u"
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
STATE_FILE      = "state.json"
FAIL_THRESHOLD  = 2 
TIMEOUT         = 15
USER_AGENT      = "Mozilla/5.0 (compatible; M3U8-Checker/1.0)"
SCREENSHOT_DIR  = "screenshots"
ZIP_NAME        = "screenshots.zip"
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

def check_stream_and_speed(url, name):
    """Checks stream, takes screenshot, and tests speed 3 times."""
    # 1. Check Status
    try:
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, allow_redirects=True, stream=True)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}", 0.0, None
        head = next(r.iter_content(chunk_size=2048), b"").decode("utf-8", "ignore")
        r.close()
        if "#EXTM3U" not in head and "#EXT-X-" not in head:
            return False, "invalid m3u8", 0.0, None
    except Exception as e:
        return False, str(e), 0.0, None

    # 2. Screenshot
    clean_name = "".join(c for c in name if c.isalnum() or c in " -_").strip()
    img_path = os.path.join(SCREENSHOT_DIR, f"{clean_name}.jpg")
    ffmpeg_cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-ss", "00:00:05", 
        "-i", url, "-vframes", "1", "-vf", "scale=640:-1", img_path
    ]
    try:
        subprocess.run(ffmpeg_cmd, timeout=25, check=False, capture_output=True)
    except:
        pass # Screenshot failed, but stream might still be up

    # 3. Speed Test (3 times)
    speeds = []
    for _ in range(3):
        try:
            start = time.time()
            # Download a small chunk to measure speed
            res = requests.get(url, timeout=10, headers={"User-Agent": USER_AGENT}, stream=True)
            bytes_downloaded = 0
            for chunk in res.iter_content(chunk_size=1024*1024):
                bytes_downloaded += len(chunk)
                if bytes_downloaded > 3 * 1024 * 1024: # Stop after 3MB
                    break
            end = time.time()
            if end - start > 0:
                speeds.append((bytes_downloaded * 8) / (end - start) / 1_000_000) # Mbps
            res.close()
        except:
            pass

    avg_speed = round(sum(speeds) / len(speeds), 2) if speeds else 0.0
    return True, "OK", avg_speed, img_path if os.path.exists(img_path) else None

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f: return json.load(f)
        except: pass
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f)

def send_discord(content, embeds=None, zip_path=None):
    payload = {"content": content}
    if embeds: payload["embeds"] = embeds
    
    files = {}
    if zip_path and os.path.exists(zip_path):
        files["file"] = (ZIP_NAME, open(zip_path, "rb"), "application/zip")

    try:
        requests.post(DISCORD_WEBHOOK, data={"payload_json": json.dumps(payload)}, files=files, timeout=30)
    except Exception as e:
        logging.error(f"Discord error: {e}")
    finally:
        if zip_path and os.path.exists(zip_path) and "file" in files:
            files["file"][1].close()

def main():
    if not DISCORD_WEBHOOK:
        logging.error("DISCORD_WEBHOOK not set.")
        sys.exit(1)

    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    
    logging.info(f"Fetching playlist: {M3U_URL}")
    try:
        text = fetch_playlist(M3U_URL)
    except Exception as e:
        send_discord(f"⚠️ **Playlist fetch failed**\n`{M3U_URL}`\nError: `{e}`")
        sys.exit(1)

    entries = list(parse_m3u(text))
    state = load_state()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    results = []
    # Use ThreadPoolExecutor to process streams in parallel (faster)
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(check_stream_and_speed, url, name): (name, url) for name, url in entries}
        for future in futures:
            name, url = futures[future]
            try:
                ok, detail, speed, img = future.result()
                results.append({"name": name, "url": url, "ok": ok, "detail": detail, "speed": speed, "img": img})
            except Exception as e:
                results.append({"name": name, "url": url, "ok": False, "detail": str(e), "speed": 0.0, "img": None})

    # Process State & Alerts
    newly_down, recovered, up_count = [], [], 0
    speed_report = []

    for res in results:
        url = res["url"]
        prev = state.get(url, {"fails": 0, "alerted": False, "name": res["name"]})
        
        if res["ok"]:
            up_count += 1
            if prev.get("alerted"): recovered.append((res["name"], url))
            state[url] = {"fails": 0, "alerted": False, "name": res["name"]}
            speed_report.append(f"🟢 **{res['name']}** - `{res['speed']} Mbps`")
        else:
            fails = prev.get("fails", 0) + 1
            alerted = prev.get("alerted", False)
            if fails >= FAIL_THRESHOLD and not alerted:
                newly_down.append((res["name"], url, res["detail"], fails))
                alerted = True
            state[url] = {"fails": fails, "alerted": alerted, "name": res["name"]}
            speed_report.append(f"🔴 **{res['name']}** - DOWN")

    save_state(state)

    # Create ZIP of screenshots
    zip_path = None
    if os.listdir(SCREENSHOT_DIR):
        with zipfile.ZipFile(ZIP_NAME, 'w') as zipf:
            for root, _, files in os.walk(SCREENSHOT_DIR):
                for file in files:
                    zipf.write(os.path.join(root, file), file)
        zip_path = ZIP_NAME

    # Build Discord Embed
    embeds = []
    for name, url, detail, fails in newly_down:
        embeds.append({"title": f"🔴 DOWN: {name}", "description": f"Failed **{fails}** checks.\n```{detail}```", "color": 0xE74C3C, "fields": [{"name": "URL", "value": f"`{url}`"}]})
    for name, url in recovered:
        embeds.append({"title": f"🟢 RECOVERED: {name}", "description": "Stream is back online.", "color": 0x2ECC71})

    # Send Summary Embed (First 10 speeds to avoid Discord limits)
    summary_text = "\n".join(speed_report[:10]) 
    if len(speed_report) > 10:
        summary_text += f"\n*...and {len(speed_report)-10} more streams*"
        
    embeds.insert(0, {
        "title": f"📊 M3U8 Hourly Report — {now}",
        "description": f"Up: `{up_count}/{len(entries)}` | Down: `{len(newly_down)}` | Recovered: `{len(recovered)}`\n\n**Average Speeds (Mbps):**\n{summary_text}",
        "color": 0x3498DB
    })

    send_discord("📦 **Screenshots attached below!**", embeds=embeds[:10], zip_path=zip_path)
    logging.info("Done.")

if __name__ == "__main__":
    main()
