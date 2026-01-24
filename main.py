import os
import asyncio
import feedparser
import edge_tts
import datetime
import hashlib
import urllib.request
from openai import OpenAI
from discord_webhook import DiscordWebhook

# --- CONFIGURATION ---
FEEDS = {
    "politics": "https://www.theverge.com/policy/rss/index.xml",
    "tech": "https://www.theverge.com/tech/rss/index.xml",
    "gaming": "https://www.nintendolife.com/feeds/latest",
    "nba_general": "https://www.espn.com/espn/rss/nba/news",
    "mavs": "https://feeds.feedburner.com/sportsblogs/mavsmoneyball.xml",
    "ut_austin": "https://texaslonghorns.com/rss?path=general",
    "sports_general": "https://www.espn.com/espn/rss/news"
}

# Priorities
MAVS_KEYWORDS = ["Mavericks", "Mavs", "Doncic", "Kyrie", "Kidd", "Gafford"]
UT_KEYWORDS = ["Longhorns", "UT Austin", "Sarkisian", "Quinn Ewers"]

SEEN_FILE = "seen_stories.txt"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- CORE LOGIC ---

def fetch_feed_safely(url):
    """Bypasses security blocks by pretending to be a browser."""
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            return feedparser.parse(response.read())
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None

def get_seen_hashes():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(line.strip() for line in f)
    return set()

def save_seen_hash(content_hash):
    with open(SEEN_FILE, "a") as f:
        f.write(f"{content_hash}\n")

def get_scored_news(category, urls, seen_hashes):
    all_entries = []
    for url in urls:
        feed = fetch_feed_safely(url)
        if feed:
            all_entries.extend(feed.entries)
    
    scored_entries = []
    for entry in all_entries:
        story_hash = hashlib.md5(entry.title.encode()).hexdigest()
        if story_hash in seen_hashes: continue

        score = 0
        title = entry.title.lower()
        
        # Scoring logic
        if any(kw.lower() in title for kw in MAVS_KEYWORDS): score += 40
        if any(kw.lower() in title for kw in UT_KEYWORDS): score += 25
        
        # Filter junk
        if "poll:" in title or "discussion:" in title: continue

        scored_entries.append((score, entry, story_hash))
    
    scored_entries.sort(key=lambda x: x[0], reverse=True)
    return scored_entries

def generate_script(category, raw_data, length_minutes):
    current_date = "Friday, January 23, 2026"
    
    prompt = f"""
    TODAY IS: {current_date}
    CATEGORY: {category}
    TARGET LENGTH: {length_minutes} minutes.

    You are a professional news anchor. Create a fast-paced, fact-dense narrative.
    
    STRICT ANTI-HALLUCINATION RULES:
    1. ONLY use the facts in the DATA section. 
    2. If the data mentions a team record, score, or date, use it. If it does NOT, DO NOT guess.
    3. NO outside context from before 2026. Ignore any data that seems to be from 2021 or 2025.
    4. NO 'intro', 'outro', or 'Script:' markers. 
    5. Write numbers and dates as words (e.g., 'twenty six and seventeen' or 'January twenty third').

    DATA:
    {raw_data}
    """
    
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "You are a literalist news orator. You provide high-density facts with zero creative filler or speculation."},
                  {"role": "user", "content": prompt}],
        temperature=0 # Absolute literalism
    )
    return response.choices[0].message.content

async def main():
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    seen_hashes = get_seen_hashes()
    
    # Define Task Groups
    tasks = [
        {"name": "politics", "urls": [FEEDS["politics"]], "len": 5, "voice": "en-US-AndrewNeural"},
        {"name": "sports", "urls": [FEEDS["nba_general"], FEEDS["mavs"], FEEDS["ut_austin"], FEEDS["sports_general"]], "len": 4.5, "voice": "en-US-AndrewNeural"},
        {"name": "tech", "urls": [FEEDS["tech"]], "len": 2.5, "voice": "en-US-BrianNeural"},
        {"name": "gaming", "urls": [FEEDS["gaming"]], "len": 2.5, "voice": "en-US-BrianNeural"}
    ]

    for t in tasks:
        print(f"Processing {t['name']}...")
        entries = get_scored_news(t['name'], t['urls'], seen_hashes)
        top_entries = entries[:15] # Provide more data for longer segments
        if not top_entries: continue

        data_payload = ""
        for score, e, h in top_entries:
            summary = getattr(e, 'summary', getattr(e, 'description', 'No details available.'))
            data_payload += f"TITLE: {e.title}\nINFO: {summary}\n\n"
            save_seen_hash(h)

        script = generate_script(t['name'], data_payload, t['len'])
        filename = f"{today}_{t['name']}.mp3"
        
        await edge_tts.Communicate(script, t['voice'], rate="+25%").save(filename)
        
        webhook = DiscordWebhook(url=webhook_url, content=f"🎙️ **{today} Briefing** | {t['name'].upper()}")
        with open(filename, "rb") as f:
            webhook.add_file(file=f.read(), filename=filename)
        webhook.execute()
        os.remove(filename)

if __name__ == "__main__":
    asyncio.run(main())
