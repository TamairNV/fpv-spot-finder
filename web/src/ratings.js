// Your thumbs up/down, kept in this browser, and a tiny model that learns from them.
import { save, store } from './util.js';

export class Ratings {
  constructor(regionKey) {
    this.key = `fpvspots:${regionKey}:ratings`;
    this.data = store(this.key, {});
    // your own notes on a place ("security at weekends", "best gaps at the north end"), by spot id
    this.notesKey = `fpvspots:${regionKey}:notes`;
    this.notes = store(this.notesKey, {});
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
    return JSON.stringify({ kind: 'fpv-spot-finder-ratings', version: 1, ratings: this.data, notes: this.notes }, null, 2);
  }

  import(text) {
    const j = JSON.parse(text);
    Object.assign(this.data, j.ratings || {});
    save(this.key, this.data);
    Object.assign(this.notes, j.notes || {});
    save(this.notesKey, this.notes);
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
