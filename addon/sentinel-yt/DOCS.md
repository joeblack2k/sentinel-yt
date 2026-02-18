# Sentinel YT Home Assistant Add-on

Sentinel YT is a YouTube guardian for Apple TV:
- YouTube Lounge monitor
- Blocklist / allowlist policy control
- Optional Gemini AI classification
- SponsorBlock module
- Web dashboard

## Add-on install
1. In Home Assistant, open **Settings -> Add-ons -> Add-on Store**.
2. Open the three-dot menu -> **Repositories**.
3. Add this repository URL:
   - `https://github.com/joeblack2k/sentinel-yt`
4. Install **Sentinel YT**.

## Configuration
- `timezone`: runtime timezone (for schedules).
- `gemini_api_key`: optional Gemini key.
- `gemini_model`: Gemini model ID.
- `build_version`: label shown in dashboard status.

## Networking
This add-on uses `host_network: true` for reliable YouTube DIAL/Lounge discovery.

Web UI:
- `http://<home-assistant-host>:8090`

## Persistent data
SQLite database is stored in:
- `/data/sentinel.db`

## Notes
- Dashboard auth is LAN-trust only in v1.
- MQTT, schedule, rules, and SponsorBlock can be managed inside Sentinel UI.

