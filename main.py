import os, asyncio, feedparser, datetime, hashlib, trafilatura, edge_tts, subprocess, requests, random, glob, sys
from openai import OpenAI
from discord_webhook import DiscordWebhook

# feeds with base priorities. stratified selection prevents source monopoly.
FEEDS = {
    "politics": [
        {"url": "https://www.pbs.org/newshour/feeds/rss/politics", "priority": 4},
        {"url": "https://prospect.org/api/rss/content.rss", "priority": 3},
        {"url": "https://thehill.com/homenews/feed/", "priority": 3},
        {"url": "https://www.theguardian.com/us-news/rss", "priority": 2},
        {"url": "https://apnews.com/hub/politics.rss", "priority": 4}
    ],
    "sports": [
        {"url": "https://www.espn.com/espn/rss/nba/news", "priority": 3},
        {"url": "https://feeds.hoopshype.com/xml/rumors.xml", "priority": 4},
        {"url": "https://texaslonghorns.com/rss?path=general", "priority": 3},
        {"url": "https://texaslonghorns.com/rss?path=football", "priority": 3}
    ],
    "tech": [
        {"url": "https://www.engadget.com/rss.xml", "priority": 3},
        {"url": "https://www.wired.com/feed/category/gear/latest/rss", "priority": 3},
        {"url": "https://arstechnica.com/feed/", "priority": 3},
        {"url": "https://www.theverge.com/rss/index.xml", "priority": 2}
    ],
    "media": [
        {"url": "https://www.serebii.net/index.rss", "priority": 5},
        {"url": "https://www.animenewsnetwork.com/all/rss.xml?ann-edition=us", "priority": 3},
        {"url": "https://www.nintendolife.com/feeds/latest", "priority": 3},
        {"url": "https://pitchfork.com/rss/news/", "priority": 3},
        {"url": "https://hypebeast.com/music/feed", "priority": 3}
    ]
}

# structural quotas to force politics/sports >> tech/media
STORY_LIMITS = {"politics": 10, "sports": 10, "media": 3, "tech": 3}
SEGMENT_TIMES = {"politics": 8, "sports": 8, "media": 2, "tech": 2}
SEEN_FILE = "seen_stories.txt"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# checks audio length via ffprobe
def get_audio_duration(file_path):
    try:
        cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return float(result.stdout.strip())
    except: return 0

# gathers diverse news and enforces source variety
def get_stratified_stories(name, configs, seen_hashes):
    now = datetime.datetime.now(datetime.timezone.utc)
    category_pool, seen_topics, source_counts = [], set(), {}
    for cfg in configs:
        feed = feedparser.parse(cfg['url'])
        for e in feed.entries:
            title = e.title.lower()
            if any(k in title for k in ["2025", "september"]): continue
            topic_key = " ".join(title.split()[:4])
            if topic_key in seen_topics: continue
            pub_date = getattr(e, 'published_parsed', None)
            if pub_date:
                dt = datetime.datetime(*pub_date[:6], tzinfo=datetime.timezone.utc)
                if (now - dt).days > 2: continue 
            h = hashlib.md5(e.title.encode()).hexdigest()
            if h in seen_hashes: continue
            
            # diversity check: limit stories per source
            if source_counts.get(cfg['url'], 0) >= 2: continue
            source_counts[cfg['url']] = source_counts.get(cfg['url'], 0) + 1
            seen_topics.add(topic_key)
            category_pool.append({"entry": e, "hash": h, "priority": cfg['priority']})
    category_pool.sort(key=lambda x: x['priority'], reverse=True)
    return category_pool[:STORY_LIMITS[name]]

# generates segment script with specific structural detail
async def generate_podcast_segment(name, configs, seen_hashes, date_str, attempt):
    stories = get_stratified_stories(name, configs, seen_hashes)
    if not stories: return "", [], []
    payload, hashes, links = "", [], []
    for item in stories:
        e = item['entry']
        text = trafilatura.extract(trafilatura.fetch_url(e.link))
        payload += f"STORY: {e.title}\nFACTS: {text[:2500] if text else e.summary}\n\n"
        hashes.append(item['hash'])
        links.append(f"[{name.upper()}] {e.title} - {e.link}")

    # structural budgeting
    style = "Write 4-5 DENSE paragraphs per story. Be exhaustive." if SEGMENT_TIMES[name] > 5 else "Summarize briefly."
    prompt = f"You are Orator. Today is {date_str}. Write the {name} segment. {style} NO intros/outros. NO asterisks."
    
    resp = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt + f"\n\nDATA:\n{payload}"}], temperature=0.2)
    return resp.choices[0].message.content, hashes, links

# main orchestrator
async def main():
    cst_now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=6)
    date_str, file_date = cst_now.strftime("%A, %B %d, %Y"), cst_now.strftime("%Y-%m-%d")
    if not os.path.exists(SEEN_FILE): open(SEEN_FILE, 'w').close()
    with open(SEEN_FILE, "r") as f: seen_hashes = set(line.strip() for line in f)
    final_file = f"{file_date}_Orator.mp3"
    notice = ""

    for attempt in range(1, 4):
        full_segment_text, all_links, all_hashes = [], [], []
        for name, configs in FEEDS.items():
            script, hashes, links = await generate_podcast_segment(name, configs, seen_hashes, date_str, attempt)
            full_segment_text.append(script)
            all_hashes.extend(hashes)
            all_links.extend(links)
        
        full_text = f"Hello, I'm Orator, and this is your briefing for {date_str}.\n\n" + "\n\n".join(full_segment_text) + "\n\nThat concludes today's briefing. Goodbye."
        voice_file = "voice.mp3"
        await edge_tts.Communicate(full_text, "en-US-AndrewNeural", rate="+22%").save(voice_file)
        
        # bg music mixing
        bg_music = "bg_music.mp3"; music_files = glob.glob("music/*.mp3")
        if music_files: bg_music = random.choice(music_files)
        if os.path.exists(bg_music):
            subprocess.run(["ffmpeg", "-y", "-i", voice_file, "-stream_loop", "-1", "-i", bg_music, "-filter_complex", "[1:a]volume=0.06[bg];[0:a][bg]amix=inputs=2:duration=first", final_file])
        else: os.rename(voice_file, final_file)

        duration = get_audio_duration(final_file)
        if duration >= 900 or attempt == 3:
            if duration < 900: notice = "\n\n⚠️ *Threshold Notice: Under 15m.*"
            break

    webhook_content = f"**{file_date} - ORATOR DAILY NEWS FOR <@{os.getenv('DISCORD_USER_ID')}>**{notice}"
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