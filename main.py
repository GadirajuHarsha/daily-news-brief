import os, asyncio, feedparser, datetime, hashlib, trafilatura, edge_tts, subprocess, requests, random, glob, sys
# from PIL import Image  # Commented out as requested
# from io import BytesIO
# from bs4 import BeautifulSoup
from openai import OpenAI
from discord_webhook import DiscordWebhook

# --- ORATOR CONFIGURATION ---
FEEDS = {
    "politics": [
        {"url": "https://www.pbs.org/newshour/feeds/rss/politics", "priority": 3},
        {"url": "https://prospect.org/api/rss/content.rss", "priority": 3},
        {"url": "https://jacobin.com/feed", "priority": 2},
        {"url": "https://www.theguardian.com/us-news/rss", "priority": 2}
    ],
    "sports": [
        {"url": "https://www.espn.com/espn/rss/nba/news", "priority": 3},
        {"url": "https://feeds.hoopshype.com/xml/rumors.xml", "priority": 3},
        {"url": "https://thedailytexan.com/category/sports/feed/", "priority": 2},
        {"url": "https://texaslonghorns.com/rss?path=general", "priority": 2}
    ],
    "tech": [
        {"url": "https://www.engadget.com/rss.xml", "priority": 3},
        {"url": "https://www.wired.com/feed/category/gear/latest/rss", "priority": 3},
        {"url": "https://arstechnica.com/feed/", "priority": 2},
        {"url": "https://www.theverge.com/rss/index.xml", "priority": 2}
    ],
    "media": [
        {"url": "https://www.serebii.net/index.rss", "priority": 3},
        {"url": "https://www.animenewsnetwork.com/all/rss.xml?ann-edition=us", "priority": 3},
        {"url": "https://pitchfork.com/rss/news/", "priority": 2},
        {"url": "https://hypebeast.com/music/feed", "priority": 2}
    ]
}

SEGMENT_TIMES = {"politics": 6, "sports": 7, "media": 4, "tech": 3}
SEEN_FILE = "seen_stories.txt"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- UTILS ---

def get_audio_duration(file_path):
    try:
        cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return float(result.stdout.strip())
    except: return 0

def get_stratified_stories(configs, seen_hashes):
    """FORCED DIVERSITY: Grabs stories from every feed to prevent source monopoly."""
    now = datetime.datetime.now(datetime.timezone.utc)
    category_pool = []
    
    # 1. Iterate through every feed to ensure one story from EACH is picked first
    for cfg in configs:
        feed = feedparser.parse(cfg['url'])
        feed_entries = []
        for e in feed.entries:
            title = e.title.lower()
            if any(k in title for k in ["2025", "september"]): continue
            
            h = hashlib.md5(e.title.encode()).hexdigest()
            if h in seen_hashes: continue
            
            pub_date = getattr(e, 'published_parsed', None)
            if pub_date:
                dt = datetime.datetime(*pub_date[:6], tzinfo=datetime.timezone.utc)
                if (now - dt).days > 2: continue 
            
            feed_entries.append({"entry": e, "hash": h, "priority": cfg['priority']})
        
        # Diversity: Grab up to 3 stories from this specific URL
        category_pool.extend(feed_entries[:3])
    
    # 2. Final sort by priority
    category_pool.sort(key=lambda x: x['priority'], reverse=True)
    return category_pool[:12]

async def generate_podcast_script(date_str, seen_hashes, attempt_num=1):
    segment_scripts = []
    source_links = []

    for name, configs in FEEDS.items():
        stories = get_stratified_stories(configs, seen_hashes)
        if not stories: continue

        payload = ""
        for item in stories:
            e = item['entry']
            text = trafilatura.extract(trafilatura.fetch_url(e.link))
            payload += f"STORY: {e.title}\nFACTS: {text[:2000] if text else e.summary}\n\n"
            source_links.append(f"[{name.upper()}] {e.title} - {e.link}")

        # Scale word target based on attempt to force length
        word_target = int(SEGMENT_TIMES[name] * (160 + (attempt_num * 30)))
        prompt = (f"You are Orator. Today is {date_str}. Write the {name} segment. "
                  f"Target {word_target} words. NO intros. NO symbols. NO asterisks.")
        
        resp = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt + f"\n\nDATA:\n{payload}"}], temperature=0.1)
        segment_scripts.append(resp.choices[0].message.content)

    full_text = f"Hello, I'm Orator, and this is your comprehensive briefing for {date_str}.\n\n" + "\n\n".join(segment_scripts) + "\n\nThat concludes today's Orator briefing. Goodbye."
    return full_text, source_links

async def main():
    cst_now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=6)
    date_str = cst_now.strftime("%A, %B %d, %Y")
    file_date = cst_now.strftime("%Y-%m-%d")
    
    if not os.path.exists(SEEN_FILE): open(SEEN_FILE, 'w').close()
    with open(SEEN_FILE, "r") as f: seen_hashes = set(line.strip() for line in f)

    final_file = f"{file_date}_godiraju_Orator.mp3"
    notice = ""

    for attempt in range(1, 4):
        full_text, source_links = await generate_podcast_script(date_str, seen_hashes, attempt)
        voice_file = "voice.mp3"
        await edge_tts.Communicate(full_text, "en-US-AndrewNeural", rate="+22%").save(voice_file)
        
        # RANDOM MUSIC SELECTION
        bg_music = "bg_music.mp3"; music_files = glob.glob("music/*.mp3")
        if music_files: bg_music = random.choice(music_files)
        
        if os.path.exists(bg_music):
            subprocess.run(["ffmpeg", "-y", "-i", voice_file, "-stream_loop", "-1", "-i", bg_music, "-filter_complex", "[1:a]volume=0.08[bg];[0:a][bg]amix=inputs=2:duration=first", final_file])
        else: os.rename(voice_file, final_file)

        duration = get_audio_duration(final_file)
        if duration >= 900 or attempt == 3:
            if duration < 900: notice = "\n\n⚠️ *Threshold Notice: Under 15m target.*"
            break
        print(f"Attempt {attempt} too short ({duration/60:.2f}m). Retrying...")

    # DELIVER
    webhook_content = f"**{file_date} - - ORATOR DAILY NEWS FOR <@{os.getenv('DISCORD_USER_ID')}>**{notice}"
    webhook = DiscordWebhook(url=os.getenv("DISCORD_WEBHOOK_URL"), content=webhook_content)
    with open(final_file, "rb") as f: webhook.add_file(file=f.read(), filename=final_file)
    with open("sources.txt", "w") as f: f.write("\n".join(source_links))
    with open("sources.txt", "rb") as f: webhook.add_file(file=f.read(), filename="sources.txt")
    webhook.execute()
    
    # Save Hashes
    with open(SEEN_FILE, "a") as f:
        for link in source_links:
            h = hashlib.md5(link.encode()).hexdigest()
            f.write(f"{h}\n")

    for f in ["voice.mp3", final_file, "sources.txt"]: 
        if os.path.exists(f): os.remove(f)

if __name__ == "__main__":
    asyncio.run(main())