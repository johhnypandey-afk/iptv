import os
import sys
import json
import logging
import requests
import subprocess
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
    """Checks stream, takes screenshot, and tests speed."""
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
        pass 

    # 3. Speed Test (Download up to 10MB to get a better average)
    speeds = []
    try:
        start = time.time()
        res = requests.get(url, timeout=10, headers={"User-Agent": USER_AGENT}, stream=True)
        bytes_downloaded = 0
        for chunk in res.iter_content(chunk_size=1024*1024):
            bytes_downloaded += len(chunk)
            if bytes_downloaded > 10 * 1024 * 1024: # Stop after 10MB
                break
        end = time.time()
        if end - start > 0:
            speed = (bytes_downloaded * 8) / (end - start) / 1_000_000 # Mbps
            speeds.append(speed)
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

def send_discord_batch(embeds, files=None):
    """Sends a single batch of up to 10 embeds to Discord."""
    payload = {"embeds": embeds}
    try:
        requests.post(
            DISCORD_WEBHOOK, 
            data={"payload_json": json.dumps(payload)}, 
            files=files, 
            timeout=30
        )
    except Exception as e:
        logging.error(f"Discord error: {e}")

def main():
    if not DISCORD_WEBHOOK:
        logging.error("DISCORD_WEBHOOK not set.")
        sys.exit(1)

    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    
    logging.info(f"Fetching playlist: {M3U_URL}")
    try:
        text = fetch_playlist(M3U_URL)
    except Exception as e:
        send_discord_batch([{"title": "⚠️ Playlist fetch failed", "description": str(e), "color": 0xE74C3C}])
        sys.exit(1)

    entries = list(parse_m3u(text))
    state = load_state()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(check_stream_and_speed, url, name): (name, url) for name, url in entries}
        for future in futures:
            name, url = futures[future]
            try:
                ok, detail, speed, img = future.result()
                results.append({"name": name, "url": url, "ok": ok, "detail": detail, "speed": speed, "img": img})
            except Exception as e:
                results.append({"name": name, "url": url, "ok": False, "detail": str(e), "speed": 0.0, "img": None})

    newly_down, recovered, up_count = [], [], 0
    speed_lines = []

    for res in results:
        url = res["url"]
        prev = state.get(url, {"fails": 0, "alerted": False, "name": res["name"]})
        
        if res["ok"]:
            up_count += 1
            if prev.get("alerted"): recovered.append((res["name"], url))
            state[url] = {"fails": 0, "alerted": False, "name": res["name"]}
            speed_lines.append(f"🟢 **{res['name']}** - `{res['speed']} Mbps`")
        else:
            fails = prev.get("fails", 0) + 1
            alerted = prev.get("alerted", False)
            if fails >= FAIL_THRESHOLD and not alerted:
                newly_down.append((res["name"], url, res["detail"], fails))
                alerted = True
            state[url] = {"fails": fails, "alerted": alerted, "name": res["name"]}
            speed_lines.append(f"🔴 **{res['name']}** - DOWN")

    save_state(state)

    # Prepare all images for Discord
    images = [f for f in os.listdir(SCREENSHOT_DIR) if f.endswith(".jpg")]
    logging.info(f"Uploading {len(images)} screenshots to Discord...")

    # We need to send images in batches of 10 (Discord limit)
    for i in range(0, len(images), 10):
        batch_images = images[i:i+10]
        files = {}
        embeds = []
        
        for idx, img_name in enumerate(batch_images):
            file_key = f"image{idx}"
            img_path = os.path.join(SCREENSHOT_DIR, img_name)
            files[file_key] = (img_name, open(img_path, "rb"), "image/jpeg")
            
            # Create an embed for each image so it shows up beautifully
            embeds.append({
                "title": f"📺 {img_name.replace('.jpg', '')}",
                "image": {"url": f"attachment://{img_name}"},
                "color": 0x2ECC71
            })
            
        send_discord_batch(embeds, files=files)
        # Close file handles
        for f in files.values():
            f[1].close()
        time.sleep(2) # Prevent Discord rate limits

    # Now send the Text Reports (Chunked into 10 embeds per message)
    logging.info("Sending text reports...")
    
    # Split the speed lines into chunks of 15 lines to fit in embeds
    chunk_size = 15
    speed_chunks = [speed_lines[i:i + chunk_size] for i in range(0, len(speed_lines), chunk_size)]
    
    report_embeds = []
    for i, chunk in enumerate(speed_chunks):
        report_embeds.append({
            "title": f"📊 Stream Speeds (Part {i+1})",
            "description": "\n".join(chunk),
            "color": 0x3498DB
        })

    # Send the text reports in batches of 10 embeds
    for i in range(0, len(report_embeds), 10):
        batch = report_embeds[i:i+10]
        # Add a header to the very first message
        if i == 0:
            header = {
                "title": f"📈 M3U8 Health Report — {now}",
                "description": f"**Total Streams:** `{len(entries)}`\n**Up:** `{up_count}`\n**Down:** `{len(newly_down)}`\n**Recovered:** `{len(recovered)}`",
                "color": 0x9B59B6
            }
            batch.insert(0, header)
        
        send_discord_batch(batch)
        time.sleep(1)

    logging.info("Done.")

if __name__ == "__main__":
    main()
