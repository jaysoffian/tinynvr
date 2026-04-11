# TinyNVR Design

Small, self-hosted NVR for recording RTSP cameras and playing them
back synchronously in a web UI. Optimized for a single household on
a local NAS, not for multi-tenant or cloud deployment.

## Scope

- 4–8 RTSP cameras, already served by a local go2rtc (or directly).
- Recording to local storage, never transcoding video.
- Web UI for synced multi-camera playback with scrubbing, ~N-day
  retention, and a download-range button.
- Target browser: Safari on macOS. It's the user's daily browser
  and the one that has to feel right. Chrome and Firefox aren't
  tested and aren't targeted — anything that works on Safari and
  also happens to work on them is bonus, not requirement.

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
  -c copy \
  -f segment -segment_time 60 -reset_timestamps 1 \
  -segment_format mp4 -segment_format_options movflags=+faststart \
  -segment_atclocktime 1 -strftime 1 \
  {camera}/%Y-%m-%d_%H-%M-%S.mp4
```

- **Nothing is ever transcoded** (`-c copy`). go2rtc is the upstream
  and is expected to normalize each camera to H.264 + AAC before
  TinyNVR sees it (the user does this via the `?mp4` stream variant
  in go2rtc's stream URLs). Recording is pure stream copy — CPU cost
  is effectively zero. If you're pointing TinyNVR at an RTSP source
  that emits `pcm_mulaw` or another non-MP4 audio codec directly,
  segments will fail to mux; fix the upstream, don't add an encode
  step here.
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

### Frontend: single `<video>` per panel

Each camera panel renders **one `<video>` element**. When the
segment ends, `onended` fires, `_onSegmentEnd` advances `wallTime`
to the next segment's start, and `loadSegmentForCamera` sets
`video.src = nextUrl`, which triggers `loadedmetadata` and
playback resumes. There's a ~100–300ms gap at each segment
boundary while the browser fetches, parses, and decodes the new
file. For a 1-minute segment length that's a minor hitch once per
minute, which is acceptable.

During that gap, `loadSegmentForCamera`'s `!found` branch used to
unconditionally clear the `<video>` element if `findSegmentAt`
returned null at the current wallTime — which happened for any
camera whose segments had a sub-second rotation drift or a
failed/missing segment at the transition moment. That produced a
brief "Offline" overlay flash. Now, during live playback (`this.playing
=== true`), the `!found` branch returns silently: the last frame
stays visible and the free-running clock advances wallTime out of
the gap, so the next tick finds a real segment and resumes
playback without ever clearing the video element.

See the **"Gapless playback investigation"** section below for the
full list of approaches we tried (double-buffering, HTTP-cache
prefetch, blob-URL prefetch, `?debug=1` instrumentation, HAR
captures), why each failed, and the current state of knowledge. If
you're picking up this problem, start there — all the false starts
are captured so you don't have to rediscover them. The "Why not
HLS?" section farther down covers the one approach that was tried
all the way to a deployable branch and backed out.

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

### The goal was gapless — we accepted a small hitch instead

The actual goal of the HLS detour was *gapless playback across
segment boundaries*. We didn't find a way to achieve that in
Safari without breaking something else. A double-buffered
`<video>` approach (two elements per panel, opacity swap, pending
slot preloaded) worked for single-camera apps but misbehaved in
a 4-camera grid — see the "Frontend: single `<video>` per panel"
section above for details. So the shipped design accepts a
~100–300ms load hitch at segment boundaries and keeps the
recorder config and storage format untouched. Byte-range scrubbing
still works unchanged (Safari seeks directly into any segment via
HTTP `Range:`), which was the original bug that motivated all
this work anyway.

### What would bring HLS back

Honest case: if TinyNVR ever needs multi-bitrate playback (phones
on cellular watching the same footage the desktop is watching at
full quality), or if it ever needs to serve footage across a WAN
where the single-bitrate assumption breaks, HLS starts earning its
keep. Neither of those is in scope.

---

## Gapless playback investigation

> If you're picking up the "segment-boundary flash is annoying"
> thread, read this whole section before trying anything. Multiple
> approaches have been tried, each built on a specific theory of the
> bug, and each failed for a specific reason. The failure modes are
> captured here so you don't have to rediscover them.

### The shipped baseline

One `<video>` element per camera panel. When `video.onended` fires,
`_onSegmentEnd` advances `wallTime` to the next segment's start,
`loadSegmentForCamera` sets `video.src = nextUrl`, and the
`<video>` element runs its full load cycle: `emptied` →
`loadstart` → `loadedmetadata` → `loadeddata` → `canplay` →
`playing`. Between `emptied` and `loadeddata` the element shows a
black frame. That's the flash. Measured duration on Safari 17+/
macOS against the on-LAN NAS:

- **~250–280ms for a concurrent transition** (family-room and
  kitchen share an ffmpeg rotation boundary and both `ended` at
  the same wall instant). Breakdown from a real debug trace:
  - `ended → loadstart`: ~11ms (JS + event loop)
  - `loadstart → loadedmetadata`: ~254ms (Safari fetches the moov
    atom and initial GOP via HTTP byte-range requests)
  - `loadedmetadata → loadeddata`: ~14ms (decoder init + first
    GOP decode — essentially free)
- **~200ms for a solo transition** (cam 3 transitioning alone).

The 254ms `loadstart → loadedmetadata` window is the entire flash.
Decoder re-init is rounding error.

### The `!found`-skip-when-playing fix

Before the investigation started, the flash looked worse than it
should have because `loadSegmentForCamera`'s `!found` branch
unconditionally cleared `video.src` whenever `findSegmentAt`
returned null at the current wall time. On a transition, if a
non-primary camera's segments had a sub-second rotation gap at
the transition moment, its `!found` branch fired, the video
element was emptied (black frame + "Offline" overlay), then a
fraction of a second later the free-running clock advanced
`wallTime` past the gap and the next tick reloaded. The result was
a visible "Offline" flash on top of the underlying `<video>` load
cycle.

Fix: during live playback (`this.playing === true`), the `!found`
branch returns silently instead of clearing state. The last frame
stays visible, the clock advances through the gap, and the next
tick finds a real segment. The overlay "Loading…" text is also
scoped to cases where the camera has no segments for the entire
day (real offline) vs. a transient gap (still loading).

This is orthogonal to the prefetch story and should stay even if
any future prefetch experiment is tried or reverted.

### Failed approach #1: HLS (MPEG-TS + hls.js)

Tried on a deleted branch called `hls`, across many hours of
live iteration on the four-camera deployment. **The tried
design did not work.**

**What was actually implemented**. The recorder switched from Matroska to
**MPEG-TS** via ffmpeg's segment muxer, *not* the HLS muxer:

```
-c copy \
-f segment -segment_format mpegts -segment_time 10 \
-strftime 1 {camera}/%Y-%m-%d/%Y-%m-%d_%H-%M-%S.ts
```

Segments were flat-ish `.ts` files (10 seconds each, in per-day
UTC subdirs). On the backend, a FastAPI endpoint synthesized an
HLS `.m3u8` playlist on demand from the daily `.idx` duration
files, emitting `EXT-X-PROGRAM-DATE-TIME` per segment and
`EXT-X-DISCONTINUITY` between non-contiguous ones, with
`EXT-X-PLAYLIST-TYPE:EVENT` on in-progress ranges so hls.js
wouldn't pin to the live edge. Segments were served raw via
`FileResponse`. Frontend used **hls.js** (not Safari's native
HLS player) to consume the playlist.

**This is classic HLS over MPEG-TS, not fragmented MP4.** The
two are completely different on-disk formats. MPEG-TS (`.ts`)
is a transport stream with PAT/PMT/PES packets, self-contained
per segment. Fragmented MP4 (`.m4s` + separate `init.mp4`) uses
`moof`/`mdat` box structure and references a shared init
segment via `EXT-X-MAP`. They require different ffmpeg muxers
(`-f segment -segment_format mpegts` vs `-f hls
-hls_segment_type fmp4`) and different playlist semantics.

Problems observed during testing (per the user, after hours of
live iteration):

- **Could not get all four streams to load and play reliably.**
  Some cameras would come up, others wouldn't, and which ones
  varied run to run.
- **When they did all play, they were never in sync** across
  the 2×2 grid. The shared-wallTime-drives-all-cameras model
  that works cleanly with plain `<video src=*.mp4>` didn't map
  naturally onto four independent hls.js instances, each with
  its own internal buffer and `currentTime`.
- **Scrubbing didn't work.** Tagging in-flight playlists with
  `EXT-X-PLAYLIST-TYPE:EVENT` got backward seeks into a
  partially-working state, but grid-wide scrubbing never
  became reliable.

Which of these are inherent to HLS-via-hls.js vs fixable with
more iteration is unclear. The branch ran out of energy before
any of them was definitively solved or definitively shown to
be unsolvable.

**Fragmented MP4 HLS was planned but never implemented.** After
the MPEG-TS iteration stalled with "hls
plan.md" wrote a 1006-line plan to pivot away from hls.js and
onto **Safari's native `<video src="playlist.m3u8">` player**
with `-hls_segment_type fmp4`. That plan introduced supporting
machinery appropriate for fMP4 HLS: one `init.mp4` per ffmpeg
run, a run-to-init association rule (each segment's owning
init is the newest historical init ≤ its timestamp), retention
that kept historical inits alive until all their segments were
deleted, startup reconciliation with a bounded unlink cap, and
Python-side init file rotation because `-strftime 1` doesn't
expand in the init filename parameter. None of this was coded
— the recorder.py at the tip of `hls` still outputs
MPEG-TS via the segment muxer. The branch was simply
abandoned with the plan sitting there.

**What this means for future attempts**:

- **MPEG-TS + hls.js failed on a known set of problems** (4-
  stream load reliability, cross-stream sync under shared
  wallTime, scrub reliability). A future attempt has to solve
  those three problems concretely, not assume they're
  superficial.
- **fMP4 HLS via Safari's native player is untested**. It
  might work where MPEG-TS + hls.js didn't — Safari's native
  HLS implementation is more mature than hls.js, and
  native-player sync could conceivably be better than
  independent hls.js instances. It also might not, and the
  init-file / retention / reconciliation complexity is real.
  Worth considering if the flash becomes intolerable, *not*
  worth jumping into without scoping how much of the 1000-
  line plan is actually necessary vs. written under pressure.

### Failed approach #2: Double-buffered `<video>` swap (1a2619f2da → 77518f7b82)

**Theory.** The flash is caused by `video.src = nextUrl` triggering
a full load cycle, which involves both network fetch and decoder
re-init. If we render two `<video>` elements per panel, keep one
"pending" element primed with the next segment, and opacity-swap
which element is visible at `onended`, there's no `src` change on
the visible element — it was already loaded and ready to play.

**Implementation.** Per-camera `activeSlot` of `'a'` or `'b'`
drove a CSS `.active` class that toggled `opacity: 1` / `0`
between two stacked, absolutely-positioned video elements.
`_preloadPending()` assigned the next segment's URL to the
pending slot with three redundant primers to work around Safari's
hidden-element preload hedging:

1. Append `#t=0.001` to the URL (Media Fragments URI spec initial
   seek).
2. `currentTime = 0.001` on `loadedmetadata`.
3. Brief muted `play()` / `pause()` cycle to force first-GOP fetch
   and decode.

**Why it failed.** Worked cleanly with a single camera. With four
cameras (8 total `<video>` elements), two things went wrong in
Safari:

- The doorbell camera started rendering a cropped frame after a
  few scrubs. Initially loaded correctly, then post-scrub the
  video element would display a zoomed-in portion of the frame.
  No error, no CSS change — the `<video>` element's internal
  rendering pipeline got stuck in a bad state.
- The other three cameras "struggled" in ways the user described
  as the flash still happening and some feeds not loading.
- A Web Inspector Network panel capture showed the pending-slot
  primer sequence producing a flurry of canceled and errored
  byte-range requests (25–116-byte error bodies in red) for each
  transition. Safari was starting a request, having it canceled
  by the next primer step, starting another, canceling again.

**Diagnosis (tentative).** Either Safari's hardware-H.264 decoder
budget capped out around ~6 simultaneous elements and the
doorbell's decoder state got evicted/corrupted, *or* the primer
sequence (`src` → `load` → `currentTime` → `play` → `pause`) hit
a race in Safari's internal state machine that it doesn't handle
well. The network evidence points more strongly at the primer
race than at the decoder budget — decoder exhaustion shows up as
frozen frames, not canceled network requests. Unclear which
dominates.

**Outcome.** Reverted. Single `<video>` per camera. The flash came
back but the doorbell stayed un-cropped.

### Failed approach #3: HTTP cache prefetch via `fetch().blob()` (f14e2b177d → da2c41a1f6)

**Theory.** Suggested by a second-opinion research agent as a
fallback to double-buffering. The single `<video>` element stays,
but ~N seconds before a transition we issue a plain `fetch()` for
the next segment and drain the response via `.blob()` to force
the browser to store the full response in its HTTP cache. The
subsequent `video.src = nextUrl` should then read the bytes from
disk cache instead of the network, cutting the `loadstart →
loadedmetadata` window from ~250ms to ~30ms.

**Implementation.** `_prefetchNextSegment(idx)` called from two
places in `loadSegmentForCamera`: inside `onloadedmetadata` after
a fresh load, and in the "already loaded" reseek branch. The
backend `/api/segments/...` endpoint already sent
`Cache-Control: public, max-age=31536000, immutable` so the
cached response was reused indefinitely.

**Why it failed.** Safari's `<video>` element does **not** read
from the same HTTP cache that `fetch()` writes into. Confirmed
via a HAR capture:

```
GET .../family-room/21-32-00.mp4  200  12344235 bytes  1401ms  (fetch prefetch)
GET .../family-room/21-31-00.mp4  206  65581    bytes    77ms  (<video> range request)
GET .../family-room/21-31-00.mp4  206  65581    bytes    91ms  (<video> range request)
GET .../family-room/21-31-00.mp4  206  65581    bytes    75ms  (<video> range request)
...
```

The prefetch row pulls the full 12 MB with a 200 response. Side
by side, Safari's `<video>` element makes dozens of 64 KB
`Range: bytes=X-Y` requests to the network fresh for the exact
same filenames. Safari apparently maintains a separate media-
resource cache for `<video>`/`<audio>` that doesn't share with
the general HTTP cache. Historical behavior confirmed by a later
debug-log capture: the `loadstart → loadedmetadata` window in
playback logs stayed at ~250ms (the fresh network fetch time)
even when the matching file had been fully prefetched 18 seconds
earlier.

**Outcome.** Pure wasted bandwidth — every segment was paying
~double the network cost for zero playback benefit. Removed in
the same revert as approach #4.

### Failed approach #4: Blob-URL prefetch (cf6d1b8c14 → da2c41a1f6)

**Theory.** If Safari's HTTP cache isn't shared with `<video>`,
bypass the cache entirely. Keep the `fetch()` prefetch, but
instead of draining the blob and hoping Safari uses the cache,
hold the blob in JS memory and expose it via
`URL.createObjectURL(blob)`. At transition time, assign the
`blob:` URL to `video.src`. The `<video>` element reads bytes
from memory, so the network-fetch phase of the load cycle is
eliminated — only decoder re-init remains.

**Implementation.** Per-camera state tracked three fields:

- `state.prefetchedFilename` — which segment the current blob holds
- `state.prefetchedBlobUrl` — `URL.createObjectURL(blob)` for it
- `state.playingBlobUrl` — the blob URL currently bound to
  `video.src`, tracked so it can be `revokeObjectURL`-ed when
  replaced

`loadSegmentForCamera` checked `state.prefetchedFilename ===
seg.filename` and used the blob URL if it matched, otherwise
fell through to the HTTP URL. Debug instrumentation logged
`{kind: "load", ev: "use-blob"}` vs `"use-http"` on every load
call so we could verify the hot path was being taken.

**What the debug log showed.** The blob path *was* being taken
for every transition after the first. Solo transition was
measurably faster:

| cam | concurrency | loadstart → loadedmetadata | total flash |
|-----|-------------|----------------------------|-------------|
|  3  | solo        | 188ms                      | 210ms       |
|  1  | solo        | 187ms                      | 380ms       |
|  0  | concurrent with cam 2 | **596ms**        | **613ms**   |
|  2  | concurrent with cam 0 | **600ms**        | **624ms**   |

**Why it failed.** Solo blob ~190ms *is* faster than solo HTTP
~250ms. But in TinyNVR's usage pattern, two cameras (family-room
and kitchen) share an ffmpeg rotation boundary and transition at
the same real-time instant. When two video elements concurrently
ingest blob URLs, Safari's metadata parse balloons to ~600ms on
**both** cameras — roughly 3× the solo case. HTTP URLs under the
same concurrent load stayed ~250ms per camera. Safari seems to
serialize blob-URL video ingestion in a way it doesn't serialize
HTTP fetches; the decoder or source-buffer setup fights over some
shared resource and both lose.

The net effect on the actual workload: **the user's concurrent
flash got worse, not better** (600ms vs 250ms baseline). The user
reported "no improvement whatsoever" after testing.

Also discovered in the same log: `_prefetchNextSegment` could
fire twice for the same segment. The dedup check required both
`prefetchedFilename` match *and* `prefetchedBlobUrl` to be set,
but while a fetch was in flight only the filename was set. A
clock-tick-driven re-entry during the fetch passed the dedup
test (null check) and started a second fetch. Small memory leak
per duplicate blob, doubled prefetch bandwidth. Latent bug, but
not the primary cause of the flash.

**Outcome.** Reverted. `da2c41a1f6` restores plain
`video.src = httpUrl` with no prefetch.

### Still on the table, not yet tried

**Approach #5: Staggered blob-URL loads.** The concurrent
penalty might be dodged by delaying the second camera's load by
~100–200ms so they never hit the metadata-parse path
simultaneously. Each camera would flash for ~190ms (solo speed)
but they'd flash at slightly different times. I genuinely don't
know whether the user's eye would find 4 sequential 190ms
flashes less annoying than 2 simultaneous 600ms flashes — this
is a "just try it" experiment. Minimal code change: wrap each
`loadSegmentForCamera(idx)` call in `loadSegmentsForAll` with
`setTimeout(..., idx * 100)`.

**Approach #6: MSE via a JS-side transmuxer (`mux.js`).** Media
Source Extensions let JavaScript feed media bytes to a
`<video>` element via a `SourceBuffer` without a `src` change
— the browser decodes appended chunks as one continuous stream,
so segment boundaries become a `SourceBuffer.appendBuffer()`
call with no `emptied` / `loadstart` cycle. **No flash at all**
if it works.

The catch: MSE requires **fragmented MP4**, which is the one
thing we can't change about the storage format (see "Why not
HLS?"). The only way to keep the storage format as-is *and* use
MSE is a JS-side transmuxer like [mux.js](https://github.com/videojs/mux.js)
(maintained by the Video.js team, ~80 KB minified) that converts
non-fragmented MP4 to fragmented MP4 in the browser. Fetch
segment bytes → run through mux.js → append to SourceBuffer →
play seamlessly.

Risks:

1. **Same contention problem**: Safari *might* serialize
   `SourceBuffer.appendBuffer()` operations across multiple
   elements the same way it serializes blob-URL ingestion.
   Unknown until tested. If it does, MSE buys us nothing over
   blob URLs.
2. **Scrubbing compatibility**: the current setup uses native
   `<video>` byte-range seeking. With MSE, the SourceBuffer is
   the source of truth and JS has to manually append the right
   bytes for a scrub target. Reimplementing scrub correctness
   against MSE is not free.
3. **Dependency and complexity**: ~80 KB of transmuxing JS,
   plus the SourceBuffer state machine, plus replacing the
   simple `video.src = url` pattern with a managed MSE pipeline
   for all four cameras.

MSE is the only remaining plausible path to *actual* zero-flash
gapless playback in Safari without touching the recorder. It's
also substantially more work than anything tried so far.

**Approach #7: `<link rel="preload" as="video">`**. The spec-
defined preload hint for `<video>`. Might populate a cache that
Safari's `<video>` element actually consults (unlike `fetch()`
blobs). Cheap to try, low confidence of working — Safari's
handling of preload hints is historically inconsistent.

### Diagnostic infrastructure

`/api/debug/log` endpoints + `?debug=1` frontend instrumentation
still live in the code for the next person to dig into this.
Load the UI with `?debug=1`, exercise the flash, then:

```bash
curl https://nvr.example.com/api/debug/log > /tmp/tinynvr-debug.log
curl -X DELETE https://nvr.example.com/api/debug/log   # clear
```

The log is newline-delimited JSON with `{perfMs, wallMs, cam,
kind, ev, file, ...}` per event. Instrumented events: every
`<video>` element lifecycle event (`emptied`, `loadstart`,
`loadedmetadata`, `loadeddata`, `canplay`, `canplaythrough`,
`playing`, `waiting`, `stalled`, `seeking`, `seeked`, `error`,
`ended`) plus every `loadSegmentForCamera` call (`kind: "load"`,
`ev: "call"`). Zero cost when `?debug=1` is not on the URL.

The first useful measurement is usually: for each transition,
compute `playing_perfMs - loadstart_perfMs` per camera and look
at the distribution across solo vs concurrent transitions. That's
what told us the flash was network-dominated in HTTP mode and
decoder-contention-dominated in blob-URL mode.

### Current assessment

The 250ms concurrent flash appears to be the Safari floor for
non-fragmented MP4 playback without rewriting the playback layer.
Everything below that requires either (a) changing the storage
format (rejected because it breaks scrubbing), (b) running bytes
through a JS transmuxer to synthesize fragmented MP4 for MSE
(approach #6, substantial work, unknown Safari behavior under
concurrency), or (c) fighting Safari's quirks in ways that have
so far made things worse.

If you're picking this up and the user is still annoyed by the
flash, the honest prioritization is:

1. **Try approach #5 (staggered loads).** 10 lines of code. Low
   risk. Might feel better, might feel worse.
2. **Try approach #7 (`<link rel="preload" as="video">`).** Also
   10 lines. Low confidence but cheap.
3. **Try approach #6 (MSE + mux.js).** Substantial work, real
   chance of a genuine fix, real chance of hitting the same
   Safari concurrency ceiling. Decide whether the user cares
   enough before committing to this.

Do not re-try double-buffering, HLS, HTTP-cache prefetch, or
blob-URL prefetch without understanding why each failed above.
