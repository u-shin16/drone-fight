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

  // How many fingers are up picks the way (count them like 1, 2, 3, 4, 5):
  //   1 = forward, 2 = back, 3 = right, 4 = left, 5 (open palm) = stop. A fist (0) = stop too.
  const HOLD = 3;  // the same count this many times in a row (about 0.3 s) before it counts
  const WAYS = {
    1: { right: 0, forward: 1, text: "1本：前" },
    2: { right: 0, forward: -1, text: "2本：後ろ" },
    3: { right: 1, forward: 0, text: "3本：右" },
    4: { right: -1, forward: 0, text: "4本：左" },
    5: { right: 0, forward: 0, text: "5本（パー）：止まる" },
    0: { right: 0, forward: 0, text: "グー：止まる" },
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
    return { thumb, index: up(8, 6), middle: up(12, 10), ring: up(16, 14), pinky: up(20, 18) };
  }

  function classify(fingers) {
    if (!fingers) {
      streak = { key: null, count: 0 };
      prev = { right: 0, forward: 0, text: "" };
      return { right: 0, forward: 0, seen: false, ready: true, text: "" };
    }
    const key = ["thumb", "index", "middle", "ring", "pinky"].filter((k) => fingers[k]).length;
    streak = streak.key === key ? { key, count: streak.count + 1 } : { key, count: 1 };
    // Stopping (fist or open palm) works at once; a move must be held for a moment.
    if (key === 0 || key === 5 || streak.count >= HOLD) {
      const way = WAYS[key];
      prev = { right: way.right, forward: way.forward, text: way.text };
    }
    return { ...prev, seen: true, ready: true };
  }

  // ---- face mode: eyes shut = stop, mouth open = forward, smile = back, head turned = right/left ----
  let mode = "hand";        // "hand" or "face"
  let faceBase = null;      // the player's own straight face, taken first
  let faceSamples = [];
  const FACE_WAYS = {
    shut: { right: 0, forward: 0, text: "目を閉じている：止まる" },
    open: { right: 0, forward: 1, text: "口を開けている：前" },
    smile: { right: 0, forward: -1, text: "笑っている：後ろ" },
    right: { right: 1, forward: 0, text: "顔が右：右" },
    left: { right: -1, forward: 0, text: "顔が左：左" },
    still: { right: 0, forward: 0, text: "真顔：止まる" },
  };

  function faceOf(found) {
    const lm = found.faceLandmarks && found.faceLandmarks[0];
    const shapes = found.faceBlendshapes && found.faceBlendshapes[0];
    if (!lm || !shapes) return null;
    const score = {};
    for (const c of shapes.categories) score[c.categoryName] = c.score;
    const left = lm[234], right = lm[454], nose = lm[1];
    const width = right.x - left.x;
    if (Math.abs(width) < 0.02) return null;
    return {
      blink: Math.min(score.eyeBlinkLeft || 0, score.eyeBlinkRight || 0),
      jaw: score.jawOpen || 0,
      smile: ((score.mouthSmileLeft || 0) + (score.mouthSmileRight || 0)) / 2,
      // 0.5 = facing the camera. The camera faces the player: turning to their right moves the nose to the image's left.
      turn: (nose.x - left.x) / width,
    };
  }

  function faceKey(f) {
    if (f.blink > 0.5) return "shut";  // first: shut eyes always stop
    const b = faceBase;
    if (f.jaw - b.jaw > 0.35) return "open";
    if (f.smile - b.smile > 0.4) return "smile";
    if (f.turn - b.turn < -0.12) return "right";
    if (f.turn - b.turn > 0.12) return "left";
    return "still";
  }

  function classifyFace(f) {
    if (!f) {
      streak = { key: null, count: 0 };
      prev = { right: 0, forward: 0, text: "" };
      return { right: 0, forward: 0, seen: false, ready: !!faceBase, text: "" };
    }
    if (!faceBase) {
      faceSamples.push(f);
      if (faceSamples.length >= 20) {  // about 2 s of a straight face
        const avg = (k) => faceSamples.reduce((sum, x) => sum + x[k], 0) / faceSamples.length;
        faceBase = { jaw: avg("jaw"), smile: avg("smile"), turn: avg("turn") };
        faceSamples = [];
      }
      return { right: 0, forward: 0, seen: true, ready: false, text: "" };
    }
    const key = faceKey(f);
    streak = streak.key === key ? { key, count: streak.count + 1 } : { key, count: 1 };
    // Shut eyes stop at once; the other faces must be held for a moment.
    if (key === "shut" || streak.count >= HOLD) {
      const way = FACE_WAYS[key];
      prev = { right: way.right, forward: way.forward, text: way.text };
    }
    return { ...prev, seen: true, ready: true };
  }

  function showStatus(lean) {
    const el = statusEl();
    if (!el) return;
    let text;
    if (!lean.seen) text = mode === "face" ? "顔をカメラに映してください"
      : "片手をカメラに映してください（指1本=前・2本=後ろ・3本=右・4本=左・パーかグー=止まる）";
    else if (!lean.ready) text = "真顔でカメラを見て、2秒ほど止まってください（基準にします）";
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
      let found = null;
      try {
        found = landmarker.detectForVideo(cam, nowStamp());
      } catch (e) { /* a dropped frame is just a miss */ }
      const lean = mode === "face" ? classifyFace(found ? faceOf(found) : null)
        : classify(fingersOf(found && found.landmarks && found.landmarks[0]));
      showStatus(lean);
      send(lean);
    }
    if (running && cam && cam.requestVideoFrameCallback) cam.requestVideoFrameCallback(tick);
    else if (running) setTimeout(tick, 120);
  }

  const landmarkers = {};
  let loadingMode = null;

  async function ensureModel() {
    if (landmarkers[mode]) { landmarker = landmarkers[mode]; return; }
    if (!loading || loadingMode !== mode) {
      const want = mode;
      loadingMode = want;
      loading = (async () => {
        const vision = await import("/static/vendor/pose/vision_bundle.mjs");
        const files = await vision.FilesetResolver.forVisionTasks("/static/vendor/pose/wasm");
        landmarkers[want] = want === "face"
          ? await vision.FaceLandmarker.createFromOptions(files, {
            baseOptions: { modelAssetPath: "/static/vendor/pose/face_landmarker.task", delegate: "CPU" },
            runningMode: "VIDEO", numFaces: 1, outputFaceBlendshapes: true,
          })
          : await vision.HandLandmarker.createFromOptions(files, {
            baseOptions: { modelAssetPath: "/static/vendor/pose/hand_landmarker.task", delegate: "CPU" },
            runningMode: "VIDEO", numHands: 1,
          });
        landmarker = landmarkers[want];
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
    faceBase = null;
    faceSamples = [];
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

  function setMode(next) {
    if (next === mode) return;
    const wasOn = running || starting;
    stop();
    mode = next;
    landmarker = landmarkers[mode] || null;
    for (const b of document.querySelectorAll("[data-gesture-mode]")) b.classList.toggle("on", b.dataset.gestureMode === mode);
    for (const el of document.querySelectorAll("[data-gesture-help]")) el.hidden = el.dataset.gestureHelp !== mode;
    if (wasOn) start();
  }
  for (const b of document.querySelectorAll("[data-gesture-mode]")) {
    b.addEventListener("click", () => setMode(b.dataset.gestureMode));
  }

  const button = camButton();
  if (button) {
    button.addEventListener("click", () => (running || starting ? stop() : start()));
  }
  window.addEventListener("pagehide", stop);

  return { setVisible, stop };
})();
window.Gesture = Gesture;
