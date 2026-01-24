import os, asyncio, feedparser, datetime, hashlib, trafilatura, edge_tts, subprocess, requests, random, glob, sys
from openai import OpenAI
from discord_webhook import DiscordWebhook

# --- 1. CONFIGURATION ---
FEEDS = {
    "politics": [
        {"url": "https://www.pbs.org/newshour/feeds/rss/politics", "weight": 20},
        {"url": "https://apnews.com/hub/politics.rss", "weight": 20},
        {"url": "https://thehill.com/homenews/feed/", "weight": 15},
        {"url": "https://prospect.org/api/rss/content.rss", "weight": 13},
        {"url": "https://jacobin.com/feed", "weight": 12}
    ],
    "sports": [
        {"url": "https://www.espn.com/espn/rss/nba/news", "weight": 20},
        {"url": "https://feeds.hoopshype.com/xml/rumors.xml", "weight": 17},
        {"url": "https://basketball.realgm.com/rss/wiretap/0/0.xml", "weight": 17},
        {"url": "https://texaslonghorns.com/rss?path=general", "weight": 14}
    ],
    "tech": [
        {"url": "https://www.engadget.com/rss.xml", "weight": 8},
        {"url": "https://www.wired.com/feed/category/gear/latest/rss", "weight": 8},
        {"url": "https://arstechnica.com/feed/", "weight": 7},
        {"url": "https://www.theverge.com/rss/index.xml", "weight": 5}
    ],
    "media": [
        {"url": "https://www.serebii.net/index.rss", "weight": 13},
        {"url": "https://www.animenewsnetwork.com/all/rss.xml?ann-edition=us", "weight": 7},
        {"url": "https://www.nintendolife.com/feeds/latest", "weight": 10},
        {"url": "https://pitchfork.com/rss/news/", "weight": 11},
        {"url": "https://hypebeast.com/music/feed", "weight": 8}
    ]
}

KEYWORDS = {
    "pokemon": 100, "serebii": 90, "shonen": 60, "luka": 100, "mavs": 80, 
    "longhorns": 70, "iphone": 50, "rap": 50, "r&b": 60, 'lakers': 60, "nba": 60,
    "zelda": 90, "mario": 80, "clairo": 80, "daniel caesar": 80, "drake": 70,
    "21 savage": 70, "jojo's": 80, "lego": 40,
}

# BOTTOM QUOTAS (Ensures diversity even with low weights)
MIN_STORY_FLOOR = {"politics": 6, "sports": 6, "tech": 4, "media": 4}

SEEN_FILE = "seen_stories.txt"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def get_topic_key(title):
    words = [w for w in title.lower().split() if len(w) > 3]
    return " ".join(words[:3])

async def generate_segment(name, stories, date_str):
    if not stories: return "", []
    payload, links = "", []
    for item in stories:
        e = item['entry']
        text = trafilatura.extract(trafilatura.fetch_url(e.link))
        payload += f"STORY: {e.title}\nCONTEXT: {text[:3000] if text else e.summary}\n\n"
        links.append(f"[{name.upper()}] {e.title} - {e.link}")

    is_major = name in ["politics", "sports"]
    style = "VERBOSE & GRANULAR. 4+ detailed paragraphs per story." if is_major else "CONCISE SUMMARY. 1-2 dense paragraphs per story."
    
    prompt = (f"You are Orator. Today is {date_str}. Write the {name} segment. "
              f"STYLE: {style} Focus on factual density. No intros/outros. No symbols.")
    
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt + f"\n\nDATA:\n{payload}"}],
        temperature=0.3
    )
    return resp.choices[0].message.content, links

async def main():
    cst_now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=6)
    date_str, file_date = cst_now.strftime("%A, %B %d, %Y"), cst_now.strftime("%Y-%m-%d")
    
    if not os.path.exists(SEEN_FILE): open(SEEN_FILE, 'w').close()
    with open(SEEN_FILE, "r") as f: seen_hashes = set(line.strip() for line in f)

    final_payload = {cat: [] for cat in FEEDS.keys()}
    all_links = []

    # --- THE ENGINE ---
    for cat, configs in FEEDS.items():
        pool = []
        seen_topics = set()
        for cfg in configs:
            feed = feedparser.parse(cfg['url'])
            for i, e in enumerate(feed.entries):
                title = e.title.lower()
                if any(k in title for k in ["2025", "september"]): continue
                
                h = hashlib.md5(e.title.encode()).hexdigest()
                if h in seen_hashes: continue
                
                t_key = get_topic_key(title)
                if t_key in seen_topics: continue
                
                # Dynamic Scoring
                score = cfg['weight'] * 10
                score += 20 if i < 3 else 0 # Top-of-feed bias
                for kw, bonus in KEYWORDS.items():
                    if kw in title: score += bonus
                
                pool.append({"score": score, "entry": e, "hash": h, "topic": t_key})
        
        pool.sort(key=lambda x: x['score'], reverse=True)
        # Apply the Bottom Quota Floor
        final_payload[cat] = pool[:MIN_STORY_FLOOR[cat]]

    # --- COMPILATION & DELIVERY ---
    full_script = []
    for cat, stories in final_payload.items():
        script, links = await generate_segment(cat, stories, date_str)
        if script:
            full_script.append(script)
            all_links.extend(links)
            with open(SEEN_FILE, "a") as f:
                for s in stories: f.write(f"{s['hash']}\n")

    full_text = f"Hello, I'm Orator, and this is your daily briefing for {date_str}.\n\n" + "\n\n".join(full_script) + "\n\nThat concludes today's briefing. Goodbye."
    
    final_file = f"{file_date}_Orator.mp3"
    voice_file = "voice.mp3"
    await edge_tts.Communicate(full_text, "en-US-AndrewNeural", rate="+22%").save(voice_file)
    
    bg_music = "bg_music.mp3"; music_files = glob.glob("music/*.mp3")
    if music_files: bg_music = random.choice(music_files)
    
    if os.path.exists(bg_music):
        subprocess.run(["ffmpeg", "-y", "-i", voice_file, "-stream_loop", "-1", "-i", bg_music, "-filter_complex", "[1:a]volume=0.08[bg];[0:a][bg]amix=inputs=2:duration=first", final_file], check=True)
    else: os.rename(voice_file, final_file)

    webhook = DiscordWebhook(url=os.getenv("DISCORD_WEBHOOK_URL"), content=f"**{file_date} - - ORATOR BRIEFING FOR <@{os.getenv('DISCORD_USER_ID')}>**")
    with open(final_file, "rb") as f: webhook.add_file(file=f.read(), filename=final_file)
    with open("sources.txt", "w") as f: f.write("\n".join(all_links))
    with open("sources.txt", "rb") as f: webhook.add_file(file=f.read(), filename="sources.txt")
    webhook.execute()
    
    for f in ["voice.mp3", final_file, "sources.txt"]: 
        if os.path.exists(f): os.remove(f)

if __name__ == "__main__":
    asyncio.run(main())