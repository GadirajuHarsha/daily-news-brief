import os
import asyncio
import feedparser
import edge_tts
import datetime
import hashlib
from openai import OpenAI
from discord_webhook import DiscordWebhook

FEEDS = {
    "politics": "https://www.theverge.com/policy/rss/index.xml",
    "sports": "https://www.espn.com/espn/rss/nba/news",
    "ut_sports": "https://texaslonghorns.com/rss?path=general",
    "tech": "https://www.theverge.com/tech/rss/index.xml",
    "gaming": "https://www.nintendolife.com/feeds/latest"
}

PRIORITIES = ["Mavericks", "Mavs", "Luka", "Kyrie", "Longhorns", "UT Austin", "Nintendo", "Switch", "iPhone", "Nvidia"]
SEEN_FILE = "seen_stories.txt"

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def get_seen_hashes():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(line.strip() for line in f)
    return set()

def save_seen_hash(content_hash):
    with open(SEEN_FILE, "a") as f:
        f.write(f"{content_hash}\n")

def get_best_stories(category, feed_url, seen_hashes):
    feed = feedparser.parse(feed_url)
    scored_entries = []
    
    for entry in feed.entries:
        story_hash = hashlib.md5(entry.title.encode()).hexdigest()
        if story_hash in seen_hashes:
            continue

        score = 0
        title = entry.title.lower()
        if any(word.lower() in title for word in PRIORITIES):
            score += 100
        
        if "poll:" in title or "discussion:" in title: continue

        scored_entries.append((score, entry, story_hash))
    
    scored_entries.sort(key=lambda x: x[0], reverse=True)
    return scored_entries

def generate_script(category, raw_data, length_minutes):
    if not raw_data.strip():
        return f"No new updates for {category} since your last briefing."

    category_rules = {
        "sports": "Focus heavily on the Dallas Mavericks and UT Austin Longhorns. Be extremely specific: name players, specific scores, and venues. If a game is rescheduled, state the teams and the original date. Avoid sweeping league-wide generalizations.",
        "politics": "Assume I have full context. Give me only the latest hard updates on votes, policy shifts, and strategic moves. Use specific names and dates.",
        "tech": "Focus on consumer hardware, devices, and product launches. Minimize corporate board-room news.",
        "gaming": "Focus on Nintendo and hardware news. No community polls or filler."
    }

    prompt = f"""
    You are a professional news orator for a personal {category} podcast. 
    TARGET LENGTH: {length_minutes} minutes.
    
    STRICT RULES:
    1. NO 'stay tuned', 'welcome back', or intro/outro music descriptions.
    2. DO NOT use headlines. Speak in a continuous, fast-paced narrative.
    3. Use specific names, dates, and numbers. No 'many people say' or 'teams are looking'.
    4. Write dates as words (e.g., 'January twenty-third').
    5. Detail: {category_rules.get(category, "")}

    DATA:
    {raw_data}
    """
    
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "You are a specific, fact-focused radio anchor. You provide hard updates with zero filler."},
                  {"role": "user", "content": prompt}],
        temperature=0.2
    )
    return response.choices[0].message.content

async def main():
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    seen_hashes = get_seen_hashes()
    
    sports_data = get_best_stories("sports", FEEDS["sports"], seen_hashes) + \
                  get_best_stories("ut_sports", FEEDS["ut_sports"], seen_hashes)
    sports_data.sort(key=lambda x: x[0], reverse=True)

    briefings = [
        {"cat": "politics", "data": get_best_stories("politics", FEEDS["politics"], seen_hashes), "len": "5", "voice": "en-US-AndrewMultilingualNeural"},
        {"cat": "sports", "data": sports_data, "len": "4", "voice": "en-US-AndrewNeural"},
        {"cat": "tech", "data": get_best_stories("tech", FEEDS["tech"], seen_hashes), "len": "2.5", "voice": "en-US-BrianNeural"},
        {"cat": "gaming", "data": get_best_stories("gaming", FEEDS["gaming"], seen_hashes), "len": "2.5", "voice": "en-US-AvaMultilingualNeural"}
    ]

    for b in briefings:
        top_stories = b['data'][:12]
        if not top_stories: continue

        extracted = []
        for score, s, h in top_stories:
            content = getattr(s, 'summary', getattr(s, 'description', ''))
            extracted.append(f"STORY: {s.title}\nDETAIL: {content}")
            save_seen_hash(h)

        script = generate_script(b['cat'], "\n\n".join(extracted), b['len'])
        filename = f"{date_str}_{b['cat']}.mp3"
        
        await edge_tts.Communicate(script, b['voice'], rate="+25%").save(filename)
        
        webhook = DiscordWebhook(url=webhook_url, content=f"📅 **{date_str}** | {b['cat'].upper()} PODCAST")
        with open(filename, "rb") as f:
            webhook.add_file(file=f.read(), filename=filename)
        webhook.execute()
        os.remove(filename)

if __name__ == "__main__":
    asyncio.run(main())
