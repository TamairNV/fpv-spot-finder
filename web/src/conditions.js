// Sun times (computed locally) and wind/rain from Open-Meteo (free, no key).
import SunCalc from 'suncalc';
import { el, SANDBOXED } from './util.js';

const hm = (d) => d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });

export function sunTimes(lon, lat, date = new Date()) {
  const t = SunCalc.getTimes(date, lat, lon);
  return {
    sunrise: t.sunrise, sunset: t.sunset,
    goldenMorningEnd: t.goldenHourEnd, goldenEveningStart: t.goldenHour,
  };
}

export function sunBox(lon, lat) {
  const t = sunTimes(lon, lat);
  const box = el('div', { class: 'box' },
    el('h4', {}, 'Light today'),
    el('div', {}, `Sunrise ${hm(t.sunrise)} · golden till ${hm(t.goldenMorningEnd)}`),
    el('div', {}, `Golden from ${hm(t.goldenEveningStart)} · sunset ${hm(t.sunset)}`));
  return box;
}

// Rough flyability for each hour. Gust limits are a judgement call, not a rule.
const LIMITS = {
  freestyle5: { gust: 28, wind: 20 },
  cine85: { gust: 12, wind: 8 },
};

const cache = new Map();

export async function weather(lon, lat) {
  const key = `${lon.toFixed(2)},${lat.toFixed(2)}`;
  if (cache.has(key)) return cache.get(key);
  const url = 'https://api.open-meteo.com/v1/forecast'
    + `?latitude=${lat.toFixed(4)}&longitude=${lon.toFixed(4)}`
    + '&hourly=wind_speed_10m,wind_gusts_10m,wind_direction_10m,precipitation_probability,precipitation'
    + '&wind_speed_unit=mph&timezone=Europe%2FLondon&forecast_days=3';
  const p = fetch(url).then((r) => {
    if (!r.ok) throw new Error(`weather ${r.status}`);
    return r.json();
  });
  cache.set(key, p);
  p.catch(() => cache.delete(key));
  return p;
}

// Where to look instead when the forecast can't be fetched (offline, or a host that blocks it).
function noForecast(box, lon, lat) {
  box.replaceChildren(el('h4', {}, 'Wind and rain'),
    el('div', { class: 'muted' }, 'Can\'t load the forecast here. ',
      el('a', { href: `https://www.windy.com/?${lat.toFixed(3)},${lon.toFixed(3)},13`, target: '_blank', rel: 'noopener' }, 'Open this spot on Windy'),
      '.'));
}

export function weatherBox(lon, lat, profile) {
  const box = el('div', { class: 'box' }, el('h4', {}, 'Wind and rain, next 18 hours'), el('div', { class: 'muted' }, 'Loading forecast…'));
  if (SANDBOXED) {
    noForecast(box, lon, lat);
    return box;
  }
  weather(lon, lat).then((w) => {
    const lim = LIMITS[profile] || LIMITS.freestyle5;
    const now = Date.now();
    const h = w.hourly;
    const strip = el('div', { class: 'wx' });
    let shown = 0;
    let firstGood = null;
    for (let i = 0; i < h.time.length && shown < 18; i++) {
      const t = new Date(h.time[i]);
      if (t.getTime() < now - 3600e3) continue;
      const gust = h.wind_gusts_10m[i], wind = h.wind_speed_10m[i], rain = h.precipitation_probability[i] ?? 0;
      const cls = gust > lim.gust * 1.3 || rain > 60 ? 'no' : gust > lim.gust || wind > lim.wind || rain > 30 ? 'meh' : 'go';
      if (cls === 'go' && !firstGood) firstGood = t;
      const dir = h.wind_direction_10m[i];
      strip.append(el('div', { class: cls, title: `${Math.round(wind)} mph, gusts ${Math.round(gust)} mph, rain ${rain}%` },
        `${t.getHours()}h`,
        el('b', {}, `${Math.round(gust)}`),
        el('span', { class: 'arrow', style: `transform: rotate(${dir + 180}deg)` }, '↑'),
        el('div', {}, `${rain}%`)));
      shown++;
    }
    const summary = firstGood
      ? `Next good window from ${firstGood.toLocaleString('en-GB', { weekday: 'short', hour: '2-digit', minute: '2-digit' })}`
      : 'No calm, dry hours in the next 18 h for this drone.';
    box.replaceChildren(el('h4', {}, 'Wind and rain, next 18 hours'),
      el('div', { class: 'muted', style: 'margin-bottom:6px;font-size:12.5px' },
        `Big number = gusts (mph), arrow = where the wind blows to, % = chance of rain. ${summary}`),
      strip);
  }).catch(() => noForecast(box, lon, lat));
  return box;
}
