# TinyNVR

A lightweight, self-hosted NVR that records RTSP camera streams and provides a web UI for synced multi-camera playback with timeline scrubbing.

- Records via ffmpeg with no transcoding video (`-c:v copy`), near-zero CPU
- 1-minute segment files, crash-safe
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

cameras:
  front-door:
    url: rtsp://your-camera:554/stream1
    enabled: true
  living-room:
    url: rtsp://your-camera:554/stream2
    enabled: true
```

### Why 1-minute segments

Segment length is fixed at 1 minute and not configurable. Two things
scale with segment length, and both favor short segments:

- **Playback latency to "live"**: a segment isn't playable until ffmpeg
  closes it (the `moov` atom is written on close), so the newest
  viewable footage is 0 to `segment_length` behind real time. At 1
  minute that's an average ~30 second lag.
- **Worst-case data loss on unclean shutdown**: if the machine loses
  power or ffmpeg is SIGKILLed mid-segment, the in-progress file has
  no `moov` atom and is unplayable — up to `segment_length` of footage
  from that camera is lost. Clean shutdowns (`docker stop`, Ctrl-C,
  `docker restart`) finalize the current segment via SIGTERM and do
  *not* lose data.

Scrubbing responsiveness does *not* scale with segment length:
segments are self-contained MP4 with `moov` at the front, so the
browser byte-ranges directly to the nearest keyframe regardless of
segment length.

Longer segments would save a trivial amount of filesystem and ffprobe
overhead, which isn't worth the playback-latency or data-loss cost.

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
