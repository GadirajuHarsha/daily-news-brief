import os
import asyncio
import feedparser
import edge_tts
from openai import OpenAI
from discord_webhook import DiscordWebhook

FEEDS = {
    "politics": "https://www.allsides.com/rss/unbiased-balanced-news",
    "sports": "https://www.espn.com/espn/rss/nba/news",
    "tech": "https://www.techmeme.com/feed.xml",
    "gaming": "https://www.nintendolife.com/feeds/latest"
}

PRIORITY_KEYWORDS = ["NBA", "Longhorns", "Nintendo", "Switch", "UT Austin", "LeBron", "iPhone", "NVIDIA", "Tesla"]

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def get_best_stories(feed_url, limit=8):
    feed = feedparser.parse(feed_url)
    scored_entries = []
    
    for entry in feed.entries:
        score = 0
        title = entry.title.lower()
        if any(word.lower() in title for word in PRIORITY_KEYWORDS):
            score += 30
        
        if "poll:" in title or "discussion:" in title:
            continue

        scored_entries.append((score, entry))
    
    scored_entries.sort(key=lambda x: x[0], reverse=True)
    return [e[1] for e in scored_entries[:limit]]

def generate_script(category, raw_data):
    if not raw_data.strip():
        return f"No detailed updates for {category} were found in the feed right now."
        
    category_instructions = {
        "tech": "Focus on consumer products, hardware, and devices. Minimize talk of corporate acquisitions or executive changes unless they affect the products directly.",
        "sports": "Be extremely specific with names and locations. If a game is cancelled, say which teams. Provide a play-by-play or statistical feel where possible.",
        "politics": "Synthesize the different viewpoints provided in the text. Ensure you mention how different sides are framing the same event.",
        "gaming": "Focus on game releases, patches, and hardware news."
    }

    prompt = f"""
    You are a professional podcast scriptwriter. Create a 3 to 4-minute spoken-word narrative for a {category} briefing.
    
    SPECIAL INSTRUCTIONS FOR {category.upper()}: {category_instructions.get(category, "")}

    STRICT FORMATTING RULES:
    1. DO NOT include headlines, bullet points, or numbers. 
    2. DO NOT use words like 'Script:', 'Title:', or asterisks like '**'.
    3. Use smooth transitions between stories (e.g., 'Turning now to...', 'In other news...').
    4. Speak in full, descriptive sentences. Be specific with names, dates, and team names.
    5. Write dates as words (e.g., 'January twenty-fifth') to ensure the TTS reads them correctly.
    6. NO POLLS or questions to the audience. 
    7. This is a solo oration. Act as the single source of truth.

    RAW DATA:
    {raw_data}
    """
    
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a professional radio news anchor. You write scripts that flow naturally without any structural markers or metadata."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3
    )
    return response.choices[0].message.content

async def main():
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    
    for category, url in FEEDS.items():
        print(f"Generating {category} briefing...")
        stories = get_best_stories(url)
        
        extracted_stories = []
        for s in stories:
            content = getattr(s, 'summary', getattr(s, 'description', ''))
            extracted_stories.append(f"STORY TITLE: {s.title}\nSTORY CONTENT: {content}")
        
        raw_text = "\n\n".join(extracted_stories)
        script = generate_script(category, raw_text)
        
        filename = f"{category}_brief.mp3"
        
        voices = {
            "politics": "en-US-SteffanNeural", 
            "sports": "en-US-AndrewNeural",
            "tech": "en-US-BrianNeural",
            "gaming": "en-US-EmmaNeural"
        }
        voice = voices.get(category, "en-US-ChristopherNeural")
        
        communicate = edge_tts.Communicate(script, voice)
        await communicate.save(filename)
        
        webhook = DiscordWebhook(url=webhook_url, content=f"🎙️ **{category.capitalize()} Podcast Briefing**")
        with open(filename, "rb") as f:
            webhook.add_file(file=f.read(), filename=filename)
        webhook.execute()
        
        if os.path.exists(filename):
            os.remove(filename)

if __name__ == "__main__":
    asyncio.run(main())
