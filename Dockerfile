FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 curl ffmpeg build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gauchOS_voice_agent.py .
COPY server.py .

EXPOSE 7860

CMD ["python", "server.py"]
