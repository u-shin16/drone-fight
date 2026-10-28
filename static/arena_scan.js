"use strict";

// A photo maps an arbitrary pad arrangement into centimetres. The user picks
// one known 20 cm square and the points needed for a shared, bounded map.
(() => {
  const canvas = document.getElementById("scan-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const photo = document.getElementById("scan-photo");
  const note = document.getElementById("scan-instruction");
  const back = document.getElementById("scan-back");
  const clear = document.getElementById("scan-clear");
  const viewport = document.querySelector(".scan-viewport");
  let zoom = 1;
  const img = new Image();
  const points = [];
  const steps = ["基準パッドの左上角", "基準パッドの右上角", "基準パッドの右下角", "基準パッドの左下角",
    ...Array.from({length: 8}, (_, i) => `パッド${i + 1}の中心`),
    "飛行可能な長方形の対角1点目", "その対角2点目",
    "別のパッドの左上角（縮尺確認）", "同じパッドの右上角", "同じパッドの右下角", "同じパッドの左下角"];
  const ref = [[-10, -10], [10, -10], [10, 10], [-10, 10]];

  function solve(from, to) {
    const a = [];
    for (let i = 0; i < 4; i++) {
      const [u, v] = from[i], [x, y] = to[i];
      a.push([u, v, 1, 0, 0, 0, -x*u, -x*v, x]);
      a.push([0, 0, 0, u, v, 1, -y*u, -y*v, y]);
    }
    for (let col = 0; col < 8; col++) {
      let pivot = col;
      for (let row = col + 1; row < 8; row++) if (Math.abs(a[row][col]) > Math.abs(a[pivot][col])) pivot = row;
      if (Math.abs(a[pivot][col]) < 1e-8) throw Error("基準パッドの四隅をもう一度指定してください");
      [a[col], a[pivot]] = [a[pivot], a[col]];
      const k = a[col][col];
      for (let c = col; c < 9; c++) a[col][c] /= k;
      for (let row = 0; row < 8; row++) if (row !== col) {
        const m = a[row][col];
        for (let c = col; c < 9; c++) a[row][c] -= m*a[col][c];
      }
    }
    return a.map(row => row[8]);
  }

  function transform(h, [u, v]) {
    const d = h[6]*u + h[7]*v + 1;
    if (Math.abs(d) < 1e-8) throw Error("写真の遠近変換に失敗しました");
    const x = (h[0]*u + h[1]*v + h[2])/d;
    const y = (h[3]*u + h[4]*v + h[5])/d;
    if (!Number.isFinite(x) || !Number.isFinite(y)) throw Error("写真の座標が不正です");
    return [x, y];
  }

  function linear(a, b) {
    const n = b.length;
    const rows = a.map((row, i) => [...row, b[i]]);
    for (let col = 0; col < n; col++) {
      let pivot = col;
      for (let row = col + 1; row < n; row++) if (Math.abs(rows[row][col]) > Math.abs(rows[pivot][col])) pivot = row;
      if (Math.abs(rows[pivot][col]) < 1e-12) throw Error("写真の遠近補正に失敗しました");
      [rows[col], rows[pivot]] = [rows[pivot], rows[col]];
      const d = rows[col][col];
      for (let j = col; j <= n; j++) rows[col][j] /= d;
      for (let row = 0; row < n; row++) if (row !== col) {
        const m = rows[row][col];
        for (let j = col; j <= n; j++) rows[row][j] -= m*rows[col][j];
      }
    }
    return rows.map(row => row[n]);
  }

  function fittedCalibration() {
    // Fit one projective camera transform to two different 20 cm squares.
    // The second square may have its own centre and a small rotation.
    const size = Math.max(canvas.width, canvas.height);
    const normal = p => [(p[0] - canvas.width/2)/size, (p[1] - canvas.height/2)/size];
    const observed = [...points.slice(0, 4), ...points.slice(14, 18)].map(normal);
    const mean = q => [q.reduce((a,p) => a+p[0], 0)/q.length, q.reduce((a,p) => a+p[1], 0)/q.length];
    const p1 = observed.slice(0, 4), p2 = observed.slice(4);
    const m1 = mean(p1), m2 = mean(p2);
    const ux = ((p1[1][0]-p1[0][0]) + (p1[2][0]-p1[3][0]))/4;
    const vx = ((p1[1][1]-p1[0][1]) + (p1[2][1]-p1[3][1]))/4;
    const uy = ((p1[3][0]-p1[0][0]) + (p1[2][0]-p1[1][0]))/4;
    const vy = ((p1[3][1]-p1[0][1]) + (p1[2][1]-p1[1][1]))/4;
    const det = ux*vy - uy*vx;
    if (Math.abs(det) < 1e-7) throw Error("基準パッドの四隅を確認してください");
    const du = m2[0]-m1[0], dv = m2[1]-m1[1];
    let par = [ux,uy,m1[0],vx,vy,m1[1],0,0,
      (du*vy-dv*uy)/det, (dv*ux-du*vx)/det, 0];
    const local = ref.map(([x,y]) => [x/10,y/10]);
    const residual = q => {
      const [a,b,c,d,e,f,g,h,cx,cy,theta] = q;
      const co = Math.cos(theta), si = Math.sin(theta);
      const world = [...local, ...local.map(([x,y]) => [cx+co*x-si*y,cy+si*x+co*y])];
      return world.flatMap(([x,y],i) => {
        const den = g*x+h*y+1;
        if (Math.abs(den) < 1e-6) return [1000,1000];
        return [(a*x+b*y+c)/den-observed[i][0],(d*x+e*y+f)/den-observed[i][1]];
      });
    };
    const cost = r => r.reduce((a,v) => a+v*v, 0);
    let damping = 1e-3, best = cost(residual(par));
    for (let iteration = 0; iteration < 80; iteration++) {
      const r = residual(par), jac = [];
      for (let j = 0; j < par.length; j++) {
        const step = 1e-5*Math.max(1,Math.abs(par[j]));
        const trial = [...par]; trial[j] += step;
        const shifted = residual(trial);
        jac.push(shifted.map((v,i) => (v-r[i])/step));
      }
      const jt = jac.map(col => col.reduce((a,v,i) => a+v*r[i], 0));
      const jtj = jac.map((col,j) => jac.map((other,k) =>
        col.reduce((a,v,i) => a+v*other[i], 0) + (j===k ? damping*Math.max(1e-8,col.reduce((a,v)=>a+v*v,0)) : 0)));
      let delta;
      try { delta = linear(jtj, jt.map(v => -v)); }
      catch (e) { break; }
      const trial = par.map((v,j) => v+delta[j]);
      const next = cost(residual(trial));
      if (next < best && Number.isFinite(next)) {
        par = trial; best = next; damping = Math.max(1e-9,damping/3);
        if (Math.max(...delta.map(Math.abs)) < 1e-7) break;
      } else damping = Math.min(1e9,damping*10);
    }
    const errors = residual(par);
    const maxError = Math.max(...Array.from({length:8},(_,i) => Math.hypot(errors[2*i],errors[2*i+1])))*size;
    const edges = [p1,p2].flatMap(quad => [0,1,2,3].map(i =>
      Math.hypot(quad[i][0]-quad[(i+1)%4][0],quad[i][1]-quad[(i+1)%4][1])*size));
    if (Math.abs(par[10]) > .35 || maxError > Math.max(5,Math.min(...edges)*.15))
      throw Error("2枚のパッドが合いません。角とロケットの向きを確認してください");
    const [a,b,c,d,e,f,g,h] = par;
    const project = ([x,y]) => {
      const den = g*x+h*y+1;
      return [((a*x+b*y+c)/den)*size+canvas.width/2,
              ((d*x+e*y+f)/den)*size+canvas.height/2];
    };
    const imageCorners = local.map(project);
    return {toWorld: solve(imageCorners, ref), toImage: solve(ref, imageCorners), maxError};
  }

  function layout() {
    if (points.length !== steps.length) throw Error("スキャンの点を最後まで指定してください");
    const h = fittedCalibration().toWorld;
    const world = points.map(p => transform(h, p));
    const check = world.slice(14);
    const edges = [0, 1, 2, 3].map(i => Math.hypot(check[i][0] - check[(i+1)%4][0],
                                                  check[i][1] - check[(i+1)%4][1]));
    const mid = [check.reduce((sum, p) => sum + p[0], 0)/4, check.reduce((sum, p) => sum + p[1], 0)/4];
    if (edges.some(length => length < 15 || length > 25) || Math.hypot(...mid) < 80)
      throw Error("遠くの別パッドを確認してください");
    const round = n => Math.round(n*10)/10;
    const pads = {};
    for (let i = 1; i <= 8; i++) pads[i] = world[i+3].map(round);
    const a = pads[4], b = pads[6], c = world[12], d = world[13];
    return {pads,
      field: [Math.min(c[0], d[0]), Math.max(c[0], d[0]),
              Math.min(c[1], d[1]), Math.max(c[1], d[1])].map(round),
      starts: {A: [...a.map(round), 0], B: [...b.map(round), 180]}};
  }

  function draw() {
    if (!img.complete || !img.naturalWidth) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    if (points.length >= 14) {
      try {
        if (points.length < steps.length) throw Error("校正待ち");
        const {toWorld, toImage} = fittedCalibration();
        const a = transform(toWorld, points[12]), b = transform(toWorld, points[13]);
        const x0 = Math.min(a[0], b[0]), x1 = Math.max(a[0], b[0]);
        const y0 = Math.min(a[1], b[1]), y1 = Math.max(a[1], b[1]);
        const corners = [[x0,y0],[x1,y0],[x1,y1],[x0,y1]].map(p => transform(toImage, p));
        ctx.beginPath(); ctx.moveTo(...corners[0]);
        corners.slice(1).forEach(p => ctx.lineTo(...p)); ctx.closePath();
        ctx.fillStyle = "rgba(242,53,69,.12)"; ctx.fill();
        ctx.strokeStyle = "#f23545"; ctx.lineWidth = 4; ctx.stroke();
      } catch (e) { /* invalid reference is reported when saving */ }
    }
    points.forEach(([x, y], i) => {
      ctx.fillStyle = i < 4 ? "#f5a524" : i < 12 ? "#21b779" : "#f23545";
      ctx.beginPath(); ctx.arc(x, y, 7, 0, 2*Math.PI); ctx.fill();
      ctx.strokeStyle = "white"; ctx.lineWidth = 2; ctx.stroke();
      ctx.fillStyle = "white"; ctx.font = "bold 16px sans-serif";
      ctx.fillText(i < 4 ? `角${i+1}` : i < 12 ? String(i-3) : i < 14 ? "範囲" : "確認", x+10, y-8);
    });
    if (points.length === steps.length) {
      try { layout(); note.textContent = "スキャン完了。地図を保存してください。"; }
      catch (e) { note.textContent = e.message + "（1点戻す・やり直すで修正）"; }
    } else {
      note.textContent = `${points.length + 1}/${steps.length}：${steps[points.length]}を押してください`;
    }
    document.getElementById("save-arena").disabled = points.length !== steps.length;
  }

  photo.addEventListener("change", () => {
    const file = photo.files[0];
    if (!file) return;
    points.length = 0;
    const url = URL.createObjectURL(file);
    img.onload = () => {
      const scale = Math.min(1, 1100/img.naturalWidth, 900/img.naturalHeight);
      canvas.width = Math.round(img.naturalWidth*scale);
      canvas.height = Math.round(img.naturalHeight*scale);
      zoom = 1;
      canvas.style.width = `${Math.min(canvas.width, viewport.clientWidth)}px`;
      canvas.hidden = false;
      URL.revokeObjectURL(url);
      draw();
    };
    img.onerror = () => { note.textContent = "写真を開けません。JPEGまたはPNGに変換してください。"; URL.revokeObjectURL(url); };
    img.src = url;
  });
  canvas.addEventListener("click", e => {
    if (points.length >= steps.length) return;
    const rect = canvas.getBoundingClientRect();
    points.push([(e.clientX-rect.left)*canvas.width/rect.width,
                 (e.clientY-rect.top)*canvas.height/rect.height]);
    draw();
  });
  back.addEventListener("click", () => { points.pop(); draw(); });
  clear.addEventListener("click", () => { points.length = 0; draw(); });
  const setZoom = factor => {
    zoom = Math.max(1, Math.min(4, zoom*factor));
    canvas.style.width = `${Math.round(Math.min(canvas.width, viewport.clientWidth)*zoom)}px`;
  };
  document.getElementById("scan-zoom-in").addEventListener("click", () => setZoom(1.5));
  document.getElementById("scan-zoom-out").addEventListener("click", () => setZoom(1/1.5));
  window.arenaScan = {layout, ready: () => points.length === steps.length};
})();
