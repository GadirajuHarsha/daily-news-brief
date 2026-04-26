import json
import os

USERS_FILE = "users.json"

def create_user():
    print(r"""
======================================
     ORATOR v4 ONBOARDING WIZARD
======================================
    """)
    username = input("Enter new User's Name (e.g. 'alex'): ").strip().lower()
    
    # Read existing
    try:
        with open(USERS_FILE, "r") as f:
            users_data = json.load(f)
    except Exception:
        users_data = {}
        
    if username in users_data:
        print(f"User '{username}' already exists. Overwrite? (y/n)")
        if input().lower() != 'y':
            return
            
    print("\n[Discord Integration]")
    webhook_val = input(f"Enter {username}'s Discord Webhook URL: ").strip()
    discord_id = input(f"Enter {username}'s Discord User ID: ").strip()
    
    print("\n[Spotify Integration]")
    spotify_url = input(f"Enter {username}'s Spotify Playlist URL (Leave blank to use default Lo-Fi): ").strip()
    if not spotify_url:
        spotify_url = "https://open.spotify.com/playlist/37i9dQZF1DXc8kgYqQLKWv"
        
    print("\n[Base Interest Profile Selection]")
    print("1) Mixed (Politics, Sports, Music)")
    print("2) Pure Sports (NBA/NFL focused)")
    print("3) Tech & Gaming (Anime, Tech, Gaming)")
    choice = input("Select a default profile baseline (1-3): ").strip()
    
    # Assemble Base Profile
    feeds = {}
    multipliers = {}
    
    if choice == "2":
        feeds = {
            "sports": [
                {"url": "https://www.espn.com/espn/rss/nba/news", "weight": 20},
                {"url": "https://www.espn.com/espn/rss/nfl/news", "weight": 20},
                {"url": "https://bleacherreport.com/articles/feed?tag_id=14", "weight": 18}
            ]
        }
        multipliers = {"nba": 2.5, "nfl": 2.5, "lakers": 1.5, "football": 1.5}
    elif choice == "3":
        feeds = {
            "media & gaming": [
                {"url": "https://www.engadget.com/rss.xml", "weight": 18},
                {"url": "https://arstechnica.com/feed/", "weight": 18},
                {"url": "https://www.crunchyroll.com/news/rss", "weight": 15},
                {"url": "https://www.nintendolife.com/feeds/latest", "weight": 15}
            ]
        }
        multipliers = {"gaming": 2.0, "anime": 2.0, "apple": 1.5, "nintendo": 1.8}
    else: # Default 1
        feeds = {
            "world": [{"url": "https://apnews.com/hub/politics.rss", "weight": 15}],
            "sports": [{"url": "https://www.espn.com/espn/rss/nba/news", "weight": 15}],
            "culture": [{"url": "https://pitchfork.com/rss/news/", "weight": 15}]
        }
        multipliers = {"economy": 1.5, "music": 1.5, "basketball": 1.5}
    
    new_user = {
        # Rather than mapping an ENV var exclusively like before, we can store raw strings 
        # or Env prefixes for dynamic reading later.
        "webhook_url_raw": webhook_val,
        "discord_user_id_raw": discord_id,
        "spotify_playlist_url": spotify_url,
        "feeds": feeds,
        "multipliers": multipliers
    }
    
    users_data[username] = new_user
    
    with open(USERS_FILE, "w") as f:
        json.dump(users_data, f, indent=4)
        
    print(f"\n[SUCCESS] Successfully generated configuration profile for {username}!")
    print(f"They have been added to {USERS_FILE}.")

if __name__ == "__main__":
    create_user()
