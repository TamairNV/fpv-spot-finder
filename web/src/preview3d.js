// 3D model of a spot from the pipeline's preview image (320 x 320 m at 2 m), with the real sun,
// the detected gaps and lone trees marked, and a little FPV sim to fly it with a keyboard or a
// radio plugged in over USB.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import SunCalc from 'suncalc';
import { el, store, save } from './util.js';

const CLASS_COLOUR = {
  0: new THREE.Color('#6f8a5a'), // ground / grass
  1: new THREE.Color('#2f6f9a'), // water
  2: new THREE.Color('#44484d'), // road
  3: new THREE.Color('#b8a27a'), // path
  4: new THREE.Color('#4f7a3f'), // ground under trees
  5: new THREE.Color('#8a8a86'), // ground under buildings
};
const TREE = new THREE.Color('#3f7d3a');
const TREE_TALL = new THREE.Color('#2b5e2c');
const BUILDING = new THREE.Color('#d9d4c8');
const OTHER = new THREE.Color('#9b8f80');

const DRONES = {
  freestyle5: { name: '5"', twr: 4.5, drag: 0.035, rate: 670, expo: 0.45, uptilt: 25, radius: 0.15 },
  cine85: { name: '85 mm', twr: 2.4, drag: 0.15, rate: 450, expo: 0.35, uptilt: 12, radius: 0.06 },
};
const G = 9.81;
// the suggested line's moves, numbered in the order to fly them
const MOVE_COLOUR = { gap: '#ff3d7f', orbit: '#ffc400', dive: '#a78bfa', skim: '#22d3ee', arch: '#fb923c' };
const DECK = 6; // preview class: a viaduct deck, drawn as a slab in the air so its arches show
const DECK_T = 2.5; // m, how thick the deck slab is
const WEAVE_OUT = 9; // m either side of a viaduct that a weave swings out to between arches
const LINE_SPEED = { freestyle5: 15, cine5: 11, cine85: 6 }; // m/s for "Watch the line"
const COARSE = matchMedia('(pointer: coarse)');
const AXES_KEY = 'fpvspots:gamepad-axes';

// The first connected radio/gamepad, or null. Embedded pages can be refused the Gamepad API
// outright (it throws), and then the keyboard still flies.
function firstPad() {
  try {
    return navigator.getGamepads ? [...navigator.getGamepads()].find(Boolean) ?? null : null;
  } catch {
    return null;
  }
}

async function loadPixels(url) {
  const img = new Image();
  img.crossOrigin = 'anonymous';
  img.src = url;
  await img.decode();
  const c = document.createElement('canvas');
  c.width = img.width;
  c.height = img.height;
  const ctx = c.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0);
  return { w: img.width, h: img.height, data: ctx.getImageData(0, 0, img.width, img.height).data };
}

export class Preview3D {
  constructor(host, { spot, preview, center, profile, radiusM, onClose }) {
    this.host = host;
    this.onClose = onClose;
    this.spot = spot;
    this.preview = preview;
    this.center = center;
    this.profile = profile;
    this.radiusM = radiusM;
    this.disposed = false;
    this.fly = null;
    this.keys = new Set();
    this.date = new Date();
    this.build();
  }

  build() {
    this.wrap = el('div', { class: 'p3d-wrap' });
    this.canvas = el('canvas', { class: 'p3d' });
    this.tag = el('div', { class: 'p3d-tag' }, 'Loading 3D…');
    this.tools = el('div', { class: 'p3d-tools' },
      el('button', { class: 'p3d-pill p3d-open', onclick: () => this.toggleFull(true) }, '⤢ Explore in 3D'));
    const close = el('button', { class: 'p3d-close', onclick: () => this.toggleFull(false) }, '✕ Close');
    this.stage = el('div', { class: 'p3d-stage' }, this.canvas, this.tag, this.tools);
    // On touch screens the small view is a picture: dragging it would fight the page scroll,
    // so a tap opens the full-screen view where every gesture moves the camera.
    if (COARSE.matches) this.stage.append(el('button', { class: 'p3d-tap', 'aria-label': 'Explore in 3D', onclick: () => this.toggleFull(true) }));
    this.wrap.append(this.stage, close);
    this.host.append(this.wrap);
    this.onEsc = (e) => {
      if (e.key !== 'Escape' || this.fly) return;
      if (this.play) this.stopLine();
      else if (this.wrap.classList.contains('full')) this.toggleFull(false);
    };
    window.addEventListener('keydown', this.onEsc);
  }

  // Small view: drag to orbit with a mouse, no wheel zoom (the wheel scrolls the card), no touch.
  // Full screen: everything.
  applyMode() {
    const full = this.wrap.classList.contains('full');
    if (this.controls) {
      this.controls.enableZoom = full;
      this.controls.enabled = !this.fly && !this.play && (full || !COARSE.matches);
    }
    this.canvas.style.touchAction = full ? 'none' : 'pan-y';
  }

  async load(url) {
    const px = await loadPixels(url);
    if (this.disposed) return;
    const n = px.w, res = this.preview.res, half = (n * res) / 2;
    this.n = n;
    this.res = res;
    this.half = half;
    this.cls = new Uint8Array(n * n);
    this.h = new Float32Array(n * n);
    this.g = new Float32Array(n * n);
    for (let i = 0; i < n * n; i++) {
      this.cls[i] = Math.round(px.data[i * 4] / 40);
      this.h[i] = px.data[i * 4 + 1] / 4;
      this.g[i] = px.data[i * 4 + 2] / 2;
    }
    this.initThree();
    this.buildTerrain();
    this.buildObjects();
    this.buildMarkers();
    this.buildLine();
    this.buildUI();
    this.setTime(this.defaultTime());
    this.tag.textContent = `${this.preview.lidar ? `EA LiDAR, ${res} m blocks` : 'Tree model + building heights (no LiDAR yet)'} · ${n * res} m square`;
    this.render();
  }

  // ---------- scene

  initThree() {
    const r = (this.renderer = new THREE.WebGLRenderer({ canvas: this.canvas, antialias: true }));
    r.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    r.shadowMap.enabled = true;
    r.shadowMap.type = THREE.PCFSoftShadowMap;
    r.toneMapping = THREE.ACESFilmicToneMapping;
    r.outputColorSpace = THREE.SRGBColorSpace;
    this.scene = new THREE.Scene();
    this.scene.fog = new THREE.Fog(0x9cc7e8, 350, 1100);
    this.camera = new THREE.PerspectiveCamera(45, 4 / 3, 0.1, 3000);
    this.camera.position.set(170, 190, 230);
    this.controls = new OrbitControls(this.camera, this.canvas);
    this.controls.target.set(0, this.groundAt(0, 0), 0);
    this.controls.maxPolarAngle = Math.PI * 0.49;
    this.controls.minDistance = 15;
    this.controls.maxDistance = 900;
    this.controls.enableDamping = true;
    this.controls.addEventListener('change', () => this.render());
    this.applyMode();
    this.sun = new THREE.DirectionalLight(0xffffff, 2.6);
    this.sun.castShadow = true;
    const sc = this.sun.shadow.camera;
    sc.left = sc.bottom = -230;
    sc.right = sc.top = 230;
    sc.near = 1;
    sc.far = 1400;
    this.sun.shadow.mapSize.set(2048, 2048);
    this.sun.shadow.bias = -0.0004;
    this.sun.shadow.normalBias = 0.6;
    this.scene.add(this.sun, this.sun.target);
    this.hemi = new THREE.HemisphereLight(0xcfe6ff, 0x4a5a3a, 0.9);
    this.scene.add(this.hemi);
    this.resizeObs = new ResizeObserver(() => this.resize());
    this.resizeObs.observe(this.wrap);
    this.resize();
  }

  resize() {
    const w = this.canvas.clientWidth, h = this.canvas.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.render();
  }

  idx(x, z) {
    const j = Math.floor((x + this.half) / this.res), i = Math.floor((z + this.half) / this.res);
    if (i < 0 || j < 0 || i >= this.n || j >= this.n) return -1;
    return i * this.n + j;
  }

  groundAt(x, z) {
    const k = this.idx(x, z);
    return k < 0 ? 0 : this.g[k];
  }

  topAt(x, z) {
    const k = this.idx(x, z);
    return k < 0 ? -Infinity : this.g[k] + (this.h[k] >= 0.75 ? this.h[k] : 0);
  }

  buildTerrain() {
    const { n, res, half } = this;
    const pos = new Float32Array(n * n * 3), col = new Float32Array(n * n * 3);
    const c = new THREE.Color();
    for (let i = 0; i < n; i++) {
      for (let j = 0; j < n; j++) {
        const k = i * n + j;
        pos[k * 3] = (j + 0.5) * res - half;
        pos[k * 3 + 1] = this.g[k];
        pos[k * 3 + 2] = (i + 0.5) * res - half;
        c.copy(CLASS_COLOUR[this.cls[k]] || CLASS_COLOUR[0]);
        c.offsetHSL(0, 0, ((k * 2654435761) % 1000) / 1000 * 0.04 - 0.02);
        col.set([c.r, c.g, c.b], k * 3);
      }
    }
    const index = [];
    for (let i = 0; i < n - 1; i++) {
      for (let j = 0; j < n - 1; j++) {
        const a = i * n + j, b = a + 1, d = a + n, e = d + 1;
        index.push(a, d, b, b, d, e);
      }
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    geo.setAttribute('color', new THREE.BufferAttribute(col, 3));
    geo.setIndex(index);
    geo.computeVertexNormals();
    const mesh = new THREE.Mesh(geo, new THREE.MeshLambertMaterial({ vertexColors: true }));
    mesh.receiveShadow = true;
    this.scene.add(mesh);
    // a skirt so the block reads as a slab of ground, not a sheet
    const base = new THREE.Mesh(new THREE.BoxGeometry(n * res, 4, n * res), new THREE.MeshLambertMaterial({ color: 0x2a2f2a }));
    let gmin = Infinity;
    for (const v of this.g) gmin = Math.min(gmin, v);
    base.position.y = gmin - 2.05;
    this.scene.add(base);
  }

  buildObjects() {
    const { n, res, half } = this;
    const cells = [];
    for (let k = 0; k < n * n; k++) {
      if (this.h[k] >= 0.75 && this.cls[k] !== 1 && this.cls[k] !== 2) cells.push(k);
    }
    const geo = new THREE.BoxGeometry(1, 1, 1);
    geo.translate(0, 0.5, 0);
    const mat = new THREE.MeshLambertMaterial();
    const inst = new THREE.InstancedMesh(geo, mat, Math.max(cells.length, 1));
    const m = new THREE.Matrix4(), c = new THREE.Color();
    cells.forEach((k, idx) => {
      const i = Math.floor(k / n), j = k % n, h = this.h[k];
      const slab = this.cls[k] === DECK ? Math.min(h, DECK_T) : h; // the arches under a deck are open air
      m.makeScale(res, slab, res);
      m.setPosition((j + 0.5) * res - half, this.g[k] + h - slab, (i + 0.5) * res - half);
      inst.setMatrixAt(idx, m);
      if (this.cls[k] === 5) c.copy(BUILDING).offsetHSL(0, 0, Math.min(h, 30) / 300);
      else if (this.cls[k] === 4) c.copy(TREE).lerp(TREE_TALL, Math.min(h / 25, 1)).offsetHSL(0, 0, ((k * 40503) % 100) / 100 * 0.05);
      else c.copy(OTHER);
      inst.setColorAt(idx, c);
    });
    inst.count = cells.length;
    inst.castShadow = inst.receiveShadow = true;
    this.scene.add(inst);
    this.objects = inst;
  }

  // Which way does a gap face? Look for the two flanks around it.
  gapAxis(x, z, width) {
    let best = 0, bestScore = -1;
    for (let a = 0; a < 180; a += 15) {
      const t = (a * Math.PI) / 180, dx = Math.cos(t), dz = Math.sin(t);
      let score = 0;
      for (const s of [-1, 1]) {
        for (let d = width / 2; d <= width / 2 + 4; d += 1) {
          const k = this.idx(x + s * dx * d, z + s * dz * d);
          if (k >= 0 && this.h[k] >= 4) { score++; break; }
        }
      }
      if (score > bestScore) { bestScore = score; best = t; }
    }
    return best; // angle of the line joining the flanks
  }

  buildMarkers() {
    const grp = (this.markers = new THREE.Group());
    this.gates = [];
    const pink = new THREE.MeshBasicMaterial({ color: 0xff3d7f, transparent: true, opacity: 0.9 });
    for (const g of this.spot.gaps_list || []) {
      const [, , width, flank, dx, dy] = g;
      const x = dx, z = -dy;
      if (Math.abs(x) > this.half || Math.abs(z) > this.half) continue;
      const r = Math.max(width / 2, 1);
      const y = this.groundAt(x, z) + Math.min(Math.max(r + 0.6, 1.6), Math.max(flank - r, 1.6));
      const ring = new THREE.Mesh(new THREE.TorusGeometry(r, Math.max(0.12, r * 0.06), 8, 40), pink.clone());
      const axis = this.gapAxis(x, z, width);
      ring.position.set(x, y, z);
      // ring plane contains the flank axis; you fly through along its normal
      ring.rotation.y = -axis + Math.PI / 2;
      grp.add(ring);
      const normal = new THREE.Vector3(0, 0, 1).applyEuler(ring.rotation);
      this.gates.push({ ring, centre: ring.position.clone(), normal, r, side: 0, hit: false });
    }
    const yellow = new THREE.LineBasicMaterial({ color: 0xffc23d });
    for (const t of this.spot.lone_list || []) {
      const [, , h, dx, dy] = t;
      const x = dx, z = -dy;
      if (Math.abs(x) > this.half || Math.abs(z) > this.half) continue;
      const rad = Math.max(5, h * 0.45);
      const pts = [];
      for (let a = 0; a <= 64; a++) pts.push(new THREE.Vector3(Math.cos((a / 64) * 2 * Math.PI) * rad, 0, Math.sin((a / 64) * 2 * Math.PI) * rad));
      const loop = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), yellow);
      loop.position.set(x, this.groundAt(x, z) + h * 0.5, z);
      grp.add(loop);
    }
    const to = this.spot.takeoff;
    if (to) {
      const x = to.dx, z = -to.dy;
      const pad = new THREE.Mesh(new THREE.CylinderGeometry(1.5, 1.5, 0.1, 32), new THREE.MeshBasicMaterial({ color: 0x3ddc84 }));
      pad.position.set(x, this.groundAt(x, z) + 0.05, z);
      grp.add(pad);
      const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.15, 0.15, 12, 6), new THREE.MeshBasicMaterial({ color: 0x3ddc84 }));
      pole.position.set(x, this.groundAt(x, z) + 6, z);
      grp.add(pole);
      this.takeoff = new THREE.Vector3(x, this.groundAt(x, z) + 0.3, z);
    } else {
      this.takeoff = new THREE.Vector3(0, this.groundAt(0, 0) + 0.3, 0);
    }
    if (this.radiusM) {
      const pts = [];
      for (let a = 0; a <= 96; a++) {
        const t = (a / 96) * 2 * Math.PI, x = Math.cos(t) * this.radiusM, z = Math.sin(t) * this.radiusM;
        pts.push(new THREE.Vector3(x, this.groundAt(x, z) + 0.6, z));
      }
      grp.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineDashedMaterial({ color: 0xffffff, dashSize: 4, gapSize: 3 })));
      grp.children.at(-1).computeLineDistances();
    }
    // N marker on the north edge
    const c = document.createElement('canvas');
    c.width = c.height = 64;
    const ctx = c.getContext('2d');
    ctx.fillStyle = '#fff';
    ctx.font = 'bold 44px system-ui';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('N', 32, 34);
    const north = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(c), depthTest: false }));
    north.scale.set(16, 16, 1);
    north.position.set(0, this.groundAt(0, -this.half + 4) + 12, -this.half - 10);
    grp.add(north);
    this.scene.add(grp);
  }

  // ---------- the suggested line (from the pipeline's line finder): take-off, then each move in order

  buildLine() {
    this.lineSprites = [];
    const moves = (this.spot.line?.moves || []).filter((m) => Math.abs(m[3]) <= this.half && Math.abs(m[4]) <= this.half);
    if (!moves.length) return;
    const pts = [this.takeoff.clone().add(new THREE.Vector3(0, 2, 0))];
    let weave = 0;
    moves.forEach(([kind, , , dx, dy, bend], i) => {
      if (bend) { // the way in bends round something: low past it first
        const bx = bend[2], bz = -bend[3];
        pts.push(new THREE.Vector3(bx, this.groundAt(bx, bz) + 2.5, bz));
      }
      const x = dx, z = -dy, prev = pts.at(-1);
      const into = new THREE.Vector3(x - prev.x, 0, z - prev.z).normalize();
      const ground = this.groundAt(x, z);
      let mark;
      if (kind === 'orbit') { // once round the tree, entering on the side we arrive from
        const tree = this.nearest(this.spot.lone_list, x, z, 3, 4, 8);
        const h = tree ? tree[2] : 10, rad = Math.max(6, h * 0.45), y = ground + Math.max(3, h * 0.5);
        const a0 = Math.atan2(-into.z, -into.x);
        for (let k = 0; k <= 12; k++) {
          const a = a0 - (k / 12) * 2 * Math.PI;
          pts.push(new THREE.Vector3(x + Math.cos(a) * rad, y, z + Math.sin(a) * rad));
        }
        mark = new THREE.Vector3(x, y + h * 0.5 + 3, z);
      } else if (kind === 'dive') { // over the top, then down the far side
        const top = this.topAt(x, z);
        pts.push(new THREE.Vector3(x, top + 10, z));
        const fx = x + into.x * 18, fz = z + into.z * 18;
        pts.push(new THREE.Vector3(fx, this.groundAt(fx, fz) + 3, fz));
        mark = new THREE.Vector3(x, top + 14, z);
      } else if (kind === 'arch') { // under the deck, a third of the way up the arch
        const k = this.idx(x, z), deck = k >= 0 ? this.h[k] : 12;
        const y = ground + Math.max(3, (deck - DECK_T) * 0.4);
        if (moves[i - 1]?.[0] === 'arch') { // weaving: out of the last arch, round the pier, back through this one
          const side = (weave++ % 2 ? 1 : -1) * WEAVE_OUT, mx = (prev.x + x) / 2 - into.z * side, mz = (prev.z + z) / 2 + into.x * side;
          pts.push(new THREE.Vector3(mx, Math.max(y, this.groundAt(mx, mz) + 3), mz));
        }
        pts.push(new THREE.Vector3(x, y, z));
        mark = new THREE.Vector3(x, ground + deck + 4, z);
      } else if (kind === 'gap') { // through the middle of the ring
        const gate = this.gates.reduce((b, g) => {
          const d = Math.hypot(g.centre.x - x, g.centre.z - z);
          return d < (b?.d ?? 6) ? { g, d } : b;
        }, null);
        const p = gate ? gate.g.centre.clone() : new THREE.Vector3(x, ground + 2.5, z);
        pts.push(p);
        mark = p.clone().add(new THREE.Vector3(0, (gate?.g.r ?? 2) + 3, 0));
      } else { // skim: low along the edge
        pts.push(new THREE.Vector3(x, ground + 2, z));
        mark = new THREE.Vector3(x, ground + 6, z);
      }
      const sp = this.numberSprite(i + 1, MOVE_COLOUR[kind] || '#ffffff', mark);
      this.lineSprites.push(sp);
      this.markers.add(sp);
    });
    this.line = new THREE.CatmullRomCurve3(pts, false, 'centripetal');
    const tube = new THREE.Mesh(new THREE.TubeGeometry(this.line, Math.max(80, pts.length * 12), 0.35, 6, false),
      new THREE.MeshBasicMaterial({ color: 0x5cc8ff, transparent: true, opacity: 0.85 }));
    this.markers.add(tube);
  }

  // nearest [.., dx, dy] row of a gaps/lone list within maxD metres of (x, z) in scene space
  nearest(list, x, z, ix, iy, maxD) {
    let best = null, bd = maxD;
    for (const row of list || []) {
      const d = Math.hypot(row[ix] - x, -row[iy] - z);
      if (d < bd) [best, bd] = [row, d];
    }
    return best;
  }

  numberSprite(n, colour, pos) {
    const c = document.createElement('canvas');
    c.width = c.height = 64;
    const ctx = c.getContext('2d');
    ctx.fillStyle = colour;
    ctx.beginPath();
    ctx.arc(32, 32, 28, 0, 2 * Math.PI);
    ctx.fill();
    ctx.lineWidth = 4;
    ctx.strokeStyle = '#111';
    ctx.stroke();
    ctx.fillStyle = '#111';
    ctx.font = 'bold 34px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(String(n), 32, 34);
    const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(c), depthTest: false }));
    sp.scale.set(7, 7, 1);
    sp.position.copy(pos);
    sp.renderOrder = 2;
    return sp;
  }

  // Follow a little drone along the line from just behind it, like a chase cam. Needs no keyboard or radio.
  playLine() {
    if (!this.line || this.fly) return;
    if (this.play) return this.stopLine();
    if (!this.wrap.classList.contains('full')) this.toggleFull(true);
    const len = this.line.getLength(), speed = LINE_SPEED[this.profile] || 12;
    this.play = { t0: performance.now(), dur: (len / speed) * 1000, len };
    this.controls.enabled = false;
    this.camera.fov = 70;
    this.camera.near = 0.05;
    this.camera.updateProjectionMatrix();
    this.lineSprites.forEach((sp) => { sp.visible = false; });
    this.dot ||= new THREE.Mesh(new THREE.SphereGeometry(0.6, 16, 12), new THREE.MeshBasicMaterial({ color: 0x5cc8ff }));
    this.scene.add(this.dot);
    this.playBtn?.classList.add('on');
    if (this.playBtn) this.playBtn.textContent = '■ Stop';
    const at = (m) => this.line.getPointAt(Math.min(1, Math.max(0, m / len)));
    const cam = new THREE.Vector3();
    let first = true;
    const loop = () => {
      if (!this.play || this.disposed) return;
      const u = Math.min(1, (performance.now() - this.play.t0) / this.play.dur), m = u * len;
      const p = at(m), ahead = at(m + 5);
      const want = at(m - 9).add(new THREE.Vector3(0, 3.5, 0));
      want.y = Math.max(want.y, this.topAt(want.x, want.z) + 1.5); // keep the camera out of the trees
      if (first) cam.copy(want);
      else cam.lerp(want, 0.2);
      first = false;
      this.dot.position.copy(p);
      this.camera.position.copy(cam);
      this.camera.lookAt(ahead);
      this.renderer.render(this.scene, this.camera);
      if (u >= 1) return this.stopLine();
      this.raf = requestAnimationFrame(loop);
    };
    this.raf = requestAnimationFrame(loop);
  }

  stopLine() {
    if (!this.play) return;
    this.play = null;
    cancelAnimationFrame(this.raf);
    this.lineSprites.forEach((sp) => { sp.visible = true; });
    if (this.dot) this.scene.remove(this.dot);
    this.playBtn?.classList.remove('on');
    if (this.playBtn) this.playBtn.textContent = '▶ Watch the line';
    this.camera.fov = 45;
    this.camera.near = 0.1;
    this.camera.updateProjectionMatrix();
    this.camera.position.set(170, 190, 230);
    this.applyMode();
    this.controls.target.set(0, this.groundAt(0, 0), 0);
    this.camera.lookAt(this.controls.target);
    this.render();
  }

  // ---------- sun

  defaultTime() {
    const t = SunCalc.getTimes(this.date, this.center[1], this.center[0]);
    const now = new Date();
    return now > t.sunrise && now < t.sunset ? now : t.goldenHour;
  }

  setTime(d) {
    this.time = d;
    const [lon, lat] = this.center;
    const p = SunCalc.getPosition(d, lat, lon);
    const azN = p.azimuth + Math.PI; // suncalc: from south, clockwise
    const alt = Math.max(p.altitude, 0.02);
    const dir = new THREE.Vector3(Math.sin(azN) * Math.cos(alt), Math.sin(alt), -Math.cos(azN) * Math.cos(alt));
    this.sun.position.copy(dir.multiplyScalar(500));
    const warm = Math.max(0, 1 - p.altitude / 0.35);
    const night = p.altitude < 0;
    this.sun.color.setHSL(0.09, 0.4 + 0.5 * warm, 0.5 + 0.4 * (1 - warm) * 0.5 + 0.1);
    this.sun.intensity = night ? 0.15 : 1.2 + 1.6 * Math.min(1, p.altitude / 0.5);
    const sky = new THREE.Color(night ? 0x14202a : 0x9cc7e8).lerp(new THREE.Color(0xf3b27a), night ? 0 : warm * 0.75);
    this.scene.background = sky;
    this.scene.fog.color = sky;
    this.hemi.intensity = night ? 0.25 : 0.7 + 0.4 * (1 - warm);
    if (this.timeLabel) {
      this.timeLabel.textContent = `${d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })} · sun ${Math.round((p.altitude * 180) / Math.PI)}° up`;
      const t0 = this.dayStart.getTime();
      this.slider.value = String(Math.round((d.getTime() - t0) / 60000));
    }
    this.render();
  }

  buildUI() {
    const [lon, lat] = this.center;
    const t = SunCalc.getTimes(this.date, lat, lon);
    this.dayStart = new Date(t.sunrise.getTime() - 45 * 60000);
    const span = Math.round((t.sunset - this.dayStart) / 60000 + 45);
    this.slider = el('input', { type: 'range', min: 0, max: span, step: 5, 'aria-label': 'Time of day' });
    this.slider.addEventListener('input', () => this.setTime(new Date(this.dayStart.getTime() + Number(this.slider.value) * 60000)));
    this.timeLabel = el('span', { class: 'p3d-time' });
    const jump = (label, d) => el('button', { onclick: () => this.setTime(d) }, label);
    const mid = (a, b) => new Date((a.getTime() + b.getTime()) / 2);
    const rowSun = el('div', { class: 'p3d-row' }, el('span', { 'aria-hidden': 'true' }, '☀'), this.slider, this.timeLabel);
    // flying needs a keyboard or a radio, so no fly button on phones
    if (!matchMedia('(hover: none)').matches) {
      this.flyBtn = el('button', { class: 'p3d-pill', onclick: () => (this.fly ? this.stopFly() : this.startFly()) }, '✈ Fly it');
      this.tools.prepend(this.flyBtn);
    }
    if (this.line) {
      this.playBtn = el('button', { class: 'p3d-pill', onclick: () => this.playLine() }, '▶ Watch the line');
      this.tools.prepend(this.playBtn);
    }
    const rowJump = el('div', { class: 'p3d-row p3d-chips' },
      jump('Golden AM', mid(t.sunrise, t.goldenHourEnd)), jump('Midday', t.solarNoon),
      jump('Golden PM', mid(t.goldenHour, t.sunset)), jump('Now', new Date()));
    // only what this model shows (a bando close-up has none of it)
    const keys = [
      this.spot.gaps_list?.length ? el('span', {}, el('b', { style: 'color:#ff3d7f' }, '◯'), ' gaps') : null,
      this.spot.lone_list?.length ? el('span', {}, el('b', { style: 'color:#ffc400' }, '◯'), ' lone trees') : null,
      this.spot.takeoff ? el('span', {}, el('b', { style: 'color:#22c55e' }, '●'), ' take-off') : null,
      this.line ? el('span', {}, el('b', { style: 'color:#5cc8ff' }, '━'), ' line to fly') : null,
      this.radiusM ? el('span', {}, '- - spot area') : null,
    ].filter(Boolean);
    const legend = keys.length ? el('div', { class: 'p3d-legend' }, keys) : null;
    this.ui = el('div', { class: 'p3d-ui' }, rowSun, rowJump, legend);
    this.wrap.append(this.ui);
  }

  // Full screen moves the view to <body>, so no scrolling or transformed parent can clip it.
  toggleFull(want) {
    const on = want ?? !this.wrap.classList.contains('full');
    if (on === this.wrap.classList.contains('full')) return;
    if (on) {
      this.home = { parent: this.wrap.parentNode, next: this.wrap.nextSibling };
      document.body.append(this.wrap);
    } else if (this.home?.parent?.isConnected) {
      this.home.parent.insertBefore(this.wrap, this.home.next);
    }
    this.wrap.classList.toggle('full', on);
    document.body.classList.toggle('p3d-full', on);
    if (!on && this.fly) this.stopFly();
    if (!on && this.play) this.stopLine();
    this.applyMode();
    this.resize();
    if (!on) this.onClose?.();
  }

  render() {
    if (this.disposed || !this.renderer) return;
    if (this.fly || this.play) return; // the fly and line loops render every frame
    cancelAnimationFrame(this.raf);
    this.raf = requestAnimationFrame(() => {
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    });
  }

  // ---------- fly mode

  startFly() {
    this.stopLine();
    const spec = DRONES[this.profile] || DRONES.freestyle5;
    this.controls.enabled = false;
    this.flyBtn?.classList.add('on');
    if (this.flyBtn) this.flyBtn.textContent = 'Stop flying';
    if (!this.wrap.classList.contains('full')) this.toggleFull(true);
    this.camera.fov = 85;
    this.camera.near = 0.05;
    this.camera.updateProjectionMatrix();
    this.axes = store(AXES_KEY, { roll: 0, pitch: 1, thr: 2, yaw: 3, inv: [false, true, false, false] });
    this.fly = {
      spec, acro: !!firstPad(), pos: new THREE.Vector3(), vel: new THREE.Vector3(),
      q: new THREE.Quaternion(), heading: 0, crashes: 0, gatesHit: 0, last: performance.now(), armed: false,
    };
    this.resetDrone();
    this.hud = el('div', { class: 'p3d-help' });
    this.wrap.append(this.hud, (this.reticle = el('div', { class: 'p3d-hud' })));
    this.onKey = (e) => {
      if (e.type === 'keydown') {
        this.keys.add(e.code);
        if (e.code === 'KeyR') this.resetDrone();
        if (e.code === 'KeyM') this.fly.acro = !this.fly.acro;
        if (e.code === 'Escape') this.stopFly();
        if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Space'].includes(e.code)) e.preventDefault();
      } else this.keys.delete(e.code);
    };
    window.addEventListener('keydown', this.onKey);
    window.addEventListener('keyup', this.onKey);
    const loop = () => {
      if (!this.fly || this.disposed) return;
      this.stepFly();
      this.renderer.render(this.scene, this.camera);
      this.raf = requestAnimationFrame(loop);
    };
    this.raf = requestAnimationFrame(loop);
  }

  stopFly() {
    if (!this.fly) return;
    this.fly = null;
    cancelAnimationFrame(this.raf);
    window.removeEventListener('keydown', this.onKey);
    window.removeEventListener('keyup', this.onKey);
    this.hud?.remove();
    this.reticle?.remove();
    this.flyBtn?.classList.remove('on');
    if (this.flyBtn) this.flyBtn.textContent = '✈ Fly it';
    this.camera.fov = 45;
    this.camera.near = 0.1;
    this.camera.updateProjectionMatrix();
    this.camera.position.set(170, 190, 230);
    this.applyMode();
    this.controls.target.set(0, this.groundAt(0, 0), 0);
    this.camera.lookAt(this.controls.target);
    this.gates.forEach((g) => { g.hit = false; g.ring.material.color.set(0xff3d7f); });
    this.render();
  }

  resetDrone() {
    const f = this.fly;
    f.pos.copy(this.takeoff).add(new THREE.Vector3(0, 0.5, 0));
    f.vel.set(0, 0, 0);
    // face the middle of the spot
    f.heading = Math.atan2(-(0 - f.pos.x), -(0 - f.pos.z));
    if (!isFinite(f.heading)) f.heading = 0;
    f.q.setFromEuler(new THREE.Euler(0, f.heading, 0, 'YXZ'));
    f.armed = false;
    f.crashMsg = '';
  }

  readSticks() {
    const k = (c) => (this.keys.has(c) ? 1 : 0);
    let roll = k('KeyD') - k('KeyA'), pitch = k('KeyW') - k('KeyS'), yaw = k('ArrowRight') - k('ArrowLeft');
    let thr = null, pad = false;
    const gp = firstPad();
    if (gp && gp.axes.length >= 4) {
      const a = this.axes;
      const ax = (i, inv) => (gp.axes[i] ?? 0) * (inv ? -1 : 1);
      roll = ax(a.roll, a.inv[0]);
      pitch = ax(a.pitch, a.inv[1]);
      thr = (ax(a.thr, a.inv[2]) + 1) / 2;
      yaw = ax(a.yaw, a.inv[3]);
      pad = true;
    }
    // keyboard: no throttle stick, so up/down set a climb rate and the sim holds it
    const climb = thr == null ? 5 * k('ArrowUp') - 4 * k('ArrowDown') + 3 * k('Space') : null;
    return { roll, pitch, yaw, thr: thr == null ? null : Math.max(0, Math.min(1, thr)), climb, pad };
  }

  stepFly() {
    const f = this.fly, s = f.spec;
    const now = performance.now();
    let left = Math.min((now - f.last) / 1000, 0.25);
    f.last = now;
    const st = this.readSticks();
    if (!f.armed && (st.thr == null || st.thr < 0.08)) f.armed = true;
    // small fixed steps, so a slow frame doesn't turn into slow motion
    while (left > 1e-4) {
      const h = Math.min(left, 1 / 120);
      this.physics(st, h);
      left -= h;
    }
    if (f.crashMsg && !f.armed && st.pad && st.thr < 0.05) this.resetDrone();
    // camera on the quad, tilted up like a real FPV cam
    this.camera.position.copy(f.pos);
    const tilt = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), (s.uptilt * Math.PI) / 180 * (f.acro && st.pad ? 1 : 0.3));
    this.camera.quaternion.copy(f.q).multiply(tilt);
    const kmh = Math.round(f.vel.length() * 3.6);
    const agl = Math.max(0, f.pos.y - this.groundAt(f.pos.x, f.pos.z));
    this.hud.innerHTML = `<b>${s.name} ${f.acro && st.pad ? 'ACRO' : 'ANGLE'}</b> · ${kmh} km/h · ${agl.toFixed(0)} m up<br>`
      + `Gaps flown: ${f.gatesHit}/${this.gates.length} · crashes ${f.crashes}<br>`
      + (f.crashMsg ? `<span style="color:#ff5a5a">${f.crashMsg}</span><br>` : '')
      + (st.pad ? 'Radio connected. M = angle/acro, R = reset, Esc = stop.'
        : 'Keys: W/S pitch, A/D roll, ←/→ yaw, ↑/↓ climb / sink, R reset, Esc stop. Plug your radio in as a USB joystick to fly acro on the sticks.')
      + (st.pad ? ' <a href="#" data-map>Remap sticks</a>' : '');
    const link = this.hud.querySelector('[data-map]');
    if (link) link.onclick = (e) => { e.preventDefault(); this.mapSticks(); };
  }

  physics(st, dt) {
    const f = this.fly, s = f.spec;
    const rate = (x) => (s.rate * Math.PI / 180) * (x * (1 - s.expo) + x * x * x * s.expo);
    if (f.acro && st.pad) {
      const w = new THREE.Vector3(-rate(st.pitch), -rate(st.yaw), -rate(st.roll)).multiplyScalar(dt);
      const dq = new THREE.Quaternion().setFromEuler(new THREE.Euler(w.x, w.y, w.z, 'XYZ'));
      f.q.multiply(dq).normalize();
    } else {
      f.heading -= rate(st.yaw) * 0.6 * dt;
      const target = new THREE.Quaternion().setFromEuler(new THREE.Euler(-st.pitch * 0.75, f.heading, -st.roll * 0.75, 'YXZ'));
      f.q.slerp(target, 1 - Math.exp(-dt * 9));
    }
    const up = new THREE.Vector3(0, 1, 0).applyQuaternion(f.q);
    let thr = st.thr;
    if (thr == null) {
      // hold the asked-for climb rate: thrust to cancel gravity on the tilted quad, plus a correction
      const tiltCos = Math.max(up.y, 0.35);
      thr = (G + 2.5 * (st.climb - f.vel.y)) / tiltCos / (s.twr * G);
      thr = Math.max(0, Math.min(1, thr));
    }
    const thrust = f.armed ? thr * s.twr * G : 0;
    const acc = up.multiplyScalar(thrust);
    acc.y -= G;
    acc.addScaledVector(f.vel, -s.drag * f.vel.length());
    f.vel.addScaledVector(acc, dt);
    const prev = f.pos.clone();
    f.pos.addScaledVector(f.vel, dt);
    // gates
    for (const g of this.gates) {
      const side = Math.sign(f.pos.clone().sub(g.centre).dot(g.normal));
      const was = Math.sign(prev.clone().sub(g.centre).dot(g.normal));
      if (side && was && side !== was && !g.hit) {
        const off = f.pos.clone().sub(g.centre);
        off.addScaledVector(g.normal, -off.dot(g.normal));
        if (off.length() < g.r) {
          g.hit = true;
          f.gatesHit++;
          g.ring.material.color.set(0x3ddc84);
        }
      }
    }
    // ground, trees, buildings, edge of the model
    const top = this.topAt(f.pos.x, f.pos.z);
    if (f.pos.y < top + s.radius) {
      const hard = f.vel.length() > 3 || top - this.groundAt(f.pos.x, f.pos.z) > 0.5;
      if (hard && f.armed && thr > 0.02) {
        f.crashes++;
        f.crashMsg = 'Crashed. R (or throttle down) to reset.';
        f.vel.set(0, 0, 0);
        f.armed = false;
      }
      f.pos.y = top + s.radius;
      if (f.vel.y < 0) f.vel.y = 0;
      f.vel.multiplyScalar(0.8);
    }
    if (Math.abs(f.pos.x) > this.half || Math.abs(f.pos.z) > this.half) {
      this.resetDrone();
      f.crashMsg = 'Left the model area: reset.';
    }
  }

  // One stick at a time: watch which axis moves most.
  async mapSticks() {
    const steps = [['roll', 'Move ROLL (aileron) left and right'], ['pitch', 'Move PITCH (elevator) up and down'],
      ['thr', 'Move THROTTLE all the way up and down'], ['yaw', 'Move YAW (rudder) left and right']];
    const map = { inv: [false, false, false, false] };
    const box = el('div', { class: 'p3d-help', style: 'top:auto;bottom:120px;right:8px' });
    this.wrap.append(box);
    for (const [name, text] of steps) {
      box.textContent = `${text}…`;
      const lo = [], hi = [], firstUp = [];
      const t0 = performance.now();
      await new Promise((resolve) => {
        const tick = () => {
          const gp = firstPad();
          if (gp) gp.axes.forEach((v, i) => {
            lo[i] = Math.min(lo[i] ?? v, v);
            hi[i] = Math.max(hi[i] ?? v, v);
            if (firstUp[i] == null && Math.abs(v) > 0.6) firstUp[i] = v;
          });
          if (performance.now() - t0 > 3000) resolve();
          else requestAnimationFrame(tick);
        };
        tick();
      });
      let best = 0;
      hi.forEach((h, i) => { if (h - lo[i] > hi[best] - lo[best]) best = i; });
      map[name] = best;
    }
    // EdgeTX/OpenTX default: stick up/right = positive, except pitch which most HID drivers flip
    map.inv = [false, true, false, false];
    save(AXES_KEY, map);
    this.axes = map;
    box.textContent = `Saved: roll ${map.roll}, pitch ${map.pitch}, throttle ${map.thr}, yaw ${map.yaw}`;
    setTimeout(() => box.remove(), 2500);
  }

  dispose() {
    this.disposed = true;
    this.stopFly();
    this.stopLine();
    this.dot?.geometry.dispose();
    this.dot?.material.dispose();
    window.removeEventListener('keydown', this.onEsc);
    document.body.classList.remove('p3d-full');
    cancelAnimationFrame(this.raf);
    this.resizeObs?.disconnect();
    this.controls?.dispose();
    this.scene?.traverse((o) => {
      o.geometry?.dispose();
      if (o.material) [].concat(o.material).forEach((m) => { m.map?.dispose(); m.dispose(); });
    });
    this.renderer?.dispose();
    this.wrap.remove();
  }
}
