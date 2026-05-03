# Orator Bot Deployment Instructions

This guide explains how to spin up the Orator Bot on your home server. Once running, you will never need to touch this again. The bot will automatically download any new code pushed to GitHub and restart itself.

## Prerequisites
- Docker installed
- Docker Compose installed

## Step 1: File Setup
You should have a folder containing the following:
- `docker-compose.yml` (Handles the containers)
- `default_sources.json` (The master list of curated RSS feeds)
- A `music/` directory containing any `.mp3` background tracks.
- A `data/` directory (Optional; Docker will create this automatically to store the database and users)
- `.env`

## Step 2: Configure API Keys
1. Open the `.env` file in a text editor.
2. Fill in the keys:
   - `OPENAI_API_KEY`: Service Account key from the OpenAI Developer dashboard.
   - `DISCORD_BOT_TOKEN`: The bot token from the Discord Developer Portal.
   - `DISCORD_WEBHOOK_URL` (Optional): If you want daily updates piped to a webhook.

*Note: Spotify API keys are no longer needed as the bot uses local `.mp3` tracks due to Spotify's recent API restrictions.*

## Step 3: Launch
Open your terminal in the directory containing `docker-compose.yml` and run:

```bash
docker compose up -d
```

That's it!

### How Automatic Updates Work
The `docker-compose.yml` includes a service called **Watchtower**. Watchtower wakes up every 5 minutes and checks the GitHub Container Registry. If it detects a newer version of the `gadirajuharsha/daily-news-brief` image, it will download it, gracefully shut down the bot, and boot up the new version automatically. You do not need to manually pull or restart the containers.
