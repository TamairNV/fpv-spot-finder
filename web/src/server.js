// What the app does when it runs with its server (spotter.tamairnv.co.uk): your 👍 / 👎 and notes
// synced between your devices, a flight log with photos, a week's flying forecast for your spots,
// and a heads-up the evening before a good day. On GitHub Pages there's no /api, so none of this
// shows and the app works as before.
//
// The first time there's something to save this device gets a random key; another device joins the
// same profile with a short code (or QR) from the first, or you make an account (username and
// password) and sign in anywhere. Changes queue up offline and go when there's signal; each item is
// last-edit-wins, so two phones can't trample each other. Star reviews from accounts are public.
import qrcode from 'qrcode-generator';
import { $, el, store, save, SANDBOXED } from './util.js';
import { theme } from './theme.js';
import './server.css';

const KEY = 'fpvspots:server-key';
const SEEN = 'fpvspots:server-seen';
const USER = 'fpvspots:account';  // the username this device is signed in as, if any
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
  constructor({ ratings, region, meta, coords, getProfile, onChange, openSpot, pageHost }) {
    this.pageHost = pageHost || null;  // account and flights show as pages of the app when it gives us one
    Object.assign(this, { ratings, region, meta, coords, getProfile, onChange, openSpot });
    const k = (s) => `fpvspots:${region}:${s}`;
    this.keys = { outbox: k('outbox'), rev: k('rev'), flights: k('flights'), photos: k('photos'), at: k('synced-at') };
    this.outbox = store(this.keys.outbox, {});
    this.flights = store(this.keys.flights, {});
    this.photos = store(this.keys.photos, {});
    this.pending = [];  // photos queued in IndexedDB: [{id, flight, url}]
    this.boxes = new Set();  // redraw functions of the flight boxes on open spot cards
    this.status = 'idle';
    this.reviewCache = new Map();  // "<drone>/<id>" -> other people's reviews, as last fetched
    this.summary = {};  // drone -> {spot id: [average stars, count]}
    this.wrapRatings();
    // first sync sends everything; after that, anything edited while the server wasn't listening yet
    this.queueEverything(store(this.keys.rev, null) == null ? 0 : store(this.keys.at, 0));
  }

  // On the one big map the app follows you from region to region without a reload: finish what's in
  // flight for the old region (its outbox stays saved under its own keys), then pick up the new one's.
  async retarget({ ratings, region, meta }) {
    if (region === this.region) return;
    clearTimeout(this.soon);
    clearTimeout(this.rrTimer);
    this.rrWatching = false;
    for (let i = 0; (this.busy || this.sending) && i < 100; i++) await new Promise((r) => setTimeout(r, 100));
    Object.assign(this, { ratings, region, meta });
    const k = (s) => `fpvspots:${region}:${s}`;
    this.keys = { outbox: k('outbox'), rev: k('rev'), flights: k('flights'), photos: k('photos'), at: k('synced-at') };
    this.outbox = store(this.keys.outbox, {});
    this.flights = store(this.keys.flights, {});
    this.photos = store(this.keys.photos, {});
    this.reviewCache = new Map();
    this.summary = {};
    this.rerank = null;
    this.forecast = null;
    this.wrapRatings();
    this.queueEverything(store(this.keys.rev, null) == null ? 0 : store(this.keys.at, 0));
    this.renderBar();
    this.loadForecast();
    this.sync();
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
    const setSaved = r.setSaved.bind(r), setReview = r.setReview.bind(r), setPlace = r.setPlace.bind(r);
    r.setSaved = (profile, id, on, extra = {}) => {
      setSaved(profile, id, on, extra);
      const k = `${profile}/${id}`, item = r.saved[k];
      this.queue('saved', k, item ? { ...item, ll: item.ll || this.coords(profile, id) } : null, item?.t || Date.now());
      this.fcDirty = true;  // the forecast watches saved spots too
    };
    r.setReview = (profile, id, value, extra = {}) => {
      setReview(profile, id, value, extra);
      const k = `${profile}/${id}`, item = r.reviews[k];
      this.queue('review', k, item || null, item?.t || Date.now());
      this.reviewCache.delete(k);
    };
    r.setPlace = (id, value) => {
      setPlace(id, value);
      const item = r.places[id];
      this.queue('place', id, item || null, item?.t || Date.now());
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
    for (const [k, item] of Object.entries(this.ratings.saved)) add('saved', k, item);
    for (const [k, item] of Object.entries(this.ratings.reviews)) add('review', k, item);
    for (const [k, item] of Object.entries(this.ratings.places)) add('place', k, item);
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
      if (e.status === 401) {  // the key's gone (signed out elsewhere, password changed, server reset)
        const was = store(USER, null);
        save(KEY, null);
        save(USER, null);
        if (was) {  // what's on here stays, and merges back in when you sign in again
          this.toast(`Signed out of ${was}: sign in again to sync with your account.`);
          this.renderAccountBtn();
        }
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
      const into = { rating: r.data, note: r.notes, flight: this.flights, saved: r.saved, place: r.places, review: r.reviews }[it.kind];
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
      save(r.savedKey, r.saved);
      save(r.placesKey, r.places);
      save(r.reviewsKey, r.reviews);
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
        el('button', { class: 'linklike', onclick: () => this.openAccount(), title: 'Sync your ratings, saved spots, notes and flights with your other devices' },
          `${{ ok: '☁︎', syncing: '☁︎', offline: '⚠︎', lost: '⚠︎', idle: '☁︎' }[this.status]} ${syncText}`),
        el('span', { class: 'sep' }, '·'),
        el('button', { class: 'linklike', onclick: () => this.openAccount() }, store(USER, null) ? `👤 ${store(USER, null)}` : this.health?.features?.includes('accounts') ? '👤 Sign in' : 'Link a device')),
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
          class: 'chip', onclick: () => { this.closeView(); this.openSpot(s.drone || this.getProfile(), s.id); },
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

  // ---- account

  renderAccountBtn() {
    const btn = $('#btn-account');
    if (!btn) return;
    const user = store(USER, null);
    btn.hidden = false;
    btn.classList.toggle('signed-in', !!user);
    btn.title = user ? `Signed in as ${user}` : 'Sign in or make an account';
    const accounts = this.health?.features?.includes('accounts');
    btn.querySelector('.lbl').textContent = user || (accounts ? 'Sign in' : 'Devices');
    this.renderBar();
  }

  openAccount(mode) {
    const user = store(USER, null);
    if (user) return this.openSignedIn(user);
    if (!this.health?.features?.includes('accounts')) {
      return this.showDialog('account', [el('h2', {}, 'Your devices'), ...this.linkBits()]);
    }
    mode ||= 'signup';
    if (mode === 'forgot') return this.openForgot();
    const signup = mode === 'signup';
    const body = [el('h2', {}, signup ? 'Make an account' : 'Sign in')];
    body.push(el('p', { class: 'muted' }, signup
      ? 'Keeps your saved spots, star ratings, notes and flight log, and lets you sign in on any phone or computer. Your star reviews show to other pilots under your username; notes and your email stay private.'
      : 'Whatever is on this device merges into your account.'));
    const name = el('input', { class: 'srv-input plain', name: 'username', autocomplete: signup ? 'username' : 'username', placeholder: signup ? 'Username (shown on your reviews)' : 'Username or email', required: true, minlength: 3, maxlength: signup ? 24 : 254, autocapitalize: 'none', spellcheck: 'false' });
    const email = signup ? el('input', { class: 'srv-input plain', type: 'email', name: 'email', autocomplete: 'email', placeholder: 'Email (for confirming and password resets)', required: true, maxlength: 254 }) : null;
    const pw = el('input', { class: 'srv-input plain', type: 'password', name: 'password', autocomplete: signup ? 'new-password' : 'current-password', placeholder: signup ? 'Password (10+ characters)' : 'Password', required: true, minlength: signup ? 10 : 1 });
    const human = signup ? this.captcha() : null;
    const err = el('p', { class: 'srv-err', role: 'alert' });
    const go = el('button', { class: 'btn', type: 'submit' }, signup ? 'Make account' : 'Sign in');
    const form = el('form', { class: 'srv-acct' }, name, email, pw, human?.node, err, go);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      go.disabled = true;
      err.textContent = '';
      try {
        if (signup) {
          await this.signup(name.value.trim(), email.value.trim(), pw.value, human?.token());
          this.showDialog('account', [el('h2', {}, 'Check your email'),
            el('p', {}, `We’ve sent a link to ${email.value.trim()}. Open it to finish making your account (it works for 48 hours).`),
            el('p', { class: 'muted small' }, 'Nothing there? Check spam. Signing in sends the link again.')]);
        } else {
          await this.login(name.value.trim(), pw.value);
          this.closeView();
        }
      } catch (ex) {
        err.textContent = ex.message;
        human?.reset();
      }
      go.disabled = false;
    });
    body.push(form);
    body.push(el('p', { class: 'small srv-alt' },
      el('button', { class: 'linklike', onclick: () => this.openAccount(signup ? 'login' : 'signup') }, signup ? 'I have an account' : 'Make an account'),
      signup ? null : el('span', { class: 'sep' }, ' · '),
      signup ? null : el('button', { class: 'linklike', onclick: () => this.openAccount('forgot') }, 'Forgot password?')));
    body.push(el('details', { class: 'box' }, el('summary', {}, 'Or link a device without an account'), ...this.linkBits()));
    this.showDialog('account', body);
    setTimeout(() => name.focus(), 50);
  }

  openForgot() {
    const email = el('input', { class: 'srv-input plain', type: 'email', autocomplete: 'email', placeholder: 'Your account’s email', required: true, maxlength: 254 });
    const human = this.captcha();
    const err = el('p', { class: 'srv-err', role: 'alert' });
    const go = el('button', { class: 'btn', type: 'submit' }, 'Send me a link');
    const form = el('form', { class: 'srv-acct' }, email, human.node, err, go);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      go.disabled = true;
      try {
        await call('account/forgot', { method: 'POST', body: { email: email.value.trim(), captcha: human.token() }, auth: false });
        this.showDialog('account', [el('h2', {}, 'Check your email'),
          el('p', {}, `If ${email.value.trim()} has an account, a reset link is on its way. It works once, for an hour.`)]);
      } catch (ex) {
        err.textContent = ex.message;
        human.reset();
      }
      go.disabled = false;
    });
    this.showDialog('account', [el('h2', {}, 'Forgot password'), el('p', { class: 'muted' }, 'We’ll email you a link to pick a new one.'), form,
      el('p', { class: 'small' }, el('button', { class: 'linklike', onclick: () => this.openAccount('login') }, 'Back to sign in'))]);
    setTimeout(() => email.focus(), 50);
  }

  // Cloudflare Turnstile, when the server has a site key: { node, token(), reset() }
  captcha() {
    const site = this.health?.turnstile;
    if (!site) return { node: null, token: () => null, reset: () => {} };
    const node = el('div', { class: 'srv-captcha' });
    let id = null, tok = null;
    loadTurnstile().then((ts) => {
      id = ts.render(node, { sitekey: site, theme: theme(), callback: (t) => { tok = t; }, 'expired-callback': () => { tok = null; } });
    }).catch(() => node.replaceChildren(el('p', { class: 'muted small' }, 'The are-you-human check couldn’t load: try again in a bit.')));
    return { node, token: () => tok, reset: () => { tok = null; if (id != null) window.turnstile?.reset(id); } };
  }

  async signup(username, email, password, captcha) {
    await this.sync();  // so everything on here is on the profile the account takes over
    const res = await call('account/signup', { method: 'POST', body: { username, email, password, captcha } });
    if (res.key) save(KEY, res.key);
  }

  // The link from the email: #verify=… confirms the account and signs this device in
  async verifyLink(token) {
    try {
      const res = await call('account/verify', { method: 'POST', body: { token }, auth: false });
      this.signedIn(res);
      await this.sync();
      this.toast(`Email confirmed: you’re signed in as ${res.username}.`);
    } catch (e) {
      alert(e.message);
    }
  }

  // #reset=… : pick a new password
  resetLink(token) {
    const pw = el('input', { class: 'srv-input plain', type: 'password', autocomplete: 'new-password', placeholder: 'New password (10+ characters)', required: true, minlength: 10 });
    const err = el('p', { class: 'srv-err', role: 'alert' });
    const go = el('button', { class: 'btn', type: 'submit' }, 'Set password and sign in');
    const form = el('form', { class: 'srv-acct' }, pw, err, go);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      go.disabled = true;
      try {
        const res = await call('account/reset', { method: 'POST', body: { token, password: pw.value }, auth: false });
        this.signedIn(res);
        await this.sync();
        this.closeView();
        this.toast(`Password changed: you’re signed in as ${res.username}, and every other device is signed out.`);
      } catch (ex) {
        err.textContent = ex.message;
      }
      go.disabled = false;
    });
    this.showDialog('account', [el('h2', {}, 'Pick a new password'), form]);
    setTimeout(() => pw.focus(), 50);
  }

  signedIn(res) {
    save(KEY, res.key);
    save(USER, res.username);
    forgetRevs();  // possibly a different profile: fetch it all, and send what's here so it merges in
    this.queueEverything();
    this.renderAccountBtn();
    this.loadForecast();
  }

  async login(username, password) {
    const res = await call('account/login', { method: 'POST', body: { username, password }, auth: false });
    this.signedIn(res);
    await this.sync();
    this.toast(`Signed in as ${res.username}.`);
  }

  async logout(wipe, gone = false) {
    if (!gone) {
      await this.sync();
      if (Object.keys(this.outbox).length && !confirm('Some changes haven’t reached the server yet (no signal?). Sign out anyway and lose them?')) return;
      try {
        await call('account/logout', { method: 'POST' });
      } catch { /* the key's dropped here either way */ }
    }
    save(KEY, null);
    save(USER, null);
    forgetRevs();
    this.outbox = {};
    save(this.keys.outbox, this.outbox);
    if (wipe) {
      this.ratings.clearAll();
      this.flights = {};
      this.photos = {};
      save(this.keys.flights, this.flights);
      save(this.keys.photos, this.photos);
      save(this.mineKey, null);
      for (const k of Object.keys(localStorage)) {
        if (/^fpvspots:[^:]+:(ratings|notes|saved|places|reviews|flights|photos|outbox|mine)$/.test(k)) localStorage.removeItem(k);
      }
    }
    this.closeView();
    this.renderAccountBtn();
    this.changed();
    if (!gone) this.toast(wipe ? 'Signed out, and this device is cleared.' : 'Signed out. Your stuff is still on this device.');
  }

  openSignedIn(user) {
    const body = [el('h2', {}, `👤 ${user}`)];
    const stats = el('p', { class: 'muted small' }, '…');
    body.push(stats);
    const saved = Object.keys(this.ratings.saved).length, places = Object.keys(this.ratings.places).length;
    const reviews = Object.keys(this.ratings.reviews).length;
    const n = (x, w) => `${x} ${w}${x === 1 ? '' : 's'}`;
    body.push(el('p', {}, `On here: ${n(saved, 'saved spot')}, ${n(places, 'place')} of your own, ${n(reviews, 'review')}, ${n(this.allFlights().length, 'flight')}.`));
    body.push(el('p', { class: 'muted small' }, 'Star reviews show to other pilots under your name. Notes, flights and photos are only yours.'));

    const old = el('input', { class: 'srv-input plain', type: 'password', autocomplete: 'current-password', placeholder: 'Current password', required: true });
    const neu = el('input', { class: 'srv-input plain', type: 'password', autocomplete: 'new-password', placeholder: 'New password (8+ characters)', required: true, minlength: 8 });
    const err = el('p', { class: 'srv-err', role: 'alert' });
    const go = el('button', { class: 'btn', type: 'submit' }, 'Change password');
    const pwForm = el('form', { class: 'srv-acct' }, old, neu, err, go);
    pwForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      go.disabled = true;
      try {
        await call('account/password', { method: 'POST', body: { old: old.value, new: neu.value } });
        err.textContent = '';
        this.toast('Password changed. Other devices need to sign in again.');
        this.closeView();
      } catch (ex) {
        err.textContent = ex.message;
      }
      go.disabled = false;
    });
    const statsBtn = el('button', { class: 'btn', hidden: true, onclick: () => this.openStats() }, '📊 Site stats');
    body.push(statsBtn);
    body.push(el('details', { class: 'box' }, el('summary', {}, 'Change password'), pwForm));
    body.push(el('details', { class: 'box' }, el('summary', {}, 'Link a device with a code'), ...this.linkBits()));
    const delPw = el('input', { class: 'srv-input plain', type: 'password', autocomplete: 'current-password', placeholder: 'Your password', required: true });
    const delErr = el('p', { class: 'srv-err', role: 'alert' });
    const delForm = el('form', { class: 'srv-acct' }, el('p', { class: 'muted small' }, 'Deletes your account, reviews, saved spots, notes, flights and photos from the server for good.'), delPw, delErr,
      el('button', { class: 'btn danger', type: 'submit' }, 'Delete my account'));
    delForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!confirm('Delete your account and everything in it? This can’t be undone.')) return;
      try {
        await call('account/delete', { method: 'POST', body: { password: delPw.value } });
        this.outbox = {};
        await this.logout(true, true);
        this.toast('Account deleted.');
      } catch (ex) {
        delErr.textContent = ex.message;
      }
    });
    body.push(el('details', { class: 'box' }, el('summary', {}, 'Delete account'), delForm));
    body.push(el('div', { class: 'srv-signout' },
      el('button', { class: 'btn', onclick: () => this.logout(false) }, 'Sign out'),
      el('button', { class: 'linklike del', onclick: () => confirm('Sign out and remove your saved spots, ratings, notes and flights from this device? They stay in your account.') && this.logout(true) }, 'Sign out and clear this device')));
    this.showDialog('account', body);
    call('me').then((m) => {
      if (m.username) save(USER, m.username);
      statsBtn.hidden = !m.admin;
      stats.textContent = `${n(m.devices, 'device')} signed in.`;
    }).catch((e) => { stats.textContent = e.status === 401 ? 'Signed out on the server: sign in again.' : 'Offline: changes sync when there’s signal.'; });
  }

  // The admin-only stats page (site-stats module, served by the API). The server says 404 to anyone else.
  async openStats() {
    const host = el('div');
    this.showDialog('stats', [host]);
    this.dialog.classList.add('wide');
    this.dialog.addEventListener('close', () => this.dialog.classList.remove('wide'), { once: true });
    try {
      const { renderStats } = await import(/* @vite-ignore */ api('stats/dashboard.js'));
      await renderStats(host, { title: 'Spot Finder stats', fetchJson: (path) => call(`stats/${path}`) });
    } catch (e) {
      host.replaceChildren(el('p', { class: 'muted' }, `Stats unavailable (${e.message}).`));
    }
  }

  // ---- devices

  linkBits() {
    const body = [];
    body.push(el('p', { class: 'muted small' }, 'A code from this device lets another one join the same saved spots, ratings, notes and flight log.'));
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
    body.push(el('h4', {}, 'Add another device'), make, out);
    body.push(el('h4', {}, 'Have a code from your other device?'), el('div', { class: 'srv-join' }, input, join),
      el('p', { class: 'muted small' }, 'What’s on this device is kept and merged in.'));
    return body;
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
      this.closeView();
      this.loadForecast();
      const m = await call('me').catch(() => ({}));
      if (m.username) save(USER, m.username);
      this.renderAccountBtn();
      this.toast(m.username ? `Linked: signed in as ${m.username}.` : 'Linked: this device now syncs with your others.');
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
        el('button', { class: 'linklike', onclick: () => { this.closeView(); this.openSpot(f.drone, f.spot); } }, f.title || f.spot),
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

  // Account and flights are pages of the app (#/account, #/flights); the forecast and stats stay dialogs.
  showDialog(view, body) {
    if (this.pageHost && PAGE_VIEWS.has(view)) {
      if (this.dialog?.open) this.dialog.close();
      this.pageView = view;
      return this.pageHost.show(view, body.filter(Boolean));
    }
    if (!this.dialog) {
      this.dialog = el('dialog', { class: 'srv-dialog' });
      document.body.append(this.dialog);
      this.dialog.addEventListener('click', (e) => { if (e.target === this.dialog) this.closeView(); });
    }
    this.dialog.dataset.view = view;
    this.dialog.replaceChildren(el('form', { method: 'dialog' }, el('button', { class: 'dlg-close', 'aria-label': 'Close' }, '×')), ...body.filter(Boolean));
    if (!this.dialog.open) this.dialog.showModal();
  }

  closeView() {
    if (this.pageView && this.pageHost?.isOpen(this.pageView)) this.pageHost.close();
    this.pageView = null;
    this.dialog?.close();
  }

  toast(text) {
    const t = el('div', { class: 'srv-toast', role: 'status' }, text);
    document.body.append(t);
    setTimeout(() => t.remove(), 4000);
  }
}

const PAGE_VIEWS = new Set(['account', 'flights']);

let tsLoad = null;
function loadTurnstile() {
  tsLoad ||= new Promise((resolve, reject) => {
    const sc = document.createElement('script');
    sc.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
    sc.async = true;
    sc.onload = () => (window.turnstile ? resolve(window.turnstile) : reject(new Error('no turnstile')));
    sc.onerror = () => { tsLoad = null; reject(new Error('turnstile blocked')); };
    document.head.append(sc);
  });
  return tsLoad;
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

// What other pilots with accounts think of a spot: {avg, n, reviews: [{user, stars, text, t}]}.
// null without a server.
export async function spotReviews(profile, id) {
  if (!S) return null;
  const k = `${profile}/${id}`;
  if (!S.reviewCache.has(k)) {
    S.reviewCache.set(k, call(`reviews?region=${encodeURIComponent(S.region)}&drone=${profile}&spot=${encodeURIComponent(id)}`, { auth: false })
      .catch((e) => { S.reviewCache.delete(k); throw e; }));
  }
  return S.reviewCache.get(k);
}

// {spot id: [average stars, count]} for the list, once it's loaded (it redraws the list when it is).
export function reviewSummary(profile) {
  if (!S) return null;
  if (!(profile in S.summary)) {
    S.summary[profile] = null;
    call(`reviews/summary?region=${encodeURIComponent(S.region)}&drone=${profile}`, { auth: false })
      .then((m) => { S.summary[profile] = m; S.onChange?.(); })
      .catch(() => { delete S.summary[profile]; });
  }
  return S.summary[profile];
}

export const signedInAs = () => (S ? store(USER, null) : null);
export const openAccount = (mode) => S?.openAccount(mode);
export const openFlights = () => S?.openFlights();
export const hasServer = () => !!S;
export const canAccount = () => !!S?.health?.features?.includes('accounts');

// A spot's 3D model and line, built by the server the first time anyone opens it. null without a server.
export async function fetchModel(region, profile, id) {
  if (!S || !region || !/^\d+-\d+$/.test(id || '')) return null;
  const m = await call(`models/${region}/${profile}/${id}`, { auth: false, timeout: 60000 });
  return { ...m, url: new URL(m.preview.file, location.href.split('#')[0]).href };
}

// Called once the spots are loaded. Does nothing if there's no server behind the app.
export async function attachServer(opts) {
  if (SANDBOXED) return null;
  let up = false, health = null;
  try {
    health = await call('health', { auth: false, timeout: 4000 });
    up = !!health.ok;
    save(SEEN, true);
  } catch {
    up = store(SEEN, false);  // offline in the field, but this app has a server: queue things for later
  }
  if (!up) return null;
  S = new Server(opts);
  S.health = health;
  await S.loadPending();
  S.mountBar();
  S.renderAccountBtn();
  $('#btn-account')?.addEventListener('click', () => S.openAccount());
  S.loadForecast();
  const m = location.hash.match(/^#(link|verify|reset)=([\w-]+)$/i);
  if (m) {
    history.replaceState(null, '', location.pathname + location.search);  // so the token isn't left in the history
    if (m[1] === 'link') S.claim(m[2]);
    else if (m[1] === 'verify') S.verifyLink(m[2]);
    else S.resetLink(m[2]);
    if (m[1] === 'reset') S.sync();
  } else S.sync();
  // an email link opened while the app's already open in this tab
  window.addEventListener('hashchange', () => {
    const h = location.hash.match(/^#(verify|reset)=([\w-]+)$/);
    if (!h) return;
    history.replaceState(null, '', location.pathname + location.search);
    if (h[1] === 'verify') S.verifyLink(h[2]);
    else S.resetLink(h[2]);
  });
  const kick = () => document.visibilityState === 'visible' && S.sync();
  document.addEventListener('visibilitychange', kick);
  window.addEventListener('online', kick);
  setInterval(kick, 120000);
  return {
    profileChanged: () => { S.renderBar(); S.loadForecast(); },
    retarget: (o) => S.retarget(o),
  };
}
