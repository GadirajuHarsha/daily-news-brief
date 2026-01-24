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
    "sports": "https://www.espn.com/espn/rss/nba/news",
    "mavs": "https://feeds.feedburner.com/sportsblogs/mavsmoneyball.xml",
    "media_anime": "https://www.animenewsnetwork.com/all/rss.xml?ann-edition=us",
    "media_nintendo": "https://www.nintendolife.com/feeds/latest",
    "media_pokemon": "https://bulbagarden.net/home/index.rss", # More detail than general feeds
    "media_lego": "https://www.jaysbrickblog.com/feed/" 
}

# Hierarchical Weights
POKEMON_KW = ["Pokemon", "Pikachu", "Niantic", "Togepi", "Scarlet", "Violet"] # Weight 200
MEDIA_KW = ["Zelda", "Mario", "Ninjago", "Shonen", "Jump", "One Piece", "Jujutsu", "Naruto"] # Weight 100
SPORTS_KW = ["Mavericks", "Mavs", "Doncic", "Kyrie", "Luka", "NBA"] # Weight 60
LEGO_KW = ["Lego"] # Weight 1 (Deprioritized)

SEEN_FILE = "seen_stories.txt"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- CORE LOGIC ---

def extract_full_text(url):
    """Fetches full article content to avoid 'meta-news' summaries."""
    downloaded = trafilatura.fetch_url(url)
    if downloaded:
        result = trafilatura.extract(downloaded, include_formatting=False, no_fallback=True)
        return result if result else ""
    return ""

def get_best_stories(feed_urls, seen_hashes):
    all_entries = []
    for url in feed_urls:
        feed = feedparser.parse(url) # Headers handled by trafilatura later if needed
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
        if any(kw.lower() in title for kw in LEGO_KW): score += 1
        
        scored_entries.append((score, entry, story_hash))
    
    scored_entries.sort(key=lambda x: x[0], reverse=True)
    return scored_entries[:10]

async def main():
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
        {"name": "politics", "urls": [FEEDS["politics"]], "v": "onyx"}, # Deep news anchor
        {"name": "sports", "urls": [FEEDS["sports"], FEEDS["mavs"]], "v": "onyx"},
        {"name": "tech", "urls": [FEEDS["tech"]], "v": "nova"}, # Energetic/Clear
        {"name": "media", "urls": [FEEDS["media_anime"], FEEDS["media_nintendo"], FEEDS["media_pokemon"], FEEDS["media_lego"]], "v": "nova"}
    ]

    for t in tasks:
        entries = get_best_stories(t['urls'], seen_hashes)
        if not entries: continue

        data_payload = ""
        link_log = []
        category_hashes = []

        for score, e, h in entries:
            # TRY TO GET FULL TEXT to avoid summaries of articles
            full_text = extract_full_text(e.link)
            content = full_text if len(full_text) > 200 else getattr(e, 'summary', getattr(e, 'description', ''))
            
            data_payload += f"STORY: {e.title}\nDETAIL: {content[:1500]}\n\n"
            link_log.append(f"- [{e.title}]({e.link})")
            category_hashes.append(h)

        # THE ORATOR SCRIPT
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": f"You are Orator, a personal news AI. TODAY IS {spoken_date}. Start every podcast with: 'Hello, I'm Orator, and here is your {t['name']} news for {spoken_date}.' No other intros/outros. No 'stay tuned'. No meta-talk about articles existing; report the actual news facts from the content."},
                      {"role": "user", "content": f"Write a specific, fact-dense script for {t['name']} using this data:\n{data_payload}"}],
            temperature=0
        )
        script = resp.choices[0].message.content
        
        filename = f"{date_str}_{t['name']}.mp3"
        
        # USE TTS-1-HD for quality, speed 1.3 for brisk pace
        response = client.audio.speech.create(
            model="tts-1-hd",
            voice=t['v'],
            input=script,
            speed=1.3 
        )
        response.stream_to_file(filename)
        
        # SEND TO DISCORD WITH LINK LOG
        ping = f"<@{user_id}>" if user_id else ""
        links_text = "\n".join(link_log)
        webhook = DiscordWebhook(url=webhook_url, content=f"{ping} 🎙️ **ORATOR {t['name'].upper()}** | {date_str}\n\n**Sources:**\n{links_text}")
        with open(filename, "rb") as f:
            webhook.add_file(file=f.read(), filename=filename)
        webhook.execute()

        with open(SEEN_FILE, "a") as f:
            for h in category_hashes: f.write(f"{h}\n")
        os.remove(filename)

if __name__ == "__main__":
    asyncio.run(main())