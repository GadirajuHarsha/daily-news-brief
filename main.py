import os
import asyncio
import feedparser
import datetime
import hashlib
import urllib.request
import trafilatura
from openai import OpenAI
from discord_webhook import DiscordWebhook

# --- ORATOR CONFIGURATION ---
FEEDS = {
    "politics": "https://www.pbs.org/newshour/feeds/rss/politics", 
    "tech": "https://www.theverge.com/rss/index.xml",
    "sports_nba": "https://www.espn.com/espn/rss/nba/news",
    "sports_mavs": "https://feeds.feedburner.com/sportsblogs/mavsmoneyball.xml",
    "sports_ut": "https://texaslonghorns.com/rss?path=general",
    "media_anime": "https://www.animenewsnetwork.com/all/rss.xml?ann-edition=us",
    "media_nintendo": "https://www.nintendolife.com/feeds/latest",
    "media_pokemon": "https://bulbagarden.net/home/index.rss"
}

# New Hierarchical Weights
POKEMON_KW = ["Pokemon", "Niantic", "Game Freak", "Scarlet", "Violet"] # 200
MEDIA_KW = ["Zelda", "Mario", "Ninjago", "Shonen", "One Piece", "Nintendo"] # 100
SPORTS_KW = ["Mavericks", "Mavs", "Doncic", "Kyrie", "Luka", "NBA"] # 60

SEEN_FILE = "seen_stories.txt"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- CORE LOGIC ---

def extract_full_text(url):
    """Fetches full article content to avoid 'meta-news' summaries."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            return trafilatura.extract(downloaded, no_fallback=True) or ""
    except: return ""
    return ""

def get_best_stories(feed_urls, seen_hashes):
    all_entries = []
    for url in feed_urls:
        feed = feedparser.parse(url)
        if feed: all_entries.extend(feed.entries)
    
    scored_entries = []
    now = datetime.datetime.now(datetime.timezone.utc)
    
    for entry in all_entries:
        # 48-HOUR CUTOFF
        pub_date = getattr(entry, 'published_parsed', None)
        if pub_date:
            dt = datetime.datetime(*pub_date[:6], tzinfo=datetime.timezone.utc)
            if (now - dt).days > 2: continue 

        story_hash = hashlib.md5(entry.title.encode()).hexdigest()
        if story_hash in seen_hashes: continue

        score = 0
        title = entry.title.lower()
        if any(kw.lower() in title for kw in POKEMON_KW): score += 200
        if any(kw.lower() in title for kw in MEDIA_KW): score += 100
        if any(kw.lower() in title for kw in SPORTS_KW): score += 60
        
        scored_entries.append((score, entry, story_hash))
    
    scored_entries.sort(key=lambda x: x[0], reverse=True)
    return scored_entries[:8] # Slightly fewer stories for better focus

async def main():
    cst_now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=6)
    date_str = cst_now.strftime("%Y-%m-%d")
    spoken_date = cst_now.strftime("%A, %B %d, %Y")
    
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    user_id = os.getenv("DISCORD_USER_ID")
    
    if not os.path.exists(SEEN_FILE): open(SEEN_FILE, 'w').close()
    with open(SEEN_FILE, "r") as f: seen_hashes = set(line.strip() for line in f)

    tasks = [
        {"name": "politics", "urls": [FEEDS["politics"]], "v": "onyx", "len": 5},
        {"name": "sports", "urls": [FEEDS["sports_nba"], FEEDS["sports_mavs"], FEEDS["sports_ut"]], "v": "onyx", "len": 4.5},
        {"name": "tech", "urls": [FEEDS["tech"]], "v": "alloy", "len": 3},
        {"name": "media", "urls": [FEEDS["media_anime"], FEEDS["media_nintendo"], FEEDS["media_pokemon"]], "v": "alloy", "len": 3}
    ]

    for t in tasks:
        print(f"--- {t['name'].upper()} ---", flush=True)
        entries = get_best_stories(t['urls'], seen_hashes)
        if not entries: continue

        data_payload = ""
        link_log = []
        category_hashes = []

        for score, e, h in entries:
            text = extract_full_text(e.link)
            content = text if len(text) > 300 else getattr(e, 'summary', '')
            data_payload += f"STORY: {e.title}\nDETAIL: {content[:1200]}\n\n"
            link_log.append(f"{e.title}: {e.link}")
            category_hashes.append(h)

        # UPDATED PROMPT: More variety, No intros/outros
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": f"You are Orator. TODAY IS {spoken_date}. Start with: 'Hello, I'm Orator, and here is your {t['name']} news for {spoken_date}.' Target {t['len']} minutes. Report on at least 5 different stories. Spend max 45 seconds per story. No conclusions. No 'stay tuned'."},
                      {"role": "user", "content": f"Data:\n{data_payload}"}],
            temperature=0
        )
        script = resp.choices[0].message.content
        
        # COST SAVING: Reverted to tts-1 (Standard)
        filename = f"{date_str}_{t['name']}.mp3"
        links_filename = f"{date_str}_{t['name']}_sources.txt"
        
        try:
            # Fix Deprecation Warning using suggested stream method
            with client.audio.speech.with_streaming_response.create(
                model="tts-1",
                voice=t['v'],
                input=script,
                speed=1.35 # Slightly faster as requested
            ) as response:
                response.stream_to_file(filename)
            
            # Create link file
            with open(links_filename, "w") as f: f.write("\n".join(link_log))

            ping = f"<@{user_id}>" if user_id else ""
            webhook = DiscordWebhook(url=webhook_url, content=f"{ping} 🎙️ **ORATOR {t['name'].upper()}**")
            
            with open(filename, "rb") as f: webhook.add_file(file=f.read(), filename=filename)
            with open(links_filename, "rb") as f: webhook.add_file(file=f.read(), filename=links_filename)
            
            webhook.execute()

            # Save progress
            with open(SEEN_FILE, "a") as f:
                for h in category_hashes: f.write(f"{h}\n")
            
            os.remove(filename)
            os.remove(links_filename)
        except Exception as e: print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())