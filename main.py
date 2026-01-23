import os
import asyncio
import feedparser
import edge_tts
from openai import OpenAI
from discord_webhook import DiscordWebhook

# priority interests
FEEDS = {
    "politics": "https://www.allsides.com/rss/unbiased-balanced-news",
    "sports": "https://www.espn.com/espn/rss/nba/news",
    "tech": "https://www.techmeme.com/feed.xml",
    "gaming": "https://www.nintendolife.com/feeds/latest"
}

PRIORITY_KEYWORDS = ["NBA", "Longhorns", "Nintendo", "Switch", "UT Austin", "LeBron"]

# initialize openai client (from github secrets)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# fetch and rank stories based on specific interests
def get_best_stories(feed_url, limit=5):
    feed = feedparser.parse(feed_url)
    scored_entries = []
    
    for entry in feed.entries:
        score = 0
        # nuanced scoring (keyword matching)
        if any(word.lower() in entry.title.lower() for word in PRIORITIES):
            score += 20
        # bias towards more recent or descriptive items
        scored_entries.append((score, entry))
    
    scored_entries.sort(key=lambda x: x[0], reverse=True)
    return [e[1] for e in scored_entries[:limit]]

def generate_script(category, raw_data):
    prompt = f"Summarize these {category} stories into a spoken script. \n\n {raw_data}"
    
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a literalist news transcribing assistant. Use ONLY the facts provided. No opinions. No outside context. No intros/outros."},
            {"role": "user", "content": prompt}
        ],
        temperature=0 # make sure its deterministic/consistent
    )
    return response.choices[0].message.content

async def main():
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    
    for category, url in FEEDS.items():
        print(f"Generating {category} briefing...")
        stories = get_best_stories(url)
        
        # combine headlines and summaries for the llm
        raw_text = "\n".join([f"{s.title}: {s.summary}" for s in stories])
        script = generate_script(category, raw_text)
        
        # audio generation (free via edge-tts)
        filename = f"{category}_brief.mp3"
        voice = "en-US-GuyNeural" if category == "sports" else "en-US-ChristopherNeural"
        
        communicate = edge_tts.Communicate(script, voice)
        await communicate.save(filename)
        
        # deliver to discord
        webhook = DiscordWebhook(url=webhook_url, content=f"🎙️ **Daily {category.capitalize()} Briefing**")
        with open(filename, "rb") as f:
            webhook.add_file(file=f.read(), filename=filename)
        webhook.execute()

if __name__ == "__main__":
    asyncio.run(main())
