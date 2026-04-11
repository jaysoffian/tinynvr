# TinyNVR Design

Small, self-hosted NVR for recording RTSP cameras and playing them
back synchronously in a web UI. Optimized for a single household on
a local NAS, not for multi-tenant or cloud deployment.

## Scope

- 4–8 RTSP cameras, already served by a local go2rtc (or directly).
- Recording to local storage, never transcoding video.
- Web UI for synced multi-camera playback with scrubbing, ~N-day
  retention, and a download-range button.
- Target browser: Safari on macOS. Chrome and Firefox should also
  work, but Safari is the one we design around because it's the
  pickiest about video.

## Storage layout

```
{storage.path}/
  {camera-name}/
    2026-04-11_00-00-00.mp4
    2026-04-11_00-01-00.mp4
    ...
    2026-04-11.idx
    2026-04-12.idx
```

- Flat per-camera directories — no day subdirs, no hour subdirs. At
  1-minute segments and 4 cameras that's ~5,800 files/day, which
  modern filesystems don't care about.
- Segments are named by their UTC start time: `YYYY-MM-DD_HH-MM-SS.mp4`.
- Daily `.idx` files are append-only text, one line per segment:
  `filename: duration_sec`. Last entry wins on duplicate filenames.
- `.mp4` files are **self-contained** MP4 with `moov` at the front
  (ffmpeg `-segment_format mp4 -segment_format_options movflags=+faststart`).
  This is load-bearing — see "Why not HLS?" and "Playback" below.

## Recording pipeline

`tinynvr/recorder.py` runs one ffmpeg subprocess per enabled camera:

```
ffmpeg -rtsp_transport tcp -use_wallclock_as_timestamps 1 -i <rtsp> \
  -c:v copy -c:a aac -b:a 128k \
  -f segment -segment_time 60 -reset_timestamps 1 \
  -segment_format mp4 -segment_format_options movflags=+faststart \
  -segment_atclocktime 1 -strftime 1 \
  {camera}/%Y-%m-%d_%H-%M-%S.mp4
```

- **Video is never transcoded** (`-c:v copy`). RTSP cameras already
  emit H.264; CPU cost is effectively zero.
- **Audio is re-encoded to AAC** (`-c:a aac -b:a 128k`). MP4 cannot
  carry `pcm_mulaw` or most other audio codecs that IP cameras emit.
  We pay this CPU cost once at record time instead of on every
  playback request.
- **`-segment_atclocktime 1`** aligns segment boundaries to minute
  boundaries on the wall clock, so segments across cameras are
  roughly synchronized. They still drift by up to 1 minute because
  ffmpeg rotates when its own I/O cycle hits the boundary, not
  atomically across processes.
- **`+faststart`** moves the `moov` atom to the front of each segment
  on clean close, so Safari can start playing immediately without
  seeking to the end of the file to find metadata.
- **Segment length is fixed at 60 seconds** via `SEGMENT_SECONDS` in
  `recorder.py`. See the README "Why 1-minute segments" section for
  the rationale (short segments minimize live-playback lag and
  worst-case data loss on unclean shutdown; scrubbing responsiveness
  doesn't scale with segment length).

After each segment closes, an inotify `IN_CLOSE_WRITE` watcher on
the camera dir dispatches a duration-index task that runs `ffprobe`
on the file and appends `filename: duration_sec` to the day's
`.idx`. Failed probes delete the segment and record `0` in the
index so we don't retry. On recorder start, `validate_indexes`
sweeps any `.mp4` files missing from `.idx` (from a prior crash)
and probes them.

## Playback pipeline

### Backend

`GET /api/segments/{camera}/{filename}` is a **pure `FileResponse`**
— no ffmpeg, no subprocess, no remux. Starlette handles HTTP
`Range:` requests natively, and because segments are non-fragmented
MP4 with `moov` at the front, Safari's byte-range scrubber can jump
to any point in any segment by fetching only the needed byte range.
This is the core reason scrubbing is fast: an intra-segment seek is
one HTTP `206 Partial Content` response, not a full file download.

### Frontend: the 4-camera timeline

`static/index.html` is an Alpine.js SPA. The key widget is a shared
timeline across all cameras with a wallTime cursor. Each panel has
its own `<video>` pipeline that seeks to the wallTime offset within
whichever segment covers it.

- `wallTime` is a single `Date` object shared across all cameras.
- `findSegmentAt(camName, time)` walks the camera's segments looking
  for `start <= time < start + duration`.
- `_clampWallTime()` pins the cursor to `[earliest_playable_start,
  latest_end - segment_length]`. The right-side pinback exists
  because cameras drift by up to a full segment — clamping to the
  newest camera's edge would leave the other three showing
  "Offline."
- Timeline hour ticks are derived from `Date.getHours()` at
  one-hour real-time steps through the local day, so DST transitions
  show 23 ticks (spring ahead, one hour absent) or 25 ticks (fall
  back, one hour repeated) instead of misaligned labels.

### Frontend: double-buffered gapless playback

Each camera panel renders **two `<video>` elements**, absolutely
positioned in the same slot, with an `.active` opacity class driven
by a per-camera `activeSlot` ('a' | 'b') in `camStates`:

- **Active slot** plays the current segment, visible.
- **Pending slot** holds the next segment, hidden, primed for an
  instant swap.

When the active segment's `onended` fires, `loadSegmentForCamera`
finds the new current segment and detects that the pending slot
already has it — so the transition is just `state.activeSlot =
otherSlot`, which flips the `.active` class and calls `.play()` on
the now-visible element. No network fetch, no `loadedmetadata`
wait, no black frame. The old active (now pending) gets primed with
the segment after the new current.

Safari hedges aggressively on `preload="auto"` for hidden `<video>`
elements — it often fetches only metadata until the element becomes
visible. `_preloadPending` works around this with three redundant
primers:

1. **`#t=0.001` URL fragment** — the Media Fragments URI spec tells
   the browser to seek to that offset during load, which forces it
   to fetch past metadata into the first GOP.
2. **`currentTime = 0.001` on `loadedmetadata`** — redundant seek
   via JS. Some Safari versions respond to one but not the other.
3. **Brief muted `play()` then `pause()`** — aggressive last resort
   to force Safari to actually start decoding.

The three together mean that by the time a segment boundary is
reached, the pending video is fully decoded and pre-rendered to its
first frame, so the opacity swap is seamless.

## Retention

`tinynvr/retention.py` runs hourly and deletes `.mp4` files whose
start time (parsed from the filename) is older than `now -
retention_days`. This is a **rolling window**, not a whole-day
drop: at any given tick up to ~60 segments per camera age out (one
per minute since the last tick) rather than an entire UTC day
disappearing at midnight UTC.

Daily `.idx` files get a one-day grace period — they're deleted
when their UTC date is more than `retention_days + 1` days old —
so an index outlives the last segment it references. Briefly stale
`.idx` entries are invisible to `list_segments` (which silently
skips entries whose `stat()` fails) and to `download_range` (which
`is_file()` -checks before handing paths to ffmpeg's concat demuxer).

## Download-range endpoint

`GET /api/cameras/{name}/download?start=...&end=...` returns a
single MP4 stitched from the segments overlapping the requested
range. Uses ffmpeg's concat demuxer with `-c:v copy -c:a aac` (AAC
re-encode is harmless since inputs are already AAC) and streams
the output to the client via `StreamingResponse`. The UI has a
selection-bar widget that sets `start`/`end` on shift-drag.

## Build & deployment

- `make run` — dev server via `uv run uvicorn`.
- `make image` — podman builds a linux/amd64 image on `alpine:edge`
  with a uv-managed Python 3.14 installed into `/usr/local`. The
  runtime stage inherits both `/app` (containing the project venv)
  and `/usr/local` (containing Python itself). ffmpeg comes from
  alpine:edge's main repo so we track current versions automatically.
- The short git SHA is passed as `--build-arg GIT_COMMIT=...`,
  stamped into both `/app/VERSION` (read by the app at import
  time) and the standard OCI image labels
  (`org.opencontainers.image.revision`, `.source`, `.title`).
- `GET /api/version` returns the SHA; the frontend renders it as
  small dimmed monospace text at the right edge of the topbar.
- In dev (no VERSION file) the endpoint returns `"dev"`.

## Config

`config.yaml` (seeded from `config.yaml.example`):

```yaml
storage:
  path: ./recordings
  retention_days: 7

cameras:
  front-door:
    url: rtsp://your-camera:554/stream1
    enabled: true
```

`segment_minutes` used to be configurable (1–60) but is now fixed
at 1 minute in `recorder.py` — see README for why. The `.venv`,
the recorder, the frontend fallback, and the retention math all
share that assumption via `SEGMENT_SECONDS` on the backend and
`_segmentDurationMs = 60000` on the frontend.

## Why not HLS?

TL;DR: HLS gives gapless playback as a byproduct of adaptive-bitrate
streaming machinery that's completely wasted on a local-network
single-bitrate NVR, and the storage format HLS requires broke
scrubbing.

We explored HLS at length. The whole approach was backed out in
favor of the current static-MP4 + double-buffered `<video>` design.
Here's why:

### HLS requires fragmented MP4 (or MPEG-TS)

HLS plays media as a sequence of "segments" fed to the browser
through a `.m3u8` playlist. For MP4 containers this means
*fragmented* MP4 — files that start with a tiny `moov` describing
the timescale and an `EXT-X-MAP` init segment, followed by `moof` +
`mdat` fragments. That's the only MP4 shape MSE / hls.js can chew.

But fragmented MP4 with `+empty_moov+frag_keyframe+default_base_moof`
doesn't have a self-describing `moov` at the front of each segment.
The browser needs the init segment loaded before it can play any
segment, and segment-relative seeks require MSE's SourceBuffer
append timing to line up exactly. When we tried this:

- **Byte-range scrubbing broke**. Safari's native `<video>` element
  can byte-range into a self-contained MP4 with `moov` at front and
  seek anywhere. With fragmented MP4, scrubbing requires hls.js to
  parse the playlist, load the init segment, append fragments in
  order, and map byte offsets to media time — all in JS. Every seek
  became a sequence of HTTP fetches and buffer appends instead of
  one `206 Partial Content`.
- **Init-segment rotation got fragile**. ffmpeg's fragmented MP4
  segmenter doesn't perfectly reuse an init segment across rotations
  for all source streams, so we ended up either writing per-segment
  init files or pinning one and hoping the timescale never changed.
- **Media timestamps drifted** across segments under playback-rate
  changes, producing audible stutter on speed controls.

### HLS adds complexity that pays off only at scale

HLS was designed for multi-bitrate adaptive streaming over lossy
networks with variable bandwidth and intermediate CDNs. For that
use case, the playlist + segment model is worth the complexity:
you can swap down to a lower bitrate mid-stream, resume after a
buffer underrun, etc. For TinyNVR:

- Single fixed bitrate (whatever the camera emits).
- Single client on a gigabit LAN talking to the NAS directly.
- No CDN, no intermediate caches, no adaptive bitrate.

The adaptive-streaming machinery — playlist generation, init
segments, segment numbering, discontinuity tags, manifest refresh
intervals, hls.js as a ~1MB JS dependency — is all dead weight.

### Double-buffering gives us gapless without any of that

The actual goal of the HLS detour was *gapless playback across
segment boundaries*. It turns out we can get that with a much
smaller change: render two `<video>` elements per panel, keep one
primed with the next segment, and opacity-swap on `onended`. No
fragmented MP4, no manifest, no new libraries, no byte-range
regression. Just ~150 lines of Alpine.js and a CSS class.

The result: byte-range scrubbing still works unchanged (Safari
seeks directly into any segment via HTTP `Range:`), segment
transitions are gapless (primed pending slot becomes visible in
one paint cycle), and the recorder config stays at
`-segment_format mp4 -segment_format_options movflags=+faststart`
— simple, testable, debuggable.

### What would bring HLS back

Honest case: if TinyNVR ever needs multi-bitrate playback (phones
on cellular watching the same footage the desktop is watching at
full quality), or if it ever needs to serve footage across a WAN
where the single-bitrate assumption breaks, HLS starts earning its
keep. Neither of those is in scope.
