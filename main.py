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
    "politics": ["https://www.pbs.org/newshour/feeds/rss/politics", "https://www.theguardian.com/us-news/rss"],
    "sports": [
        "https://feeds.hoopshype.com/xml/rumors.xml", # NBA General
        "https://www.cbssports.com/xml/rss/itnba.xml", # NBA General
        "https://feeds.feedburner.com/sportsblogs/mavsmoneyball.xml", # Mavs
        "https://texaslonghorns.com/rss?path=general", # UT Austin
        "https://www.cbssports.com/xml/rss/itmain.xml" # General Sports
    ],
    "tech": ["https://www.theverge.com/rss/index.xml", "https://www.engadget.com/rss.xml"],
    "media": [
        "https://www.animenewsnetwork.com/all/rss.xml", # Anime
        "https://www.nintendolife.com/feeds/latest", # Nintendo
        "https://pokemongohub.net/feed", # Pokemon
        "https://kotaku.com/rss", # General Gaming
        "https://www.eurogamer.net/feed" # General Gaming
    ]
}

# --- SCORING ENGINE ---
# Weights: High numbers lead the segment, lower numbers fill the gaps.
PRIORITIES = {
    "sports": {"mavs": 60, "mavericks": 60, "longhorns": 40, "ut austin": 40, "nba": 20},
    "media": {"pokemon": 100, "zelda": 60, "mario": 60, "shonen": 60, "anime": 40},
    "tech": {"iphone": 50, "nvidia": 50, "tesla": 50, "review": 40} # Boosts product news
}

SEEN_FILE = "seen_stories.txt"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- LOGIC ---

def get_full_text(url):
    downloaded = trafilatura.fetch_url(url)
    return trafilatura.extract(downloaded) if downloaded else ""

async def main():
    cst_now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=6)
    date_str = cst_now.strftime("%A, %B %d, %Y")
    
    seen_hashes = set()
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f: seen_hashes = set(line.strip() for line in f)

    master_payload = ""
    source_links = []

    for category, urls in FEEDS.items():
        all_entries = []
        for url in urls:
            feed = feedparser.parse(url)
            all_entries.extend(feed.entries)
        
        scored = []
        for e in all_entries:
            h = hashlib.md5(e.title.encode()).hexdigest()
            if h in seen_hashes: continue
            
            score = 0
            t = e.title.lower()
            # Dynamic Scoring based on the category priorities
            if category in PRIORITIES:
                for kw, weight in PRIORITIES[category].items():
                    if kw in t: score += weight
            
            scored.append((score, e, h))
        
        # Pull up to 10 distinct stories per segment to reach 4-5 mins
        scored.sort(key=lambda x: x[0], reverse=True)
        top_entries = scored[:10] 
        
        category_text = f"\n\n--- {category.upper()} SEGMENT ---\n"
        for _, e, h in top_entries:
            full_text = get_full_text(e.link)
            # Filter: If content is very short, use the snippet
            content = full_text if len(full_text) > 300 else getattr(e, 'summary', '')
            category_text += f"STORY: {e.title}\nCONTEXT: {content[:1500]}\n"
            source_links.append(f"{category.upper()}: {e.title} - {e.link}")
            with open(SEEN_FILE, "a") as f: f.write(f"{h}\n")
        
        master_payload += category_text

    # THE ORATOR UNIFIED PROMPT
    prompt = f"""
    You are Orator, an expert news assistant. Today is {date_str}.
    Create a unified 20-minute podcast script. 
    
    GUIDELINES:
    1. INTRO: 'Hello, I'm Orator, and this is your comprehensive briefing for {date_str}.'
    2. SEGMENTS: Politics, Sports, Tech, and Media. 
    3. TIMING: Spend exactly 4.5 to 5 minutes on each segment. Be extremely detailed. 
    4. ACCURACY: Report the specific facts from the DATA provided. Never say 'This article describes'; tell me what happened.
    5. TRANSITIONS: Use smooth bridges (e.g., 'From world headlines to the baseline, here is sports...').
    6. NO OUTRO: End strictly with 'That concludes today's Orator briefing.'
    
    DATA: {master_payload}
    """
    
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "You are a professional news anchor. You avoid all filler, meta-talk, and generalizations."},
                  {"role": "user", "content": prompt}],
        temperature=0.2
    )
    script = resp.choices[0].message.content

    # FREE HIGH-QUALITY TTS
    filename = f"{cst_now.strftime('%Y-%m-%d')}_Orator_Full.mp3"
    # 'AndrewNeural' is excellent for an authoritative news persona
    communicate = edge_tts.Communicate(script, "en-US-AndrewNeural", rate="+20%")
    await communicate.save(filename)

    # Deliver to Discord
    webhook = DiscordWebhook(url=os.getenv("DISCORD_WEBHOOK_URL"), content=f"<@{os.getenv('DISCORD_USER_ID')}> 🎙️ **ORATOR COMPREHENSIVE BRIEFING** | {date_str}")
    with open(filename, "rb") as f: webhook.add_file(file=f.read(), filename=filename)
    
    # Sources Attachment
    with open("sources.txt", "w") as f: f.write("\n".join(source_links))
    with open("sources.txt", "rb") as f: webhook.add_file(file=f.read(), filename="sources.txt")
    
    webhook.execute()
    os.remove(filename)
    os.remove("sources.txt")

if __name__ == "__main__":
    asyncio.run(main())