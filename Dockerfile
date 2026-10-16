FROM python:3.13-slim

# libusb for pyusb, tzdata for the timezone in config.toml
RUN apt-get update \
 && apt-get install -y --no-install-recommends libusb-1.0-0 tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./

# mount config.toml (and token.json) here; the Things cache is written here too
ENV STATE_DIRECTORY=/state
ENTRYPOINT ["python", "briefing.py"]
