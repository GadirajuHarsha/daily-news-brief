import os
import asyncio
import feedparser
import datetime
import hashlib
import trafilatura
import edge_tts
from openai import OpenAI
from discord_webhook import DiscordWebhook

# --- ORATOR CONFIGURATION ---
FEEDS = {
    "politics": [
        {"url": "https://www.hoover.org/publications/hoover-daily-report/feed", "priority": 3}, # Econ focus
        {"url": "https://www.pbs.org/newshour/feeds/rss/politics", "priority": 3},
        {"url": "https://www.theguardian.com/us-news/rss", "priority": 2}
    ],
    "sports": [
        {"url": "https://www.espn.com/espn/rss/nba/news", "priority": 3},
        {"url": "https://feeds.hoopshype.com/xml/rumors.xml", "priority": 2},
        {"url": "https://thedailytexan.com/category/sports/feed/", "priority": 2},
        {"url": "https://texaslonghorns.com/rss?path=general", "priority": 2}
    ],
    "tech": [
        {"url": "https://www.engadget.com/rss.xml", "priority": 3},
        {"url": "https://www.wired.com/feed/category/gear/latest/rss", "priority": 3}
    ],
    "media": [
        {"url": "https://www.serebii.net/index.rss", "priority": 3},
        {"url": "https://www.animenewsnetwork.com/all/rss.xml?ann-edition=us", "priority": 3},
        {"url": "https://pitchfork.com/rss/news/", "priority": 2},
        {"url": "https://hypebeast.com/music/feed", "priority": 2}
    ]
}

# TIERED TIME BLOCKING (Minutes)
SEGMENT_TIMES = {
    "politics": 6,
    "sports": 6,
    "media": 3,
    "tech": 2
}

SEEN_FILE = "seen_stories.txt"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- CORE LOGIC ---

def get_full_text(url):
    try:
        downloaded = trafilatura.fetch_url(url)
        return trafilatura.extract(downloaded, no_fallback=True) or ""
    except: return ""

def get_clustered_stories(configs, seen_hashes):
    """Prevents duplicate stories by comparing Title Topic Keys."""
    now = datetime.datetime.now(datetime.timezone.utc)
    pool = []
    seen_topic_keys = set()
    
    for cfg in configs:
        feed = feedparser.parse(cfg['url'])
        if not feed or not feed.entries: continue
        
        for e in feed.entries:
            # 1. TEMPORAL LOCK: Only Jan 2026+
            title = e.title.lower()
            if "2025" in title or "september" in title: continue
            
            # 2. 48-HOUR FILTER
            pub_date = getattr(e, 'published_parsed', None)
            if pub_date:
                dt = datetime.datetime(*pub_date[:6], tzinfo=datetime.timezone.utc)
                if (now - dt).days > 2: continue 

            # 3. TOPIC KEY DE-DUPLICATION (Simple semantic check)
            words = title.split()
            topic_key = " ".join(words[:3]) if len(words) >= 3 else title
            if topic_key in seen_topic_keys: continue
            
            h = hashlib.md5(e.title.encode()).hexdigest()
            if h in seen_hashes: continue
            
            seen_topic_keys.add(topic_key)
            pool.append({"priority": cfg['priority'], "entry": e, "hash": h})

    pool.sort(key=lambda x: x['priority'], reverse=True)
    return pool[:12]

async def generate_segment_script(name, data, date_str, time_target):
    prompt = f"""
    You are Orator. Today is {date_str}. 
    Write the {name} segment of the podcast. 
    Target Length: {time_target} minutes (approx {time_target * 160} words).
    
    RULES:
    1. NO intros. NO 'Hello I am Orator' in this segment.
    2. NO symbols like * or #. 
    3. NO META-NEWS. If a story is just 'someone wrote an article', DISCARD IT. Report actual facts (scores, names, quotes).
    4. Provide specific details for NBA and Longhorns.
    
    DATA: {data}
    """
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )
    return resp.choices[0].message.content

async def main():
    cst_now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=6)
    date_str = cst_now.strftime("%A, %B %d, %Y")
    file_date = cst_now.strftime("%Y-%m-%d")
    
    if not os.path.exists(SEEN_FILE): open(SEEN_FILE, 'w').close()
    with open(SEEN_FILE, "r") as f: seen_hashes = set(line.strip() for line in f)

    segment_scripts = []
    source_links = []

    for name, configs in FEEDS.items():
        print(f"Selecting {name}...")
        stories = get_clustered_stories(configs, seen_hashes)
        if not stories: continue

        payload = ""
        for item in stories:
            e = item['entry']
            text = get_full_text(e.link)
            payload += f"STORY: {e.title}\nFACTS: {text[:1800]}\n\n"
            source_links.append(f"[{name.upper()}] {e.title} - {e.link}")
            with open(SEEN_FILE, "a") as f: f.write(f"{item['hash']}\n")
        
        script = await generate_segment_script(name, payload, date_str, SEGMENT_TIMES[name])
        segment_scripts.append(script)

    # FINAL ASSEMBLY
    full_text = f"Hello, I'm Orator, and this is your comprehensive briefing for {date_str}.\n\n"
    full_text += "\n\n".join(segment_scripts)
    full_text += "\n\nThat concludes today's Orator briefing. Goodbye."
    
    filename = f"{file_date}_Orator.mp3"
    communicate = edge_tts.Communicate(full_text, "en-US-AndrewNeural", rate="+22%")
    await communicate.save(filename)

    webhook_content = f"**{file_date} - ORATOR DAILY NEWS FOR <@{os.getenv('DISCORD_USER_ID')}>**"
    webhook = DiscordWebhook(url=os.getenv("DISCORD_WEBHOOK_URL"), content=webhook_content)
    with open(filename, "rb") as f: webhook.add_file(file=f.read(), filename=filename)
    with open("sources.txt", "w") as f: f.write("\n".join(source_links))
    with open("sources.txt", "rb") as f: webhook.add_file(file=f.read(), filename="sources.txt")
    
    webhook.execute()
    os.remove(filename)
    os.remove("sources.txt")

if __name__ == "__main__":
    asyncio.run(main())