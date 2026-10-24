// Small top-down pictures of the top spots for the list, drawn from the same 2 m grids as the
// 3D view (red = land class, green = height, blue = ground), with hill shading from the north-west.
// Rows get drawn as they scroll into view.

const GROUND = [[176, 198, 140], [86, 148, 204], [104, 109, 114], [214, 196, 152], [126, 160, 96], [182, 177, 168]];
const TREE = [74, 128, 64], TREE_TALL = [34, 82, 38], BUILDING = [228, 222, 210];
const cache = new Map();
let observer = null;

async function draw(url) {
  const img = new Image();
  img.src = url;
  await img.decode();
  const n = img.width, res = 2;
  const c = document.createElement('canvas');
  c.width = c.height = n;
  const ctx = c.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0);
  const src = ctx.getImageData(0, 0, n, n).data;
  const z = (i, j) => {
    const k = (Math.max(0, Math.min(n - 1, i)) * n + Math.max(0, Math.min(n - 1, j))) * 4;
    return src[k + 2] / 2 + src[k + 1] / 4;
  };
  // the middle 240 m: the spot itself, not the whole preview square
  const size = Math.round(n * 0.75), o = Math.round((n - size) / 2);
  const out = ctx.createImageData(size, size);
  for (let i = 0; i < size; i++) {
    for (let j = 0; j < size; j++) {
      const I = i + o, J = j + o, k = (I * n + J) * 4;
      const cls = Math.round(src[k] / 40), h = src[k + 1] / 4;
      let col;
      if (h >= 0.75 && cls !== 1 && cls !== 2) {
        if (cls === 5) col = BUILDING;
        else {
          const t = Math.min(h / 25, 1);
          col = TREE.map((v, q) => v + (TREE_TALL[q] - v) * t);
        }
      } else col = GROUND[cls] || GROUND[0];
      const dx = (z(I, J + 1) - z(I, J - 1)) / (2 * res), dy = (z(I + 1, J) - z(I - 1, J)) / (2 * res);
      const shade = Math.max(0.45, Math.min(1.35, (1 + dx + dy) / Math.sqrt(dx * dx + dy * dy + 1)));
      const p = (i * size + j) * 4;
      out.data[p] = Math.min(255, col[0] * shade);
      out.data[p + 1] = Math.min(255, col[1] * shade);
      out.data[p + 2] = Math.min(255, col[2] * shade);
      out.data[p + 3] = 255;
    }
  }
  const tile = document.createElement('canvas');
  tile.width = tile.height = size;
  tile.getContext('2d').putImageData(out, 0, 0);
  const small = document.createElement('canvas');
  small.width = small.height = 96;
  const sctx = small.getContext('2d');
  sctx.imageSmoothingQuality = 'high';
  sctx.drawImage(tile, 0, 0, 96, 96);
  return small.toDataURL('image/webp', 0.85);
}

const done = new Map();

export function thumb(url) {
  if (!cache.has(url)) {
    const p = draw(url).then((src) => { done.set(url, src); return src; });
    cache.set(url, p);
    p.catch(() => cache.delete(url));
  }
  return cache.get(url);
}

// Fill in <img data-thumb="url"> elements under root as they come into view (the list is
// re-rendered often, so each call starts a fresh observer for the current rows).
export function watchThumbs(root, scroller) {
  observer?.disconnect();
  observer = 'IntersectionObserver' in window ? new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (!e.isIntersecting) return;
      observer.unobserve(e.target);
      const img = e.target;
      thumb(img.dataset.thumb).then((src) => { img.src = src; img.classList.add('ready'); }, () => img.remove());
    });
  }, { root: scroller, rootMargin: '300px 0px' }) : null;
  root.querySelectorAll('img[data-thumb]').forEach((img) => {
    const src = done.get(img.dataset.thumb);
    if (src) {
      img.src = src;
      img.classList.add('ready');
    } else observer?.observe(img);
  });
}
