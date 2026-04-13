# TinyNVR

A tiny, self-hosted NVR for people with minimal NVR needs.

All I wanted was to record my four RTSP streams to disk and provide a basic
2x2 time-synced playback screen. I didn't need AI, motion detected, transcoding, none of that. I looked at a few other NVRs (Frigate, LightNVR, Viseron), but none fit the bill. At best, they had a ton of features I didn't want and had to figure out how disable. But even in "record-only" mode, I either couldn't get them to work  (Viseron), or they insisted on running an `ffmpeg` transcoding process to produce  smaller streams (Frigate), or they didn't have a grid-play back screen for anything but live view (LightNVR).

So I worked with Claude to write this NVR and frontend UI. All it does on the recording side is run `ffmpeg` pointed at RTSP streams to save footage to disk in one minute mp4 segments. The RTSP streams must already be in the correct format (H.264+AAC). Individual camera recording can be enabled/disabled via HTTP POST request.

The frontend UI is a grid with synced timeline. You can select a time range for download. The mp4 segments are combined into a single file.

The only browser I care about is Safari and it was tricky figuring out gapless playback. Chrome and Firefox probably work too, but I don't test with them. See [DESIGN.md](DESIGN.md) for the full design.

The only deployment I care about is an OCI image.

## Requirements

- A host capable of running an OCI image with enough storage for at least one day of recordings. I use [TrueNAS Scale](https://apps.truenas.com/managing-apps/installing-custom-apps/#installing-via-yaml-).
- One or more RTSP streams in H.264 video + AAC audio format. I use [go2rtc](https://go2rtc.org) for my Kasa KC100 indoor cameras and [Scrypted](https://www.scrypted.app) with its rebroadcast plugin for my Reolink doorbell.

## Configuration

Copy `config.yaml.example` to `config.yaml` and edit:

```yaml
storage:
  path: /recordings
  retention_days: 7

cameras:
  doorbell:
    url: "rtsp://host:8554/doorbell?mp4"
    enabled: true
  dining-room:
    url: "rtsp://host:8554/dining-room?mp4"
    enabled: true
  family-room:
    url: "rtsp://host:8554/family-room?mp4"
    enabled: true
  kitchen:
    url: "rtsp://host:8554/kitchen?mp4"
    enabled: true
```

## Webhook

POST to `/api/webhook/{camera_name}` to toggle recording for a camera on or off:

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

(This updates the `config.yaml` so that the camera enabled/disabled state survives restarts.)

## Podman

```bash
# Build and export OCI image to `tinynvr.tar`:
make image

# Build and run OCE image:
make run
```

The `/config` volume expects a directory containing `config.yaml`. The `/recordings` volume is the persistent segment store. Both must be writable.

Sample compose file:

```yaml
services:
  tinynvr:
    container_name: tinynvr
    hostname: tinynvr
    image: localhost/tinynvr:latest
    pull_policy: never
    environment:
      TZ: UTC
    ports:
      - "8554:8554"
    user: "816:816"
    read_only: true
    restart: unless-stopped
    security_opt:
      - no-new-privileges
    volumes:
      - /mnt/pool0/nvr/config:/config
      - /mnt/pool0/nvr/recordings:/recordings
      - type: tmpfs
        target: /tmp
```

## Development

Requires [mise-en-place](https://mise.jdx.dev) and [Podman](https://podman.io).

```bash
make setup
make check
```

## License

[MIT](./LICENSE)
