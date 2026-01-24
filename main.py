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
    "politics": "https://www.pbs.org/newshour/feeds/rss/politics", 
    "tech": "https://www.theverge.com/tech/rss/index.xml",
    "sports_nba": "https://www.espn.com/espn/rss/nba/news",
    "sports_mavs": "https://feeds.feedburner.com/sportsblogs/mavsmoneyball.xml",
    "sports_ut": "https://texaslonghorns.com/rss?path=general",
    "media_anime": "https://www.animenewsnetwork.com/all/rss.xml?ann-edition=us",
    "media_nintendo": "https://www.nintendolife.com/feeds/latest",
    "media_pokemon": "https://pokemongohub.net/feed"
}

# New Hierarchical Priorities
POKEMON_KEYWORDS = ["Pokemon", "Pikachu", "Niantic", "Game Freak", "Scarlet", "Violet"]
MEDIA_KEYWORDS = ["Zelda", "Mario", "Anime", "Manga", "Crunchyroll", "One Piece", "Crunchyroll"]
SPORTS_KEYWORDS = ["Mavericks", "Mavs", "Doncic", "Kyrie", "Luka", "NBA"]
LEGO_KEYWORDS = ["Lego", "Minifigure"] # Deprioritized in the script logic

SEEN_FILE = "seen_stories.txt"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- CORE LOGIC ---

def fetch_feed_safely(url):
    """Mimics a modern Chrome browser to bypass WAF/Bot blocks."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/rss+xml, application/xml;q=0.9, */*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as response:
            return feedparser.parse(response.read())
    except Exception as e:
        print(f"FAILED to fetch {url}: {e}")
        return None

def get_best_stories(feed_urls, seen_hashes, limit=15):
    all_entries = []
    for url in feed_urls:
        feed = fetch_feed_safely(url)
        if feed: all_entries.extend(feed.entries)
    
    scored_entries = []
    now = datetime.datetime.now(datetime.timezone.utc)
    
    for entry in all_entries:
        # --- STRICT 48-HOUR FILTER ---
        pub_date = getattr(entry, 'published_parsed', None)
        if pub_date:
            dt = datetime.datetime(*pub_date[:6], tzinfo=datetime.timezone.utc)
            if (now - dt).days > 2: continue 

        story_hash = hashlib.md5(entry.title.encode()).hexdigest()
        if story_hash in seen_hashes: continue

        score = 0
        title = entry.title.lower()
        
        # New Scoring Weights
        if any(kw.lower() in title for kw in POKEMON_KEYWORDS): score += 100 # Top Tier
        if any(kw.lower() in title for kw in MEDIA_KEYWORDS): score += 60  # High Tier
        if any(kw.lower() in title for kw in SPORTS_KEYWORDS): score += 50 # Priority Sports
        if any(kw.lower() in title for kw in LEGO_KEYWORDS): score += 5    # Minimal Lego
        
        scored_entries.append((score, entry, story_hash))
    
    scored_entries.sort(key=lambda x: x[0], reverse=True)
    return scored_entries[:limit]

async def main():
    # --- DATE FIX (CST OFFSET) ---
    cst_now = datetime.datetime.now() - datetime.timedelta(hours=6)
    date_str = cst_now.strftime("%Y-%m-%d")
    spoken_date = cst_now.strftime("%A, %B %d, %Y")
    
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    user_id = os.getenv("DISCORD_USER_ID")
    seen_hashes = set()
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            seen_hashes = set(line.strip() for line in f)

    tasks = [
        {"name": "politics", "urls": [FEEDS["politics"]], "len": 5, "v": "en-US-AndrewNeural"},
        {"name": "sports", "urls": [FEEDS["sports_nba"], FEEDS["sports_mavs"], FEEDS["sports_ut"]], "len": 4, "v": "en-US-AndrewNeural"},
        {"name": "tech", "urls": [FEEDS["tech"]], "len": 2.5, "v": "en-US-BrianNeural"},
        {"name": "media", "urls": [FEEDS["media_anime"], FEEDS["media_nintendo"], FEEDS["media_pokemon"], FEEDS["media_lego"]], "len": 3, "v": "en-US-BrianNeural"}
    ]

    for t in tasks:
        entries = get_best_stories(t['urls'], seen_hashes)
        if not entries: 
            print(f"SKIPPING {t['name']} - No fresh data found.")
            continue

        data_payload = ""
        for score, e, h in entries:
            summary = getattr(e, 'summary', getattr(e, 'description', ''))
            data_payload += f"STORY: {e.title}\nDETAIL: {summary}\n\n"
            with open(SEEN_FILE, "a") as f: f.write(f"{h}\n")

        # Zero Temperature Hallucination Guard
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": f"You are a professional anchor. TODAY IS {spoken_date}. Use ONLY data provided. No outside context. No halluncinations. Speak at 1.25x speed."},
                      {"role": "user", "content": f"Briefing for {t['name']}:\n{data_payload}"}],
            temperature=0
        )
        script = resp.choices[0].message.content
        
        filename = f"{date_str}_{t['name']}.mp3"
        await edge_tts.Communicate(script, t['v'], rate="+25%").save(filename)
        
        ping = f"<@{user_id}>" if user_id else ""
        webhook = DiscordWebhook(url=webhook_url, content=f"{ping} 🎙️ **{spoken_date}** | {t['name'].upper()}")
        with open(filename, "rb") as f:
            webhook.add_file(file=f.read(), filename=filename)
        webhook.execute()
        os.remove(filename)

if __name__ == "__main__":
    asyncio.run(main())
