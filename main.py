import os, asyncio, feedparser, datetime, hashlib, trafilatura, subprocess, random, glob, time, json
import numpy as np
import edge_tts
import chromadb
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import yt_dlp
from openai import AsyncOpenAI
from discord_webhook import DiscordWebhook
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

MIN_STORY_FLOOR = 5 
MAX_PER_SECTION = 8
WORD_BUDGET = 2200
SEEN_FILE = "seen_stories.txt"

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

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
    denom = np.linalg.norm(vec1) * np.linalg.norm(vec2)
    return float(np.dot(vec1, vec2) / denom) if denom != 0 else 0.0

def calculate_hybrid_score(text, raw_weight, user_multipliers):
    text_lower = text.lower()
    score = raw_weight
    for kw, mult in user_multipliers.items():
        if kw in text_lower: score *= mult
    
    cutoff = time.time() - (72 * 3600)
    chroma_results = collection.query(
        query_texts=[text[:500]], n_results=1, where={"timestamp": {"$gt": cutoff}}
    )
    dup_penalty = 1.0
    if chroma_results and chroma_results['documents'] and chroma_results['documents'][0]:
        past_doc = chroma_results['documents'][0][0]
        if get_cosine_similarity(text[:1000], past_doc[:1000]) > 0.85: dup_penalty = 0.1
    return score * dup_penalty

# --- Async Fetching Pipeline ---
async def producer(url, q, url_to_weight_dict):
    def fetch(): return feedparser.parse(url)
    try:
        feed = await asyncio.to_thread(fetch)
        for entry in feed.entries:
            link = entry.get("link", "")
            if not link: continue
            categories = url_to_weight_dict.get(url, [])
            if categories: await q.put((entry, categories))
    except Exception: pass

async def consumer_worker(q, raw_entries_list):
    while True:
        try:
            item = await q.get()
            entry, categories = item
            link = entry.get("link")
            hsh = hashlib.md5(link.encode()).hexdigest()
            text = entry.get("summary", "")
            if len(text) < 150: text = await asyncio.to_thread(robust_extract, link) or text
            if len(text) >= 100:
                raw_entries_list.append({
                    "hash": hsh, "title": entry.get("title", ""),
                    "link": link, "text": text, "categories": categories
                })
            q.task_done()
        except asyncio.CancelledError: break
        except Exception: q.task_done()

def generate_background_music(playlist_url):
    cid = os.getenv("SPOTIPY_CLIENT_ID")
    secret = os.getenv("SPOTIPY_CLIENT_SECRET")
    local_fallback = random.choice(glob.glob("music/*.mp3")) if glob.glob("music/*.mp3") else None
    
    if not playlist_url or not cid or not secret: return local_fallback
        
    try:
        sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(client_id=cid, client_secret=secret))
        results = sp.playlist_tracks(playlist_url)
        tracks = results['items']
        if not tracks: return local_fallback
        
        tr = random.choice(tracks)['track']
        query = f"{tr['name']} {tr['artists'][0]['name']} audio"
        
        with yt_dlp.YoutubeDL({
            'format': 'bestaudio/best', 'outtmpl': 'temp_bg_music',
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}], 'quiet': True,
        }) as ydl:
            ydl.download([f"ytsearch1:{query}"])
        return "temp_bg_music.mp3"
    except Exception: return local_fallback

# --- Briefing Generation Engine ---
async def process_user(user_name, user_config, scraped_articles, seen):
    webhook_url = user_config.get("webhook_url_raw") or os.getenv(user_config.get("webhook_env", ""), "")
    if not webhook_url: return seen
        
    discord_id = user_config.get("discord_user_id_raw") or os.getenv(user_config.get("discord_user_id_env", ""), "User")
    playlist_url = user_config.get("spotify_playlist_url", "")
    feeds_conf = user_config.get("feeds", {})
    mult_conf = user_config.get("multipliers", {})

    user_inventory = {cat: [] for cat in feeds_conf.keys()}
    valid_urls_for_user = {i["url"] for items in feeds_conf.values() for i in items}

    for art in scraped_articles:
        if art["hash"] in seen: continue
        matched = []
        for cdata in art["categories"]:
            if cdata["url"] in valid_urls_for_user:
                for ux_cat, ux_items in feeds_conf.items():
                    if any(x["url"] == cdata["url"] for x in ux_items):
                        matched.append({"cat": ux_cat, "weight": cdata["weight"]})

        if not matched: continue
        best_cat = matched[0]["cat"]
        score = calculate_hybrid_score(art["text"][:1500], matched[0]["weight"], mult_conf)
        
        if score > 0:
            user_inventory[best_cat].append({
                "title": art["title"], "text": art["text"], "link": art["link"],
                "score": score, "hash": art["hash"]
            })

    all_selected, segments = [], {}
    for cat in user_inventory:
        top = sorted(user_inventory[cat], key=lambda x: x["score"], reverse=True)[:MAX_PER_SECTION]
        if len(top) >= MIN_STORY_FLOOR:
            segments[cat] = top
            all_selected.extend(top)

    if not all_selected: return seen

    file_date = datetime.datetime.now().strftime("%B %d, %Y")
    system_prompt = "You are Orator, an expert podcast host. Write an engaging, smooth narrative script reading the news. Output ONLY the words to be spoken. Do not use sound effect brackets. Speak naturally. Bridge topics smoothly."
    
    current_words = 0
    full_script = ""
    for cat, stories in segments.items():
        rem = WORD_BUDGET - current_words
        if rem < 200: break
        prompt = f"Write the {cat} segment. Aim for ~{(rem//len(segments))} words.\n\n"
        for s in stories: prompt += f"TITLE: {s['title']}\nCONTENT: {s['text'][:500]}\n\n"
        
        resp = await client.chat.completions.create(model="gpt-4o-mini", messages=[
            {"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}
        ], temperature=0.7)
        chunk = resp.choices[0].message.content.strip()
        full_script += "\n\n" + chunk
        current_words += len(chunk.split())

    for s in all_selected:
        try:
            collection.add(documents=[s["text"][:1000]], metadatas=[{"timestamp": time.time()}], ids=[s["hash"]])
            seen.add(s["hash"])
        except: pass

    voice_file, final_file, sources = f"voice_{user_name}.mp3", f"{file_date}_{user_name}_Orator.mp3", "sources.txt"
    await edge_tts.Communicate(full_script, 'en-US-BrianNeural').save(voice_file)
    bg_music = generate_background_music(playlist_url)
    
    if bg_music and os.path.exists(bg_music):
        subprocess.run([
            "ffmpeg", "-y", "-i", voice_file, "-stream_loop", "-1", "-i", bg_music, 
            "-filter_complex", "[0:a]aresample=44100[v];[1:a]aresample=44100,volume=0.06[bg];[v][bg]amix=inputs=2:duration=first[out]",
            "-map", "[out]", "-ar", "44100", "-b:a", "128k", final_file
        ], check=True)
    else: subprocess.run(["ffmpeg", "-y", "-i", voice_file, "-ar", "44100", "-b:a", "128k", final_file], check=True)

    with open(sources, "w") as f: f.write("\n".join(s["link"] for s in all_selected))
    
    def deliver_litterbox(fp):
        import requests
        try:
            resp_link = requests.post("https://litterbox.catbox.moe/resources/internals/api.php", data={"reqtype": "fileupload", "time": "72h"}, files={"fileToUpload": open(fp, "rb")})
            wh = DiscordWebhook(url=webhook_url, content=f"**{file_date} - ORATOR BRIEFING FOR <@{discord_id}>**\n\n🎙️ Listen:\n{resp_link.text}")
            with open(sources, "rb") as fs: wh.add_file(file=fs.read(), filename="sources.txt")
            return wh.execute()
        except: return None

    if not deliver_litterbox(final_file) or getattr(deliver_litterbox(final_file), 'status_code', 500) != 200:
        fb_file = "fb_" + final_file
        subprocess.run(["ffmpeg", "-y", "-i", final_file, "-b:a", "64k", fb_file], check=True)
        wh = DiscordWebhook(url=webhook_url, content=f"**{file_date} - ORATOR FALLBACK**")
        with open(fb_file, "rb") as f2: wh.add_file(file=f2.read(), filename=fb_file)
        wh.execute()
        if os.path.exists(fb_file): os.remove(fb_file)

    for f in [voice_file, final_file, sources]:
        if os.path.exists(f): os.remove(f)
    if bg_music and bg_music.startswith("temp_bg_music") and os.path.exists(bg_music): os.remove(bg_music)
    return seen

async def generate_global_briefings():
    try:
        with open("users.json", "r") as f: USERS = json.load(f)
    except: return
        
    seen = set(line.strip() for line in open(SEEN_FILE, "r")) if os.path.exists(SEEN_FILE) else set()
    global_url_params = {}
    for user, info in USERS.items():
        for cat, feeds in info.get("feeds", {}).items():
            for fd in feeds:
                u = fd["url"]
                if u not in global_url_params: global_url_params[u] = []
                global_url_params[u].append({"category": cat, "weight": fd["weight"], "url": u})

    q = asyncio.Queue()
    raw_entries = []
    consumers = [asyncio.create_task(consumer_worker(q, raw_entries)) for _ in range(12)]
    await asyncio.gather(*[asyncio.create_task(producer(u, q, global_url_params)) for u in global_url_params])
    await q.join(); [c.cancel() for c in consumers]
    
    for user, config in USERS.items(): seen = await process_user(user, config, raw_entries, seen)
    with open(SEEN_FILE, "w") as f: f.write("\n".join(list(seen)[-300:]))


# --- DISCORD BOT APPLICATION ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@tasks.loop(time=datetime.time(hour=13, minute=0, tzinfo=datetime.timezone.utc)) # 8:00 AM EST (13:00 UTC)
async def daily_brief_timer():
    print("Executing standard scheduled briefings...")
    await generate_global_briefings()

@bot.event
async def on_ready():
    print(f"Orator Bot {bot.user} has successfully booted and intercepted the server matrix.")
    if not daily_brief_timer.is_running():
        daily_brief_timer.start()

@bot.command()
async def tune(ctx, keyword: str, weight: float):
    user_id = str(ctx.author.id)
    try:
        with open("users.json", "r") as f: users_data = json.load(f)
        if user_id not in users_data:
            await ctx.send("Error: You are not registered in the database. Use `!onboard <webhook>` first.")
            return
        users_data[user_id]["multipliers"][keyword.lower()] = weight
        with open("users.json", "w") as f: json.dump(users_data, f, indent=4)
        await ctx.send(f"✅ Successfully tuned your platform: Algorithm multiplier for `{keyword}` mathematically set to `{weight}`x.")
    except Exception as e:
        await ctx.send(f"Fatal error adjusting parameters: {e}")

@bot.command()
async def onboard(ctx, webhook: str):
    user_id = str(ctx.author.id)
    try:
        try:
            with open("users.json", "r") as f: users = json.load(f)
        except:
            users = {}
            
        if user_id in users:
            await ctx.send(f"Your configuration already exists! Use `!tune <topic> <weight>` to edit your profile.")
            return
            
        users[user_id] = {
            "webhook_url_raw": webhook, "discord_user_id_raw": user_id, "spotify_playlist_url": "https://open.spotify.com/playlist/37i9dQZF1DXc8kgYqQLKWv",
            "feeds": { "world": [{"url": "https://apnews.com/hub/politics.rss", "weight": 15}] },
            "multipliers": { "technology": 1.5 }
        }
        with open("users.json", "w") as f: json.dump(users, f, indent=4)
        await ctx.send(f"🎉 Bootstrapped securely into the Orator grid! You completely govern your own state. Use `!tune <category> <weight>` to add properties or `!set_spotify <url>` to change music.")
    except Exception as e: await ctx.send(f"Failure initializing local node: {e}")

@bot.command()
async def set_spotify(ctx, spotify_url: str):
    user_id = str(ctx.author.id)
    try:
        with open("users.json", "r") as f: users = json.load(f)
        if user_id not in users:
            await ctx.send("Please `!onboard` yourself first.")
            return
        users[user_id]["spotify_playlist_url"] = spotify_url
        with open("users.json", "w") as f: json.dump(users, f, indent=4)
        await ctx.send("✅ Customized your podcast background music stream.")
    except Exception as e: await ctx.send("Error modifying music schema.")

@bot.command()
async def force_briefing(ctx):
    await ctx.send("Manually forcing global extraction protocols. Expect audio drops shortly...")
    await generate_global_briefings()

if __name__ == "__main__":
    bot_token = os.getenv("DISCORD_BOT_TOKEN")
    if bot_token:
        bot.run(bot_token)
    else:
        print("CRITICAL MISSING: DISCORD_BOT_TOKEN is not mapped correctly in .env!")