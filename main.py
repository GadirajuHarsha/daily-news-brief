import os, asyncio, feedparser, datetime, hashlib, trafilatura, edge_tts, subprocess, requests, random, glob, sys
from openai import OpenAI
from discord_webhook import DiscordWebhook

# --- CONFIGURATION ---
FEEDS = {
    "politics": [
        {"url": "https://www.pbs.org/newshour/feeds/rss/politics", "priority": 4},
        {"url": "https://prospect.org/api/rss/content.rss", "priority": 3},
        {"url": "https://thehill.com/homenews/feed/", "priority": 3},
        {"url": "https://www.theguardian.com/us-news/rss", "priority": 2},
        {"url": "https://apnews.com/hub/politics.rss", "priority": 4}
    ],
    "sports": [
        {"url": "https://www.espn.com/espn/rss/nba/news", "priority": 4},
        {"url": "https://feeds.hoopshype.com/xml/rumors.xml", "priority": 4},
        {"url": "https://basketball.realgm.com/rss/wiretap/0/0.xml", "priority": 3},
        {"url": "https://texaslonghorns.com/rss?path=general", "priority": 3},
        {"url": "https://thedailytexan.com/category/sports/feed/", "priority": 2}
    ],
    "tech": [
        {"url": "https://www.engadget.com/rss.xml", "priority": 3},
        {"url": "https://www.wired.com/feed/category/gear/latest/rss", "priority": 3},
        {"url": "https://arstechnica.com/feed/", "priority": 3}
    ],
    "media": [
        {"url": "https://www.serebii.net/index.rss", "priority": 5},
        {"url": "https://www.animenewsnetwork.com/all/rss.xml?ann-edition=us", "priority": 3},
        {"url": "https://pitchfork.com/rss/news/", "priority": 3},
        {"url": "https://hypebeast.com/music/feed", "priority": 3}
    ]
}

# structural quotas: politics/sports (10 stories) vs tech/media (3-4 stories)
STORY_LIMITS = {"politics": 10, "sports": 10, "media": 4, "tech": 4}
SEGMENT_TIMES = {"politics": 8, "sports": 8, "media": 2, "tech": 2}
SEEN_FILE = "seen_stories.txt"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def get_audio_duration(file_path):
    """checks audio length via ffprobe"""
    try:
        cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=True)
        return float(result.stdout.strip())
    except: return 0

def get_stratified_stories(name, configs, seen_hashes):
    """gathers diverse news with a wider temporal window if needed"""
    now = datetime.datetime.now(datetime.timezone.utc)
    category_pool = []
    seen_topics = set()
    
    # 72-hour window to ensure we don't skip categories like Sports on slow days
    time_threshold = 3 

    for cfg in configs:
        feed = feedparser.parse(cfg['url'])
        if not feed.entries: continue
        
        count_from_this_feed = 0
        for e in feed.entries:
            title = e.title.lower()
            # hard block 2025/September hallucinations
            if any(k in title for k in ["2025", "september"]): continue
            
            # semantic de-duplication
            topic_key = " ".join(title.split()[:4])
            if topic_key in seen_topics: continue
            
            # temporal check
            pub_date = getattr(e, 'published_parsed', None)
            if pub_date:
                dt = datetime.datetime(*pub_date[:6], tzinfo=datetime.timezone.utc)
                if (now - dt).days > time_threshold: continue 
            
            h = hashlib.md5(e.title.encode()).hexdigest()
            if h in seen_hashes: continue
            
            # allow more stories per feed for the high-volume segments
            if count_from_this_feed >= 4: break 
            
            seen_topics.add(topic_key)
            category_pool.append({"entry": e, "hash": h, "priority": cfg['priority']})
            count_from_this_feed += 1
            
    category_pool.sort(key=lambda x: x['priority'], reverse=True)
    return category_pool[:STORY_LIMITS[name]]

async def generate_podcast_segment(name, configs, seen_hashes, date_str, attempt):
    """generates deep segment scripts with high word-count targets"""
    stories = get_stratified_stories(name, configs, seen_hashes)
    if not stories: 
        print(f"DEBUG: No stories found for {name}")
        return "", [], []
    
    payload = ""
    hashes, links = [], []
    for item in stories:
        e = item['entry']
        text = trafilatura.extract(trafilatura.fetch_url(e.link))
        # Use more text to give the AI more to talk about
        payload += f"STORY: {e.title}\nFACTS: {text[:3500] if text else e.summary}\n\n"
        hashes.append(item['hash'])
        links.append(f"[{name.upper()}] {e.title} - {e.link}")

    # Aggressive word targets to hit 8 minutes
    word_target = int(SEGMENT_TIMES[name] * (180 + (attempt * 40)))
    detail_level = "Provide granular, step-by-step facts. Use quotes and statistics." if SEGMENT_TIMES[name] > 5 else "Summarize into 3 key facts."

    prompt = (f"You are Orator. Today is {date_str}. Write the {name} segment. "
              f"MANDATORY TARGET: {word_target} words. {detail_level} "
              f"NO intros/outros. NO asterisks or markdown symbols.")
    
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt + f"\n\nDATA:\n{payload}"}],
        temperature=0.2
    )
    return resp.choices[0].message.content, hashes, links

async def main():
    """main orchestrator"""
    cst_now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=6)
    date_str, file_date = cst_now.strftime("%A, %B %d, %Y"), cst_now.strftime("%Y-%m-%d")
    
    if not os.path.exists(SEEN_FILE): open(SEEN_FILE, 'w').close()
    with open(SEEN_FILE, "r") as f: seen_hashes = set(line.strip() for line in f)
    
    final_file = f"{file_date}_godiraju_Orator.mp3"
    notice = ""

    for attempt in range(1, 4):
        full_segment_text, all_links, all_hashes = [], [], []
        for name, configs in FEEDS.items():
            script, hashes, links = await generate_podcast_segment(name, configs, seen_hashes, date_str, attempt)
            if script:
                full_segment_text.append(script)
                all_hashes.extend(hashes)
                all_links.extend(links)
        
        full_text = f"Hello, I'm Orator, and this is your briefing for {date_str}.\n\n" + "\n\n".join(full_segment_text) + "\n\nThat concludes today's Orator briefing. Goodbye."
        voice_file = "voice.mp3"
        await edge_tts.Communicate(full_text, "en-US-AndrewNeural", rate="+22%").save(voice_file)
        
        bg_music = "bg_music.mp3"; music_files = glob.glob("music/*.mp3")
        if music_files: bg_music = random.choice(music_files)
        
        if os.path.exists(bg_music):
            # VOLUME AT 0.02 TO OVERCOMPENSATE (VERY QUIET)
            subprocess.run(["ffmpeg", "-y", "-i", voice_file, "-stream_loop", "-1", "-i", bg_music, "-filter_complex", "[1:a]volume=0.02[bg];[0:a][bg]amix=inputs=2:duration=first", final_file], check=True)
        else: os.rename(voice_file, final_file)

        duration = get_audio_duration(final_file)
        if duration >= 1080 or attempt == 3: # 18m target
            if duration < 900: notice = f"\n\n⚠️ *Threshold Notice: {duration/60:.1f}m briefing today.*"
            break
        print(f"Attempt {attempt} too short ({duration/60:.2f}m). Retrying with more detail...")

    webhook_content = f"**{file_date} - - ORATOR DAILY NEWS FOR <@{os.getenv('DISCORD_USER_ID')}>**{notice}"
    webhook = DiscordWebhook(url=os.getenv("DISCORD_WEBHOOK_URL"), content=webhook_content)
    with open(final_file, "rb") as f: webhook.add_file(file=f.read(), filename=final_file)
    with open("sources.txt", "w") as f: f.write("\n".join(all_links))
    with open("sources.txt", "rb") as f: webhook.add_file(file=f.read(), filename="sources.txt")
    webhook.execute()
    
    with open(SEEN_FILE, "a") as f:
        for h in all_hashes: f.write(f"{h}\n")
    for f in ["voice.mp3", final_file, "sources.txt"]: 
        if os.path.exists(f): os.remove(f)

if __name__ == "__main__":
    asyncio.run(main())