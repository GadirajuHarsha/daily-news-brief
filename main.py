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
    "politics": "https://apnews.com/hub/politics.rss", # More robust than Verge for politics
    "tech": "https://www.theverge.com/tech/rss/index.xml",
    "sports_nba": "https://www.espn.com/espn/rss/nba/news",
    "sports_mavs": "https://feeds.feedburner.com/sportsblogs/mavsmoneyball.xml",
    "sports_ut": "https://texaslonghorns.com/rss?path=general",
    "media_anime": "https://www.animenewsnetwork.com/news/rss.xml",
    "media_nintendo": "https://www.nintendolife.com/feeds/latest",
    "media_lego": "https://www.jaysbrickblog.com/feed/",
    "media_pokemon": "https://pokemongolive.com/news/rss"
}

PRIORITIES = ["Mavericks", "Mavs", "Doncic", "Kyrie", "Cooper Flagg", "Nvidia", "Nintendo", "Switch", "Zelda", "Mario", "Pokemon", "Lego"]
UT_PRIORITIES = ["Longhorns", "UT Austin"]
SEEN_FILE = "seen_stories.txt"

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- CORE LOGIC ---

def fetch_feed_safely(url):
    """Bypasses security blocks by pretending to be a browser."""
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as response:
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

def get_best_stories(feed_urls, seen_hashes, limit=12):
    all_entries = []
    for url in feed_urls:
        feed = fetch_feed_safely(url)
        if feed: all_entries.extend(feed.entries)
    
    scored_entries = []
    now = datetime.datetime.now(datetime.timezone.utc)
    
    for entry in all_entries:
        # --- TEMPORAL FILTERING (Crucial for fixing old news) ---
        pub_date = getattr(entry, 'published_parsed', None)
        if pub_date:
            dt = datetime.datetime(*pub_date[:6], tzinfo=datetime.timezone.utc)
            if (now - dt).days > 2: continue # Ignore stories older than 48 hours
        
        story_hash = hashlib.md5(entry.title.encode()).hexdigest()
        if story_hash in seen_hashes: continue

        score = 0
        title = entry.title.lower()
        if any(kw.lower() in title for kw in PRIORITIES): score += 60
        if any(kw.lower() in title for kw in UT_PRIORITIES): score += 20
        
        if "poll:" in title or "discussion:" in title: continue
        scored_entries.append((score, entry, story_hash))
    
    scored_entries.sort(key=lambda x: x[0], reverse=True)
    return scored_entries[:limit]

def generate_script(category, raw_data, length_minutes):
    current_date = datetime.datetime.now().strftime("%A, %B %d, %Y")
    
    prompt = f"""
    TODAY IS: {current_date}
    CATEGORY: {category}
    TARGET LENGTH: {length_minutes} minutes.

    STRICT RULES:
    1. ONLY use facts in the DATA section. If the data is empty, say 'No new specific updates'.
    2. NO outside knowledge. If you don't have the score of the Mavs game in the DATA, don't mention a score.
    3. NO sweeping generalizations. Provide names, specific stats, and dates.
    4. Write dates and large numbers as words (e.g., 'January twenty-third').
    5. Narrative flow only. NO headlines or 'Stay tuned' filler.
    """
    
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "You are a fact-dense, specific news orator. You avoid all filler and generalizations."},
                  {"role": "user", "content": prompt + f"\n\nDATA:\n{raw_data}"}],
        temperature=0
    )
    return response.choices[0].message.content

async def main():
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    user_id = os.getenv("DISCORD_USER_ID")
    seen_hashes = get_seen_hashes()
    
    # Task Groups
    tasks = [
        {"name": "politics", "urls": [FEEDS["politics"]], "len": 5, "voice": "en-US-AndrewNeural"},
        {"name": "sports", "urls": [FEEDS["sports_nba"], FEEDS["sports_mavs"], FEEDS["sports_ut"]], "len": 4.5, "voice": "en-US-AndrewNeural"},
        {"name": "tech", "urls": [FEEDS["tech"]], "len": 2.5, "voice": "en-US-BrianNeural"},
        {"name": "media", "urls": [FEEDS["media_anime"], FEEDS["media_nintendo"], FEEDS["media_lego"], FEEDS["media_pokemon"]], "len": 2.5, "voice": "en-US-BrianNeural"}
    ]

    for t in tasks:
        entries = get_best_stories(t['urls'], seen_hashes)
        if not entries: continue

        data_payload = ""
        for score, e, h in entries:
            summary = getattr(e, 'summary', getattr(e, 'description', ''))
            data_payload += f"TITLE: {e.title}\nDETAIL: {summary}\n\n"
            save_seen_hash(h)

        script = generate_script(t['name'], data_payload, t['len'])
        filename = f"{today}_{t['name']}.mp3"
        
        await edge_tts.Communicate(script, t['voice'], rate="+25%").save(filename)
        
        # Ping logic: <@ID>
        ping = f"<@{user_id}>" if user_id else ""
        webhook = DiscordWebhook(url=webhook_url, content=f"{ping} 🎙️ **{today}** | {t['name'].upper()}")
        with open(filename, "rb") as f:
            webhook.add_file(file=f.read(), filename=filename)
        webhook.execute()
        os.remove(filename)

if __name__ == "__main__":
    asyncio.run(main())
