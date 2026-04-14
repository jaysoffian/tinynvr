# TinyNVR Design

Small, self-hosted NVR for recording RTSP cameras and playing them
back synchronously in a web UI. Optimized for a single household on
a local NAS, not for multi-tenant or cloud deployment.

## Scope

- 4–8 RTSP cameras, already served by a local go2rtc (or directly).
- Recording to local storage, never transcoding.
- Web UI for synced multi-camera playback with scrubbing, ~N-day
  retention, and a download-range button.
- Target browser: Safari on macOS. It's the user's daily browser
  and the one that has to feel right. Chrome and Firefox aren't
  tested and aren't targeted — anything that works on Safari and
  also happens to work on them is bonus, not requirement.

## Storage layout

```
{storage.path}/
  tinynvr.db           # SQLite segment index (WAL mode)
  tinynvr.db-wal
  tinynvr.db-shm
  2026-04-13/
    04/
      front-door/
        26-56.mp4
        27-02.mp4
        ...
      kitchen/
        26-56.mp4
        ...
    05/
      front-door/
        ...
  2026-04-14/
    ...
```

- **Date-first, hour-bucketed.** Frigate-style layout: segments are
  grouped first by UTC date, then by hour, then by camera, then
  minute-second leaf files. The single biggest benefit is retention:
  an expired hour is one `shutil.rmtree` call per camera instead of
  ~60 per-file `unlink`s.
- Segment start time is fully encoded in the path components:
  `YYYY-MM-DD/HH/<camera>/MM-SS.mp4`. `_parse_segment_path` in
  `recorder.py` reconstructs the UTC start time from the path.
- `.mp4` files are **self-contained** MP4 with `moov` at the front
  (ffmpeg `-segment_format mp4 -segment_format_options movflags=+faststart`).
  This is load-bearing — see "Why not HLS?" and "Playback" below.
- The SQLite DB at `{storage.path}/tinynvr.db` is the segment index —
  one row per segment with `(camera, start_utc, duration_ms,
  size_bytes)`. It is the source of truth for all read paths
  (`list_segments`, `download_range`, `recordings_range`); the
  filesystem is only walked by `_validate_segments` at recorder
  startup to catch segments left by a prior crash.

## Recording pipeline

`tinynvr/recorder.py` runs one ffmpeg subprocess per enabled
camera. The full argv:

```
ffmpeg
  -hide_banner -loglevel warning
  -rtsp_transport tcp
  -timeout 30000000
  -i <rtsp_url>
  -c copy
  -metadata title=<camera_name>
  -f segment
  -reset_timestamps 1
  -segment_time 60
  -segment_format mp4
  -segment_format_options movflags=+faststart
  -segment_atclocktime 1
  -strftime 1
  {storage}/%Y-%m-%d/%H/<camera>/%M-%S.mp4
```

The subprocess is launched with `TZ=UTC` explicitly forced in its
environment so strftime in the output pattern always expands to
UTC regardless of the container's timezone. If this ever leaks,
segment paths and `_parse_segment_path` fall out of sync.

Every flag, and why it's there:

- **`-hide_banner -loglevel warning`** — suppress the startup
  banner and info chatter. Warnings and errors still reach stderr
  where `_monitor` captures them into `last_error`. Without this,
  every restart dumps a codec table into the logs.
- **`-rtsp_transport tcp`** — force TCP for the RTSP media
  session. UDP is the default and drops packets under any load,
  producing decode errors in the segments. TCP costs a bit of
  latency (irrelevant for a recording NVR) and in exchange
  delivers every frame on a LAN.
- **`-timeout 30000000`** — 30 seconds (in microseconds), the
  RTSP socket I/O timeout. If the upstream goes silent, ffmpeg
  exits with an error and `_monitor` restarts it with exponential
  backoff. Without a timeout, ffmpeg hangs forever on a stalled
  TCP socket and no segments land on disk.
- **`-i <rtsp_url>`** — input URL from `config.yaml`, taken
  verbatim. The user is responsible for making sure the upstream
  emits H.264 + AAC; go2rtc's `?mp4` stream variant handles this.
- **`-c copy`** — the load-bearing decision: nothing is ever
  transcoded. ffmpeg is just demuxing RTSP and remuxing into MP4,
  so CPU stays near zero on any host. The cost is that the
  upstream must already be in a codec combination MP4 accepts —
  if an RTSP source emits `pcm_mulaw` or similar directly,
  segments fail to mux. Fix the upstream, don't add an encode
  step here.
- **`-metadata title=<camera_name>`** — embed the camera name in
  the MP4 `title` tag. Cosmetic: when a downloaded file is opened
  in QuickTime or VLC, the player shows the camera name instead
  of just the filename.
- **`-f segment`** — use the segment muxer. The segment muxer is
  a wrapper around another muxer (specified by `-segment_format`)
  that rotates output files on a time or size trigger. Distinct
  from `-f hls`, which would emit `.m3u8` + fragmented segments
  (see "Why not HLS?" for why we don't).
- **`-reset_timestamps 1`** — each output segment starts at
  timestamp 0 instead of carrying through the RTSP timeline. This
  makes every segment individually playable: a player opening
  `27-02.mp4` sees a self-contained video beginning at t=0, not
  one with a timestamp offset from some distant origin. Without
  this, seeking inside a segment would need context the file
  doesn't carry.
- **`-segment_time 60`** — nominal segment length in seconds,
  sourced from `_SEGMENT_SECONDS`. See the "Why 60 seconds"
  discussion below.
- **`-segment_format mp4`** — the inner muxer is non-fragmented
  MP4. The segment muxer's default is MPEG-TS, which would emit
  `.ts` files. We want MP4 specifically because (a) it's the
  format the download-range concat demuxer and browser-side
  mp4box.js both consume, and (b) it's what the non-fragmented-
  MP4-with-moov-at-front guarantee applies to.
- **`-segment_format_options movflags=+faststart`** — options
  passed through to the inner MP4 muxer. `+faststart` relocates
  the `moov` atom from end-of-file (MP4 default, needs a seek
  back after mdat is written) to the front on clean close. This
  is what makes byte-range access into a segment possible without
  downloading the whole file first. Only applies to cleanly
  closed segments — a SIGKILLed segment has no `moov` and is
  unplayable (see the data-loss bullet below).
- **`-segment_atclocktime 1`** — align segment rotations to
  minute boundaries on the wall clock, not `ffmpeg_start_time +
  N×60`. This keeps the four cameras' segments roughly aligned
  so they stitch together cleanly in the grid UI. They still
  drift by up to a full segment because each ffmpeg rotates when
  its own I/O cycle hits the boundary, not atomically across
  processes.
- **`-strftime 1`** — enable `%Y`, `%m`, `%H`, etc. expansion in
  the output filename. Without this, the pattern is literal and
  ffmpeg overwrites the same file on every rotation. Note: the
  segment muxer does **not** support `-strftime_mkdir 1` (that
  flag only exists on the `hls` muxer), so the recorder has to
  pre-create leaf directories itself — see "Pre-creating leaf
  directories" below.
- **Output pattern `{storage}/%Y-%m-%d/%H/<camera>/%M-%S.mp4`**
  — strftime fields resolve at rotation time against the
  subprocess's UTC environment, producing the storage layout
  documented above.

### Why 60 seconds

Segment length is fixed at 60 seconds and intentionally not
configurable. Three things scale with segment length, and all
three favor short segments:

- **Playback latency to "live"**: a segment isn't playable until
  ffmpeg closes it (the `moov` atom is written on close), so the
  newest viewable footage is 0 to `segment_length` behind real
  time. At 1 minute that's an average ~30 second lag.
- **Worst-case data loss on unclean shutdown**: if the machine
  loses power or ffmpeg is SIGKILLed mid-segment, the in-progress
  file has no `moov` atom and is unplayable — up to
  `segment_length` of footage from that camera is lost. Clean
  shutdowns (`docker stop`, Ctrl-C, `docker restart`) finalize
  the current segment via SIGTERM and do *not* lose data.
- **Scrub latency**: under the current MSE + mp4box.js frontend,
  each scrub into a new segment fetches the full segment,
  transmuxes it, and appends it to the SourceBuffer. Cost is
  roughly linear in segment length (~12 MB at 60s; at 600s it
  would be ~120 MB per scrub). Short segments also match the
  ~5-minute MSE buffer trim at a useful granularity — at 600s
  segments the trim window wouldn't even fit one segment.

A historical note: the pre-MSE playback path (`video.src = url`
with Safari's native byte-range scrubber) did *not* scale scrub
latency with segment length, because Safari would byte-range
directly to the moov atom + nearest keyframe and fetch a few
hundred KB regardless of the file's total size. That optimization
was traded away when MSE shipped — see the Playback section's
"Known trade-offs" and the "Rejected playback approaches" section
for why MSE was still worth it. The 60-second choice became
*more* defensible under MSE, not less.

**Pre-creating leaf directories.** ffmpeg's segment muxer cannot
create intermediate directories from its strftime output pattern
(`-strftime_mkdir 1` exists only on a different muxer we don't use).
Each recorder runs a small `_dir_precreate_loop` that `mkdir -p`s
the current and next hour's leaf dir (`{storage}/YYYY-MM-DD/HH/<camera>/`),
waking ~30 seconds before each wall-clock hour boundary. `_spawn`
also pre-creates them synchronously before launching ffmpeg, so the
very first segment has a home.

**Indexing.** A single application-wide `SegmentWatcher` runs one
inotify instance for the whole storage tree. On startup it walks
every existing leaf dir and adds a `CLOSE_WRITE` watch; new leaf
dirs get a watch added by `_dir_precreate_loop` as it creates them.
When retention `rmtree`s an old hour dir, the kernel drops those
watches automatically via `IN_IGNORED`. The watch count is bounded
by `num_cameras × num_existing_hour_dirs`, well under the kernel's
default watch limit.

When a `.mp4` closes, the watcher parses `(camera, start_utc)`
from the path, looks up the matching recorder, and dispatches
`CameraRecorder.index_segment`. That runs `ffprobe` and inserts a
row into `segments` via `db.insert_segment`. Failed probes delete
the segment on disk and never get a row — there is no
"duration 0" sentinel.

On recorder start, `_validate_segments` walks
`{storage}/*/*/{camera}/*.mp4`, finds segments whose `start_utc` is
not in the DB (crashed probe-before-insert, SIGKILL, lost inotify
event), and re-probes them in parallel with a semaphore.
`INSERT OR REPLACE` makes recovery idempotent.

## Playback pipeline

### Backend

`GET /api/segments/{camera}/{filename}` is a **pure `FileResponse`**.
Starlette handles HTTP `Range:` requests natively, and because
segments are non-fragmented MP4 with `moov` at the front, the
browser can byte-range into any segment. This is what lets mp4box.js on the frontend fetch segments
as ArrayBuffers cheaply, and what lets the download-range endpoint
stitch segments via ffmpeg's concat demuxer without a remux pass.

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

### Frontend: MSE playback via mp4box.js

Each camera panel renders **one `<video>` element** backed by a
`MediaSource` with separate video and audio `SourceBuffer`s (both
in `mode='sequence'`). Segments are fetched as full ArrayBuffers,
fragmented in-browser by [mp4box.js](https://github.com/gpac/mp4box.js)
(0.5.3, ~156 KB, vendored via Makefile), and appended to the
SourceBuffers. Segment boundaries are gapless — the next segment's
data is already in the buffer via prefetch, so playback continues
without any `emptied` / `loadstart` cycle.

`mode='sequence'` was essential. An earlier attempt with
`mode='segments'` + per-segment `timestampOffset` broke because
mp4box's fMP4 output has non-zero `baseMediaDecodeTime` values,
which produced boundary stalls and misalignment between
`video.currentTime` and the anchor model. `sequence` auto-advances
timestamps and sidesteps both. Init segments are appended once per
SourceBuffer lifetime (not per segment) — codec parameters are
stable within a recording session since everything is `-c copy`
from a stable go2rtc upstream. After a flush (scrub or date
change), init is re-appended.

**Anchor model.** Each camera tracks an `anchorMs` (the wall time
of the first segment appended to its SourceBuffers). The video
element's `currentTime` maps to wall time as
`wallMs = anchorMs + currentTime * 1000`. A **dynamic clock master**
is re-picked each raf tick: any camera currently rendering valid
video (has a `currentSegment`, `readyState >= HAVE_FUTURE_DATA`,
not paused/seeking/loading) qualifies, with hysteresis preferring
the previous tick's master to avoid flapping. `wallTime` is
snapped to the master's `currentTime`-derived value every tick,
preserving the "UI time matches on-screen pixels" invariant. Every
camera — master included — runs the same hard-seek drift correction
at a 500ms threshold; the master self-corrects to a no-op because
`wallTime` was just sourced from it. See the **"Hybrid clock
design"** section below for why there's no privileged camera and
why drift correction is hard-seek rather than PI-controlled
`playbackRate` trimming.

**Prefetch.** After appending the current segment,
`_enqueuePrefetch` enqueues the next segment (from
`findNextSegment`). The append loop processes both sequentially.
When playback reaches the boundary, the next segment is already
buffered and playback continues without interruption.

**Scrub.** Timeline scrubs (`_scrubToPoint`, `_fsScrubToPoint`)
only update `wallTime` and the cursor during drag — segment loads
are deferred to mouseup (`endScrub` / `endFsScrub`). Keyboard
scrubs (`_seekByKey`) load immediately but abort any in-flight
fetch first (per-camera `AbortController`). If the target segment
is far from the currently buffered range (> 2s outside), the
SourceBuffers are flushed and re-anchored before appending.

**Loading guards.** While any segment is loading (`videoLoading`)
and no camera is rendering, the clock freezes `wallTime` at the
scrub target instead of free-running. The video element is paused
before a flush so it doesn't auto-play from the wrong position
when new buffer data appears. Both prevent the snap-back and
fast-forward artifacts that appeared when the clock drifted ahead
of the video during loading.

**Buffer cleanup.** After each successful append, if the buffered
range exceeds ~5 minutes, old data behind the playhead is trimmed
lazily via `SourceBuffer.remove()`.

**The `!found`-skip-when-playing fix is narrow.** During live
playback, if `findSegmentAt` returns null and `wallTime` is within
2 seconds past the end of the currently loaded segment, the
`!found` branch returns silently: the last frame stays visible and
the clock advances through a sub-second rotation gap. Any further
"not found" — a deliberate scrub into an empty region, or a real
multi-minute outage — clears `currentSegment`, pauses the camera's
video, and lets the clock-master selection pick a different
camera.

**Known trade-offs of the MSE approach**:

- **Scrub latency** ~300–500ms for a large time jump (full segment
  fetch + transmux + append) vs ~250ms under the old `video.src =
  url` approach that got Safari's native byte-range seek for free.
  Timeline drags defer loading to mouseup so the cursor moves
  smoothly but the frame only updates on release.
- **Sequence-mode drift**: auto-advancing timestamps accumulate
  sub-second duration mismatches between segments (~50 ms/segment
  worst case). Bounded by the 500ms hard-seek drift correction,
  reset on scrub.
- **Full segment fetch**: ~12 MB per segment, vs the few-hundred-KB
  moov+first-GOP that the old byte-range approach needed. Fine on a
  LAN.
- **Complexity**: the playback layer grew from ~80 lines under
  `video.src = url` to ~300 lines under MSE. The state machine is
  not simple.

If scrub latency ever becomes intolerable, the avenue is
incremental mp4box parsing (fetch the moov first via a byte range,
then stream mdat chunks so the first GOP can be appended before
the full download completes).

See **"Rejected playback approaches"** below for the approaches
tried before MSE and why each failed.

## Scrub preview sprites

Hovering over the timeline pops up a per-camera preview of the
moment under the cursor. Each camera renders one row showing a
single thumbnail at native aspect ratio, and the cursor's
sub-second position within the segment selects which of the 6
pre-baked thumbnails is visible — Plex / Infuse style.

**Sprite generation** (`tinynvr/sprite.py`). For each closed
segment, ffmpeg samples 6 keyframes and tiles them into a single
JPEG sibling of the source MP4 (`MM-SS.mp4` → `MM-SS.jpg`). The
key flag is `-skip_frame:v nokey` placed *before* `-i`: the
decoder only emits keyframes to the filter graph, so a 60-second
segment with GOP=2 only decodes ~30 I-frames instead of ~1500
total — ~99% CPU savings vs. a naive `fps=1/10` filter alone.
Putting `-skip_frame:v nokey` after `-i` silently does nothing
(ffmpeg treats it as an output option). Output is `tile=6x1`,
`scale=400:-2`, `-qscale:v 2`. Each sprite is ~30–60 KB, so total
sprite storage at 7-day retention is ~3% of video storage.

Co-locating sprites with their source MP4 means retention's
`shutil.rmtree({hour_dir})` cleans them up for free — no separate
sweep, no DB rows, no schema change.

**Generation.** The post-process hook in
`CameraRecorder.index_segment` fires the sprite ffmpeg as a
background task (`asyncio.create_task`, with the task stashed on
the recorder so the GC doesn't drop it) right after the DB insert
succeeds. Failures log a warning and never affect indexing. New
segments are sprited within a second of being closed.

For the one-time backfill of segments recorded before the feature
shipped, run `uv run python -m tinynvr.sprite_backfill`. It walks
the storage tree, generates any missing `.jpg` siblings 4-way
parallel, and exits. Once that's done on a given deployment, the
script (and `tinynvr/sprite_backfill.py`) can be deleted —
post-process keeps everything sprited going forward.

**Serving.** The existing `GET /api/segments/{camera}/{filename}`
route is reused for sprite requests: a `.jpg` filename resolves to
the sibling JPEG path and is served via `FileResponse`. There is
no on-demand generation in the serving path — a missing sprite
returns 404 and the frontend's `background-image` silently shows
the row's `#000` background. Declaring a separate `{filename}.jpg`
route wouldn't help here: FastAPI matches in declaration order
and the existing `{filename}` catch-all would shadow it. Sprites
are served with `Cache-Control: public, max-age=31536000,
immutable` so the browser caches aggressively.

**Frontend layout.** In the multi-camera grid view, the popover
stacks one row per camera in `this.cameras` order, matching the
playback grid mental model. In fullscreen single-camera view, only
the focused camera's row is shown (and at a much larger thumb
size, since the popover only has one row to spend its budget on).
Both views render their own `.timeline-hover-preview` element —
one inside the bottom timeline's cursor region, one inside each
fullscreen overlay's `.fs-timeline` — but they share state via
the same `hoverPreview` Alpine object.

Each row shows the thumb whose index is
`floor((cursorMs - segStartMs) / 10000)`, clamped to 0..5. The
single tile is rendered by setting `background-image` to the
sprite URL, `background-size` to `(6 * displayW) × displayH` (the
full strip's display footprint), and `background-position` to
`-idx * displayW` — the row's `width × height` box only exposes
one tile of the underlying 6×1 sprite. Native frame dimensions
come from `videoWidth` / `videoHeight` on the corresponding
`<video>` element's first `loadedmetadata` event; until detected,
a 16:9 fallback is used. Each tile is fit into a `(maxW, maxH)`
box preserving aspect — `(280, 130)` in grid mode and `(480, 270)`
in fullscreen — so 16:9, 4:3, and portrait cameras coexist
sensibly in one popover.

A memo key built from `(fullscreenIdx, camera, segment filename,
thumb index)` for each rendered row short-circuits rebuilds on
every mousemove — Alpine reactivity only fires when the user
actually crosses a 10-second bucket boundary, a segment boundary,
or toggles fullscreen.

The currently-recording segment has no sprite (ffmpeg hasn't
closed the source MP4 yet). The preview row is silently omitted —
matches the existing 0-to-60s behind-live lag.

## Retention

`tinynvr/retention.py` runs hourly. The unit of deletion is a whole
**hour directory**: when an hour dir's wall-clock end
(`hour_start + 1h`) is already older than `now - retention_days`,
the entire `{storage}/YYYY-MM-DD/HH/` subtree is removed with a
single `shutil.rmtree`, dropping every camera's segments for that
hour in one call instead of per-file `unlink`s. Empty date dirs are
`rmdir`ed opportunistically on the same pass.

After the rmtree pass, the DB is pruned with a single
`DELETE FROM segments WHERE start_utc < ?`. The cutoff is
**hour-aligned** (`cutoff.replace(minute=0, second=0, microsecond=0)`)
so row deletion stays in lockstep with disk deletion: a row is
dropped iff the hour dir holding its file has already been
removed. Without alignment, rows inside the still-alive
most-recent hour would get dropped prematurely and their segments
would disappear from `list_segments` until the next recorder
restart's `_validate_segments` sweep.

Retention never touches the current or next wall-clock hour, so
active recorders can't have their working directory yanked out
from under them. With `retention_days >= 1` and a 60-minute loop,
we're only deleting hours that are days old — no conflict with
`_dir_precreate_loop` or `SegmentWatcher`.

## Download-range endpoint

`GET /api/cameras/{name}/download?start=...&end=...` returns a
single MP4 stitched from the segments overlapping the requested
range. The handler queries `list_segments_for_range`, writes a
concat list (one `file '/path/to/MM-SS.mp4'` per matching
segment) to a tempfile, and streams an ffmpeg subprocess's
stdout to the client via `StreamingResponse`. The UI has a
selection-bar widget that sets `start`/`end` on shift-drag.

The ffmpeg argv:

```
ffmpeg -hide_banner -loglevel error
  -f concat -safe 0 -i <concat_list>
  -ss <ss_offset> -t <trim_dur>
  -c:v copy
  -c:a aac
  -movflags +frag_keyframe+empty_moov+default_base_moof
  -f mp4 pipe:1
```

- **`-f concat -safe 0 -i <concat_list>`** — the concat demuxer
  reads a text file listing input files and presents them as one
  contiguous virtual stream. `-safe 0` allows absolute paths in
  the list; the default rejects them as a path-traversal
  mitigation, which we don't need since the paths come from our
  own segment index.
- **`-ss <ss_offset>` and `-t <trim_dur>`** — seek and duration,
  placed **after** `-i` so they operate on the concatenated
  virtual stream rather than on the first file only. `ss_offset`
  is computed as `start_dt − first_segment_start` so the trim is
  relative to the first segment's wall-clock start; `trim_dur` is
  the requested range clamped to available footage.
- **`-c:v copy`** — video passes through without re-encode.
  Inputs are already H.264 and the trim output is still H.264,
  so no pixels are decoded. Output-side `-ss` on `-c:v copy`
  starts from the nearest preceding keyframe and drops frames
  until the target, so the first up-to-GOP of video is cheap
  padding, not a re-encode.
- **`-c:a aac`** — audio **is** re-encoded, despite the inputs
  already being AAC. This is deliberate: with output-side `-ss`
  and `-c:a copy`, ffmpeg can only drop whole AAC frames
  (~23 ms each) until the trim target, which leaves the start
  audio up to a frame out of sync with the video. Decoding to
  PCM and re-encoding lets ffmpeg emit audio that starts exactly
  at `ss_offset` and stays tight. The re-encode is lossy but
  the output is still AAC in — it's a harmless single round-trip.
- **`-movflags +frag_keyframe+empty_moov+default_base_moof`** —
  output is **fragmented MP4**. This is load-bearing and
  contradicts the on-disk format constraint by design: ffmpeg is
  writing to `pipe:1` (stdout) and pipes aren't seekable, but
  non-fragmented MP4 needs to seek back to the front of the file
  after mdat is written to fill in the `moov` atom. Fragmented
  MP4 is the only MP4 shape that can be written straight through
  a pipe. `empty_moov` writes a minimal header up front,
  `frag_keyframe` starts a new fragment at each video keyframe,
  and `default_base_moof` makes each fragment's data offsets
  relative to its own `moof` (smaller and more widely supported).
  **Do not "fix" this to match the on-disk format** — the two
  formats serve different purposes (scrubbable storage vs
  streamable output) and both are correct for their role.
- **`-f mp4 pipe:1`** — force MP4 container and write to stdout.
  Without `-f mp4`, ffmpeg would guess the container from the
  output filename, and `pipe:1` has no extension to guess from.

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
at 1 minute in `recorder.py` — see the Recording section above. The
recorder, the frontend fallback, and the retention math all share
that assumption via `_SEGMENT_SECONDS` on the backend and
`_segmentDurationMs = 60000` on the frontend.

## Why not HLS?

TL;DR: HLS gives gapless playback as a byproduct of adaptive-bitrate
streaming machinery that's wasted on a local LAN single-bitrate
NVR, and both on-disk formats it supports broke something when
tried.

HLS needs either fragmented MP4 (`.m4s` + shared init segment) or
MPEG-TS segments. Both were tried. Both lost.

- **Fragmented MP4 would break byte-range access to self-contained
  segments.** Non-fragmented MP4 with `moov` at front lets any
  client byte-range directly into a keyframe with one
  `206 Partial Content`. Fragmented MP4 requires parsing the
  playlist, loading the init segment, and appending fragments in
  order before any frame appears. The non-fragmented shape is also
  what the download-range endpoint's ffmpeg concat demuxer consumes
  with `-c copy`, and what mp4box.js in the browser transmuxes to
  feed MSE. Switching the on-disk format would break all three
  read paths.
- **MPEG-TS + hls.js never worked in a 4-camera grid.** Tried on
  the deleted `hls` branch with ffmpeg `-segment_format mpegts`
  10-second segments and a FastAPI-synthesized `.m3u8` using
  `EXT-X-PROGRAM-DATE-TIME` and `EXT-X-DISCONTINUITY`. Three
  problems were observed and none were solved before the branch
  ran out of energy: (1) four cameras would not load and play
  reliably (different cameras came up run-to-run), (2) when they
  did all play they were never in sync across the grid — the
  shared-`wallTime`-drives-all-cameras model that works with
  plain `<video>` didn't map onto four independent hls.js
  instances each with its own internal buffer, (3) grid-wide
  scrubbing didn't work.

The adaptive-bitrate machinery HLS exists for — playlist
generation, init segments, discontinuity tags, hls.js as a ~1 MB
dependency — buys nothing on a single-bitrate LAN NVR. The only
thing it was going to earn us was gapless playback, and
MSE + mp4box.js got us that without changing the on-disk format.

A fragmented-MP4 HLS retry via Safari's native `<video
src="playlist.m3u8">` was planned on the delete `hls` branch but
never implemented. If scrub-latency
pain ever makes this worth revisiting, the plan is there — but
scope what's actually needed instead of picking it up whole.

## Rejected playback approaches

Read this before proposing anything in the segment-boundary /
gapless / prefetch area. Each approach here was built on a
specific theory; each failed for a specific reason.

### The pre-MSE baseline

Before MSE shipped, each camera used one `<video>` element with
`video.src = nextUrl` on `onended`. That produced a ~250ms flash
at every segment boundary on Safari 17+ / macOS — almost entirely
`loadstart → loadedmetadata` (Safari refetching the moov atom and
initial GOP via HTTP byte-range requests). Concurrent transitions
(two cameras sharing an ffmpeg rotation boundary) stayed at the
same ~250ms per camera; decoder re-init was rounding error. The
goal of everything below was eliminating that flash.

### The `!found`-skip-when-playing narrowing

Before the investigation started, `loadSegmentForCamera`'s
`!found` branch unconditionally cleared `video.src` whenever
`findSegmentAt` returned null at the current wall time. On a
transition, a non-primary camera with a sub-second rotation gap
would flash "Offline" on top of the underlying load cycle. The
narrowing: during live playback, `!found` within 2s past the
current segment's end returns silently instead of clearing state.
Still needed under MSE to keep ffmpeg rotation hitches from
bouncing cameras out of master eligibility. Do not remove.

### Rejected approaches

1. **Double-buffered `<video>` swap** (1a2619f2da → 77518f7b82).
   Theory: two `<video>` elements per panel, pending slot primed
   with next segment, opacity swap at `onended`. Primer sequence
   was `#t=0.001` URL fragment + `currentTime = 0.001` on
   `loadedmetadata` + muted play/pause cycle. Worked with a single
   camera. With four cameras (8 `<video>` elements), Safari's
   doorbell camera started rendering a cropped frame after a few
   scrubs, and a Web Inspector network capture showed the primer
   sequence producing a flurry of canceled byte-range requests.
   Either the hardware-decoder budget hit its limit around ~6
   simultaneous elements, or the primer sequence raced Safari's
   internal state machine — the canceled-requests evidence points
   at the race. **Reverted.** Do not reintroduce two `<video>`
   elements per panel.

2. **HTTP-cache prefetch via `fetch().blob()`** (f14e2b177d →
   da2c41a1f6). Theory: warm Safari's HTTP cache N seconds before
   a transition so the subsequent `video.src = nextUrl` reads from
   disk instead of network. **Safari's `<video>` does not read
   from the same cache `fetch()` writes into.** A HAR capture
   confirmed: the prefetch pulled the full 12 MB with a 200
   response, and side-by-side the `<video>` element then made
   dozens of fresh 64 KB `Range:` requests for the same file.
   Pure wasted bandwidth. **Reverted.**

3. **Blob-URL prefetch** (cf6d1b8c14 → da2c41a1f6). Theory:
   since Safari's `<video>` doesn't share the HTTP cache, bypass
   it. Hold the prefetched blob in JS memory, expose via
   `URL.createObjectURL(blob)`, and assign the blob URL at
   transition time. Solo transitions dropped to ~190ms. But in
   TinyNVR's actual workload two cameras share an ffmpeg rotation
   boundary and transition concurrently; under concurrent load
   Safari serialized blob-URL video ingestion and **both**
   cameras ballooned to ~600ms — almost 3× the solo case, and
   worse than the ~250ms HTTP baseline. Net effect: concurrent
   flash got worse. **Reverted.**

4. **Staggered loads** (b42e903a46 → afa7572382). Theory: delay
   each camera's load by `idx * 100ms` so concurrent transitions
   don't hit Safari's metadata parse simultaneously. Did not help.
   The fundamental issue — Safari re-fetching and re-parsing the
   moov atom on every `video.src` change — cannot be dodged by
   timing alone. **Reverted.**

5. **HLS (MPEG-TS + hls.js)** — see "Why not HLS?" above.

### Why MSE + mp4box.js shipped where blob-URL failed

The blob-URL approach (#3) failed because Safari serialized
blob-URL video ingestion across concurrent transitions. MSE with
`MediaSource` / `SourceBuffer` feeds bytes directly from JS to the
decoder without any `src` change; in Safari 17+ four independent
`MediaSource` instances run without the serialization penalty
that killed blob-URL. Segment boundaries become an
`appendBuffer()` call with no `emptied` / `loadstart` cycle at
all. Safari MSE contention was the big risk from the plan and
didn't materialize.

Approach #7 (`<link rel="preload" as="video">`) was considered and
not tried — MSE solved the gapless problem, making it moot.

Do not re-try any of #1–#5 without reading the failure details
above.

## Hybrid clock design

The raf-driven playback clock is the most subtle part of the
frontend. The invariant is **the `wallTime` shown in the UI must
match the pixels on screen** — "14:25:54" in the header has to
mean the frame you are looking at was recorded at 14:25:54. If
the clock runs ahead of the video (because a video element
stalled, decoded slowly, or is in a gap), the user sees a
misleading timestamp. Everything here is in service of that
invariant.

### Shipped design

1. `wallTime` is authoritative for UI, scrubber, and segment
   lookups.
2. Each raf tick picks a **dynamic clock master**: any camera
   whose `currentSegment` covers `wallTime` and whose `<video>` is
   ready (`readyState >= HAVE_FUTURE_DATA`, not paused/seeking,
   not `videoLoading`). Hysteresis prefers the previous tick's
   master so selection doesn't flap every frame.
3. If a master exists, `wallTime` is snapped to
   `master.anchorMs + master.video.currentTime * 1000` before
   anything else runs — this preserves "UI time matches pixels."
4. If no master qualifies, distinguish two sub-cases via the
   segment list:
   - **Known gap** (no camera has a segment covering `wallTime`):
     look up `_nextFootageAfter(wallMs)`; if more than 1s ahead,
     jump `wallTime` forward and set `_seekUntil = now + 500` so
     per-camera sync can load cleanly. Short gaps (< 1s) play
     through in real time so sub-second rotation hitches don't
     trigger a skip.
   - **Stall** (some camera has a segment covering `wallTime` but
     none is ready): freeze `wallTime` if any camera is
     `videoLoading`, otherwise free-run briefly.
5. Every camera — master included — runs hard-seek drift
   correction at a **500ms threshold**. The master self-corrects
   to a no-op because `wallTime` was just sourced from it.

This replaced an earlier static "camera 0 is always master"
design that broke in two ways: (1) scrub-into-gap snap-back — if
cam 0 had no footage at the target, the clock kept reading its
stale segment's `currentTime` and snapped `wallTime` backward;
(2) cam-0-offline killed playback — free-running `wallTime`
drifted past cameras 1–3 and their drift correction visibly
jerked back every few seconds for the entire outage. Dynamic
master selection fixes both.

### Do not re-litigate

- **Don't make camera 0 the master again.** The asymmetry was
  the bug.
- **Don't try PI-controlled `playbackRate` on Safari.** The
  expert recommendation for multi-stream sync is a PI controller
  on `video.playbackRate` (small trims like `playbackRate = 1.02`
  instead of visible seeks). Shaka Player and Chromecast
  multi-room do this. Tested via `static/rate_test.html`
  (reachable at `/rate_test.html` with `?debug=1`-style
  auto-report to `/api/debug/log`) against real segments on
  Safari 26.3.1 / WebKit 605.1.15. Effective rates were
  incoherent: didn't track targets, didn't scale linearly, and
  one test reported `currentTime` going **backwards** over 8
  seconds of wall time. The same harness on Chrome and Firefox
  honored every rate cleanly, so it's a WebKit limitation, not a
  harness bug. Safari is the only target browser, so the idea
  was abandoned. Keep `rate_test.html` around — if a future
  WebKit build changes this, re-run before proposing any control
  loop. Until then, the 500ms hard-seek is the drift mechanism.
- **Don't remove the `!found`-narrow-grace (2s past segEnd).** It
  keeps brief ffmpeg rotation hitches from clearing a camera's
  `currentSegment` and kicking it out of master eligibility —
  without it, every minute boundary would cause a handoff.
- **Don't use pure free-running `wallTime`.** A "wallTime is
  authoritative, videos drift-correct toward it" design was
  considered and rejected — the invariant breaks during any
  video stall.
- **Don't synthesize fake video for gaps on the backend.** The
  segment list is already ground truth; the clock tick skips
  through known gaps. Disk synthesis is needless complexity.

### `?test_gap` dev hack

The gap-skip path is hard to exercise in production — it requires
a simultaneous outage across *every* camera. The main UI accepts
`?debug=1&test_gap=14:30:00-14:35:00`, which makes
`_pickClockMaster`, `_anyCamHasFootageAt`, and `_nextFootageAfter`
pretend the selected day has no footage inside the local-time
window. Timeline rendering is unaffected; only the clock tick
sees the fake gap, so the panels flash "Offline", a `gap_skip`
event lands in `/api/debug/log`, and `wallTime` jumps to the
window end.

### Diagnostic infrastructure

`?debug=1` on any URL turns on frontend instrumentation that
posts events to `/api/debug/log` (newline-delimited JSON). Events
include every `<video>` lifecycle event, every
`loadSegmentForCamera` call, MSE `append-error`, a per-second
`drift` event with per-camera `drift_ms` / readyState / loading
state, and the `gap_skip` event from the clock tick.
`curl https://nvr.example.com/api/debug/log` to read,
`curl -X DELETE` to clear. Zero cost when `?debug=1` is not on
the URL.
