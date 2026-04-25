import os, asyncio, feedparser, datetime, hashlib, trafilatura, subprocess, random, glob, time
import numpy as np
import edge_tts
import chromadb
from openai import AsyncOpenAI
from discord_webhook import DiscordWebhook
from dotenv import load_dotenv

load_dotenv()

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

MIN_STORY_FLOOR = 5 
MAX_PER_SECTION = 8
WORD_BUDGET = 2200

SEEN_FILE = "seen_stories.txt"
client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
DISCORD_URL = os.getenv("DISCORD_WEBHOOK_URL")

def send_status(msg):
    print(msg)
    try:
        webhook = DiscordWebhook(url=DISCORD_URL, content=f"**Status:** {msg}")
        webhook.execute()
    except: pass

def get_topic_key(title):
    words = [w for w in title.lower().split() if len(w) > 3]
    return " ".join(words[:3])

def init_chroma():
    chroma_client = chromadb.PersistentClient(path="./chroma_db")
    collection = chroma_client.get_or_create_collection(name="news_memory")
    return collection

async def producer(queue):
    send_status("Producer: Spacing out concurrent RSS feed requests...")
    
    async def fetch_feed(cfg, cat):
        try:
            feed = await asyncio.to_thread(feedparser.parse, cfg['url'])
            for i, e in enumerate(feed.entries[:16]):
                await queue.put({"entry": e, "cat": cat, "cfg": cfg, "index": i})
        except Exception as ex:
            pass

    tasks = [fetch_feed(cfg, cat) for cat, configs in FEEDS.items() for cfg in configs]
    await asyncio.gather(*tasks)
    
    # Send poison pills to consumers
    for _ in range(10): 
        await queue.put(None)

async def consumer_worker(queue, category_pools, seen_hashes, seen_topics, collection, cst_now):
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            break
            
        e, cat, cfg, i = item['entry'], item['cat'], item['cfg'], item['index']
        
        # Fast synchronous duplication rejection
        title = e.title.lower() if hasattr(e, 'title') else ""
        h = hashlib.md5(title.encode()).hexdigest()
        if h in seen_hashes:
            queue.task_done()
            continue
            
        t_key = get_topic_key(title)
        if t_key in seen_topics:
            queue.task_done()
            continue
            
        # Async extract full context
        link = getattr(e, 'link', '')
        summary = getattr(e, 'summary', '')
        text = ""
        try:
            downloaded = await asyncio.to_thread(trafilatura.fetch_url, link)
            text = await asyncio.to_thread(trafilatura.extract, downloaded) if downloaded else summary
        except: text = summary
        
        if not text:
            queue.task_done()
            continue

        # RAG Deduplication Logic
        sim_score = 0.0
        try:
            # Query ChromaDB context limit: 72 hours logic
            # Calculate distance of embedded text against history
            res = await asyncio.to_thread(collection.query, query_texts=[text[:1000]], n_results=1)
            if res['distances'] and len(res['distances'][0]) > 0:
                dist = res['distances'][0][0]
                # Map distance to similarity heavily avoiding identical matches
                sim_score = max(0.0, 1.0 - float(dist))
        except: pass
            
        # Algorithmic Scoring Component
        base_score = float(cfg['weight'] * 50)
        if i < 3: base_score *= 1.25
        for kw, mult in MULTIPLIERS.items():
            if kw in title: base_score *= mult
            
        # The Semantic Deduplication Hybrid
        base_score = base_score * (1.0 - sim_score)
        
        # Recency Weighting Component
        recency_score = 0
        if hasattr(e, 'published_parsed') and e.published_parsed:
            pub_dt = datetime.datetime.fromtimestamp(time.mktime(e.published_parsed), datetime.timezone.utc)
            delta = cst_now - pub_dt
            hours = delta.total_seconds() / 3600
            if hours <= 4:
                recency_score = 20
            else:
                recency_score = max(0, 5 - (hours * 0.1)) # decay factor
                
        final_score = base_score + recency_score
        
        # Append safe data
        seen_topics.add(t_key)
        category_pools[cat].append({
            "score": final_score, 
            "entry": e, 
            "hash": h, 
            "topic": t_key,
            "text": text
        })
        queue.task_done()

async def generate_segment(name, stories, date_str, current_word_count):
    if not stories or current_word_count >= WORD_BUDGET: return "", []
    send_status(f"Generating script for **{name.upper()}** ({len(stories)} stories allowed)...")
    
    payload, links = "", []
    for item in stories:
        e = item['entry']
        text = item['text']
        payload += f"STORY: {e.title}\nCONTEXT: {text[:2500]}\n\n"
        links.append(f"[{name.upper()}] {e.title} - {e.link}")
    
    prompt = (f"You are Orator. Today is {date_str}. Write the {name} segment. "
              f"STYLE: VERBOSE & GRANULAR. 2-3 short, highly factual paragraphs per story. Focus on data density. No abstract symbols. "
              f"CRITICAL CONSTRAINT: You have a strict remaining budget of {WORD_BUDGET - current_word_count} words left to use. Be concise where necessary.")
    
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt + f"\n\nDATA:\n{payload}"}],
            temperature=0.3
        )
        return resp.choices[0].message.content, links
    except Exception as e:
        print(f"API Error: {e}")
        return "", []


async def main():
    cst_now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=6)
    date_str, file_date = cst_now.strftime("%A, %B %d, %Y"), cst_now.strftime("%Y-%m-%d")
    
    send_status(f"Starting Orator v3.0 Pipeline for **{date_str}**")
    
    if not os.path.exists(SEEN_FILE): open(SEEN_FILE, 'w').close()
    with open(SEEN_FILE, "r") as f: seen_hashes = set(line.strip() for line in f)

    collection = init_chroma()
    queue = asyncio.Queue(maxsize=1000)
    seen_topics = set()
    category_pools = {cat: [] for cat in FEEDS.keys()}

    # Phase 2: Start Producer and multiple Consumers
    send_status("Initializing Async Data Pipeline & RAG checks...")
    workers = []
    num_workers = 10
    for _ in range(num_workers):
        workers.append(asyncio.create_task(consumer_worker(queue, category_pools, seen_hashes, seen_topics, collection, cst_now)))
    
    await asyncio.gather(producer(queue))
    await asyncio.gather(*workers) # wait for queue to process payload

    # Ranking logic
    all_scores = [item['score'] for cat in category_pools.values() for item in cat]
    threshold = np.percentile(all_scores, 90) if all_scores else 0
    final_payload = {cat: [] for cat in FEEDS.keys()}

    for cat, pool in category_pools.items():
        pool.sort(key=lambda x: x['score'], reverse=True)
        final_payload[cat] = pool[:MIN_STORY_FLOOR]
        for s in pool[MIN_STORY_FLOOR:]:
            if s['score'] >= threshold and len(final_payload[cat]) < MAX_PER_SECTION:
                final_payload[cat].append(s)

    # Phase 3: Segment Generation with strict Word tracking
    full_script, all_links, full_story_objects = [], [], []
    current_words = 0
    
    for cat, stories in final_payload.items():
        if current_words >= WORD_BUDGET:
            break
            
        script, links = await generate_segment(cat, stories, date_str, current_words)
        if script:
            words_in_script = len(script.split())
            if current_words + words_in_script > WORD_BUDGET:
                # Force chunk script at nearest sentence to stay under budget
                cutoff = WORD_BUDGET - current_words
                sentences = script.split('.')
                trimmed_script = ""
                for sent in sentences:
                    if len(trimmed_script.split()) + len(sent.split()) > cutoff:
                        break
                    trimmed_script += sent + "."
                script = trimmed_script.strip()
                words_in_script = len(script.split())
                
            current_words += words_in_script
            full_script.append(script)
            all_links.extend(links)
            full_story_objects.extend(stories)

    if not full_script:
        send_status("Pipeline yielded nothing.")
        return

    # Ingest winning stories to ChromaDB for 72 hour permanence
    send_status(f"Ingesting {len(full_story_objects)} featured stories into ChromaDB...")
    docs, metas, ids = [], [], []
    for item in full_story_objects:
        text, h = item['text'], item['hash']
        docs.append(text[:1000])
        metas.append({"timestamp": cst_now.timestamp()})
        ids.append(h)
    
    if docs:
        collection.add(documents=docs, metadatas=metas, ids=ids)
        
    # Prune ChromaDB (72 hour memory window)
    cutoff_time = cst_now.timestamp() - (72 * 3600)
    try:
        old_records = collection.get(where={"timestamp": {"$lt": cutoff_time}})
        if old_records and old_records['ids']:
            collection.delete(ids=old_records['ids'])
    except: pass
    
    # Save standard file cache
    with open(SEEN_FILE, "a") as f:
        for s in full_story_objects: f.write(f"{s['hash']}\n")

    # Phase 4: Construct output using Voice 
    full_text = f"Hello, I'm Orator, and this is your daily briefing for {date_str}.\n" + "\n\n".join(full_script) + "\n\nGoodbye."
    send_status(f"Generated text ({current_words} words). Initializing Edge-TTS Audio Generation...")
    
    final_file = f"{file_date}_Orator.mp3"
    voice_file = "voice.mp3"
    
    communicate = edge_tts.Communicate(full_text, 'en-US-ChristopherNeural', rate="+10%")
    await communicate.save(voice_file)
    
    bg_music = "bg_music.mp3"; music_files = glob.glob("music/*.mp3")
    if music_files: bg_music = random.choice(music_files)
    
    # Simple direct 128kbps stereo ffmpeg command
    send_status("Mastering final MP3 tracking to 128kbps...")
    if os.path.exists(bg_music):
        cmd = [
            "ffmpeg", "-y", "-i", voice_file, "-stream_loop", "-1", "-i", bg_music, 
            "-filter_complex", 
            "[0:a]aresample=44100,pan=stereo|c0=c0|c1=c0[v];" 
            "[1:a]aresample=44100,volume=0.08[bg];"
            "[v][bg]amix=inputs=2:duration=first[out]",
            "-map", "[out]", "-ar", "44100", "-b:a", "128k", final_file
        ]
    else:
        cmd = ["ffmpeg", "-y", "-i", voice_file, "-ar", "44100", "-b:a", "128k", final_file]
        
    subprocess.run(cmd, check=True)
    
    fsize_mb = os.path.getsize(final_file) / (1024 * 1024)
    if fsize_mb > 24.5:
        send_status(f"CRITICAL: Final mastered file exceeded limits ({fsize_mb:.2f} MB). Aborting delivery.")
        return

    # Final Delivery
    send_status(f"Uploading briefing ({fsize_mb:.2f} MB) to Discord...")
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