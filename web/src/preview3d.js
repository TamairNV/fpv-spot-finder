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
const AXES_KEY = 'fpvspots:gamepad-axes';

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
  constructor(host, { spot, preview, center, profile, radiusM }) {
    this.host = host;
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
    this.wrap.append(this.canvas, this.tag);
    this.host.append(this.wrap);
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
    this.buildUI();
    this.setTime(this.defaultTime());
    this.tag.textContent = `${this.preview.lidar ? 'EA LiDAR, 2 m blocks' : 'Tree model + building heights (no LiDAR yet)'} · ${n * res} m square`;
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
      m.makeScale(res, h, res);
      m.setPosition((j + 0.5) * res - half, this.g[k], (i + 0.5) * res - half);
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
    this.timeLabel = el('span', { style: 'min-width:120px' });
    const jump = (label, d) => el('button', { onclick: () => this.setTime(d) }, label);
    const mid = (a, b) => new Date((a.getTime() + b.getTime()) / 2);
    const rowSun = el('div', { class: 'p3d-row' }, '☀', this.slider, this.timeLabel);
    const rowJump = el('div', { class: 'p3d-row' },
      jump('Golden AM', mid(t.sunrise, t.goldenHourEnd)), jump('Midday', t.solarNoon),
      jump('Golden PM', mid(t.goldenHour, t.sunset)), jump('Now', new Date()),
      el('span', { style: 'flex:1' }),
      (this.flyBtn = el('button', { onclick: () => (this.fly ? this.stopFly() : this.startFly()) }, 'Fly it')),
      el('button', { onclick: () => this.toggleFull(), title: 'Full screen' }, '⤢'));
    const legend = el('div', { class: 'p3d-row', style: 'justify-content:space-between' },
      el('span', {}, el('b', { style: 'color:#ff3d7f' }, '◯'), ' gaps'),
      el('span', {}, el('b', { style: 'color:#ffc23d' }, '◯'), ' lone trees'),
      el('span', {}, el('b', { style: 'color:#3ddc84' }, '●'), ' take-off'),
      el('span', {}, '- - spot area'));
    this.ui = el('div', { class: 'p3d-ui' }, legend, rowSun, rowJump);
    this.wrap.append(this.ui);
  }

  toggleFull() {
    const on = this.wrap.classList.toggle('full');
    document.body.classList.toggle('p3d-full', on);
    this.resize();
  }

  render() {
    if (this.disposed || !this.renderer) return;
    if (this.fly) return; // the fly loop renders every frame
    cancelAnimationFrame(this.raf);
    this.raf = requestAnimationFrame(() => {
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    });
  }

  // ---------- fly mode

  startFly() {
    const spec = DRONES[this.profile] || DRONES.freestyle5;
    this.controls.enabled = false;
    this.flyBtn.classList.add('on');
    this.flyBtn.textContent = 'Stop flying';
    if (!this.wrap.classList.contains('full')) this.toggleFull();
    this.camera.fov = 85;
    this.camera.near = 0.05;
    this.camera.updateProjectionMatrix();
    this.axes = store(AXES_KEY, { roll: 0, pitch: 1, thr: 2, yaw: 3, inv: [false, true, false, false] });
    this.fly = {
      spec, acro: !!navigator.getGamepads?.()[0], pos: new THREE.Vector3(), vel: new THREE.Vector3(),
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
    this.flyBtn.classList.remove('on');
    this.flyBtn.textContent = 'Fly it';
    this.camera.fov = 45;
    this.camera.near = 0.1;
    this.camera.updateProjectionMatrix();
    this.camera.position.set(170, 190, 230);
    this.controls.enabled = true;
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
    const gp = navigator.getGamepads ? [...navigator.getGamepads()].find(Boolean) : null;
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
          const gp = [...navigator.getGamepads()].find(Boolean);
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
