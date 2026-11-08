// Light and dark: follows the phone or computer until you pick one, then remembers it.
// index.html sets data-theme before anything paints, so there's no flash of the wrong one.
import { store, save } from './util.js';

const KEY = 'fpvspots:theme';
const SYSTEM_DARK = matchMedia('(prefers-color-scheme: dark)');
const listeners = new Set();

export const themePref = () => store(KEY, 'system');
export const theme = () => {
  const p = themePref();
  return p === 'light' || p === 'dark' ? p : SYSTEM_DARK.matches ? 'dark' : 'light';
};

function apply() {
  const p = themePref();
  if (p === 'light' || p === 'dark') document.documentElement.dataset.theme = p;
  else delete document.documentElement.dataset.theme;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = theme() === 'dark' ? '#12171b' : '#ffffff';
  listeners.forEach((fn) => fn(theme()));
}

export function setTheme(pref) {
  save(KEY, pref);
  apply();
}

export const toggleTheme = () => setTheme(theme() === 'dark' ? 'light' : 'dark');
export const onTheme = (fn) => listeners.add(fn);

SYSTEM_DARK.addEventListener('change', () => themePref() === 'system' && apply());
apply();
