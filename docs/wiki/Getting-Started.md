# Getting Started

## Prerequisites
- Docker + Docker Compose
- LAN access to your Apple TV / YouTube Lounge target
- Optional Gemini API key for AI decisions

## 1) Clone and Configure
```bash
git clone https://github.com/<your-account>/<your-repo>.git
cd <your-repo>
cp .env.example .env
```

## 2) Set Environment Values
Example `.env`:
```env
SENTINEL_PORT=8090
SENTINEL_DB_PATH=/data/sentinel.db
SENTINEL_BUILD_VERSION=v1
GEMINI_API_KEY=your_key_here
GEMINI_MODEL=gemini-2.0-flash
TZ=Europe/Amsterdam
```

## 3) Run Sentinel
```bash
docker compose --env-file .env up -d --build
```

Dashboard:
`http://<host-ip>:8090`

## 4) Pair First TV
1. Open YouTube on Apple TV -> Settings -> Link with TV code.
2. In Sentinel: `Devices` tab -> `Scan Network`.
3. Press `Pair` on the matching device row.
4. Enter the TV code and submit.

## 5) Verify
- `/healthz` returns `{"status":"ok"}`
- `/api/status` reports connected devices and module states

## 6) Pull Docker Image from GitHub Container Registry
```bash
docker pull ghcr.io/<owner>/<repo>:latest
```

