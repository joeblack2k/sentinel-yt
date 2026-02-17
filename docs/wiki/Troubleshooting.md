# Troubleshooting

## Device not found in scan
1. Confirm Sentinel container runs with host networking on Linux.
2. Confirm Apple TV and Sentinel host are on same LAN/VLAN.
3. Run scan again while YouTube app is open on TV.

## Pairing fails with code error
1. Request a fresh TV code in YouTube settings.
2. Use the `Pair` button on the exact discovered row (avoid stale rows).
3. Retry within code validity window.

## "Device disconnected" events
- Sentinel reconnects automatically with backoff.
- If persistent, re-pair device and check network stability.

## Gemini unavailable / quota/auth issues
- Sentinel fail-open behavior allows playback when Gemini is down.
- Local blocklist/allowlist still applies.
- Configure `failure_webhook_url` to notify Home Assistant.

## SponsorBlock not skipping
1. Verify SponsorBlock module is enabled.
2. Verify SponsorBlock schedule is active now.
3. Ensure temporary release is not active.
4. Reduce minimum segment length if needed.

## History too large
- Use DB stats to monitor size.
- Purge only cache or history via `/api/admin/purge`.

## Data persistence
- SQLite data is persisted in mounted `/data`.
- If you recreate containers without the volume, pairing/history are lost.

