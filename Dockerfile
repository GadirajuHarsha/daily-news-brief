FROM python:3.12-slim

WORKDIR /app

# Install system dependencies including FFmpeg
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy main executable footprint
# We don't copy users.json or chroma_db directly because they will be volume-mounted
COPY main.py .
COPY onboard.py .
# If user has a statically held music dir, just copy it too as fallback.
COPY music ./music 

CMD ["python", "-u", "main.py"]
