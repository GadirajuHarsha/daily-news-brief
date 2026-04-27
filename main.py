import os, asyncio, feedparser, datetime, hashlib, trafilatura, subprocess, random, glob, time, json, urllib.parse
import numpy as np
import edge_tts
import chromadb
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import yt_dlp
from openai import AsyncOpenAI
import discord
from discord.ext import commands, tasks
from discord import ui
from dotenv import load_dotenv

load_dotenv()

MIN_STORY_FLOOR = 5 
MAX_PER_SECTION = 8
WORD_BUDGET = 2200
SEEN_FILE = "seen_stories.txt"

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
collection = chromadb.PersistentClient(path="./chroma_db").get_or_create_collection(name="orator_news_rag")

def robust_extract(url):
    try:
        if (d := trafilatura.fetch_url(url)) and (text := trafilatura.extract(d)) and len(text) > 100: return text
    except: pass
    return None

def get_cosine_similarity(text1, text2):
    v1, v2 = np.array([sum(ord(c) for c in text1)]), np.array([sum(ord(c) for c in text2)])
    denom = np.linalg.norm(v1) * np.linalg.norm(v2)
    return float(np.dot(v1, v2) / denom) if denom != 0 else 0.0

def calculate_hybrid_score(text, raw_weight, user_multipliers):
    text_lower = text.lower()
    score = raw_weight
    for kw, mult in user_multipliers.items():
        if kw in text_lower: score *= mult
    
    past = collection.query(query_texts=[text[:500]], n_results=1, where={"timestamp": {"$gt": time.time() - (72 * 3600)}})
    if past and past['documents'] and past['documents'][0] and get_cosine_similarity(text[:1000], past['documents'][0][0][:1000]) > 0.85:
        return score * 0.1
    return score

async def producer(url, q, url_to_weight_dict):
    try:
        feed = await asyncio.to_thread(lambda: feedparser.parse(url))
        for entry in feed.entries:
            if (l := entry.get("link", "")) and (categories := url_to_weight_dict.get(url, [])): await q.put((entry, categories))
    except: pass

async def consumer_worker(q, raw_entries_list):
    while True:
        try:
            entry, categories = await q.get()
            link, text = entry.get("link"), entry.get("summary", "")
            if len(text) < 150: text = await asyncio.to_thread(robust_extract, link) or text
            if len(text) >= 100:
                raw_entries_list.append({"hash": hashlib.md5(link.encode()).hexdigest(), "title": entry.get("title", ""), "link": link, "text": text, "categories": categories})
            q.task_done()
        except asyncio.CancelledError: break
        except: q.task_done()

def generate_background_music(url):
    cid, secret = os.getenv("SPOTIPY_CLIENT_ID"), os.getenv("SPOTIPY_CLIENT_SECRET")
    fallback = random.choice(glob.glob("music/*.mp3")) if glob.glob("music/*.mp3") else None
    if not url or not cid or not secret: return fallback
    try:
        sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(client_id=cid, client_secret=secret))
        if not (tracks := sp.playlist_tracks(url)['items']): return fallback
        tr = random.choice(tracks)['track']
        query = f"{tr['name']} {tr['artists'][0]['name']} audio"
        with yt_dlp.YoutubeDL({'format': 'bestaudio/best', 'outtmpl': 'temp_bg_music', 'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}], 'quiet': True}) as ydl: ydl.download([f"ytsearch1:{query}"])
        return "temp_bg_music.mp3"
    except: return fallback

# --- DELIVERY / GENERATION ---
async def process_user(bot, user_name, user_config, scraped_articles, seen):
    channel_id = user_config.get("discord_channel_id")
    if not channel_id: return seen
    
    ch = bot.get_channel(int(channel_id))
    if not ch: return seen

    playlist_url = user_config.get("spotify_playlist_url", "")
    feeds_conf = user_config.get("feeds", {})
    mult_conf = user_config.get("multipliers", {})

    user_inventory = {cat: [] for cat in feeds_conf.keys()}
    valid_urls_for_user = {i["url"] for items in feeds_conf.values() for i in items}

    for art in scraped_articles:
        if art["hash"] in seen: continue
        if matched := [{"cat": ux_cat, "weight": cdata["weight"]} for cdata in art["categories"] if cdata["url"] in valid_urls_for_user for ux_cat, ux_items in feeds_conf.items() if any(x["url"] == cdata["url"] for x in ux_items)]:
            if (score := calculate_hybrid_score(art["text"][:1500], matched[0]["weight"], mult_conf)) > 0:
                user_inventory[matched[0]["cat"]].append({**art, "score": score})

    segments, all_selected = {}, []
    for cat in user_inventory:
        if len(top := sorted(user_inventory[cat], key=lambda x: x["score"], reverse=True)[:MAX_PER_SECTION]) >= MIN_STORY_FLOOR:
            segments[cat] = top; all_selected.extend(top)

    if not all_selected: return seen

    sys_prompt = "You are Orator. Extract and narrate the core thesis of the following article verbatim, ensuring you preserve the original author's exact voice, while cleanly compressing the contextual edge details into rapid-fire bullet-point sentences. Speak naturally. Output ONLY the words to be spoken."
    full_script, cw = "", 0

    for cat, stories in segments.items():
        if (rem := WORD_BUDGET - cw) < 200: break
        prompt = f"Write the {cat} segment. Aim for ~{(rem//len(segments))} words.\n\n" + "".join(f"TITLE: {s['title']}\nCONTENT: {s['text'][:500]}\n\n" for s in stories)
        chunk = (await client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": prompt}], temperature=0.7)).choices[0].message.content.strip()
        full_script += "\n\n" + chunk; cw += len(chunk.split())

    for s in all_selected:
        try: collection.add(documents=[s["text"][:1000]], metadatas=[{"timestamp": time.time()}], ids=[s["hash"]]); seen.add(s["hash"])
        except: pass

    fn = f"{datetime.datetime.now().strftime('%B_%d')}_{user_name}.mp3"
    await edge_tts.Communicate(full_script, 'en-US-BrianNeural').save("v.mp3")
    
    if (bg := generate_background_music(playlist_url)) and os.path.exists(bg):
        subprocess.run(["ffmpeg", "-y", "-i", "v.mp3", "-stream_loop", "-1", "-i", bg, "-filter_complex", "[0:a]aresample=44100[v];[1:a]aresample=44100,volume=0.06[bg];[v][bg]amix=inputs=2:duration=first[out]", "-map", "[out]", "-ar", "44100", "-b:a", "128k", fn], check=True)
    else: subprocess.run(["ffmpeg", "-y", "-i", "v.mp3", "-ar", "44100", "-b:a", "128k", fn], check=True)

    sources = "\n".join(s["link"] for s in all_selected)
    await ch.send(content="**🎙️ Your Daily Briefing is Here.**\nSources:\n" + "\n".join(s["link"][:80]+"..." for s in all_selected[:5]), file=discord.File(fn))

    for f in ["v.mp3", fn, bg]:
        if f and os.path.exists(f): os.remove(f)
    return seen

async def generate_global_briefings(bot):
    try:
        with open("users.json", "r") as f: USERS = json.load(f)
    except: return
    seen = set(line.strip() for line in open(SEEN_FILE, "r")) if os.path.exists(SEEN_FILE) else set()
    global_url_params = {}
    for user, info in USERS.items():
        for cat, feeds in info.get("feeds", {}).items():
            for fd in feeds:
                if fd["url"] not in global_url_params: global_url_params[fd["url"]] = []
                global_url_params[fd["url"]].append({"category": cat, "weight": fd["weight"], "url": fd["url"]})

    q = asyncio.Queue()
    raw_entries = []
    consumers = [asyncio.create_task(consumer_worker(q, raw_entries)) for _ in range(12)]
    await asyncio.gather(*[asyncio.create_task(producer(u, q, global_url_params)) for u in global_url_params])
    await q.join(); [c.cancel() for c in consumers]
    for user, config in USERS.items(): seen = await process_user(bot, user, config, raw_entries, seen)
    with open(SEEN_FILE, "w") as f: f.write("\n".join(list(seen)[-300:]))

# --- DISCORD UI APP ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

class AddTopicModal(ui.Modal, title='Add a New News Topic'):
    topic_query = ui.TextInput(label='What topic do you want to add?', style=discord.TextStyle.paragraph)
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        topic = self.topic_query.value; uid = str(interaction.user.id)
        u = f"https://news.google.com/rss/search?q={urllib.parse.quote(topic)}"
        with open("users.json") as f: data = json.load(f)
        if uid in data:
            data[uid]["feeds"].setdefault("dynamic", []).append({"url": u, "weight": 15})
            data[uid]["multipliers"][topic.lower()[:15]] = 2.0
            with open("users.json", "w") as f: json.dump(data, f, indent=4)
            with open("miss_analytics.txt", "a") as f: f.write(f"[{datetime.datetime.now()}] RSS DB Fallback Sync: {topic}\n")
            await interaction.followup.send(f"✅ Added `{topic}` to your daily podcast! We will aggressively pull news about this from now on.", ephemeral=True)

class TuningView(ui.View):
    def __init__(self, cat: str):
        super().__init__(); self.cat = cat
    async def adjust(self, i: discord.Interaction, adj: float):
        uid = str(i.user.id)
        with open("users.json") as f: data = json.load(f)
        c = data.get(uid, {}).get("multipliers", {}).get(self.cat, 1.0)
        data[uid]["multipliers"][self.cat] = round(max(0.0, c + adj), 3)
        with open("users.json", "w") as f: json.dump(data, f, indent=4)
        await i.response.send_message(f"✅ Adjusted `{self.cat}` successfully!", ephemeral=True)
    @ui.button(label='[ -- ]', style=discord.ButtonStyle.danger)
    async def d2(self, i, b): await self.adjust(i, -1.0)
    @ui.button(label='[ - ]', style=discord.ButtonStyle.secondary)
    async def d1(self, i, b): await self.adjust(i, -0.5)
    @ui.button(label='[ + ]', style=discord.ButtonStyle.secondary)
    async def u1(self, i, b): await self.adjust(i, 0.5)
    @ui.button(label='[ ++ ]', style=discord.ButtonStyle.primary)
    async def u2(self, i, b): await self.adjust(i, 1.0)
    @ui.button(label='Add New Topic', style=discord.ButtonStyle.success, row=1)
    async def newt(self, i, b): await i.response.send_modal(AddTopicModal())

class SpotifyModal(ui.Modal, title='Add Spotify Background Music'):
    url_input = ui.TextInput(label='Paste your Spotify Playlist Link here', style=discord.TextStyle.short)
    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        with open("users.json") as f: data = json.load(f)
        data[uid]["spotify_playlist_url"] = self.url_input.value
        with open("users.json", "w") as f: json.dump(data, f, indent=4)
        await interaction.response.send_message("✅ Spotify background music synchronized.", ephemeral=True)

@bot.command()
async def onboard(ctx):
    uid = str(ctx.author.id)
    try:
        with open("users.json") as f: users = json.load(f)
    except: users = {}
    if uid in users: return await ctx.send("You already have a podcast set up! Use `!tune` to adjust your topics.")
    
    overwrites = {ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False), ctx.author: discord.PermissionOverwrite(read_messages=True)}
    ch = await ctx.guild.create_text_channel(name=f"{ctx.author.name.lower()}-news-pod", overwrites=overwrites)
    
    users[uid] = {
        "discord_channel_id": str(ch.id), "spotify_playlist_url": "https://open.spotify.com/playlist/37i9dQZF1DXc8kgYqQLKWv",
        "feeds": {"world": [{"url": "https://apnews.com/hub/politics.rss", "weight": 15}]}, "multipliers": {"gaming": 1.5}
    }
    with open("users.json", "w") as f: json.dump(users, f, indent=4)
    await ctx.send(f"🎉 Welcome to Orator! I created your private news channel right here: <#{ch.id}>. Your daily podcast will drop there every morning!")

@bot.command()
async def tune(ctx, category: str):
    await ctx.send(f"**Tuning your preferences for:** `{category}`", view=TuningView(category))

@bot.command()
async def music(ctx):
    await ctx.send("Add your Custom Spotify Playlist:", view=type("MV", (ui.View,), {"b": ui.button(label="Link Spotify", style=discord.ButtonStyle.blurple)(lambda s,i,b: i.response.send_modal(SpotifyModal()))})())

@bot.command()
async def nerds(ctx):
    uid = str(ctx.author.id)
    with open("users.json") as f: data = json.load(f)
    if uid not in data: return await ctx.send("Unregistered endpoint.")
    c = data[uid]
    text = "**[ ADVANCED PODCAST DIAGNOSTICS ]**\n"
    text += "```json\n" + json.dumps({"feeds": c.get("feeds"), "multipliers": c.get("multipliers")}, indent=2) + "\n```"
    await ctx.send(text)

@tasks.loop(time=datetime.time(hour=13, minute=0, tzinfo=datetime.timezone.utc))
async def daily_brief_timer(): await generate_global_briefings(bot)

@bot.event
async def on_ready():
    print(f"Orator Bot {bot.user} GUI Environment Active.")
    if not daily_brief_timer.is_running(): daily_brief_timer.start()

if __name__ == "__main__":
    b_tok = os.getenv("DISCORD_BOT_TOKEN")
    if b_tok: bot.run(b_tok)
    else: print("CRITICAL ERROR: Discord Token Offline in .env")