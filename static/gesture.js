/* Move the drone with one hand: the laptop camera sees which finger is held out. */
const Gesture = (() => {

  let shown = false;
  let live = false;
  let running = false;
  let starting = false;
  let generation = 0;
  let stream = null;
  let landmarker = null;
  let loading = null;
  let prev = { right: 0, forward: 0, text: "" };
  let lastSent = { right: 0, forward: 0 };
  let lastSentAt = 0;
  let lastDetect = 0;
  let stamp = 0;

  const video = () => document.getElementById("gesture-video");
  const statusEl = () => document.getElementById("gesture-status");
  const camButton = () => document.getElementById("gesture-cam");

  // Which single finger is held out picks the way:
  //   index = forward, thumb = right, middle = left, pinky = back. A fist, an open
  //   hand or two fingers at once = stay still. Ring finger is ignored (it lifts with its neighbours).
  const HOLD = 3;  // the same reading this many times in a row (about 0.3 s) before it counts
  const WAYS = {
    index: { right: 0, forward: 1, text: "人差し指：前" },
    thumb: { right: 1, forward: 0, text: "親指：右" },
    middle: { right: -1, forward: 0, text: "中指：左" },
    pinky: { right: 0, forward: -1, text: "小指：後ろ" },
  };
  let streak = { key: null, count: 0 };

  function dist(a, b) {
    return Math.hypot(a.x - b.x, a.y - b.y);
  }

  function fingersOf(lm) {
    if (!lm || lm.length < 21) return null;
    const wrist = lm[0];
    const up = (tip, pip) => dist(lm[tip], wrist) > dist(lm[pip], wrist) * 1.12;
    // The thumb is out when its tip is well away from the base of the little finger.
    const thumb = dist(lm[4], lm[17]) > dist(lm[2], lm[17]) * 1.25 && dist(lm[4], lm[5]) > dist(lm[0], lm[5]) * 0.45;
    return { thumb, index: up(8, 6), middle: up(12, 10), pinky: up(20, 18) };
  }

  function classify(fingers) {
    if (!fingers) {
      streak = { key: null, count: 0 };
      prev = { right: 0, forward: 0, text: "" };
      return { right: 0, forward: 0, seen: false, ready: true, text: "" };
    }
    const out = Object.keys(WAYS).filter((k) => fingers[k]);
    const key = out.length === 1 ? out[0] : "stop";
    streak = streak.key === key ? { key, count: streak.count + 1 } : { key, count: 1 };
    if (streak.count >= HOLD) {
      const way = WAYS[key];
      prev = way ? { right: way.right, forward: way.forward, text: way.text }
        : { right: 0, forward: 0, text: out.length ? "指が2本以上：止まる" : "グー：止まる" };
    }
    return { ...prev, seen: true, ready: true };
  }

  function showStatus(lean) {
    const el = statusEl();
    if (!el) return;
    let text;
    if (!lean.seen) text = "片手をカメラに映してください（人差し指=前・親指=右・中指=左・小指=後ろ・グー=止まる）";
    else text = lean.text || "指を1本だけ出してください";
    if (live && lean.seen) {
      text += lean.right || lean.forward ? "。この方向へ一歩ずつ動きます" : "";
    } else if (!live && lean.seen && (lean.right || lean.forward)) {
      text += "。離陸すると、この方向へ動きます";
    }
    el.textContent = text;
  }

  async function send(lean) {
    if (!live || typeof post !== "function") return;
    const now = performance.now();
    const changed = lean.right !== lastSent.right || lean.forward !== lastSent.forward;
    if (!changed && now - lastSentAt < 250) return;
    lastSent = { right: lean.right, forward: lean.forward };
    lastSentAt = now;
    try { await post("/api/gesture", { right: lean.right, forward: lean.forward }); } catch (e) { /* next tick retries */ }
  }

  function nowStamp() {
    const t = performance.now();
    stamp = t > stamp ? t : stamp + 1;
    return stamp;
  }

  function tick() {
    if (!running) return;
    const cam = video();
    const t = performance.now();
    if (landmarker && cam && cam.readyState >= 2 && t - lastDetect >= 100) {
      lastDetect = t;
      let landmarks = null;
      try {
        const found = landmarker.detectForVideo(cam, nowStamp());
        landmarks = found.landmarks && found.landmarks[0];
      } catch (e) { /* a dropped frame is just a miss */ }
      const lean = classify(fingersOf(landmarks));
      showStatus(lean);
      send(lean);
    }
    if (running && cam && cam.requestVideoFrameCallback) cam.requestVideoFrameCallback(tick);
    else if (running) setTimeout(tick, 120);
  }

  async function ensureModel() {
    if (landmarker) return;
    if (!loading) {
      loading = (async () => {
        const vision = await import("/static/vendor/pose/vision_bundle.mjs");
        const files = await vision.FilesetResolver.forVisionTasks("/static/vendor/pose/wasm");
        landmarker = await vision.HandLandmarker.createFromOptions(files, {
          baseOptions: {
            modelAssetPath: "/static/vendor/pose/hand_landmarker.task",
            delegate: "CPU",
          },
          runningMode: "VIDEO",
          numHands: 1,
        });
      })();
    }
    try {
      await loading;
    } catch (e) {
      loading = null;
      throw e;
    }
  }

  function stop() {
    generation++;
    running = false;
    starting = false;
    stream && stream.getTracks().forEach((track) => track.stop());
    stream = null;
    const cam = video();
    if (cam) cam.srcObject = null;
    const view = document.getElementById("gesture-view");
    if (view) view.hidden = true;
    const button = camButton();
    if (button) {
      button.disabled = false;
      button.textContent = "カメラをオン";
    }
    streak = { key: null, count: 0 };
    prev = { right: 0, forward: 0, text: "" };
    if (live && (lastSent.right || lastSent.forward) && typeof post === "function") {
      lastSent = { right: 0, forward: 0 };
      post("/api/gesture", { right: 0, forward: 0 });
    }
  }

  async function start() {
    if (running || starting) return;
    const mine = ++generation;
    starting = true;
    const button = camButton();
    const view = document.getElementById("gesture-view");
    const el = statusEl();
    if (button) button.disabled = true;
    if (view) view.hidden = false;
    if (el) el.textContent = "カメラと手の認識を準備しています…";
    try {
      await ensureModel();
      stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } },
      });
    } catch (e) {
      starting = false;
      if (button) button.disabled = false;
      if (view) view.hidden = true;
      if (el) {
        el.textContent = e && e.name === "NotAllowedError"
          ? "カメラが許可されていません。ブラウザの許可を確認してください"
          : (landmarker ? "カメラを使えませんでした" : "手の認識を読み込めませんでした");
      }
      stream && stream.getTracks().forEach((track) => track.stop());
      stream = null;
      return;
    }
    if (mine !== generation || !shown) {
      stream.getTracks().forEach((track) => track.stop());
      stream = null;
      starting = false;
      return;
    }
    const cam = video();
    cam.srcObject = stream;
    const played = cam.play();
    if (played && played.catch) played.catch(() => {});
    streak = { key: null, count: 0 };
    prev = { right: 0, forward: 0, text: "" };
    lastDetect = 0;
    running = true;
    starting = false;
    if (button) {
      button.disabled = false;
      button.textContent = "カメラをオフ";
    }
    if (el) el.textContent = "片手をカメラに映してください";
    if (cam.requestVideoFrameCallback) cam.requestVideoFrameCallback(tick);
    else setTimeout(tick, 120);
  }

  function setVisible(show, isLive) {
    shown = !!show;
    live = !!isLive && shown;
    const card = document.getElementById("gesture-card");
    if (!card) return;
    card.hidden = !shown;
    if (!shown) stop();
  }

  const button = camButton();
  if (button) {
    button.addEventListener("click", () => (running || starting ? stop() : start()));
  }
  window.addEventListener("pagehide", stop);

  return { setVisible, stop };
})();
window.Gesture = Gesture;
