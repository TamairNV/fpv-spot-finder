// Your thumbs up/down, kept in this browser, and a tiny model that learns from them.
import { save, store } from './util.js';

export const savedList = (item) => (!item ? null : item.list !== undefined ? item.list : item.fav ? null : 'want');

export class Ratings {
  constructor(regionKey) {
    this.key = `fpvspots:${regionKey}:ratings`;
    this.data = store(this.key, {});
    // your own notes on a place ("security at weekends", "best gaps at the north end"), by spot id
    this.notesKey = `fpvspots:${regionKey}:notes`;
    this.notes = store(this.notesKey, {});
    // spots you've saved (★), by "<drone>/<id>"; your own places off the list, by id; your star reviews
    this.savedKey = `fpvspots:${regionKey}:saved`;
    this.saved = store(this.savedKey, {});
    this.placesKey = `fpvspots:${regionKey}:places`;
    this.places = store(this.placesKey, {});
    this.reviewsKey = `fpvspots:${regionKey}:reviews`;
    this.reviews = store(this.reviewsKey, {});
  }

  isSaved(profile, id) {
    return !!this.saved[`${profile}/${id}`];
  }

  setSaved(profile, id, on, extra = {}) {
    const k = `${profile}/${id}`;
    if (!on) delete this.saved[k];
    else this.saved[k] = { t: Date.now(), ...extra };
    save(this.savedKey, this.saved);
  }

  // A saved entry is on one list, "want" (want to go) or "been" (been here), and can be a favourite as
  // well, or only a favourite. Entries saved before the lists existed count as "want".
  savedItem(profile, id) {
    return this.saved[`${profile}/${id}`] || null;
  }

  setSavedState(profile, id, change, extra = {}) {
    const item = this.savedItem(profile, id);
    const cur = item || {};
    const next = { ...cur, ...extra, list: savedList(item), fav: !!cur.fav, ...change };
    if (change.list === 'been' && cur.list !== 'been') next.been_t = Date.now();
    delete next.t;
    if (!next.list && !next.fav) this.setSaved(profile, id, false);
    else this.setSaved(profile, id, true, next);
    return this.savedItem(profile, id);
  }

  savedFor(profile) {
    return Object.entries(this.saved).filter(([k]) => k.startsWith(`${profile}/`))
      .map(([k, r]) => ({ id: k.slice(profile.length + 1), ...r }));
  }

  review(profile, id) {
    return this.reviews[`${profile}/${id}`] || null;
  }

  setReview(profile, id, value, extra = {}) {
    const k = `${profile}/${id}`;
    if (!value?.stars) delete this.reviews[k];
    else this.reviews[k] = { stars: value.stars, text: value.text || '', t: Date.now(), ...extra };
    save(this.reviewsKey, this.reviews);
  }

  // places of your own: { name, ll: [lon, lat], note, drone }
  setPlace(id, value) {
    if (!value) delete this.places[id];
    else this.places[id] = { ...value, t: Date.now() };
    save(this.placesKey, this.places);
  }

  placeList() {
    return Object.entries(this.places).map(([id, p]) => ({ id, ...p })).sort((a, b) => b.t - a.t);
  }

  // what an account sync keeps: everything, so signing out can wipe it off a shared device
  clearAll() {
    for (const [obj, key] of [[this.data, this.key], [this.notes, this.notesKey], [this.saved, this.savedKey],
      [this.places, this.placesKey], [this.reviews, this.reviewsKey]]) {
      for (const k of Object.keys(obj)) delete obj[k];
      save(key, obj);
    }
  }

  note(id) {
    return this.notes[id]?.text || '';
  }

  setNote(id, text, extra = {}) {
    if (!text.trim()) delete this.notes[id];
    else this.notes[id] = { text, t: Date.now(), ...extra };
    save(this.notesKey, this.notes);
  }

  get(profile, id) {
    return this.data[`${profile}/${id}`]?.v ?? 0;
  }

  set(profile, id, v, extra = {}) {
    const k = `${profile}/${id}`;
    if (!v) delete this.data[k];
    else this.data[k] = { v, t: Date.now(), ...extra };
    save(this.key, this.data);
  }

  forProfile(profile) {
    return Object.entries(this.data)
      .filter(([k]) => k.startsWith(`${profile}/`))
      .map(([k, r]) => ({ id: k.slice(profile.length + 1), ...r }));
  }

  export() {
    return JSON.stringify({ kind: 'fpv-spot-finder-ratings', version: 1, ratings: this.data, notes: this.notes,
      saved: this.saved, places: this.places, reviews: this.reviews }, null, 2);
  }

  import(text) {
    const j = JSON.parse(text);
    Object.assign(this.data, j.ratings || {});
    save(this.key, this.data);
    Object.assign(this.notes, j.notes || {});
    save(this.notesKey, this.notes);
    for (const [name, key] of [['saved', this.savedKey], ['places', this.placesKey], ['reviews', this.reviewsKey]]) {
      Object.assign(this[name], j[name] || {});
      save(key, this[name]);
    }
  }
}

const featureKeys = (feats) => Object.keys(feats[0]?.properties.components || {});

function vec(f, keys) {
  return keys.map((k) => f.properties.components[k] ?? 0);
}

// L2-regularised logistic regression by plain gradient descent: a dozen ratings is plenty
// to tilt the ranking, and it can't run away with so few points.
export function learnTaste(feats, ratings, profile) {
  const rated = ratings.forProfile(profile);
  const byId = new Map(feats.map((f) => [f.properties.id, f]));
  const keys = featureKeys(feats);
  const X = [], y = [];
  for (const r of rated) {
    const f = byId.get(r.id);
    if (!f) continue;
    X.push(vec(f, keys));
    y.push(r.v > 0 ? 1 : 0);
  }
  const pos = y.filter((v) => v).length, neg = y.length - pos;
  if (pos < 2 || neg < 1) return { ready: false, pos, neg };
  const n = keys.length;
  const w = new Array(n).fill(0);
  let b = 0;
  const lam = 0.5, lr = 0.3;
  for (let it = 0; it < 600; it++) {
    const gw = new Array(n).fill(0);
    let gb = 0;
    for (let i = 0; i < X.length; i++) {
      const z = b + X[i].reduce((s, x, j) => s + x * w[j], 0);
      const p = 1 / (1 + Math.exp(-z));
      const e = p - y[i];
      for (let j = 0; j < n; j++) gw[j] += e * X[i][j];
      gb += e;
    }
    for (let j = 0; j < n; j++) w[j] -= lr * (gw[j] / X.length + lam * w[j] / X.length);
    b -= lr * (gb / X.length);
  }
  const predict = (f) => 1 / (1 + Math.exp(-(b + vec(f, keys).reduce((s, x, j) => s + x * w[j], 0))));
  const weights = Object.fromEntries(keys.map((k, j) => [k, w[j]]));
  return { ready: true, pos, neg, predict, weights };
}

// How alike two spots are, on the same component scores the ranking uses.
export function likeThis(feats, target) {
  const keys = featureKeys(feats);
  const t = vec(target, keys);
  const sd = keys.map((_, j) => {
    const vals = feats.map((f) => vec(f, keys)[j]);
    const m = vals.reduce((a, b) => a + b, 0) / vals.length;
    return Math.sqrt(vals.reduce((a, b) => a + (b - m) ** 2, 0) / vals.length) || 1;
  });
  return (f) => {
    const v = vec(f, keys);
    const d = Math.sqrt(v.reduce((s, x, j) => s + ((x - t[j]) / sd[j]) ** 2, 0) / keys.length);
    return 1 / (1 + d * d);
  };
}
