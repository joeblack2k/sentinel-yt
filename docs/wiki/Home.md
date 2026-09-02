# Sentinel Kids Guardian

Sentinel is the parent-side Guardian for SubTube Kids. It maintains the
approved catalog from the existing YouTube Kids browser session and exposes a
small Kids-only dataplane.

## Pages

- [Getting Started](Getting-Started)
- [Dashboard Functions](Dashboard-Functions)
- [API Reference](API-Reference)
- [Troubleshooting](Troubleshooting)

## What Sentinel Does

1. Reads configured YouTube Kids channel and playlist sources.
2. Applies the shared `/blocklist` rules and policy flags.
3. Classifies eligible content through the configured OpenCodex model.
4. Resolves playable media and stores the result in the SQLite backlog.
5. Exposes only approved, currently playable thumbnails to SubTube Kids.
6. Revalidates playback and records Kids watch events.

The blocklist is authoritative. AI output can hide or reject content, but it
cannot publish a blocked source or item.
