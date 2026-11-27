FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg gcc && rm -rf /var/lib/apt/lists/*

# Deno: the JavaScript runtime yt-dlp uses by default for YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

CMD ["python", "music_bot_pro.py"]
