# TinyNVR

A lightweight, self-hosted NVR that records RTSP camera streams and provides a web UI for synced multi-camera playback with timeline scrubbing.

- Records via ffmpeg with no transcoding (`-c copy`)
- 1-minute self-contained MP4 segments with `moov` at front for instant byte-range scrubbing
- Synced multi-camera grid with a 24-hour timeline that handles DST transitions
- Rolling retention (configurable, default 7 days)
- Webhook to toggle cameras on/off

See [DESIGN.md](DESIGN.md) for the full design, including the storage
layout, recording/playback pipelines, and the reasoning against HLS.

## Requirements

- Linux (uses inotify to index segments as ffmpeg finishes writing them)
- **RTSP streams must be H.264 video + AAC audio.** The recorder is
  pure stream copy (`-c copy`) and writes MP4 segments; anything
  other than H.264+AAC will fail to mux. The expected deployment is
  behind [go2rtc](https://github.com/AlexxIT/go2rtc) with its `?mp4`
  stream variant, which normalizes camera streams to H.264+AAC for
  you — point TinyNVR at the go2rtc RTSP URLs, not at the cameras
  directly.

## Quick start

```bash
git clone https://github.com/jaysoffian/tinynvr
cd tinynvr
mise install
cp config.yaml.example config.yaml
# Edit config.yaml with your RTSP URLs
make run
```

The web UI is at http://localhost:8554.

## Configuration

Copy `config.yaml.example` to `config.yaml` and edit:

```yaml
storage:
  path: ./recordings
  retention_days: 7

cameras:
  front-door:
    url: rtsp://your-camera:554/stream1
    enabled: true
  living-room:
    url: rtsp://your-camera:554/stream2
    enabled: true
```

## Webhook

POST to `/api/webhook/{camera_name}` to toggle a camera on or off:

```bash
# Disable a camera
curl -X POST http://tinynvr:8554/api/webhook/living-room \
  -H 'Content-Type: application/json' \
  -d '{"enabled": false}'

# Re-enable it
curl -X POST http://tinynvr:8554/api/webhook/living-room \
  -H 'Content-Type: application/json' \
  -d '{"enabled": true}'
```

## Docker

```bash
# Build (alpine:edge + uv-installed Python 3.14 + ffmpeg from edge)
make image

# Run
podman run -d \
  -p 8554:8554 \
  -v /path/to/recordings:/recordings \
  -v /path/to/config-dir:/config \
  tinynvr:latest
```

`make image` stamps the short git SHA into `/app/VERSION` and the
standard OCI image labels (`org.opencontainers.image.revision`,
`.source`, `.title`). The running app reads `/app/VERSION` at startup
and exposes it via `GET /api/version`; the web UI shows it as small
dimmed text at the right edge of the topbar.

The `/config` volume expects a directory containing `config.yaml`
(the container reads `TINYNVR_CONFIG=/config/config.yaml`). The
`/recordings` volume is the persistent segment store.

## Development

Requires [mise](https://mise.jdx.dev) (installs uv and prek).

```bash
mise install        # install uv and prek
uv sync             # install Python dependencies
prek install        # install git hooks
make check          # run all linters (ruff, pyright)
make run            # start dev server with reload
```
