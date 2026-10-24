// "Can I fly here?" for any point: reads the legal lookup image the pipeline wrote.
// Pixel = 20 m. Blue = most restrictive reason code, red + green = flag bits (land owners, hazards).
import { merc } from './util.js';

export class LegalLookup {
  constructor(meta) {
    this.meta = meta;
    this.ready = null;
  }

  load(url) {
    this.ready = new Promise((resolve, reject) => {
      const img = new Image();
      img.crossOrigin = 'anonymous';
      img.onload = () => {
        const c = document.createElement('canvas');
        c.width = img.width;
        c.height = img.height;
        const ctx = c.getContext('2d', { willReadFrequently: true });
        ctx.drawImage(img, 0, 0);
        this.w = img.width;
        this.h = img.height;
        this.px = ctx.getImageData(0, 0, img.width, img.height).data;
        const [tl, , br] = this.meta.region.legal_corners;
        [this.x0, this.y0] = merc(tl);
        [this.x1, this.y1] = merc(br);
        resolve(this);
      };
      img.onerror = reject;
      img.src = url;
    });
    return this.ready;
  }

  at(lon, lat) {
    if (!this.px) return null;
    const [x, y] = merc([lon, lat]);
    const col = Math.floor(((x - this.x0) / (this.x1 - this.x0)) * this.w);
    const row = Math.floor(((this.y0 - y) / (this.y0 - this.y1)) * this.h);
    if (col < 0 || row < 0 || col >= this.w || row >= this.h) return null;
    const i = (row * this.w + col) * 4;
    return { code: this.px[i + 2], flags: this.px[i] | (this.px[i + 1] << 8) };
  }
}

const CODE = { CLEAR: 0, FRZ: 1, RESTRICTED: 2 };

// Plain-English verdicts for one point, for both drones.
export function verdicts(meta, hit, zoneNames = []) {
  if (!hit) return null;
  const codes = meta.legal_codes;
  const flagText = Object.entries(meta.flag_bits)
    .filter(([bit]) => hit.flags & Number(bit))
    .map(([, t]) => t);
  const zone = zoneNames.length ? ` (${zoneNames.join(', ')})` : '';
  const out = [];
  if (hit.code === CODE.FRZ || hit.code === CODE.RESTRICTED) {
    const msg = `${codes[hit.code]}${zone}`;
    out.push({ who: '5" freestyle (A3)', ok: false, text: msg });
    out.push({ who: '85 mm whoop (A1)', ok: false, text: msg });
  } else {
    out.push({
      who: '5" freestyle (A3)',
      ok: hit.code === CODE.CLEAR,
      text: hit.code === CODE.CLEAR ? 'Clear of the A3 150 m / 50 m distances here' : codes[hit.code],
    });
    out.push({ who: '85 mm whoop (A1)', ok: true, text: 'OK under A1. Never over crowds, and avoid flying over people you don\'t know' });
  }
  return { verdicts: out, flags: flagText };
}
