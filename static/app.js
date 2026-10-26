// cinematix - shared script for every app page (_base.html): reloads that
// keep the scroll position, the theme, the dialogs ("sheets"), relative
// times and the settings dialog. Page scripts (index.js, movie.js) build on
// the globals defined here: cxReload, Sheets, formatRelative.

// --- Instant loading screen. sw.js answers page loads with a cached
// shell (shell.html) that paints immediately and then swaps in the real
// page. In-app refreshes add ?direct=1 to skip the shell, so the current
// page stays on screen until the new one is ready instead of flashing
// the loading screen; the parameter is removed from the address bar here.
const CX_SCROLL_KEY = 'cx-scroll:' + location.pathname;
(function () {
  const url = new URL(location.href);
  if (url.searchParams.has('direct')) {
    url.searchParams.delete('direct');
    history.replaceState(history.state, '', url);
  }
  const sw = document.documentElement.dataset.sw;
  if (sw && 'serviceWorker' in navigator) {
    navigator.serviceWorker.register(sw).catch(() => {});
  }
  // A refresh through cxReload() is a new page load, so the browser starts
  // at the top - and the showtime list is only built by the page's own
  // script anyway. Restore the saved position once that script has run.
  let saved = null;
  try {
    saved = sessionStorage.getItem(CX_SCROLL_KEY);
    sessionStorage.removeItem(CX_SCROLL_KEY);
  } catch (e) { /* storage unavailable */ }
  if (saved !== null) {
    history.scrollRestoration = 'manual';
    document.addEventListener('DOMContentLoaded', () => window.scrollTo(0, Number(saved)));
  }
})();

// Refreshes the page in place (fresh data, same scroll position). Pass
// { keepScroll: false } when the new page scrolls somewhere itself.
function cxReload({ keepScroll = true } = {}) {
  if (keepScroll) {
    try { sessionStorage.setItem(CX_SCROLL_KEY, String(window.scrollY)); } catch (e) { /* storage unavailable */ }
  }
  const url = new URL(location.href);
  url.searchParams.set('direct', '1');
  location.replace(url);
}

// Small viewports get bottom sheets and touch wording; the breakpoint
// matches app.css.
const cxPhone = window.matchMedia('(max-width: 759px)');

// --- Relative times ("2 minutes ago"), the same wording as webui.relativedate.
function formatRelative(isoStr) {
  const seconds = Math.max((Date.now() - new Date(isoStr).getTime()) / 1000, 0);
  if (seconds < 30) return 'just now';
  const minutes = Math.floor((seconds + 30) / 60);
  if (minutes < 60) return `${minutes} minute${minutes !== 1 ? 's' : ''} ago`;
  const hours = Math.floor((seconds + 1800) / 3600);
  if (hours < 24) return `${hours} hour${hours !== 1 ? 's' : ''} ago`;
  const days = Math.floor((seconds + 43200) / 86400);
  return `${days} day${days !== 1 ? 's' : ''} ago`;
}
// Every <time class="rel-time" datetime="..."> stays current without a reload.
setInterval(() => {
  for (const el of document.querySelectorAll('time.rel-time[datetime]')) {
    el.textContent = formatRelative(el.getAttribute('datetime'));
  }
}, 15000);

// --- Sheets: every dialog on every page. A sheet is a .modal-overlay with
// a .modal inside. While one is open, everything else on the page is inert
// (so Tab stays inside and screen readers read only the dialog), the page
// doesn't scroll under it, Escape and a tap on the backdrop close it, and
// focus returns to the button that opened it. Pages react to a sheet
// closing through its 'sheetclose' event.
const Sheets = (function () {
  const stack = [];

  // Everything outside the overlay: the siblings of the overlay and of
  // each of its ancestors up to <body>.
  function outside(overlay) {
    const els = [];
    for (let node = overlay; node && node !== document.body; node = node.parentElement) {
      for (const sibling of node.parentElement.children) {
        if (sibling !== node && sibling.tagName !== 'SCRIPT') els.push(sibling);
      }
    }
    return els;
  }

  function setInert(entry, on) {
    for (const el of entry.inerted) el.inert = on;
  }

  function open(overlay, { focus } = {}) {
    if (stack.some(e => e.overlay === overlay)) return;
    if (stack.length) setInert(stack[stack.length - 1], false);
    const entry = { overlay, opener: document.activeElement, inerted: outside(overlay).filter(el => !el.inert) };
    stack.push(entry);
    overlay.classList.add('open');
    setInert(entry, true);
    document.documentElement.classList.add('sheet-open');
    const target = focus || overlay.querySelector('[data-sheet-close]') || overlay.querySelector('.modal');
    if (target) target.focus({ preventScroll: true });
  }

  function close(overlay) {
    const i = stack.findIndex(e => e.overlay === overlay);
    if (i === -1) return;
    const [entry] = stack.splice(i, 1);
    setInert(entry, false);
    overlay.classList.remove('open');
    if (stack.length) setInert(stack[stack.length - 1], true);
    else document.documentElement.classList.remove('sheet-open');
    if (entry.opener && entry.opener.isConnected && !stack.length) entry.opener.focus({ preventScroll: true });
    overlay.dispatchEvent(new CustomEvent('sheetclose'));
  }

  const isOpen = overlay => stack.some(e => e.overlay === overlay);
  const top = () => (stack.length ? stack[stack.length - 1].overlay : null);

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && top()) { e.preventDefault(); close(top()); }
  });
  document.addEventListener('click', (e) => {
    const overlay = top();
    if (!overlay) return;
    if (e.target === overlay) close(overlay);
    else if (e.target.closest('[data-sheet-close]') && overlay.contains(e.target)) close(overlay);
  });

  return { open, close, isOpen, top };
})();

// --- Theme: a choice for this device only (localStorage), applied before
// the first paint by the inline script in _head.html. "Auto" follows the
// system setting and changes with it.
const THEME_KEY = 'cx-theme';
const themeQuery = window.matchMedia('(prefers-color-scheme: light)');

function themeChoice() {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    return stored === 'light' || stored === 'dark' ? stored : 'auto';
  } catch (e) { return 'auto'; }
}

function applyTheme() {
  const choice = themeChoice();
  const theme = choice === 'auto' ? (themeQuery.matches ? 'light' : 'dark') : choice;
  document.documentElement.dataset.theme = theme;
  document.querySelector('meta[name="theme-color"]').content = theme === 'light' ? '#eef1f5' : '#0e1116';
  for (const b of document.querySelectorAll('[data-theme-choice]')) {
    b.setAttribute('aria-pressed', String(b.dataset.themeChoice === choice));
  }
}

themeQuery.addEventListener('change', applyTheme);
for (const b of document.querySelectorAll('[data-theme-choice]')) {
  b.addEventListener('click', () => {
    try {
      if (b.dataset.themeChoice === 'auto') localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, b.dataset.themeChoice);
    } catch (e) { /* storage unavailable: applies to this page only */ }
    applyTheme();
  });
}
applyTheme();

// --- Settings dialog (every app page, _global_settings_modal.html) ---
(function () {
  const overlay = document.getElementById('global-settings-overlay');
  if (!overlay) return;
  document.getElementById('global-settings-btn').addEventListener('click', () => Sheets.open(overlay));

  // TMDb API key: hidden by default, the eye shows it.
  const keyInput = document.getElementById('tmdb_api_key');
  const keyToggle = document.getElementById('tmdb-key-toggle');
  const eyeSlash = document.getElementById('tmdb-key-eye-slash');
  keyToggle.addEventListener('click', () => {
    const showing = keyInput.type === 'text';
    keyInput.type = showing ? 'password' : 'text';
    eyeSlash.hidden = showing;
    keyToggle.setAttribute('aria-pressed', String(!showing));
    keyToggle.setAttribute('aria-label', showing ? 'Show API key' : 'Hide API key');
  });

  // Save stays disabled until something changes.
  const form = document.getElementById('global-settings-form');
  const saveBtn = document.getElementById('global-settings-apply-btn');
  trackChanges(form, saveBtn);

  // The test button probes whatever is in the field, unsaved edits included.
  const endpoint = document.getElementById('ntfy_endpoint');
  const testBtn = document.getElementById('ntfy-test-btn');
  const status = document.getElementById('ntfy-test-status');
  testBtn.addEventListener('click', async () => {
    const value = endpoint.value.trim();
    if (!value) {
      showStatus(status, 'Enter an ntfy topic URL first.', false);
      return;
    }
    testBtn.disabled = true;
    testBtn.textContent = 'Sending…';
    status.hidden = true;
    try {
      const data = await postJson(testBtn.dataset.url, { ntfy_endpoint: value });
      showStatus(status, data.ok ? 'Test notification sent.' : (data.error || 'Could not send the test notification.'), data.ok);
    } catch (e) {
      showStatus(status, 'Could not reach the server.', false);
    }
    testBtn.disabled = false;
    testBtn.textContent = 'Send test';
  });
})();

// --- Small helpers shared by the page scripts ---

// Keeps a form's submit button disabled until a named field differs from
// how the page loaded it. Returns a function to call after changing a
// field from script.
function trackChanges(form, button) {
  const fields = Array.from(form.elements).filter(el => el.name && el.type !== 'hidden');
  const isToggle = el => el.type === 'checkbox' || el.type === 'radio';
  // Keyed by element, not name: several checkboxes can share a name.
  const initial = new Map(fields.map(el => [el, isToggle(el) ? el.checked : el.value]));
  const extra = Array.from(form.elements).filter(el => el.name && el.type === 'hidden' && el.dataset.track !== undefined);
  for (const el of extra) initial.set(el, el.value);
  const update = () => {
    button.disabled = ![...initial].some(([el, value]) => (isToggle(el) ? el.checked : el.value) !== value);
  };
  for (const el of fields) {
    el.addEventListener('input', update);
    el.addEventListener('change', update);
  }
  update();
  return update;
}

function showStatus(el, text, ok) {
  el.textContent = text;
  el.classList.toggle('error', !ok);
  el.classList.toggle('success', ok);
  el.hidden = false;
}

async function postJson(url, body) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok && data.ok === undefined) throw new Error(resp.status);
  return data;
}
