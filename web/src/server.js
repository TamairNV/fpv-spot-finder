// What the app does when it runs with its server (spotter.tamairnv.co.uk): your 👍 / 👎 and notes
// synced between your devices, a flight log with photos, a week's flying forecast for your spots,
// and a heads-up the evening before a good day. On GitHub Pages there's no /api, so none of this
// shows and the app works as before.
//
// No accounts: the first time there's something to save this device gets a random key; another
// device joins the same profile with a short code (or QR) from the first. Changes queue up offline
// and go when there's signal; each item is last-edit-wins, so two phones can't trample each other.
import qrcode from 'qrcode-generator';
import { $, el, store, save, SANDBOXED } from './util.js';
import './server.css';

const KEY = 'fpvspots:server-key';
const SEEN = 'fpvspots:server-seen';
const api = (path) => new URL(`api/${path}`, location.href.split('#')[0]).href;
const hm = (iso) => iso.slice(11, 16);
const dayName = (iso) => new Date(`${iso.slice(0, 10)}T12:00`).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
const today = () => new Date().toLocaleDateString('en-CA');  // yyyy-mm-dd, local

let S = null;  // the one Server instance, once attached

async function call(path, { method = 'GET', body, form, auth = true, timeout = 15000 } = {}) {
  const headers = {};
  const key = store(KEY, null);
  if (auth && key) headers.Authorization = `Bearer ${key}`;
  if (body) headers['Content-Type'] = 'application/json';
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeout);
  try {
    const res = await fetch(api(path), { method, headers, body: form || (body && JSON.stringify(body)), signal: ctl.signal });
    const js = await res.json().catch(() => ({}));
    if (!res.ok) throw Object.assign(new Error(js.detail || `HTTP ${res.status}`), { status: res.status });
    return js;
  } finally {
    clearTimeout(timer);
  }
}

async function ensureKey() {
  let key = store(KEY, null);
  if (!key) {
    key = (await call('profile', { method: 'POST', auth: false })).key;
    save(KEY, key);
  }
  return key;
}

// Photos waiting for signal, in IndexedDB (too big for localStorage).
const idb = () => new Promise((resolve, reject) => {
  const r = indexedDB.open('fpvspots', 1);
  r.onupgradeneeded = () => r.result.createObjectStore('photos', { keyPath: 'id' });
  r.onsuccess = () => resolve(r.result);
  r.onerror = () => reject(r.error);
});
async function idbDo(mode, fn) {
  const db = await idb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('photos', mode);
    const out = fn(tx.objectStore('photos'));
    tx.oncomplete = () => resolve(out?.result ?? out);
    tx.onerror = () => reject(tx.error);
  });
}

class Server {
  constructor({ ratings, region, meta, coords, getProfile, onChange, openSpot }) {
    Object.assign(this, { ratings, region, meta, coords, getProfile, onChange, openSpot });
    const k = (s) => `fpvspots:${region}:${s}`;
    this.keys = { outbox: k('outbox'), rev: k('rev'), flights: k('flights'), photos: k('photos'), at: k('synced-at') };
    this.outbox = store(this.keys.outbox, {});
    this.flights = store(this.keys.flights, {});
    this.photos = store(this.keys.photos, {});
    this.pending = [];  // photos queued in IndexedDB: [{id, flight, url}]
    this.boxes = new Set();  // redraw functions of the flight boxes on open spot cards
    this.status = 'idle';
    this.wrapRatings();
    // first sync sends everything; after that, anything edited while the server wasn't listening yet
    this.queueEverything(store(this.keys.rev, null) == null ? 0 : store(this.keys.at, 0));
  }

  // ---- sync

  wrapRatings() {
    const r = this.ratings;
    const set = r.set.bind(r), setNote = r.setNote.bind(r);
    r.set = (profile, id, v, extra = {}) => {
      set(profile, id, v, extra);
      const k = `${profile}/${id}`, item = r.data[k];
      this.queue('rating', k, item ? { ...item, ll: this.coords(profile, id) } : null, item?.t || Date.now());
      this.fcDirty = true;  // the forecast follows your 👍 spots
    };
    r.setNote = (id, text, extra = {}) => {
      setNote(id, text, extra);
      const item = r.notes[id];
      this.queue('note', id, item || null, item?.t || Date.now());
    };
  }

  queueEverything(since = 0) {
    const add = (kind, k, value) => {
      if ((value.t || 1) > since) this.outbox[`${kind}|${k}`] = { kind, k, value, t: value.t || 1 };
    };
    for (const [k, item] of Object.entries(this.ratings.data)) {
      const [profile, ...rest] = k.split('/');
      add('rating', k, { ...item, ll: this.coords(profile, rest.join('/')) });
    }
    for (const [k, item] of Object.entries(this.ratings.notes)) add('note', k, item);
    for (const [k, item] of Object.entries(this.flights)) add('flight', k, item);
    save(this.keys.outbox, this.outbox);
  }

  queue(kind, k, value, t) {
    this.outbox[`${kind}|${k}`] = { kind, k, value, t };
    save(this.keys.outbox, this.outbox);
    clearTimeout(this.soon);
    this.soon = setTimeout(() => this.sync(), 1200);
  }

  async sync() {
    if (this.busy) return (this.again = true);
    const changes = Object.values(this.outbox);
    if (!store(KEY, null) && !changes.length) return;  // nothing to save yet: no profile needed
    this.busy = true;
    this.setStatus('syncing');
    const started = Date.now();
    try {
      await ensureKey();
      const res = await call('sync', { method: 'POST', body: { region: this.region, rev: store(this.keys.rev, 0), changes } });
      // drop what was sent, unless it was edited again while in flight
      for (const c of changes) {
        const id = `${c.kind}|${c.k}`;
        if (this.outbox[id]?.t === c.t) delete this.outbox[id];
      }
      save(this.keys.outbox, this.outbox);
      const changed = this.apply(res.items);
      save(this.keys.rev, res.rev);
      save(this.keys.at, started);
      const photosChanged = JSON.stringify(res.photos) !== JSON.stringify(this.photos);
      this.photos = res.photos || {};
      save(this.keys.photos, this.photos);
      await this.sendPhotos();
      this.setStatus('ok');
      if (changed || photosChanged) this.changed();
      if (this.fcDirty || changed) {
        this.fcDirty = false;
        this.loadForecast();
      }
      if (!this.rerank) this.loadRerank();
      else this.renderBar();  // the re-rank offer counts your ratings
    } catch (e) {
      this.setStatus(e.status === 401 ? 'lost' : 'offline');
      if (e.status === 401) {  // the key's gone (server reset): start a new profile with everything on here
        save(KEY, null);
        forgetRevs();
        this.queueEverything();
      }
    } finally {
      this.busy = false;
      if (this.again) {
        this.again = false;
        this.sync();
      }
    }
  }

  apply(items) {
    const r = this.ratings;
    let changed = false;
    for (const it of items) {
      if (this.outbox[`${it.kind}|${it.k}`]?.t > it.t) continue;  // our unsent edit is newer
      const into = { rating: r.data, note: r.notes, flight: this.flights }[it.kind];
      if (!into) continue;
      const cur = into[it.k];
      if (cur && (cur.t || 0) > it.t) continue;
      if (it.value == null) {
        if (!cur) continue;
        delete into[it.k];
      } else into[it.k] = { ...it.value, t: it.t };
      changed = true;
    }
    if (changed) {
      save(r.key, r.data);
      save(r.notesKey, r.notes);
      save(this.keys.flights, this.flights);
      // a note written on another device, for the spot open here
      const area = document.querySelector('.note-text');
      const open = location.hash.slice(1).split(/[./]/)[1];
      if (area && open && document.activeElement !== area) area.value = r.notes[open]?.text || '';
    }
    return changed;
  }

  setStatus(s) {
    this.status = s;
    this.renderBar();
  }

  // ---- flights

  flightsAt(spotId) {
    return Object.entries(this.flights).filter(([, f]) => f.spot === spotId)
      .map(([id, f]) => ({ id, ...f })).sort((a, b) => b.date.localeCompare(a.date) || b.t - a.t);
  }

  allFlights() {
    return Object.entries(this.flights).map(([id, f]) => ({ id, ...f })).sort((a, b) => b.date.localeCompare(a.date) || b.t - a.t);
  }

  saveFlight(id, value) {
    const t = Date.now();
    if (value) this.flights[id] = { ...value, t };
    else delete this.flights[id];
    save(this.keys.flights, this.flights);
    this.queue('flight', id, value ? { ...value } : null, t);
  }

  async addPhotos(flightId, files) {
    for (const file of files) {
      const id = crypto.randomUUID();
      await idbDo('readwrite', (s) => s.put({ id, flight: flightId, blob: file })).catch(() => {});
      this.pending.push({ id, flight: flightId, url: URL.createObjectURL(file) });
    }
    this.changed();
    this.sendPhotos().then(() => this.changed());
  }

  async loadPending() {
    const rows = await idbDo('readonly', (s) => s.getAll()).catch(() => []);
    this.pending = (rows || []).map((r) => ({ id: r.id, flight: r.flight, url: URL.createObjectURL(r.blob) }));
  }

  async sendPhotos() {
    if (this.sending || !this.pending.length) return;
    this.sending = true;
    try {
      await ensureKey();
      for (const p of [...this.pending]) {
        const row = await idbDo('readonly', (s) => s.get(p.id));
        if (!row) continue;
        const form = new FormData();
        form.append('file', row.blob, 'photo.jpg');
        try {
          const info = await call(`photos?region=${encodeURIComponent(this.region)}&flight=${encodeURIComponent(p.flight)}`,
            { method: 'POST', form, timeout: 120000 });
          (this.photos[p.flight] ||= []).push(info);
        } catch (e) {
          if (!e.status || e.status >= 500) throw e;  // no signal: try again later
          alert(`That photo couldn't be added: ${e.message}`);  // a real refusal (not an image, too big)
        }
        await idbDo('readwrite', (s) => s.delete(p.id));
        URL.revokeObjectURL(p.url);
        this.pending = this.pending.filter((x) => x.id !== p.id);
      }
      save(this.keys.photos, this.photos);
    } catch {
      /* offline: they stay queued */
    } finally {
      this.sending = false;
    }
  }

  async deletePhoto(flightId, photoId) {
    await call(`photos/${photoId}`, { method: 'DELETE' });
    this.photos[flightId] = (this.photos[flightId] || []).filter((p) => p.id !== photoId);
    save(this.keys.photos, this.photos);
    this.changed();
  }

  changed() {
    for (const draw of this.boxes) draw.isConnected() ? draw() : this.boxes.delete(draw);
    this.renderBar();
    this.onChange?.();
  }

  // ---- re-rank the whole region with your ratings (the pipeline's scorer, on the server)

  get mineKey() {
    return `fpvspots:${this.region}:mine`;
  }

  async loadRerank() {
    if (!store(KEY, null)) return;
    try {
      this.rerank = await call(`rerank?region=${encodeURIComponent(this.region)}`);
    } catch {
      return;
    }
    const st = this.rerank.job?.state;
    if (st === 'queued' || st === 'running') {
      this.rrWatching = true;
      clearTimeout(this.rrTimer);
      this.rrTimer = setTimeout(() => this.loadRerank(), 3000);
    } else if (this.rrWatching) {
      this.rrWatching = false;
      if (st === 'done' && this.rerank.copy) return this.useMine(this.rerank.copy.path);
      if (st === 'failed') this.toast('Re-ranking didn’t work this time. The standard ranking is still here.');
    }
    this.renderBar();
  }

  async startRerank() {
    this.rerank = { ...this.rerank, job: { state: 'queued' } };
    this.renderBar();
    try {
      await this.sync();  // the server ranks with what it has: send the latest first
      this.rerank = await call('rerank', { method: 'POST', body: { region: this.region } });
      this.rrWatching = true;
      this.loadRerank();
    } catch (e) {
      this.rerank = { ...this.rerank, job: null };
      this.renderBar();
      alert(e.message);
    }
  }

  useMine(path) {
    save(this.mineKey, path);
    location.reload();
  }

  rerankRow() {
    const rr = this.rerank;
    if (!rr) return null;
    const st = rr.job?.state;
    const using = store(this.mineKey, null);
    const votes = Object.values(this.ratings.data);
    const up = votes.filter((r) => r.v > 0).length, down = votes.filter((r) => r.v < 0).length;
    const when = (iso) => (iso ? new Date(iso).toLocaleString('en-GB', { weekday: 'short', hour: '2-digit', minute: '2-digit' }) : '');
    const btn = (text, fn, title) => el('button', { class: 'linklike', onclick: fn, title }, text);
    const sep = () => el('span', { class: 'sep' }, '·');
    if (st === 'queued' || st === 'running') return el('div', { class: 'srv-row rr' }, el('span', {}, '⏳ Re-ranking the whole region with your ratings… (a minute or two)'));
    if (using && rr.copy) {
      const made = Object.values(rr.copy.ratings || {}).reduce((a, r) => [a[0] + (r.up || 0), a[1] + (r.down || 0)], [0, 0]);
      return el('div', { class: 'srv-row rr on' }, el('span', {}, `★ Your ranking (${made[0]} 👍, ${made[1]} 👎, ${when(rr.copy.made)})`), sep(),
        btn('Update', () => this.startRerank(), 'Re-rank again with your latest ratings'), sep(),
        btn('Standard ranking', () => { save(this.mineKey, null); location.reload(); }));
    }
    if (rr.copy) {
      return el('div', { class: 'srv-row rr' }, btn('★ Use your ranking', () => this.useMine(rr.copy.path), `Made ${when(rr.copy.made)}`), sep(),
        btn('Update it', () => this.startRerank()));
    }
    if (rr.ready && up + down >= 2) {
      return el('div', { class: 'srv-row rr' }, btn(`★ Re-rank the whole region with my ratings (${up} 👍, ${down} 👎)`, () => this.startRerank(),
        'The server re-scores every 50 m square, steered towards places like your 👍 and away from your 👎'));
    }
    return null;
  }

  // ---- the bar above the list: forecast, sync status, flights

  mountBar() {
    this.bar = el('div', { class: 'srv-bar' });
    const now = $('#now-box');
    now.after(this.bar);
    this.renderBar();
  }

  renderBar() {
    if (!this.bar) return;
    const fc = this.forecast?.drone === this.getProfile() ? this.forecast : null;
    const n = this.allFlights().length;
    const syncText = { syncing: 'Syncing…', ok: 'Synced', offline: 'Offline: will sync', lost: 'Not synced', idle: 'On this device' }[this.status];
    let fcLine;
    if (this.fcError) fcLine = el('span', { class: 'muted' }, 'Forecast unavailable right now');
    else if (!fc) fcLine = el('span', { class: 'muted' }, 'Loading the week’s forecast…');
    else if (fc.next) {
      const nx = fc.next;
      const when = nx.start.slice(0, 10) === today() ? 'today' : dayName(nx.start);
      const where = fc.mine && nx.spots[0]?.title ? ` at ${nx.spots[0].title}${nx.n_spots > 1 ? ` +${nx.n_spots - 1}` : ''}` : '';
      fcLine = el('span', {}, el('b', {}, 'Next good window: '), `${when} ${hm(nx.start)}–${hm(nx.end)}${where}`);
    } else fcLine = el('span', {}, el('b', {}, 'No calm, dry daylight window '), 'in the next week for this drone');
    this.bar.replaceChildren(
      el('button', { class: 'srv-fc', onclick: () => this.openForecast() }, el('span', { class: 'ico' }, '🌤'), fcLine, el('span', { class: 'chev' }, '›')),
      el('div', { class: 'srv-row' },
        el('button', { class: 'linklike', onclick: () => this.openFlights() }, n ? `✈ Your flights (${n})` : '✈ Flight log'),
        el('span', { class: 'sep' }, '·'),
        el('button', { class: 'linklike', onclick: () => this.openDevices(), title: 'Sync your ratings, notes and flights with your other devices' },
          `${{ ok: '☁︎', syncing: '☁︎', offline: '⚠︎', lost: '⚠︎', idle: '☁︎' }[this.status]} ${syncText}`),
        el('span', { class: 'sep' }, '·'),
        el('button', { class: 'linklike', onclick: () => this.openDevices() }, 'Link a device')),
      this.rerankRow() || '');
  }

  // ---- forecast

  async loadForecast() {
    const drone = this.getProfile();
    const [lon, lat] = this.meta.region.center;
    try {
      this.forecast = await call(`forecast?region=${this.region}&drone=${drone}&lon=${lon}&lat=${lat}`);
      this.fcError = false;
    } catch {
      this.fcError = true;
    }
    this.renderBar();
    if (this.dialog?.open && this.dialog.dataset.view === 'forecast') this.openForecast();
  }

  openForecast() {
    const fc = this.forecast?.drone === this.getProfile() ? this.forecast : null;
    const who = this.meta.profiles[this.getProfile()]?.short || this.getProfile();
    const body = [el('h2', {}, 'Flying forecast')];
    if (!fc) body.push(el('p', { class: 'muted' }, this.fcError ? 'The forecast can’t be loaded right now.' : 'Loading…'));
    else {
      body.push(el('p', { class: 'muted small' }, fc.mine
        ? `Calm, dry daylight hours at your 👍 spots and the spots you’ve flown, for the ${who}: gusts under ${fc.limits.gust} mph, wind under ${fc.limits.wind}, rain under 30%.`
        : `For the middle of the region, for the ${who}: gusts under ${fc.limits.gust} mph, wind under ${fc.limits.wind}, rain under 30%. 👍 some spots and it forecasts those instead.`));
      body.push(el('ol', { class: 'srv-days' }, fc.days.map((d) => el('li', { class: d.best ? 'good' : '' },
        el('div', { class: 'd-head' },
          el('b', {}, d.date === today() ? 'Today' : dayName(d.date)),
          d.best ? el('span', {}, `${hm(d.best.start)}–${hm(d.best.end)} · gusts ≤${d.best.gust} mph · rain ≤${d.best.rain}%`)
            : el('span', { class: 'muted' }, 'no good window')),
        d.strip.length ? el('div', { class: 'srv-strip' }, d.strip.map((h) => el('i', { class: h.cls, title: `${h.h}:00 · gusts ${h.gust} mph · rain ${h.rain}%` }, h.h))) : null,
        fc.mine && d.spots.length ? el('div', { class: 'd-spots' }, d.spots.map((s) => el('button', {
          class: 'chip', onclick: () => { this.dialog.close(); this.openSpot(s.drone || this.getProfile(), s.id); },
        }, s.title)), d.n_spots > d.spots.length ? el('span', { class: 'muted small' }, ` +${d.n_spots - d.spots.length} more`) : null) : null))));
      body.push(el('p', { class: 'muted small' }, `From Open-Meteo, updated ${fc.updated.slice(11)}. Limits are a judgement call; check on the day.`));
    }
    body.push(this.notifyBox());
    this.showDialog('forecast', body);
  }

  notifyBox() {
    const box = el('div', { class: 'box' }, el('h4', {}, 'Heads-up the evening before'));
    const can = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
    if (!can) {
      const ios = /iPhone|iPad/.test(navigator.userAgent);
      box.append(el('p', { class: 'muted small' }, ios
        ? 'On an iPhone, add the app to your home screen first (Share, then Add to Home Screen), open it from there, and turn this on in that copy.'
        : 'This browser can’t do notifications.'));
      return box;
    }
    const btn = el('button', { class: 'btn' }, '…');
    const msg = el('p', { class: 'muted small' }, `At 6:30 pm, if tomorrow has a calm, dry window at your spots for the ${this.meta.profiles[this.getProfile()]?.short || 'drone'}, you get a notification. Nothing on bad days.`);
    box.append(msg, btn);
    const refresh = async () => {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      let on = false;
      if (sub && store(KEY, null)) on = (await call(`push/status?endpoint=${encodeURIComponent(sub.endpoint)}`).catch(() => ({}))).on;
      btn.textContent = on ? '🔕 Turn off' : '🔔 Turn on';
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          if (on) {
            await call('push/unsubscribe', { method: 'POST', body: { endpoint: sub.endpoint } });
            await sub.unsubscribe();
          } else {
            if (await Notification.requestPermission() !== 'granted') throw new Error('Notifications are blocked for this site in the browser settings.');
            await ensureKey();
            const { key } = await call('push/key');
            const s = sub || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64(key) });
            const [lon, lat] = this.meta.region.center;
            await call('push/subscribe', { method: 'POST', body: { region: this.region, drone: this.getProfile(), sub: s.toJSON(), lon, lat } });
          }
        } catch (e) {
          alert(e.message || e);
        }
        btn.disabled = false;
        refresh();
      };
    };
    refresh().catch(() => { btn.textContent = 'Notifications unavailable'; btn.disabled = true; });
    return box;
  }

  // ---- devices

  openDevices() {
    const body = [el('h2', {}, 'Your devices')];
    body.push(el('p', {}, 'Your 👍 / 👎, notes and flight log sync between every device you link. No account: a code from this device lets another one join.'));
    const out = el('div', { class: 'srv-link' });
    const make = el('button', { class: 'btn' }, 'Show a code for another device');
    make.onclick = async () => {
      make.disabled = true;
      try {
        await ensureKey();
        await this.sync();
        const { code } = await call('link', { method: 'POST' });
        const url = `${location.origin}${location.pathname}?region=${this.region}#link=${code}`;
        const qr = qrcode(0, 'M');
        qr.addData(url);
        qr.make();
        out.replaceChildren(
          el('div', { class: 'qr', html: qr.createSvgTag({ cellSize: 4, margin: 2, scalable: true }) }),
          el('div', {}, el('p', {}, 'Scan this with the other phone’s camera, or type the code there:'),
            el('div', { class: 'code' }, `${code.slice(0, 4)}-${code.slice(4)}`),
            el('p', { class: 'muted small' }, 'Works once, for 10 minutes.')));
      } catch (e) {
        out.replaceChildren(el('p', { class: 'muted' }, `Can’t make a code right now (${e.message}).`));
      }
      make.disabled = false;
    };
    const input = el('input', { class: 'srv-input', placeholder: 'ABCD-EFGH', autocomplete: 'off', autocapitalize: 'characters', 'aria-label': 'Code from your other device' });
    const join = el('button', { class: 'btn', onclick: () => this.claim(input.value, join) }, 'Join');
    body.push(el('div', { class: 'box' }, el('h4', {}, 'Add another device'), make, out));
    body.push(el('div', { class: 'box' }, el('h4', {}, 'Have a code from your other device?'), el('div', { class: 'srv-join' }, input, join),
      el('p', { class: 'muted small' }, 'What’s on this device is kept and merged in.')));
    body.push(el('p', { class: 'muted small', id: 'srv-devices' }));
    this.showDialog('devices', body);
    if (store(KEY, null)) {
      call('me').then((m) => {
        const c = m.counts[this.region] || {};
        const n = (x, w) => `${x || 0} ${w}${x === 1 ? '' : 's'}`;
        $('#srv-devices').textContent = `${n(m.devices, 'device')} linked · here: ${n(c.rating, 'rating')}, ${n(c.note, 'note')}, ${n(c.flight, 'flight')}.`;
      }).catch(() => {});
    }
  }

  async claim(code, btn) {
    if (!code.trim()) return;
    if (btn) btn.disabled = true;
    try {
      const { key } = await call('link/claim', { method: 'POST', body: { code }, auth: false });
      save(KEY, key);
      // a new profile for this device: start from rev 0 and send everything we have so it merges in
      forgetRevs();  // every region re-sends what it has the next time it's opened
      this.queueEverything();
      await this.sync();
      this.dialog?.close();
      this.loadForecast();
      this.toast('Linked: this device now syncs with your others.');
    } catch (e) {
      alert(e.message);
    }
    if (btn) btn.disabled = false;
  }

  // ---- flights list

  openFlights() {
    const flights = this.allFlights();
    const body = [el('h2', {}, 'Your flights')];
    if (!flights.length) {
      body.push(el('p', {}, 'Open a spot and press “Log a flight” after you’ve flown it: the date, a note, and photos from your phone. They sync to your other devices, and the forecast keeps an eye on the spots you’ve flown.'));
    } else {
      body.push(el('ol', { class: 'srv-flights' }, flights.map((f) => el('li', {},
        el('button', { class: 'linklike', onclick: () => { this.dialog.close(); this.openSpot(f.drone, f.spot); } }, f.title || f.spot),
        el('span', { class: 'muted' }, ` · ${dayName(f.date)}${f.drone ? ` · ${this.meta.profiles[f.drone]?.short || f.drone}` : ''}`),
        f.note ? el('div', { class: 'small' }, f.note) : null,
        this.thumbs(f.id, false)))));
    }
    this.showDialog('flights', body);
  }

  thumbs(flightId, editable) {
    const shots = (this.photos[flightId] || []).map((p) => ({ ...p, src: api(`photos/${p.id}_t.jpg`), full: api(`photos/${p.id}.jpg`) }));
    const waiting = this.pending.filter((p) => p.flight === flightId).map((p) => ({ src: p.url, full: p.url, waiting: true }));
    if (!shots.length && !waiting.length) return null;
    return el('div', { class: 'srv-thumbs' }, [...shots, ...waiting].map((p) => el('span', { class: `ph${p.waiting ? ' waiting' : ''}` },
      el('a', { href: p.full, target: '_blank', rel: 'noopener' }, el('img', { src: p.src, alt: 'Flight photo', loading: 'lazy' })),
      p.waiting ? el('i', { title: 'Uploads when there’s signal' }, '⏳') : null,
      editable && !p.waiting ? el('button', {
        class: 'x', 'aria-label': 'Delete photo',
        onclick: () => confirm('Delete this photo?') && this.deletePhoto(flightId, p.id).catch((e) => alert(e.message)),
      }, '×') : null)));
  }

  // ---- shared bits

  showDialog(view, body) {
    if (!this.dialog) {
      this.dialog = el('dialog', { class: 'srv-dialog' });
      document.body.append(this.dialog);
      this.dialog.addEventListener('click', (e) => { if (e.target === this.dialog) this.dialog.close(); });
    }
    this.dialog.dataset.view = view;
    this.dialog.replaceChildren(el('form', { method: 'dialog' }, el('button', { class: 'dlg-close', 'aria-label': 'Close' }, '×')), ...body.filter(Boolean));
    if (!this.dialog.open) this.dialog.showModal();
  }

  toast(text) {
    const t = el('div', { class: 'srv-toast', role: 'status' }, text);
    document.body.append(t);
    setTimeout(() => t.remove(), 4000);
  }
}

function forgetRevs() {
  try {
    for (const k of Object.keys(localStorage)) if (/^fpvspots:[^:]+:rev$/.test(k)) localStorage.removeItem(k);
  } catch { /* no storage */ }
}

function b64(s) {
  const pad = '='.repeat((4 - (s.length % 4)) % 4);
  const raw = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

// The flight log box on a spot card. Null when there's no server.
export function flightBox(spot, profile, rerender) {
  if (!S) return null;
  const p = spot.properties;
  const box = el('div', { class: 'box srv-flightbox' });
  const draw = () => {
    const flights = S.flightsAt(p.id);
    const form = el('form', { class: 'srv-form', hidden: true });
    const date = el('input', { type: 'date', value: today(), max: today(), required: true, 'aria-label': 'Date flown' });
    const note = el('textarea', { rows: 2, placeholder: 'How was it? Wind, people about, the best line…', 'aria-label': 'Flight note' });
    const files = el('input', { type: 'file', accept: 'image/*', multiple: true, 'aria-label': 'Photos' });
    form.append(
      el('label', {}, 'When ', date),
      note,
      el('label', { class: 'files' }, '📷 Photos ', files),
      el('div', { class: 'row' }, el('button', { class: 'btn', type: 'submit' }, 'Save'),
        el('button', { class: 'linklike', type: 'button', onclick: () => { form.hidden = true; add.hidden = false; } }, 'Cancel')));
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const id = crypto.randomUUID();
      S.saveFlight(id, {
        spot: p.id, drone: profile, title: p.title, ll: spot.geometry.coordinates, date: date.value || today(), note: note.value.trim(),
      });
      if (files.files.length) await S.addPhotos(id, [...files.files]);
      draw();
      rerender?.();
    });
    const add = el('button', { class: 'btn', onclick: () => { form.hidden = false; add.hidden = true; date.focus(); } }, '✈ Log a flight');
    box.replaceChildren(
      el('h4', {}, flights.length ? `Your flights here (${flights.length})` : 'Flown it?'),
      flights.length ? el('ol', { class: 'srv-flights' }, flights.map((f) => {
        const more = el('input', { type: 'file', accept: 'image/*', multiple: true, hidden: true });
        more.addEventListener('change', async () => {
          await S.addPhotos(f.id, [...more.files]);
          draw();
        });
        return el('li', {},
          el('b', {}, dayName(f.date)), f.drone && f.drone !== profile ? el('span', { class: 'muted' }, ` · ${S.meta.profiles[f.drone]?.short || f.drone}`) : null,
          f.note ? el('div', { class: 'small' }, f.note) : null,
          S.thumbs(f.id, true),
          el('div', { class: 'row' },
            el('button', { class: 'linklike', onclick: () => more.click() }, '+ photos'), more,
            el('button', { class: 'linklike del', onclick: () => { if (confirm('Delete this flight and its photos from the log?')) { (S.photos[f.id] || []).forEach((ph) => S.deletePhoto(f.id, ph.id).catch(() => {})); S.saveFlight(f.id, null); draw(); rerender?.(); } } }, 'Delete')));
      })) : el('p', { class: 'muted small' }, 'Log it with a note and photos: it syncs to your other devices, and the forecast keeps an eye on spots you’ve flown.'),
      add, form);
  };
  // a sync or an upload finishing redraws it, unless you're in the middle of logging a flight
  const redraw = () => {
    const form = box.querySelector('.srv-form');
    if ((form && !form.hidden) || box.contains(document.activeElement)) return;
    draw();
  };
  redraw.isConnected = () => box.isConnected;
  draw();
  S.boxes.add(redraw);
  return box;
}

// A spot's 3D model and line, built by the server the first time anyone opens it. null without a server.
export async function fetchModel(region, profile, id) {
  if (!S || !region || !/^\d+-\d+$/.test(id || '')) return null;
  const m = await call(`models/${region}/${profile}/${id}`, { auth: false, timeout: 60000 });
  return { ...m, url: new URL(m.preview.file, location.href.split('#')[0]).href };
}

// Called once the spots are loaded. Does nothing if there's no server behind the app.
export async function attachServer(opts) {
  if (SANDBOXED) return null;
  let up = false;
  try {
    up = !!(await call('health', { auth: false, timeout: 4000 })).ok;
    save(SEEN, true);
  } catch {
    up = store(SEEN, false);  // offline in the field, but this app has a server: queue things for later
  }
  if (!up) return null;
  S = new Server(opts);
  await S.loadPending();
  S.mountBar();
  S.loadForecast();
  const m = location.hash.match(/^#link=([A-Z0-9-]+)$/i);
  if (m) {
    history.replaceState(null, '', location.pathname + location.search);
    S.claim(m[1]);
  } else S.sync();
  const kick = () => document.visibilityState === 'visible' && S.sync();
  document.addEventListener('visibilitychange', kick);
  window.addEventListener('online', kick);
  setInterval(kick, 120000);
  return {
    profileChanged: () => { S.renderBar(); S.loadForecast(); },
  };
}
