const TESTS = [1.0, 1.02, 1.05, 1.1, 0.98, 0.9];
const RUN_SECONDS = 8;
const EPSILON = 0.005; // effective rate must be within 0.5% of target to "honor"

const video = document.getElementById("v");
const statusEl = document.getElementById("status");
const resultsBody = document.querySelector("#results tbody");
const segEl = document.getElementById("seg");
const startBtn = document.getElementById("start");

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = cls || "";
}

async function postDebug(event) {
  try {
    await fetch("/api/debug/log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        events: [
          {
            wallMs: Date.now(),
            perfMs: Math.round(performance.now()),
            ...event,
          },
        ],
      }),
      keepalive: true,
    });
  } catch {}
}

function appendRow(target, effective, dMedia, dWall, honored) {
  const tr = document.createElement("tr");
  const fmt = (n) => (n == null ? "—" : n.toFixed(4));
  tr.innerHTML =
    `<td class="label">${target.toFixed(2)}×</td>` +
    `<td>${fmt(effective)}</td>` +
    `<td>${fmt(dMedia)}</td>` +
    `<td>${fmt(dWall)}</td>` +
    `<td class="${honored ? "ok" : "bad"}">${honored ? "yes" : "NO"}</td>`;
  resultsBody.appendChild(tr);
}

async function pickSegment() {
  const cams = await (await fetch("/api/cameras")).json();
  if (!cams.length) throw new Error("no cameras configured");
  const urlDate = new URLSearchParams(location.search).get("date");
  const candidateDates = [];
  if (urlDate) candidateDates.push(urlDate);
  const d = new Date();
  const today = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  if (!candidateDates.includes(today)) candidateDates.push(today);
  const yest = new Date(d.getTime() - 86400000);
  candidateDates.push(
    `${yest.getFullYear()}-${String(yest.getMonth() + 1).padStart(2, "0")}-${String(yest.getDate()).padStart(2, "0")}`,
  );

  for (const cam of cams) {
    for (const date of candidateDates) {
      const segs = await (
        await fetch(`/api/cameras/${encodeURIComponent(cam.name)}/segments?date_str=${date}`)
      ).json();
      const usable = segs.find((s) => s.duration_sec >= RUN_SECONDS + 2);
      if (usable) return { cam: cam.name, date, seg: usable };
    }
  }
  throw new Error("no usable segment found");
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function waitForEvent(target, eventName, timeoutMs, label) {
  return new Promise((resolve, reject) => {
    let done = false;
    const finish = (err) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      target.removeEventListener(eventName, onEvent);
      target.removeEventListener("error", onErr);
      if (err) reject(err);
      else resolve();
    };
    const onEvent = () => finish();
    const onErr = () =>
      finish(new Error(`${label || eventName}: error code ${target.error?.code}`));
    target.addEventListener(eventName, onEvent);
    target.addEventListener("error", onErr);
    const timer = setTimeout(
      () =>
        finish(
          new Error(
            `${label || eventName} timeout (readyState=${target.readyState}, networkState=${target.networkState})`,
          ),
        ),
      timeoutMs,
    );
  });
}

async function primeSegment(url) {
  // Attach src and explicitly kick off loading. Safari's preload=auto
  // is best-effort; video.load() is load-bearing on WebKit to actually
  // start the fetch for a freshly-assigned src. Called before the
  // user gesture so the video has readyState >= 1 by the time the
  // click handler calls play() synchronously.
  video.src = url;
  video.load();
  setStatus("Loading metadata…", "pending");
  await waitForEvent(video, "loadedmetadata", 15000, "loadedmetadata");
}

async function waitForLoadedData() {
  // After the user-gesture play() kicks buffering, wait for
  // readyState >= 2 so subsequent seeks resolve quickly.
  if (video.readyState < 2) {
    await waitForEvent(video, "loadeddata", 15000, "loadeddata");
  }
}

async function seekTo(t) {
  if (Math.abs(video.currentTime - t) < 0.2) return;
  video.currentTime = t;
  await waitForEvent(video, "seeked", 5000, `seek to ${t}`);
}

async function runRateTest(target, startTime) {
  // Video stays in the "playing" state across the whole test suite;
  // we only seek and change playbackRate between iterations. This
  // avoids calling play() after an await, which would be outside the
  // user-gesture context and rejected by WebKit.
  await seekTo(startTime);
  video.playbackRate = target;
  // Small settle so the playhead is actually at startTime and the
  // rate change has taken effect before we sample.
  await sleep(100);
  const startMedia = video.currentTime;
  const startWall = performance.now();
  await sleep(RUN_SECONDS * 1000);
  const dMedia = video.currentTime - startMedia;
  const dWall = (performance.now() - startWall) / 1000;
  const effective = dMedia / dWall;
  const honored = Math.abs(effective - target) < EPSILON;
  appendRow(target, effective, dMedia, dWall, honored);
  await postDebug({
    kind: "rate_test",
    target,
    effective,
    delta_media_s: Number(dMedia.toFixed(4)),
    delta_wall_s: Number(dWall.toFixed(4)),
    honored,
    ua: navigator.userAgent,
  });
  return { target, effective, honored };
}

let _segInfo = null;

async function prepare() {
  try {
    setStatus("Picking a segment…", "pending");
    const { cam, date, seg } = await pickSegment();
    _segInfo = { cam, date, seg };
    segEl.textContent = `${cam} / ${seg.filename} (${seg.duration_sec}s, ${date})`;
    await primeSegment(
      `/api/segments/${encodeURIComponent(cam)}/${encodeURIComponent(seg.filename)}`,
    );
    setStatus('Ready — click "Start test" to begin.', "ok");
    startBtn.disabled = false;
  } catch (e) {
    setStatus(`Prep failed: ${e.message}`, "bad");
    startBtn.disabled = true;
    await postDebug({ kind: "rate_test_error", stage: "prepare", message: e.message });
  }
}

async function runAll() {
  try {
    const { seg } = _segInfo;
    await waitForLoadedData();
    // Interleave tests across the segment so each one has fresh buffer
    // ahead of it instead of re-measuring the same 8s window.
    let startTime = 1.0;
    for (const target of TESTS) {
      setStatus(`Running ${target.toFixed(2)}× from ${startTime.toFixed(1)}s …`, "pending");
      await runRateTest(target, startTime);
      startTime += RUN_SECONDS;
      if (startTime + RUN_SECONDS + 1 > seg.duration_sec) startTime = 1.0;
    }
    const all = Array.from(resultsBody.querySelectorAll("tr"));
    const honored = all.filter((tr) => tr.lastElementChild.textContent === "yes").length;
    const verdict =
      honored === TESTS.length
        ? "WebKit honors all tested rates — PI controller path is viable."
        : honored === 1 && TESTS[0] === 1.0
          ? "WebKit only honored 1.0 — sub-unity rate trimming is a no-op on this build. Use hard-seek correction."
          : `WebKit honored ${honored}/${TESTS.length} rates — partial support. Check table for which.`;
    setStatus(verdict, honored === TESTS.length ? "ok" : "bad");
    await postDebug({
      kind: "rate_test_verdict",
      honored,
      total: TESTS.length,
      ua: navigator.userAgent,
    });
  } catch (e) {
    setStatus(`Failed: ${e.message}`, "bad");
    await postDebug({ kind: "rate_test_error", stage: "run", message: e.message });
  }
}

startBtn.disabled = true;
prepare();

startBtn.addEventListener(
  "click",
  () => {
    startBtn.disabled = true;
    startBtn.style.display = "none";
    // Kick play() synchronously inside the user-gesture handler so
    // WebKit accepts it. The returned promise is awaited asynchronously
    // inside runAll(); the important part is that play() is called in
    // this exact tick, not after an await.
    const playPromise = video.play();
    playPromise.catch(async (e) => {
      setStatus(`play() rejected: ${e.message}`, "bad");
      await postDebug({ kind: "rate_test_error", stage: "user_gesture_play", message: e.message });
    });
    runAll();
  },
  { once: true },
);
