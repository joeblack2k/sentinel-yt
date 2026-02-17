# Sentinel Wiki

Sentinel is a YouTube AI guardian for Apple TV sessions.  
This wiki explains every user-facing function and every API in plain English.

## Pages
- [Getting Started](Getting-Started)
- [Dashboard Functions](Dashboard-Functions)
- [API Reference](API-Reference)
- [Troubleshooting](Troubleshooting)

## What Sentinel Does
1. Discovers YouTube Lounge devices on your LAN.
2. Pairs to your TV with a YouTube TV code.
3. Monitors current/up-next videos in real time.
4. Decides ALLOW/BLOCK via local lists and optional Gemini.
5. On BLOCK, forces playback to a safe candidate video.
6. Stores history and supports manual allow/block control.

## What SponsorBlock Does
SponsorBlock is a separate module that skips known sponsor segments.  
It has independent state, schedule, and webhook controls.

