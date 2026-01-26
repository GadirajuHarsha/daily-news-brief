import os, asyncio, feedparser, datetime, hashlib, trafilatura, subprocess, requests, random, glob, sys
import numpy as np
import soundfile as sf
from kokoro import KPipeline
from openai import OpenAI
from discord_webhook import DiscordWebhook

# feeds are organized by three segments
FEEDS = {
    "politics": [
        {"url": "https://www.pbs.org/newshour/feeds/rss/politics", "weight": 18},
        {"url": "https://apnews.com/hub/politics.rss", "weight": 20},
        {"url": "https://www.reutersagency.com/feed/?best-topics=political-news&post_type=best", "weight": 18},
        {"url": "https://thehill.com/homenews/feed/", "weight": 15},
        {"url": "https://prospect.org/api/rss/content.rss", "weight": 13},
        {"url": "https://jacobin.com/feed", "weight": 13},
        {"url": "https://gao.gov/blog/feed", "weight": 11},
    ],
    "sports": [
        {"url": "https://www.espn.com/espn/rss/nba/news", "weight": 18},
        {"url": "https://basketball.realgm.com/rss/wiretap/0/0.xml", "weight": 16},
        {"url": "https://feeds.hoopshype.com/xml/rumors.xml", "weight": 15},
        {"url": "https://www.cbssports.com/xml/rss/itnba.xml", "weight": 15},
        {"url": "https://api.foxsports.com/v1/rss?partnerKey=zBa1u7En6Sjz9N8H&tag=nba", "weight": 15},
        {"url": "https://texaslonghorns.com/rss?path=football", "weight": 14},
        {"url": "https://texaslonghorns.com/rss?path=general", "weight": 12},
        {"url": "https://thedailytexan.com/category/sports/feed/", "weight": 8},
    ],
    "media": [
        {"url": "https://www.serebii.net/index.rss", "weight": 17},
        {"url": "https://www.crunchyroll.com/news/rss", "weight": 15},
        {"url": "https://pitchfork.com/rss/news/", "weight": 14},
        {"url": "https://anitrendz.net/news/feed", "weight": 12},
        {"url": "https://www.animenewsnetwork.com/all/rss.xml?ann-edition=us", "weight": 12},
        {"url": "https://www.nintendolife.com/feeds/latest", "weight": 12},
        {"url": "https://aws.amazon.com/blogs/aws/feed/", "weight": 12},
        {"url": "https://hypebeast.com/music/feed", "weight": 10},
        {"url": "https://www.engadget.com/rss.xml", "weight": 9},
        {"url": "https://www.wired.com/feed/category/gear/latest/rss", "weight": 9},
        {"url": "https://arstechnica.com/feed/", "weight": 9},
        {"url": "https://corp.ign.com/feeds/news", "weight": 9},
        {"url": "https://www.theverge.com/rss/index.xml", "weight": 7},
    ]
}

MULTIPLIERS = {
    "pokemon": 2.0, "serebii": 1.9, "shonen": 1.5, "luka": 2.0, "mavs": 1.8, 
    "longhorns": 1.7, "iphone": 1.5, "rap": 1.4, "r&b": 1.6, "lakers": 1.3, 
    "nba": 1.6, "zelda": 1.9, "mario": 1.6, "clairo": 1.8, "daniel caesar": 1.8, 
    "drake": 1.7, "21 savage": 1.7, "jojo's": 1.8, "economy": 1.4, "texas": 1.5
}

# story governance
MIN_STORY_FLOOR = 5 
MAX_PER_SECTION = 8
TOTAL_MAX = 18 

SEEN_FILE = "seen_stories.txt"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
DISCORD_URL = os.getenv("DISCORD_WEBHOOK_URL")

def send_status(msg):
    """Sends a non-tagging status update to Discord so you can track progress."""
    print(msg)
    try:
        webhook = DiscordWebhook(url=DISCORD_URL, content=f"**Status:** {msg}")
        webhook.execute()
    except: pass

def get_topic_key(title):
    words = [w for w in title.lower().split() if len(w) > 3]
    return " ".join(words[:3])

async def generate_segment(name, stories, date_str):
    if not stories: return "", []
    send_status(f"Generating script for **{name.upper()}** ({len(stories)} stories)...")
    payload, links = "", []
    for item in stories:
        e = item['entry']
        text = trafilatura.extract(trafilatura.fetch_url(e.link))
        payload += f"STORY: {e.title}\nCONTEXT: {text[:3000] if text else e.summary}\n\n"
        links.append(f"[{name.upper()}] {e.title} - {e.link}")
    
    prompt = (f"You are Orator. Today is {date_str}. Write the {name} segment. "
              f"STYLE: VERBOSE & GRANULAR. 4+ detailed paragraphs per story. Focus on factual density. No symbols.")
    
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt + f"\n\nDATA:\n{payload}"}],
        temperature=0.3
    )
    return resp.choices[0].message.content, links

async def main():
    cst_now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=6)
    date_str, file_date = cst_now.strftime("%A, %B %d, %Y"), cst_now.strftime("%Y-%m-%d")
    
    send_status(f"Starting Orator Pipeline for **{date_str}**")
    
    if not os.path.exists(SEEN_FILE): open(SEEN_FILE, 'w').close()
    with open(SEEN_FILE, "r") as f: seen_hashes = set(line.strip() for line in f)

    category_pools = {cat: [] for cat in FEEDS.keys()}
    all_scores = []

    send_status("Parsing RSS feeds and calculating scores...")
    for cat, configs in FEEDS.items():
        seen_topics = set()
        for cfg in configs:
            feed = feedparser.parse(cfg['url'])
            for i, e in enumerate(feed.entries[:16]):
                title = e.title.lower()
                h = hashlib.md5(e.title.encode()).hexdigest()
                if h in seen_hashes: continue
                t_key = get_topic_key(title)
                if t_key in seen_topics: continue
                
                score = float(cfg['weight'] * 50)
                if i < 3: score *= 1.25
                for kw, mult in MULTIPLIERS.items():
                    if kw in title: score *= mult
                
                category_pools[cat].append({"score": score, "entry": e, "hash": h, "topic": t_key})
                all_scores.append(score)
                seen_topics.add(t_key)

    threshold = np.percentile(all_scores, 90) if all_scores else 0
    final_payload = {cat: [] for cat in FEEDS.keys()}
    total_count = 0

    for cat, pool in category_pools.items():
        pool.sort(key=lambda x: x['score'], reverse=True)
        final_payload[cat] = pool[:MIN_STORY_FLOOR]
        for s in pool[MIN_STORY_FLOOR:]:
            if s['score'] >= threshold and len(final_payload[cat]) < MAX_PER_SECTION and total_count < TOTAL_MAX:
                final_payload[cat].append(s)
        total_count += len(final_payload[cat])

    full_script, all_links = [], []
    for cat, stories in final_payload.items():
        script, links = await generate_segment(cat, stories, date_str)
        if script:
            full_script.append(script)
            all_links.extend(links)
            with open(SEEN_FILE, "a") as f:
                for s in stories: f.write(f"{s['hash']}\n")

    full_text = f"Hello, I'm Orator, and this is your daily briefing for {date_str}.\n\n" + "\n\n".join(full_script) + "\n\nGoodbye."
    
    send_status(f"Initializing Kokoro Audio Generation (~{len(full_text)//100} sentences)...")
    final_file = f"{file_date}_Orator.mp3"
    voice_file = "voice.wav"
    pipeline = KPipeline(lang_code='a') 
    generator = pipeline(full_text, voice='am_michael', speed=1.1, split_pattern=r'\n+')
    
    audio_chunks = []
    for gs, ps, audio in generator:
        audio_chunks.append(audio)
    
    combined_audio = np.concatenate(audio_chunks)
    sf.write(voice_file, combined_audio, 24000)
    
    bg_music = "bg_music.mp3"; music_files = glob.glob("music/*.mp3")
    if music_files: bg_music = random.choice(music_files)
    
    # --- FAILSAFE MASTERING LOOP ---
    # Discord Limit is 25MB (26,214,400 bytes). We target 24MB for safety.
    bitrates = ["160k", "128k", "96k", "64k"]
    success = False

    for br in bitrates:
        send_status(f"Mastering audio at **{br}** Stereo (Upsampled to 44.1kHz)...")
        
        if os.path.exists(bg_music):
            cmd = [
                "ffmpeg", "-y", "-i", voice_file, "-stream_loop", "-1", "-i", bg_music, 
                "-filter_complex", 
                "[0:a]aresample=44100,pan=stereo|c0=c0|c1=c0[v];" 
                "[1:a]aresample=44100,volume=0.08[bg];"
                "[v][bg]amix=inputs=2:duration=first[out]",
                "-map", "[out]", "-ar", "44100", "-b:a", br, final_file
            ]
        else:
            cmd = ["ffmpeg", "-y", "-i", voice_file, "-ar", "44100", "-b:a", br, final_file]
            
        subprocess.run(cmd, check=True)
        
        fsize_mb = os.path.getsize(final_file) / (1024 * 1024)
        if fsize_mb < 24.5:
            send_status(f"Final file size: **{fsize_mb:.2f} MB**. Within Discord limits.")
            success = True
            break
        else:
            send_status(f"File too large ({fsize_mb:.2f} MB). Retrying with lower bitrate...")

    if not success:
        send_status("CRITICAL: Could not compress file under 25MB even at 64k. Aborting.")
        return

    # Final Delivery
    send_status("Uploading briefing to Discord...")
    webhook = DiscordWebhook(url=DISCORD_URL, content=f"**{file_date} - ORATOR BRIEFING FOR <@{os.getenv('DISCORD_USER_ID')}>**")
    with open(final_file, "rb") as f: webhook.add_file(file=f.read(), filename=final_file)
    with open("sources.txt", "w") as f: f.write("\n".join(all_links))
    with open("sources.txt", "rb") as f: webhook.add_file(file=f.read(), filename="sources.txt")
    webhook.execute()
    
    send_status("Briefing delivered successfully. Cleaning up.")
    for f in [voice_file, final_file, "sources.txt"]: 
        if os.path.exists(f): os.remove(f)

if __name__ == "__main__":
    asyncio.run(main())