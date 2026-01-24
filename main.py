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
# Base Priority: 3 = High, 2 = Medium, 1 = Support
FEEDS = {
    "politics": [
        {"url": "https://www.pbs.org/newshour/feeds/rss/politics", "priority": 3},
        {"url": "https://prospect.org/api/rss/content.rss", "priority": 2},
        {"url": "https://www.theguardian.com/us-news/rss", "priority": 2},
        {"url": "https://jacobin.com/feed", "priority": 1} # Lowered base to prevent takeover
    ],
    "sports": [
        {"url": "https://feeds.hoopshype.com/xml/rumors.xml", "priority": 3},
        {"url": "https://www.espn.com/espn/rss/nba/news", "priority": 3},
        {"url": "https://basketball.realgm.com/rss/wiretap/0/0.xml", "priority": 2},
        {"url": "https://thedailytexan.com/category/sports/feed/", "priority": 1},
        {"url": "https://texaslonghorns.com/rss?path=general", "priority": 1}
    ],
    "tech": [
        {"url": "https://www.engadget.com/rss.xml", "priority": 3},
        {"url": "https://www.wired.com/feed/category/gear/latest/rss", "priority": 3},
        {"url": "https://arstechnica.com/feed/", "priority": 2}
    ],
    "media": [
        {"url": "https://www.serebii.net/index.rss", "priority": 3},
        {"url": "https://www.animenewsnetwork.com/all/rss.xml?ann-edition=us", "priority": 3},
        {"url": "https://pitchfork.com/rss/news/", "priority": 2},
        {"url": "https://www.complex.com/music/feed", "priority": 2}, # Rap/R&B/Music
        {"url": "https://www.xxlmag.com/feed", "priority": 2} # Rap Focus
    ]
}

# KEYWORD WEIGHTS (Added to base priority)
ULTRA = ["pokemon", "serebii", "shonen", "one piece", "kendrick", "drake", "rap", "r&b", "luka"]
HIGH = ["zelda", "mario", "nba", "quinn ewers", "longhorns", "iphone", "nvidia", "hardware", "review"]

SEEN_FILE = "seen_stories.txt"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- CORE LOGIC ---

def get_full_text(url):
    try:
        downloaded = trafilatura.fetch_url(url)
        return trafilatura.extract(downloaded, no_fallback=True) or ""
    except: return ""

def get_stratified_stories(feed_configs, seen_hashes):
    """Guarantees source variety while allowing high-scoring content to shine."""
    now = datetime.datetime.now(datetime.timezone.utc)
    pool = []
    
    for cfg in feed_configs:
        feed = feedparser.parse(cfg['url'])
        if not feed or not feed.entries: continue
        
        for e in feed.entries:
            # 48-HOUR FILTER
            pub_date = getattr(e, 'published_parsed', None)
            if pub_date:
                dt = datetime.datetime(*pub_date[:6], tzinfo=datetime.timezone.utc)
                if (now - dt).days > 2: continue 

            h = hashlib.md5(e.title.encode()).hexdigest()
            if h in seen_hashes: continue
            
            # Tiered Scoring: Base Priority (10s) + Keyword Bonus (100s)
            score = cfg['priority'] * 10 
            t = e.title.lower()
            if any(k in t for k in ULTRA): score += 300
            if any(k in t for k in HIGH): score += 100
            
            pool.append({"score": score, "entry": e, "hash": h, "url": cfg['url']})

    # Sort by score
    pool.sort(key=lambda x: x['score'], reverse=True)
    
    # Selection: Ensure the top 12 are picked, but limited to 4 per specific URL 
    # to avoid one feed owning more than 33% of a segment.
    selected = []
    url_counts = {}
    for item in pool:
        u = item['url']
        url_counts[u] = url_counts.get(u, 0) + 1
        if url_counts[u] <= 4: # Flexibility: 4 stories max per source
            selected.append(item)
        if len(selected) >= 12: break
        
    return selected

async def generate_segment_script(name, data, date_str):
    prompts = {
        "politics": "Balanced US policy and economic news. Nuanced, central-left edge. Mix PBS and Guardian facts heavily.",
        "sports": "Primary focus: General NBA updates and UT Longhorns. Specific names and stats. NBA should be the lead.",
        "tech": "Consumer hardware and product launches. Strictly no corporate gossip or Elon talk.",
        "media": "High-density Pokemon and Anime news. Seamlessly mix in Rap, R&B, and Indie music updates."
    }
    
    prompt = f"""
    You are Orator. Today is {date_str}. 
    Write a 5-minute {name} podcast segment (approx 1,000 words).
    
    STRICT RULES:
    1. INTRO: 'Hello, I'm Orator, and here is your {name} news for {date_str}.'
    2. NO symbols like * or #. 
    3. Spend exactly equal time on each major story. Don't linger.
    4. PERSPECTIVE: {prompts.get(name)}
    
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

    full_script = []
    source_links = []

    for name, configs in FEEDS.items():
        top_items = get_stratified_stories(configs, seen_hashes)
        if not top_items: continue

        payload = ""
        for item in top_items:
            e = item['entry']
            text = get_full_text(e.link)
            payload += f"STORY: {e.title}\nFACTS: {text[:2000]}\n\n"
            source_links.append(f"[{name.upper()}] {e.title} - {e.link}")
            with open(SEEN_FILE, "a") as f: f.write(f"{item['hash']}\n")
        
        segment_script = await generate_segment_script(name, payload, date_str)
        full_script.append(segment_script)

    final_text = "\n\n".join(full_script) + "\n\nThat concludes today's Orator briefing."
    
    # Filename personalization
    filename = f"{file_date}_godiraju_Orator.mp3"
    communicate = edge_tts.Communicate(final_text, "en-US-AndrewNeural", rate="+22%")
    await communicate.save(filename)

    # Discord message personalization
    webhook_content = f"**{date_str} - - ORATOR DAILY NEWS FOR <@{os.getenv('DISCORD_USER_ID')}>**"
    webhook = DiscordWebhook(url=os.getenv("DISCORD_WEBHOOK_URL"), content=webhook_content)
    
    with open(filename, "rb") as f: webhook.add_file(file=f.read(), filename=filename)
    with open("sources.txt", "w") as f: f.write("\n".join(source_links))
    with open("sources.txt", "rb") as f: webhook.add_file(file=f.read(), filename="sources.txt")
    
    webhook.execute()
    os.remove(filename)
    os.remove("sources.txt")

if __name__ == "__main__":
    asyncio.run(main())