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

# --- REFINED SCORING WEIGHTS ---
POKEMON_KW = ["Pokemon", "Niantic", "Game Freak", "Pikachu", "Togepi", "Paldea"] # Weight: 200
MEDIA_KW = ["Zelda", "Mario", "Anime", "Manga", "Crunchyroll", "One Piece", "Link"] # Weight: 100
SPORTS_KW = ["Mavericks", "Mavs", "Doncic", "Kyrie", "Luka", "NBA"] # Weight: 60
LEGO_KW = ["Lego"] # Weight: 1 (Minimal)

SEEN_FILE = "seen_stories.txt"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def fetch_feed_safely(url):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as response:
            return feedparser.parse(response.read())
    except Exception as e:
        print(f"CRITICAL: Failed to fetch {url} - {e}")
        return None

def get_best_stories(feed_urls, seen_hashes, limit=12):
    all_entries = []
    for url in feed_urls:
        feed = fetch_feed_safely(url)
        if feed:
            print(f"Fetched {len(feed.entries)} items from {url}")
            all_entries.extend(feed.entries)
    
    scored_entries = []
    # Force 48-hour window for current date relevance
    now = datetime.datetime.now(datetime.timezone.utc)
    
    for entry in all_entries:
        # 1. TEMPORAL FILTER (Ignore anything older than 2 days)
        pub_date = getattr(entry, 'published_parsed', None)
        if pub_date:
            dt = datetime.datetime(*pub_date[:6], tzinfo=datetime.timezone.utc)
            if (now - dt).days > 2: continue 

        story_hash = hashlib.md5(entry.title.encode()).hexdigest()
        if story_hash in seen_hashes: continue

        score = 0
        title = entry.title.lower()
        
        # 2. SCORING LOGIC
        if any(kw.lower() in title for kw in POKEMON_KW): score += 200
        if any(kw.lower() in title for kw in MEDIA_KW): score += 100
        if any(kw.lower() in title for kw in SPORTS_KW): score += 60
        if any(kw.lower() in title for kw in LEGO_KW): score += 1
        
        scored_entries.append((score, entry, story_hash))
    
    scored_entries.sort(key=lambda x: x[0], reverse=True)
    return scored_entries[:limit]

async def main():
    # SYNC TO AUSTIN (CST)
    cst_now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=6)
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
        {"name": "sports", "urls": [FEEDS["sports_nba"], FEEDS["sports_mavs"], FEEDS["sports_ut"]], "len": 4.5, "v": "en-US-AndrewNeural"},
        {"name": "tech", "urls": [FEEDS["tech"]], "len": 2.5, "v": "en-US-BrianNeural"},
        {"name": "media", "urls": [FEEDS["media_anime"], FEEDS["media_nintendo"], FEEDS["media_pokemon"], FEEDS["media_lego"]], "len": 3, "v": "en-US-BrianNeural"}
    ]

    for t in tasks:
        entries = get_best_stories(t['urls'], seen_hashes)
        if not entries:
            print(f"Skipping {t['name']} - No fresh data found.")
            continue

        data_payload = ""
        for score, e, h in entries:
            summary = getattr(e, 'summary', getattr(e, 'description', ''))
            data_payload += f"STORY: {e.title}\nDETAIL: {summary}\n\n"
            with open(SEEN_FILE, "a") as f: f.write(f"{h}\n")

        # LLM Clerk Prompt (Literalist Guard)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": f"You are a specific news reporter. TODAY IS {spoken_date}. Use ONLY provided data. NO outside info. No old stats. If scores aren't in data, do not guess."},
                      {"role": "user", "content": f"Write a 1.25x speed script for {t['name']}:\n{data_payload}"}],
            temperature=0
        )
        script = resp.choices[0].message.content
        
        filename = f"{date_str}_{t['name']}.mp3"
        try:
            await edge_tts.Communicate(script, t['v'], rate="+25%").save(filename)
        except Exception as e:
            print(f"AUDIO ERROR for {t['name']}: {e}")
            continue
        
        ping = f"<@{user_id}>" if user_id else ""
        webhook = DiscordWebhook(url=webhook_url, content=f"{ping} 🎙️ **{spoken_date}** | {t['name'].upper()}")
        with open(filename, "rb") as f:
            webhook.add_file(file=f.read(), filename=filename)
        webhook.execute()
        os.remove(filename)

if __name__ == "__main__":
    asyncio.run(main())
