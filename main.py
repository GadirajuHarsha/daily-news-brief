import os, asyncio, feedparser, datetime, hashlib, trafilatura, subprocess, random, glob, time, json
import numpy as np
import edge_tts
import chromadb
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import yt_dlp
from openai import AsyncOpenAI
from discord_webhook import DiscordWebhook
from dotenv import load_dotenv

load_dotenv()

MIN_STORY_FLOOR = 5 
MAX_PER_SECTION = 8
WORD_BUDGET = 2200
SEEN_FILE = "seen_stories.txt"

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Initialize ChromaDB efficiently
def get_chroma_db():
    chroma_client = chromadb.PersistentClient(path="./chroma_db")
    return chroma_client.get_or_create_collection(name="orator_news_rag")

collection = get_chroma_db()

# --- Utility Functions ---
def robust_extract(url):
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(downloaded)
            if text and len(text) > 100: return text
    except: pass
    return None

def get_cosine_similarity(text1, text2):
    vec1 = np.array([sum(ord(c) for c in text1)]) 
    vec2 = np.array([sum(ord(c) for c in text2)]) 
    num = np.dot(vec1, vec2)
    denom = np.linalg.norm(vec1) * np.linalg.norm(vec2)
    if denom == 0: return 0.0
    return float(num / denom)

def calculate_hybrid_score(text, raw_weight, user_multipliers):
    text_lower = text.lower()
    score = raw_weight
    for kw, mult in user_multipliers.items():
        if kw in text_lower: score *= mult
    
    # RAG memory check (last 72 hours)
    cutoff = time.time() - (72 * 3600)
    chroma_results = collection.query(
        query_texts=[text[:500]],
        n_results=1,
        where={"timestamp": {"$gt": cutoff}}
    )
    
    dup_penalty = 1.0
    if chroma_results and chroma_results['documents'] and chroma_results['documents'][0]:
        past_doc = chroma_results['documents'][0][0]
        sim = get_cosine_similarity(text[:1000], past_doc[:1000])
        if sim > 0.85: dup_penalty = 0.1 # Heavily penalize
    
    return score * dup_penalty

# --- Async Fetching Pipeline ---
async def producer(url, q, url_to_weight_dict):
    def fetch(): return feedparser.parse(url)
    try:
        feed = await asyncio.to_thread(fetch)
        for entry in feed.entries:
            link = entry.get("link", "")
            if not link: continue
            
            # Identify which categories this URL maps to in the massive dict
            categories = url_to_weight_dict.get(url, [])
            if not categories: continue
            
            await q.put((entry, categories))
    except Exception as e:
        print(f"Error fetching {url}: {e}")

async def consumer_worker(q, raw_entries_list):
    while True:
        try:
            item = await q.get()
            entry, categories = item
            link = entry.get("link")
            hsh = hashlib.md5(link.encode()).hexdigest()
            
            text = entry.get("summary", "")
            if len(text) < 150: text = await asyncio.to_thread(robust_extract, link) or text
            if len(text) < 100:
                q.task_done()
                continue
            
            raw_entries_list.append({
                "hash": hsh,
                "title": entry.get("title", ""),
                "link": link,
                "text": text,
                "categories": categories # e.g. [{"category_name": "sports", "weight": 20}]
            })
            q.task_done()
        except asyncio.CancelledError:
            break
        except Exception:
            q.task_done()

def generate_background_music(playlist_url):
    cid = os.getenv("SPOTIPY_CLIENT_ID")
    secret = os.getenv("SPOTIPY_CLIENT_SECRET")
    
    bg_music = "bg_music.mp3"; music_files = glob.glob("music/*.mp3")
    local_fallback = random.choice(music_files) if music_files else None
    
    if not playlist_url or not cid or not secret:
        return local_fallback
        
    try:
        sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(client_id=cid, client_secret=secret))
        results = sp.playlist_tracks(playlist_url)
        tracks = results['items']
        if not tracks: return local_fallback
        
        tr = random.choice(tracks)['track']
        query = f"{tr['name']} {tr['artists'][0]['name']} audio"
        
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': 'temp_bg_music',
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
            'quiet': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"ytsearch1:{query}"])
            
        return "temp_bg_music.mp3"
    except Exception as e:
        print(f"Spotify/YtDLP failure: {e}")
        return local_fallback

# --- Main Logic ---
async def process_user(user_name, user_config, scraped_articles, seen):
    print(f"\n--- Processing User: {user_name} ---")
    
    webhook_url = os.getenv(user_config.get("webhook_env", ""), "")
    if not webhook_url:
        print(f"WARNING: No valid webhook provided for {user_name}, skipping.")
        return seen
        
    discord_id = os.getenv(user_config.get("discord_user_id_env", ""), "User")
    playlist_url = user_config.get("spotify_playlist_url", "")
    feeds_conf = user_config.get("feeds", {})
    mult_conf = user_config.get("multipliers", {})

    # Score articles per user
    user_inventory = {cat: [] for cat in feeds_conf.keys()}
    
    # Pre-compute valid URLs for this user to save time
    valid_urls_for_user = set()
    for cat, items in feeds_conf.items():
        for i in items: valid_urls_for_user.add(i["url"])

    for art in scraped_articles:
        if art["hash"] in seen: continue
        
        # Check if the article's source is monitored by this user
        matched_categories = []
        for cdata in art["categories"]:
            if cdata["url"] in valid_urls_for_user:
                # Find which category this URL belongs to in the UX
                for ux_cat, ux_items in feeds_conf.items():
                    if any(x["url"] == cdata["url"] for x in ux_items):
                        matched_categories.append({"cat": ux_cat, "weight": cdata["weight"]})

        if not matched_categories: continue
        
        # We process the first matched category for simplicity
        best_cat = matched_categories[0]["cat"]
        score = calculate_hybrid_score(art["text"][:1500], matched_categories[0]["weight"], mult_conf)
        
        if score > 0:
            user_inventory[best_cat].append({
                "title": art["title"], "text": art["text"], "link": art["link"],
                "score": score, "hash": art["hash"]
            })

    # Top selections
    all_selected, segments = [], {}
    for cat in user_inventory:
        sorted_arts = sorted(user_inventory[cat], key=lambda x: x["score"], reverse=True)
        top = sorted_arts[:MAX_PER_SECTION]
        if len(top) >= MIN_STORY_FLOOR:
            segments[cat] = top
            all_selected.extend(top)

    if not all_selected:
        print(f"Not enough news for {user_name} today.")
        return seen

    print(f"[{user_name}] Generated {len(all_selected)} featured stories.")

    # LLM Script Gen
    file_date = datetime.datetime.now().strftime("%B %d, %Y")
    system_prompt = "You are Orator, an expert podcast host. Write an engaging, smooth narrative script reading the news. Output ONLY the words to be spoken. Do not use sound effect brackets. Speak naturally. Bridge topics smoothly."
    
    current_words = 0
    full_script = ""
    for cat, stories in segments.items():
        rem = WORD_BUDGET - current_words
        if rem < 200: break
        
        prompt = f"Write the {cat} segment using these stories. Aim for ~{(rem//len(segments))} words.\n\n"
        for s in stories: prompt += f"TITLE: {s['title']}\nCONTENT: {s['text'][:500]}\n\n"
        
        resp = await client.chat.completions.create(model="gpt-4o-mini", messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ], temperature=0.7)
        chunk = resp.choices[0].message.content.strip()
        full_script += "\n\n" + chunk
        current_words += len(chunk.split())

    # RAG Ingestion
    for s in all_selected:
        try:
            collection.add(
                documents=[s["text"][:1000]], metadatas=[{"timestamp": time.time()}], ids=[s["hash"]]
            )
            seen.add(s["hash"])
        except: pass

    # TTS & Mastering
    voice_file = f"voice_{user_name}.mp3"
    final_file = f"{file_date}_{user_name}_Orator.mp3"
    
    print(f"[{user_name}] Generating TTS (BrianNeural)...")
    await edge_tts.Communicate(full_script, 'en-US-BrianNeural').save(voice_file)
    
    bg_music = generate_background_music(playlist_url)
    
    print(f"[{user_name}] Mastering tracks...")
    if bg_music and os.path.exists(bg_music):
        cmd = [
            "ffmpeg", "-y", "-i", voice_file, "-stream_loop", "-1", "-i", bg_music, 
            "-filter_complex", 
            "[0:a]aresample=44100[v];"
            "[1:a]aresample=44100,volume=0.06[bg];"
            "[v][bg]amix=inputs=2:duration=first[out]",
            "-map", "[out]", "-ar", "44100", "-b:a", "128k", final_file
        ]
        subprocess.run(cmd, check=True)
    else:
        subprocess.run(["ffmpeg", "-y", "-i", voice_file, "-ar", "44100", "-b:a", "128k", final_file], check=True)

    # Delivery
    fsize_mb = os.path.getsize(final_file) / (1024 * 1024)
    print(f"[{user_name}] Uploading {fsize_mb:.2f} MB to Litterbox...")
    
    sources = "sources.txt"
    with open(sources, "w") as f: f.write("\n".join(s["link"] for s in all_selected))
    
    def deliver_payload_via_litterbox(fp):
        import requests
        try:
            resp_link = requests.post(
                "https://litterbox.catbox.moe/resources/internals/api.php", data={"reqtype": "fileupload", "time": "72h"}, files={"fileToUpload": open(fp, "rb")}
            )
            wh = DiscordWebhook(url=webhook_url, content=f"**{file_date} - ORATOR BRIEFING FOR <@{discord_id}>**\n\n🎙️ Listen (Expires 72h):\n{resp_link.text}")
            with open(sources, "rb") as fs: wh.add_file(file=fs.read(), filename="sources.txt")
            return wh.execute()
        except: return None
        
    def deliver_payload_direct(fp):
        wh = DiscordWebhook(url=webhook_url, content=f"**{file_date} - ORATOR FALLBACK**")
        with open(fp, "rb") as f2: wh.add_file(file=f2.read(), filename=fp)
        return wh.execute()

    resp = deliver_payload_via_litterbox(final_file)
    if resp and resp.status_code == 200:
        print(f"[{user_name}] Delivered successfully.")
    else:
        print(f"[{user_name}] Falling back to Discord direct upload.")
        fb_file = "fb_" + final_file
        subprocess.run(["ffmpeg", "-y", "-i", final_file, "-b:a", "64k", fb_file], check=True)
        deliver_payload_direct(fb_file)
        if os.path.exists(fb_file): os.remove(fb_file)

    for f in [voice_file, final_file, sources]:
        if os.path.exists(f): os.remove(f)
    if bg_music and bg_music.startswith("temp_bg_music") and os.path.exists(bg_music):
        os.remove(bg_music)

    return seen

async def main():
    print("Status: Starting Orator v4.0 Multi-User Pipeline")
    
    with open("users.json", "r") as f:
        USERS = json.load(f)
        
    seen = set()
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f: seen = set(line.strip() for line in f)

    # 1. Map all global urls to avoid duplicate scraping
    global_url_params = {}
    for user, info in USERS.items():
        for cat, feeds in info.get("feeds", {}).items():
            for fd in feeds:
                u = fd["url"]
                if u not in global_url_params: global_url_params[u] = []
                # attach raw weight for initial passing
                global_url_params[u].append({"category": cat, "weight": fd["weight"], "url": u})

    q = asyncio.Queue()
    raw_entries = []
    consumers = [asyncio.create_task(consumer_worker(q, raw_entries)) for _ in range(12)]
    
    print(f"Global Scraper initialized over {len(global_url_params)} unique endpoints...")
    producers = [asyncio.create_task(producer(u, q, global_url_params)) for u in global_url_params]
    
    await asyncio.gather(*producers)
    await q.join()
    for c in consumers: c.cancel()
    
    print(f"Scraped and extracted {len(raw_entries)} master articles.")

    # 2. Sequential User Build Loop
    for user, config in USERS.items():
        seen = await process_user(user, config, raw_entries, seen)

    with open(SEEN_FILE, "w") as f:
        f.write("\n".join(list(seen)[-300:])) # Purge memory to last 300 to keep it clean

    print("Pipeline finished successfully.")

if __name__ == "__main__":
    asyncio.run(main())