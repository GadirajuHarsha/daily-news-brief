import os
import asyncio
import feedparser
import datetime
import hashlib
import urllib.request
from openai import OpenAI
from discord_webhook import DiscordWebhook

# --- CONFIGURATION ---
FEEDS = {
    "politics": "https://www.pbs.org/newshour/feeds/rss/politics", 
    "tech": "https://www.theverge.com/rss/index.xml", # Broad feed is more stable than tech-specific
    "sports_nba": "https://www.espn.com/espn/rss/nba/news",
    "sports_mavs": "https://feeds.feedburner.com/sportsblogs/mavsmoneyball.xml",
    "sports_ut": "https://texaslonghorns.com/rss?path=general",
    "media_anime": "https://www.animenewsnetwork.com/all/rss.xml?ann-edition=us",
    "media_nintendo": "https://www.nintendolife.com/feeds/latest",
    "media_pokemon": "https://pokemongohub.net/feed",
    "media_lego": "https://www.jaysbrickblog.com/feed/" 
}

# Hierarchical Weights
POKEMON_KW = ["Pokemon", "Niantic", "Pikachu", "Game Freak"]
MEDIA_KW = ["Zelda", "Mario", "Anime", "Manga", "Nintendo", "Link"]
SPORTS_KW = ["Mavericks", "Mavs", "Doncic", "Kyrie", "Luka", "NBA"]
LEGO_KW = ["Lego"]

SEEN_FILE = "seen_stories.txt"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- LOGIC ---

def fetch_feed_safely(url):
    print(f"Attempting to fetch: {url}", flush=True)
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as response:
            return feedparser.parse(response.read())
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}", flush=True)
        return None

def get_best_stories(feed_urls, seen_hashes):
    all_entries = []
    for url in feed_urls:
        feed = fetch_feed_safely(url)
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
        if any(kw.lower() in title for kw in LEGO_KW): score += 5
        
        scored_entries.append((score, entry, story_hash))
    
    scored_entries.sort(key=lambda x: x[0], reverse=True)
    return scored_entries[:12]

def generate_audio_openai(text, voice_name, filename):
    """Uses OpenAI's stable TTS-1 model."""
    # Mapping your preferred voices to OpenAI equivalents
    # Andrew/Guy -> Onyx or Fable | Brian/Christopher -> Alloy or Nova
    voice_map = {
        "en-US-AndrewNeural": "onyx",
        "en-US-BrianNeural": "alloy"
    }
    selected_voice = voice_map.get(voice_name, "alloy")
    
    response = client.audio.speech.create(
        model="tts-1",
        voice=selected_voice,
        input=text,
        speed=1.25 # Your requested 1.25x speed
    )
    response.stream_to_file(filename)

async def main():
    print("Starting News Briefing Script (OpenAI TTS Edition)...", flush=True)
    cst_now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=6)
    date_str = cst_now.strftime("%Y-%m-%d")
    spoken_date = cst_now.strftime("%A, %B %d, %Y")
    
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    user_id = os.getenv("DISCORD_USER_ID")
    
    if not os.path.exists(SEEN_FILE):
        open(SEEN_FILE, 'w').close()
        
    with open(SEEN_FILE, "r") as f:
        seen_hashes = set(line.strip() for line in f)

    tasks = [
        {"name": "politics", "urls": [FEEDS["politics"]], "len": 5, "v": "en-US-AndrewNeural"},
        {"name": "sports", "urls": [FEEDS["sports_nba"], FEEDS["sports_mavs"], FEEDS["sports_ut"]], "len": 4, "v": "en-US-AndrewNeural"},
        {"name": "tech", "urls": [FEEDS["tech"]], "len": 2.5, "v": "en-US-BrianNeural"},
        {"name": "media", "urls": [FEEDS["media_anime"], FEEDS["media_nintendo"], FEEDS["media_pokemon"], FEEDS["media_lego"]], "len": 3, "v": "en-US-BrianNeural"}
    ]

    for t in tasks:
        print(f"--- Processing Category: {t['name']} ---", flush=True)
        entries = get_best_stories(t['urls'], seen_hashes)
        
        if not entries: 
            print(f"  Result: No qualifying stories found.", flush=True)
            continue

        data_payload = ""
        category_hashes = []
        for score, e, h in entries:
            summary = getattr(e, 'summary', getattr(e, 'description', ''))
            data_payload += f"STORY: {e.title}\nDETAIL: {summary}\n\n"
            category_hashes.append(h)

        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": f"You are a professional news anchor. TODAY IS {spoken_date}. Use ONLY provided data. No outside info. No hallucinations."},
                      {"role": "user", "content": f"Briefing for {t['name']}:\n{data_payload}"}],
            temperature=0
        )
        script = resp.choices[0].message.content
        
        filename = f"{date_str}_{t['name']}.mp3"
        
        try:
            print(f"  Generating OpenAI Audio for {t['name']}...", flush=True)
            generate_audio_openai(script, t['v'], filename)
            
            print(f"  Sending to Discord...", flush=True)
            ping = f"<@{user_id}>" if user_id else ""
            webhook = DiscordWebhook(url=webhook_url, content=f"{ping} 🎙️ **{spoken_date}** | {t['name'].upper()}")
            with open(filename, "rb") as f:
                webhook.add_file(file=f.read(), filename=filename)
            webhook.execute()
            
            # ONLY SAVE HASHES IF SUCCESSFUL
            with open(SEEN_FILE, "a") as f:
                for h in category_hashes: f.write(f"{h}\n")
            
            os.remove(filename)
            print(f"  Finished {t['name']}.", flush=True)
        except Exception as e:
            print(f"  CRITICAL ERROR for {t['name']}: {e}", flush=True)

    print("Workflow Complete.", flush=True)

if __name__ == "__main__":
    asyncio.run(main())