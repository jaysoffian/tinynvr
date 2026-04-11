# TinyNVR

A lightweight, self-hosted NVR that records RTSP camera streams and provides a web UI for synced multi-camera playback with timeline scrubbing.

- Records via ffmpeg with no transcoding (`-c copy`), near-zero CPU
- Configurable segment length (1–60 minutes), crash-safe
- 2×2 synced multi-camera viewer with 24-hour timeline
- HomeAssistant webhook to toggle cameras on/off
- 7-day rolling retention

## Requirements

- Linux (uses inotify to index segments as ffmpeg finishes writing them)
- [mise](https://mise.jdx.dev) (installs uv and prek)
- ffmpeg

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
  segment_minutes: 1    # 1–60

cameras:
  front-door:
    url: rtsp://your-camera:554/stream1
    enabled: true
  living-room:
    url: rtsp://your-camera:554/stream2
    enabled: true
```

## HomeAssistant webhook

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
docker build -t tinynvr .
docker run -d \
  -p 8554:8554 \
  -v /path/to/recordings:/recordings \
  -v /path/to/config.yaml:/app/config.yaml \
  tinynvr
```

## Development

```bash
mise install        # install uv and prek
uv sync             # install Python dependencies
prek install        # install git hooks
make check          # run all linters (ruff, pyright)
make run            # start dev server with reload
```
