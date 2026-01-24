import os
import asyncio
import feedparser
import datetime
import hashlib
import trafilatura
import edge_tts
import subprocess
import requests
from PIL import Image
from io import BytesIO
from bs4 import BeautifulSoup
from openai import OpenAI
from discord_webhook import DiscordWebhook

# --- ORATOR CONFIGURATION ---
FEEDS = {
    "politics": [
        {"url": "https://www.pbs.org/newshour/feeds/rss/politics", "priority": 3},
        {"url": "https://www.hoover.org/publications/hoover-daily-report/feed", "priority": 3},
        {"url": "https://www.theguardian.com/us-news/rss", "priority": 2},
        {"url": "https://apnews.com/hub/politics.rss", "priority": 2}
    ],
    "sports": [
        {"url": "https://www.espn.com/espn/rss/nba/news", "priority": 3},
        {"url": "https://feeds.hoopshype.com/xml/rumors.xml", "priority": 2},
        {"url": "https://basketball.realgm.com/rss/wiretap/0/0.xml", "priority": 2},
        {"url": "https://thedailytexan.com/category/sports/feed/", "priority": 2},
        {"url": "https://texaslonghorns.com/rss?path=general", "priority": 2}
    ],
    "tech": [
        {"url": "https://www.engadget.com/rss.xml", "priority": 3},
        {"url": "https://www.wired.com/feed/category/gear/latest/rss", "priority": 3},
        {"url": "https://arstechnica.com/feed/", "priority": 2},
        {"url": "https://www.theverge.com/rss/index.xml", "priority": 2}
    ],
    "media": [
        {"url": "https://www.serebii.net/index.rss", "priority": 3},
        {"url": "https://www.animenewsnetwork.com/all/rss.xml?ann-edition=us", "priority": 3},
        {"url": "https://pitchfork.com/rss/news/", "priority": 2},
        {"url": "https://hypebeast.com/music/feed", "priority": 2},
        {"url": "https://kotaku.com/rss", "priority": 2}
    ]
}

SEGMENT_TIMES = {"politics": 6, "sports": 6, "media": 4, "tech": 3}
SEEN_FILE = "seen_stories.txt"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- UTILS ---

def get_og_image(url):
    """Scrapes the preview image of an article."""
    try:
        r = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        soup = BeautifulSoup(r.text, 'html.parser')
        img = soup.find("meta", property="og:image")
        return img["content"] if img else None
    except: return None

def create_collage(image_urls, output_path):
    """Creates a 2x2 collage from the top story of each section."""
    images = []
    for url in image_urls:
        try:
            r = requests.get(url)
            img = Image.open(BytesIO(r.content)).convert("RGB")
            img = img.resize((400, 300))
            images.append(img)
        except: 
            images.append(Image.new("RGB", (400, 300), color="gray"))
    
    collage = Image.new("RGB", (800, 600))
    collage.paste(images[0], (0, 0))
    collage.paste(images[1], (400, 0))
    collage.paste(images[2], (0, 300))
    collage.paste(images[3], (400, 300))
    collage.save(output_path)

def get_stratified_stories(configs, seen_hashes):
    """FORCED variety: Takes top stories per feed first."""
    now = datetime.datetime.now(datetime.timezone.utc)
    category_pool = []
    
    for cfg in configs:
        feed = feedparser.parse(cfg['url'])
        feed_entries = []
        for e in feed.entries:
            # Temporal Filter
            if any(k in e.title.lower() for k in ["2025", "september"]): continue
            pub_date = getattr(e, 'published_parsed', None)
            if pub_date:
                dt = datetime.datetime(*pub_date[:6], tzinfo=datetime.timezone.utc)
                if (now - dt).days > 2: continue 
            
            h = hashlib.md5(e.title.encode()).hexdigest()
            if h in seen_hashes: continue
            feed_entries.append({"entry": e, "hash": h, "priority": cfg['priority']})
        
        # Take top 2 from each individual feed
        category_pool.extend(feed_entries[:2])
    
    return category_pool[:12]

async def main():
    cst_now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=6)
    date_str = cst_now.strftime("%A, %B %d, %Y")
    file_date = cst_now.strftime("%Y-%m-%d")
    
    if not os.path.exists(SEEN_FILE): open(SEEN_FILE, 'w').close()
    with open(SEEN_FILE, "r") as f: seen_hashes = set(line.strip() for line in f)

    segment_scripts = []
    source_links = []
    thumbnail_urls = []

    for name, configs in FEEDS.items():
        print(f"Selecting {name}...")
        stories = get_stratified_stories(configs, seen_hashes)
        if not stories: continue
        
        # Grab thumbnail for collage from the very first story
        thumb = get_og_image(stories[0]['entry'].link)
        thumbnail_urls.append(thumb or "https://via.placeholder.com/400x300.png?text=Orator")

        payload = ""
        for item in stories:
            e = item['entry']
            text = trafilatura.extract(trafilatura.fetch_url(e.link))
            payload += f"STORY: {e.title}\nFACTS: {text[:1500] if text else e.summary}\n\n"
            source_links.append(f"[{name.upper()}] {e.title} - {e.link}")
            with open(SEEN_FILE, "a") as f: f.write(f"{item['hash']}\n")
        
        # Multi-prompting for length
        prompt = f"You are Orator. Today is {date_str}. Write the {name} segment. Target: {SEGMENT_TIMES[name]} mins. NO intros/outros. NO asterisks. Be fact-dense."
        resp = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt + f"\n\nDATA:\n{payload}"}], temperature=0)
        segment_scripts.append(resp.choices[0].message.content)

    # FINAL ASSEMBLY
    full_text = f"Hello, I'm Orator, and this is your comprehensive briefing for {date_str}.\n\n" + "\n\n".join(segment_scripts) + "\n\nThat concludes today's Orator briefing. Goodbye."
    
    # Audio & Thumbnail
    voice_file = "voice.mp3"
    await edge_tts.Communicate(full_text, "en-US-AndrewNeural", rate="+22%").save(voice_file)
    create_collage(thumbnail_urls, "thumbnail.jpg")

    # MIX MUSIC (Requires bg_music.mp3 in your repo)
    final_file = f"{file_date}_godiraju_Orator.mp3"
    subprocess.run(["ffmpeg", "-y", "-i", voice_file, "-stream_loop", "-1", "-i", "bg_music.mp3", "-filter_complex", "[1:a]volume=0.08[bg];[0:a][bg]amix=inputs=2:duration=first", final_file])

    # DISCORD
    webhook = DiscordWebhook(url=os.getenv("DISCORD_WEBHOOK_URL"), content=f"**{file_date} - - ORATOR DAILY NEWS FOR <@{os.getenv('DISCORD_USER_ID')}>**")
    with open(final_file, "rb") as f: webhook.add_file(file=f.read(), filename=final_file)
    with open("thumbnail.jpg", "rb") as f: webhook.add_file(file=f.read(), filename="thumbnail.jpg")
    with open("sources.txt", "w") as f: f.write("\n".join(source_links))
    with open("sources.txt", "rb") as f: webhook.add_file(file=f.read(), filename="sources.txt")
    webhook.execute()
    
    for f in [voice_file, "thumbnail.jpg", final_file, "sources.txt"]: os.remove(f)

if __name__ == "__main__":
    asyncio.run(main())