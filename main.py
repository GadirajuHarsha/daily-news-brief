import os
import asyncio
import feedparser
import datetime
import hashlib
import trafilatura
import edge_tts
from openai import OpenAI
from discord_webhook import DiscordWebhook

# --- ORATOR FEEDS ---
FEEDS = {
    "politics": [
        "https://www.pbs.org/newshour/feeds/rss/politics",
        "https://prospect.org/api/rss/content.rss", # Left-leaning Economy/Policy
        "https://jacobin.com/feed" # Explicitly Left Economics
    ],
    "sports": [
        "https://feeds.hoopshype.com/xml/rumors.xml", 
        "https://www.cbssports.com/xml/rss/itnba.xml",
        "https://thedailytexan.com/category/sports/feed/", # UT Austin Focus
        "https://texaslonghorns.com/rss?path=general"
    ],
    "tech": [
        "https://www.engadget.com/rss.xml", # Pure Consumer Hardware
        "https://www.theverge.com/rss/index.xml",
        "https://arstechnica.com/feed/"
    ],
    "media": [
        "https://www.serebii.net/index.rss", # Pokemon High Density
        "https://www.animenewsnetwork.com/all/rss.xml?ann-edition=us", # Shonen/Anime
        "https://www.nintendolife.com/feeds/latest",
        "https://kotaku.com/rss"
    ]
}

# --- PRIORITY WEIGHTS ---
ULTRA = ["pokemon", "serebii", "shonen", "one piece", "quinn ewers", "longhorns"]
HIGH = ["zelda", "mario", "nba", "iphone", "nvidia", "hardware", "review"]
# Mavs is specifically NOT in these lists to deprioritize them as requested.

SEEN_FILE = "seen_stories.txt"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- CORE LOGIC ---

def get_full_text(url):
    try:
        downloaded = trafilatura.fetch_url(url)
        return trafilatura.extract(downloaded, no_fallback=True) or ""
    except: return ""

def get_best_stories(urls, seen_hashes):
    all_entries = []
    for url in urls:
        feed = feedparser.parse(url)
        if feed: all_entries.extend(feed.entries)
    
    scored = []
    now = datetime.datetime.now(datetime.timezone.utc)
    
    for e in all_entries:
        # STRICT 48-HOUR FILTER (Kills the September news)
        pub_date = getattr(e, 'published_parsed', None)
        if pub_date:
            dt = datetime.datetime(*pub_date[:6], tzinfo=datetime.timezone.utc)
            if (now - dt).days > 2: continue 

        h = hashlib.md5(e.title.encode()).hexdigest()
        if h in seen_hashes: continue
        
        score = 0
        t = e.title.lower()
        if any(k in t for k in ULTRA): score += 200
        if any(k in t for k in HIGH): score += 100
        
        scored.append((score, e, h))
    
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:10]

async def generate_segment_script(name, data, date_str):
    """Specific prompt for each segment to ensure length and tone."""
    prompts = {
        "politics": "Focus on high-level policy, legislative moves, and economic trends with a nuanced, slightly left-of-center perspective.",
        "sports": "Focus on general NBA updates and UT Austin sports. Deprioritize the Dallas Mavericks. Report actual scores and names.",
        "tech": "Focus strictly on consumer hardware, gadget reviews, and product launches. Ignore Elon Musk or corporate boardroom drama.",
        "media": "Heavily prioritize Pokemon, Shonen manga, and Nintendo hardware news."
    }
    
    prompt = f"""
    You are Orator. Today is {date_str}. 
    Write the {name} segment of a news podcast.
    Target length: 1,000 words (approx 5 minutes of speech).
    
    STRICT RULES:
    1. INTRO: 'Hello, I'm Orator, and here is your {name} news for {date_str}.'
    2. NO symbols like * or #. Use plain text only.
    3. Be extremely detailed. Don't summarize; report every fact in the data.
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
    
    if not os.path.exists(SEEN_FILE): open(SEEN_FILE, 'w').close()
    with open(SEEN_FILE, "r") as f: seen_hashes = set(line.strip() for line in f)

    full_script = []
    source_links = []

    for name, urls in FEEDS.items():
        print(f"Processing {name}...")
        top_entries = get_best_stories(urls, seen_hashes)
        if not top_entries: continue

        payload = ""
        for _, e, h in top_entries:
            text = get_full_text(e.link)
            payload += f"STORY: {e.title}\nFACTS: {text[:2000]}\n\n"
            source_links.append(f"[{name.upper()}] {e.title} - {e.link}")
            with open(SEEN_FILE, "a") as f: f.write(f"{h}\n")
        
        segment_script = await generate_segment_script(name, payload, date_str)
        full_script.append(segment_script)

    # COMBINE & SIGN OFF
    final_text = "\n\n".join(full_script) + "\n\nThat concludes today's Orator briefing."
    
    # FREE TTS: Edge-TTS
    filename = f"{cst_now.strftime('%Y-%m-%d')}_Orator.mp3"
    # Andrew is the best authoritative voice
    communicate = edge_tts.Communicate(final_text, "en-US-AndrewNeural", rate="+22%")
    await communicate.save(filename)

    # Deliver
    webhook = DiscordWebhook(url=os.getenv("DISCORD_WEBHOOK_URL"), content=f"<@{os.getenv('DISCORD_USER_ID')}> 🎙️ **ORATOR COMPREHENSIVE**")
    with open(filename, "rb") as f: webhook.add_file(file=f.read(), filename=filename)
    
    with open("sources.txt", "w") as f: f.write("\n".join(source_links))
    with open("sources.txt", "rb") as f: webhook.add_file(file=f.read(), filename="sources.txt")
    
    webhook.execute()
    os.remove(filename)
    os.remove("sources.txt")

if __name__ == "__main__":
    asyncio.run(main())