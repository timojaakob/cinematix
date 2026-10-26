// cinematix - the movie page (movie.html). Server data comes from the
// #movie-data JSON block; the showtime row is drawn by showtimes.js. Parts:
//   1. header details        5. bells (which showtimes push freed seats)
//   2. movie settings        6. seat map (dialog, or a side panel when wide)
//   3. check now + polling   7. showtimes: changes, list, calendar, filters
//   4. watch rules editor

const DATA = JSON.parse(document.getElementById('movie-data').textContent);
const MOVIE_ID = DATA.movie_id;
const PICK_MODE = DATA.pick_mode;
const WATCH_RULES = DATA.watch_rules;
let MATCHES = DATA.matches;

// From 900 px the header details are always open; from 1200 px the seat
// map is a panel next to the list. The same breakpoints as app.css.
const mqAside = window.matchMedia('(min-width: 900px)');
const mqPanel = window.matchMedia('(min-width: 1200px)');

// Old localStorage keys (before 1.0) move to the cx- names once.
function storageGet(key, oldKey) {
  try {
    let value = localStorage.getItem(key);
    if (value === null && oldKey && localStorage.getItem(oldKey) !== null) {
      value = localStorage.getItem(oldKey);
      localStorage.setItem(key, value);
    }
    if (oldKey) localStorage.removeItem(oldKey);
    return value;
  } catch (e) { return null; }
}
function storageSet(key, value) {
  try { localStorage.setItem(key, value); } catch (e) { /* private mode */ }
}

// Redraws the showtimes (list or calendar and the changes). Replaced in
// part 7 once everything it needs exists.
let redrawShowtimes = () => {};

// --- 1. Header details: folded on phones, always open from 900 px. The
// overview is clamped to 3 lines, "Read more" only if it overflows. ---
(function () {
  const about = document.getElementById('about');
  const overview = document.getElementById('overview');
  const toggle = document.getElementById('overview-toggle');
  const sync = () => { if (mqAside.matches) about.open = true; };
  const update = () => {
    if (!overview) return;
    const p = overview.querySelector('p');
    toggle.hidden = !overview.classList.contains('clamped') || p.scrollHeight <= p.clientHeight + 1;
  };
  sync();
  mqAside.addEventListener('change', () => { sync(); update(); });
  about.addEventListener('toggle', update);
  if (overview) {
    toggle.addEventListener('click', () => { overview.classList.remove('clamped'); toggle.hidden = true; });
    window.addEventListener('resize', update);
    update();
  }
})();

// --- 2. Movie settings sheet ---
const settingsOverlay = document.getElementById('settings-overlay');
const settingsForm = document.getElementById('settings-form');
const updateSettingsState = trackChanges(settingsForm, document.getElementById('settings-apply-btn'));
document.getElementById('settings-btn').addEventListener('click', () => Sheets.open(settingsOverlay));

// Stop monitoring: the first click arms the button, the second removes.
(function () {
  const btn = document.getElementById('remove-movie-btn');
  const cancel = document.getElementById('remove-movie-cancel-btn');
  const setConfirming = (on) => {
    btn.classList.toggle('confirming', on);
    cancel.hidden = !on;
    btn.textContent = on ? 'Yes, stop monitoring' : 'Stop monitoring';
  };
  btn.addEventListener('click', () => {
    if (btn.classList.contains('confirming')) document.getElementById('remove-movie-form').submit();
    else setConfirming(true);
  });
  cancel.addEventListener('click', () => setConfirming(false));
  settingsOverlay.addEventListener('sheetclose', () => setConfirming(false));
})();

// --- 3. Check now + live status ---
let lastUpdatedAt = DATA.updated_at;
const checkBtn = document.getElementById('check-now-btn');
const checkLabel = document.getElementById('check-now-label');
const checkStatus = document.getElementById('check-now-status');
const liveDot = document.getElementById('live-dot');
const checkedText = document.getElementById('checked-text');
const announce = document.getElementById('shows-announce');

function renderCheckedText() {
  if (lastUpdatedAt) checkedText.textContent = `Checked ${formatRelative(lastUpdatedAt)}`;
}
setInterval(renderCheckedText, 15000);

function setChecking(isChecking) {
  checkBtn.disabled = isChecking;
  checkBtn.classList.toggle('checking', isChecking);
  checkLabel.textContent = isChecking ? 'Checking…' : 'Check now';
  liveDot.classList.toggle('checking', isChecking);
}

// Fresh results arrived: redraw the showtimes in place, so the scroll
// position, open filters and any open sheet stay as they are. Falls back
// to a page reload if the data can't be fetched.
async function refreshShowtimes() {
  try {
    const resp = await fetch(DATA.urls.matches);
    if (!resp.ok) throw new Error(resp.status);
    const data = await resp.json();
    lastUpdatedAt = data.updated_at;
    renderCheckedText();
    applyMatches(data.matches);
    const changed = data.matches.filter(m => m.changed).length;
    announce.textContent = `Showtimes updated: ${data.matches.length} showtimes${changed ? `, ${changed} changed in the last 24 hours` : ''}.`;
  } catch (e) {
    cxReload();
  }
}

// Guards against the background poll piling on top of an in-flight wait.
let watching = false;

async function waitForResult() {
  if (watching) return;
  watching = true;
  setChecking(true);
  for (let i = 0; i < 40; i++) {
    await new Promise(r => setTimeout(r, 2000));
    try {
      const data = await (await fetch(DATA.urls.status)).json();
      if (data.updated_at && data.updated_at !== lastUpdatedAt) { await refreshShowtimes(); break; }
      if (!data.checking) break;
    } catch (e) { /* transient; keep waiting */ }
  }
  setChecking(false);
  watching = false;
}

checkBtn.addEventListener('click', async () => {
  setChecking(true);
  checkStatus.hidden = true;
  try {
    const data = await postJson(DATA.urls.check_now, { movie_id: MOVIE_ID });
    if (!data.ok) throw new Error();
  } catch (e) {
    setChecking(false);
    checkStatus.textContent = 'Could not reach the scraper. It may be restarting; try again in a minute.';
    checkStatus.hidden = false;
    return;
  }
  waitForResult();
});

// The scraper also checks on its own schedule, so poll for that too and
// reflect it exactly like a manual check.
async function pollForBackgroundCheck() {
  if (watching || document.hidden) return;
  try {
    const data = await (await fetch(DATA.urls.status)).json();
    if (data.updated_at && data.updated_at !== lastUpdatedAt) { refreshShowtimes(); return; }
    if (data.checking) waitForResult();
  } catch (e) { /* transient network hiccup; retry on the next tick */ }
}
pollForBackgroundCheck();
setInterval(pollForBackgroundCheck, 10000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) pollForBackgroundCheck(); });

// Start times in 24-hour form, every 30 minutes; the last choice of an
// "until" field is 23:59. A saved value that isn't on the list is added.
const HALF_HOURS = Array.from({ length: 48 }, (_, i) => `${pad2(Math.floor(i / 2))}:${i % 2 ? '30' : '00'}`);
function ensureOption(select, value) {
  if (!value || Array.from(select.options).some(o => o.value === value)) return;
  const option = new Option(value, value);
  const before = Array.from(select.options).find(o => o.value > value);
  select.add(option, before || null);
}
function timeSelect(value, label, until, onChange) {
  const select = document.createElement('select');
  select.setAttribute('aria-label', label);
  for (const t of until ? [...HALF_HOURS.slice(1), '23:59'] : HALF_HOURS) select.add(new Option(t, t));
  ensureOption(select, value);
  select.value = value;
  select.addEventListener('change', () => onChange(select.value));
  return select;
}

// --- 4. Watch rules in the settings sheet. The rules are kept in a hidden
// input as JSON, so the normal "Save changes" flow submits them. ---
(function () {
  const section = document.getElementById('watch-rules-section');
  const list = document.getElementById('watch-rules');
  const input = document.getElementById('watch-rules-input');
  const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const today = () => dateKey(new Date());
  const PRESETS = {
    evenings: () => ({ days: [1, 2, 3, 4, 5], from: '17:00', to: '23:59', date_from: '', date_to: '' }),
    weekends: () => ({ days: [6, 7], from: '00:00', to: '23:59', date_from: '', date_to: '' }),
    range: () => ({ days: [1, 2, 3, 4, 5, 6, 7], from: '00:00', to: '23:59', date_from: today(), date_to: '' }),
    custom: () => ({ days: [1, 2, 3, 4, 5, 6, 7], from: '00:00', to: '23:59', date_from: '', date_to: '' }),
  };
  let rules = JSON.parse(input.value || '[]');

  function save() {
    input.value = JSON.stringify(rules);
    updateSettingsState();
  }

  function dateField(value, label, onChange) {
    const field = document.createElement('input');
    field.type = 'date';
    field.value = value;
    field.setAttribute('aria-label', label);
    field.addEventListener('change', () => { onChange(field.value); save(); });
    return field;
  }

  function between(label, from, to) {
    const line = el('div', 'rule-range');
    line.append(el('span', '', label), from, el('span', '', 'to'), to);
    return line;
  }

  function render() {
    list.replaceChildren();
    rules.forEach((rule, i) => {
      const box = el('div', 'watch-rule');
      const days = el('div', 'rule-days');
      DAYS.forEach((name, d) => {
        const label = el('label', 'chip');
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = rule.days.includes(d + 1);
        cb.addEventListener('change', () => {
          rule.days = cb.checked ? [...rule.days, d + 1].sort() : rule.days.filter(x => x !== d + 1);
          save();
        });
        label.append(cb, ` ${name}`);
        days.appendChild(label);
      });
      const times = between('Starts',
        timeSelect(rule.from, 'Earliest start', false, v => { rule.from = v; save(); }),
        timeSelect(rule.to, 'Latest start', true, v => { rule.to = v; save(); }));
      const dates = between('Dates',
        dateField(rule.date_from, 'First date (empty for no limit)', v => { rule.date_from = v; }),
        dateField(rule.date_to, 'Last date (empty for no limit)', v => { rule.date_to = v; }));
      const remove = el('button', 'link-btn', 'Remove rule');
      remove.type = 'button';
      remove.addEventListener('click', () => { rules.splice(i, 1); render(); save(); });
      box.append(days, times, dates, remove);
      list.appendChild(box);
    });
  }

  for (const b of section.querySelectorAll('[data-preset]')) {
    b.addEventListener('click', () => { rules.push(PRESETS[b.dataset.preset]()); render(); save(); });
  }
  for (const radio of document.querySelectorAll('input[name="seat_alerts"]')) {
    radio.addEventListener('change', () => { section.hidden = radio.value !== 'custom' || !radio.checked; });
  }
  render();
})();

// --- 5. Bells: which dates and showtimes can send a "seats freed up" push.
// Only shown in "Custom" mode. Mirrors storage.watches_showtime(). ---
let picks = {
  dates: new Set(DATA.picks.dates),
  ids: new Set(DATA.picks.ids),
  off: new Set(DATA.picks.off),
};

// start_time is Prague local time without an offset, like the rules.
const showDay = m => (m.start_time || '').slice(0, 10);
function matchesRule(rule, m) {
  const day = showDay(m);
  const time = (m.start_time || '').slice(11, 16);
  const weekday = new Date(`${day}T00:00:00`).getDay() || 7;
  return rule.days.includes(weekday) && rule.from <= time && time <= rule.to
    && (!rule.date_from || day >= rule.date_from) && (!rule.date_to || day <= rule.date_to);
}
const ruleCovers = m => WATCH_RULES.some(rule => matchesRule(rule, m));
const watchedIn = (p, m) => !p.off.has(m.event_id)
  && (p.dates.has(showDay(m)) || p.ids.has(m.event_id) || ruleCovers(m));
const isWatched = m => watchedIn(picks, m);
const copyPicks = () => ({ dates: new Set(picks.dates), ids: new Set(picks.ids), off: new Set(picks.off) });

async function savePicks(next) {
  const before = picks;
  picks = next;
  redrawShowtimes();
  try {
    const resp = await fetch(DATA.urls.watch, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        watched_dates: [...next.dates], watched_event_ids: [...next.ids], unwatched_event_ids: [...next.off],
      }),
    });
    if (!resp.ok) throw new Error(resp.status);
  } catch (e) {
    picks = before;
    redrawShowtimes();
    checkStatus.textContent = 'Could not save the bell. Try again.';
    checkStatus.hidden = false;
  }
}

// Turning one showtime off on a picked date keeps the date's other showtimes.
function toggleShowtime(m) {
  const next = copyPicks();
  if (isWatched(m)) {
    next.ids.delete(m.event_id);
    if (next.dates.delete(showDay(m))) {
      for (const other of MATCHES) if (showDay(other) === showDay(m) && other.event_id !== m.event_id) next.ids.add(other.event_id);
    }
    if (ruleCovers(m)) next.off.add(m.event_id);
  } else {
    next.off.delete(m.event_id);
    if (!watchedIn(next, m)) next.ids.add(m.event_id);
  }
  savePicks(next);
}

const dayShows = day => MATCHES.filter(m => showDay(m) === day);
const isDayWatched = day => dayShows(day).every(isWatched);

function toggleDate(day) {
  const next = copyPicks();
  const on = isDayWatched(day);
  for (const m of dayShows(day)) {
    next.ids.delete(m.event_id);
    next.off.delete(m.event_id);
    if (on && ruleCovers(m)) next.off.add(m.event_id);
  }
  if (on) next.dates.delete(day); else next.dates.add(day);
  savePicks(next);
}

const dayBell = key => bellButton(isDayWatched(key), `Push freed seats for every showtime on ${dayTitle(key)}`, () => toggleDate(key));

// --- 6. Seat map: one grid for the whole hall (first column holds the row
// names), so a seating area is one box spanning its rows and columns. On
// screens from 1200 px it lives in the panel next to the list and follows
// the selected showtime; on smaller screens it opens as a dialog. ---
const seatmapOverlay = document.getElementById('seatmap-overlay');
const seatmapModal = seatmapOverlay.querySelector('.modal');
const seatmapPanel = document.getElementById('seatmap-panel');
const seatmapPanelEmpty = document.getElementById('seatmap-panel-empty');
const seatmapView = document.getElementById('seatmap-view');
const seatmapEl = document.getElementById('seatmap');
const seatmapScroll = document.getElementById('seatmap-scroll');
const seatmapTitle = document.getElementById('seatmap-title');
const seatmapSummary = document.getElementById('seatmap-summary');
const seatmapStats = document.getElementById('seatmap-stats');
const seatmapEditHint = document.getElementById('seatmap-edit-hint');
const seatmapReadout = document.getElementById('seatmap-readout');
const seatmapBook = document.getElementById('seatmap-book');
const seatmapZoom = document.getElementById('seatmap-zoom-btn');
const seatmapLegendFits = document.getElementById('seatmap-legend-fits');
const seatmapLegendAreas = document.getElementById('seatmap-legend-areas');
const seatmapBtns = Object.fromEntries(['edit', 'skip', 'clear', 'cancel', 'save'].map(k => [k, document.getElementById(`seatmap-${k}-btn`)]));
const seatsUrl = eid => DATA.urls.seats.replace('EVENT_ID', encodeURIComponent(eid));
const AREA_STEPS = ['preferred', 'acceptable'];
const AREA_NAMES = { preferred: 'preferred area', acceptable: 'acceptable area' };
let seatmapSeq = 0;
let seatmapData = null;
let seatmapShow = null;
// While editing: {step: index into AREA_STEPS, corner: first picked seat, areas}.
let editing = null;
// Showing only the seating areas, bigger. On by default on phones.
let zoomed = cxPhone.matches;
// How the last seat was picked, for the wording of the edit hint.
let lastPointer = window.matchMedia('(pointer: coarse)').matches ? 'touch' : 'mouse';
document.addEventListener('pointerdown', (e) => { lastPointer = e.pointerType === 'mouse' ? 'mouse' : 'touch'; }, true);

const inPanel = () => mqPanel.matches;
const selectedEventId = () => (seatmapShow ? seatmapShow.event_id : null);

// The view moves between the panel and the dialog with the screen width.
function placeSeatmap() {
  if (inPanel()) {
    if (Sheets.isOpen(seatmapOverlay)) Sheets.close(seatmapOverlay);
    seatmapPanel.appendChild(seatmapView);
    seatmapView.hidden = !seatmapShow;
    seatmapPanelEmpty.hidden = !!seatmapShow;
    if (!seatmapShow) selectFirstShow();
  } else {
    seatmapModal.appendChild(seatmapView);
    seatmapView.hidden = false;
  }
}

function closeSeatmap() {
  seatmapSeq++;
  editing = null;
  seatmapShow = null;
  redrawShowtimes();
}
seatmapOverlay.addEventListener('sheetclose', closeSeatmap);

function rowIndexRange(rows, rect) {
  const idx = rows.map((r, i) => [r.y, i]).filter(([y]) => y >= rect.rows[0] && y <= rect.rows[1]).map(([, i]) => i);
  return idx.length ? [idx[0], idx[idx.length - 1]] : null;
}

// The part of the hall that is drawn: everything, or, zoomed, the seating
// areas with one seat of room around them.
function visibleBox(data, areas) {
  const rects = AREA_STEPS.map(a => areas[a]).filter(Boolean);
  if (editing || !zoomed || !rects.length) return { minX: data.min_x, maxX: data.max_x, rows: data.rows };
  const minY = Math.min(...rects.map(r => r.rows[0])) - 1;
  const maxY = Math.max(...rects.map(r => r.rows[1])) + 1;
  return {
    minX: Math.max(data.min_x, Math.min(...rects.map(r => r.cols[0])) - 1),
    maxX: Math.min(data.max_x, Math.max(...rects.map(r => r.cols[1])) + 1),
    rows: data.rows.filter(r => r.y >= minY && r.y <= maxY),
  };
}

const seatState = seat => (editing && seat.state === 'freed' ? 'free' : seat.state);

function describeSeat(row, seat) {
  const state = { taken: 'taken', free: 'free', freed: 'freed in the last 24 hours' }[seat.state];
  const area = seat.area ? `, ${AREA_NAMES[seat.area]}` : '';
  return `Row ${row.name}, ${seat.wheelchair ? 'wheelchair space' : 'seat'} ${seat.name}: ${state}${area}`;
}

function drawSeatmap() {
  const data = seatmapData;
  const areas = editing ? editing.areas : data.areas;
  const box = visibleBox(data, areas);
  const cols = box.maxX - box.minX + 1;
  seatmapEl.replaceChildren();
  seatmapEl.style.setProperty('--cols', cols);
  seatmapEl.classList.toggle('editing', !!editing);
  seatmapScroll.classList.toggle('editing', !!editing);
  seatmapEl.classList.toggle('zoomed', box.rows !== data.rows);
  const screen = el('div', 'seatmap-screen', 'Screen');
  seatmapEl.appendChild(screen);
  let focusable = null;
  box.rows.forEach((row, i) => {
    const name = el('span', 'seatmap-row-name', row.name);
    name.style.gridRow = String(i + 2);
    seatmapEl.appendChild(name);
    for (const seat of row.seats) {
      if (seat.x < box.minX || seat.x > box.maxX) continue;
      const s = el('i', seatState(seat) + (seat.fits && !editing ? ' fits' : '') + (seat.wheelchair ? ' wheelchair' : '')
        + (!editing && seat.area === null ? ' outside' : '')
        + (editing && editing.corner && editing.corner.x === seat.x && editing.corner.y === row.y ? ' corner' : ''));
      s.style.gridRow = String(i + 2);
      s.style.gridColumn = String(seat.x - box.minX + 2);
      s.dataset.x = seat.x;
      s.dataset.y = row.y;
      s.dataset.label = describeSeat(row, seat);
      if (editing) {
        s.setAttribute('role', 'button');
        s.setAttribute('aria-label', s.dataset.label);
        s.tabIndex = -1;
        if (!focusable || (editing.corner && s.classList.contains('corner'))) focusable = s;
      }
      seatmapEl.appendChild(s);
    }
  });
  if (focusable) focusable.tabIndex = 0;
  for (const area of AREA_STEPS) {
    const rect = areas[area];
    const span = rect && rowIndexRange(box.rows, rect);
    if (!span) continue;
    const frame = el('div', `seatmap-area ${area}`);
    frame.style.gridRow = `${span[0] + 2} / ${span[1] + 3}`;
    frame.style.gridColumn = `${Math.max(rect.cols[0], box.minX) - box.minX + 2} / ${Math.min(rect.cols[1], box.maxX) - box.minX + 3}`;
    seatmapEl.appendChild(frame);
  }
  seatmapLegendAreas.hidden = !(areas.preferred || areas.acceptable);
  const hasAreas = !!(data.areas.preferred || data.areas.acceptable);
  seatmapZoom.hidden = !hasAreas || !!editing;
  seatmapZoom.setAttribute('aria-pressed', String(zoomed));
  seatmapZoom.textContent = zoomed ? 'Whole hall' : 'My areas only';
  updateSeatmapControls();
  updateScrollCue();
}

// Free seats as a row of counts (in the area colours when the hall has
// areas), then one line with what changed and whether your group fits.
function describeSeatmap() {
  const data = seatmapData;
  const stats = [[data.free, `free of ${data.capacity}`, '']];
  if (data.free_by_area) {
    stats.push([data.free_by_area.preferred, 'preferred', 'preferred'], [data.free_by_area.acceptable, 'acceptable', 'acceptable']);
  }
  seatmapStats.replaceChildren(...stats.map(([n, label, cls]) => {
    const item = el('div', `seatmap-stat ${cls}`);
    item.append(el('strong', '', String(n)), el('span', '', label));
    return item;
  }));
  seatmapStats.hidden = false;
  const parts = [];
  if (data.freed) parts.push(`${data.freed} freed in the last 24 hours.`);
  if (data.seats_wanted > 1) {
    const where = data.free_by_area ? ' in your areas' : '';
    parts.push(data.fits ? `${data.seats_wanted} together fit${where}.` : `${data.seats_wanted} together don't fit${where}.`);
  }
  parts.push(`As of ${formatRelative(data.checked_at)}.`);
  seatmapSummary.textContent = parts.join(' ');
  seatmapLegendFits.hidden = !(data.seats_wanted > 1);
}

function updateSeatmapControls() {
  const loaded = !!seatmapData;
  seatmapBtns.edit.hidden = !loaded || !!editing;
  for (const k of ['skip', 'clear', 'cancel', 'save']) seatmapBtns[k].hidden = !editing;
  seatmapBtns.skip.hidden = !editing || editing.step >= AREA_STEPS.length;
  seatmapBtns.save.disabled = !!editing && editing.step < AREA_STEPS.length;
  seatmapBook.hidden = !!editing || !(seatmapShow && seatmapShow.booking_link && !seatmapShow.sold);
  seatmapEditHint.hidden = !editing;
  if (!editing) return;
  const pick = lastPointer === 'touch' ? 'Tap' : 'Select';
  if (editing.step >= AREA_STEPS.length) {
    seatmapEditHint.textContent = 'Save the areas, or cancel. They apply to every showtime in this hall.';
  } else {
    const area = AREA_STEPS[editing.step];
    seatmapEditHint.textContent = editing.corner
      ? `${pick} the opposite corner seat of the ${area} area.`
      : `${pick} one corner seat of the ${area} area${area === 'acceptable' ? ' (it may include the preferred area)' : ''}, or skip it.`
        + (lastPointer === 'mouse' ? ' You can also drag from corner to corner.' : '');
  }
}

// Edge shadows show that the hall goes on to the left or right.
function updateScrollCue() {
  const max = seatmapScroll.scrollWidth - seatmapScroll.clientWidth;
  seatmapScroll.classList.toggle('more-left', seatmapScroll.scrollLeft > 2);
  seatmapScroll.classList.toggle('more-right', seatmapScroll.scrollLeft < max - 2);
}
seatmapScroll.addEventListener('scroll', updateScrollCue, { passive: true });
window.addEventListener('resize', updateScrollCue);

function tapSeat(x, y) {
  if (!editing || editing.step >= AREA_STEPS.length) return;
  if (!editing.corner) {
    editing.corner = { x, y };
  } else {
    const c = editing.corner;
    editing.areas[AREA_STEPS[editing.step]] = { rows: [Math.min(c.y, y), Math.max(c.y, y)], cols: [Math.min(c.x, x), Math.max(c.x, x)] };
    editing.corner = null;
    editing.step++;
  }
  const keepScroll = seatmapScroll.scrollLeft;
  drawSeatmap();
  seatmapScroll.scrollLeft = keepScroll;
}

const seatAt = target => (target && target.closest ? target.closest('#seatmap i[data-x]') : null);

// Readout: what the seat under the pointer (or focus, or a tap) is.
for (const type of ['pointerover', 'focusin', 'click']) {
  seatmapEl.addEventListener(type, (e) => {
    const seat = seatAt(e.target);
    if (seat) seatmapReadout.textContent = seat.dataset.label;
  });
}
seatmapEl.addEventListener('pointerleave', () => { if (!editing) seatmapReadout.textContent = ''; });

// Mouse: press on one corner and release on the other, or click twice.
// Touch: tap twice (a press can't pick a seat, it may start a scroll).
let dragFrom = null;
seatmapEl.addEventListener('pointerdown', (e) => {
  const seat = seatAt(e.target);
  if (!editing || !seat || e.pointerType !== 'mouse') return;
  e.preventDefault();
  dragFrom = editing.corner ? null : { x: +seat.dataset.x, y: +seat.dataset.y };
  tapSeat(+seat.dataset.x, +seat.dataset.y);
});
document.addEventListener('pointerup', (e) => {
  if (!dragFrom) return;
  const seat = seatAt(document.elementFromPoint(e.clientX, e.clientY));
  if (seat && (+seat.dataset.x !== dragFrom.x || +seat.dataset.y !== dragFrom.y)) tapSeat(+seat.dataset.x, +seat.dataset.y);
  dragFrom = null;
});
seatmapEl.addEventListener('click', (e) => {
  const seat = seatAt(e.target);
  if (editing && seat && lastPointer !== 'mouse') tapSeat(+seat.dataset.x, +seat.dataset.y);
});

// Keyboard: arrows move between seats, Enter or Space picks a corner.
seatmapEl.addEventListener('keydown', (e) => {
  const seat = seatAt(e.target);
  if (!editing || !seat) return;
  lastPointer = 'keyboard';
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    const { x, y } = seat.dataset;
    tapSeat(+x, +y);
    const again = seatmapEl.querySelector(`i[data-x="${x}"][data-y="${y}"]`);
    if (again) { again.tabIndex = 0; again.focus(); }
    return;
  }
  const moves = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
  if (!moves[e.key]) return;
  e.preventDefault();
  const [dx, dy] = moves[e.key];
  const seats = Array.from(seatmapEl.querySelectorAll('i[data-x]'));
  const x = +seat.dataset.x;
  const y = +seat.dataset.y;
  let next = null;
  if (dx) {
    const row = seats.filter(s => +s.dataset.y === y).sort((a, b) => a.dataset.x - b.dataset.x);
    next = row[row.indexOf(seat) + dx];
  } else {
    const ys = [...new Set(seats.map(s => +s.dataset.y))].sort((a, b) => a - b);
    const ny = ys[ys.indexOf(y) + dy];
    if (ny !== undefined) {
      next = seats.filter(s => +s.dataset.y === ny).sort((a, b) => Math.abs(a.dataset.x - x) - Math.abs(b.dataset.x - x))[0];
    }
  }
  if (next) {
    seat.tabIndex = -1;
    next.tabIndex = 0;
    next.focus();
  }
});

seatmapZoom.addEventListener('click', () => { zoomed = !zoomed; drawSeatmap(); });
seatmapBtns.edit.addEventListener('click', () => {
  editing = { step: 0, corner: null, areas: {} };
  drawSeatmap();
  // Seats get bigger to pick; start with the middle of the hall in view.
  seatmapScroll.scrollLeft = (seatmapScroll.scrollWidth - seatmapScroll.clientWidth) / 2;
  updateScrollCue();
  const first = seatmapEl.querySelector('i[tabindex="0"]');
  if (lastPointer === 'keyboard' && first) first.focus();
});
seatmapBtns.skip.addEventListener('click', () => { editing.corner = null; editing.step++; drawSeatmap(); });
seatmapBtns.clear.addEventListener('click', () => { editing = { step: AREA_STEPS.length, corner: null, areas: {} }; drawSeatmap(); });
seatmapBtns.cancel.addEventListener('click', () => { editing = null; drawSeatmap(); });
seatmapBtns.save.addEventListener('click', async () => {
  seatmapBtns.save.disabled = true;
  try {
    const resp = await fetch(DATA.urls.areas, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hall: seatmapData.hall, ...editing.areas }),
    });
    if (!resp.ok) throw new Error(resp.status);
    editing = null;
    loadSeatmap(seatmapShow);
  } catch (e) {
    seatmapEditHint.textContent = 'Could not save the areas. Try again.';
    seatmapBtns.save.disabled = false;
  }
});

async function loadSeatmap(m) {
  const seq = ++seatmapSeq;
  try {
    const resp = await fetch(seatsUrl(m.event_id));
    const data = await resp.json();
    if (seq !== seatmapSeq) return;
    if (!resp.ok) { seatmapSummary.textContent = data.error || 'Could not load the seats.'; return; }
    seatmapData = data;
    describeSeatmap();
    drawSeatmap();
  } catch (e) {
    if (seq === seatmapSeq) seatmapSummary.textContent = 'Could not load the seats. Try again in a minute.';
  }
}

function openSeatmap(m) {
  const dt = new Date(m.start_time);
  seatmapTitle.textContent = `${shortDate(dt)}, ${hhmm(dt)} · ${m.auditorium || 'Seats'}`;
  seatmapSummary.textContent = 'Loading the seats…';
  seatmapReadout.textContent = '';
  seatmapStats.hidden = true;
  seatmapZoom.hidden = true;
  seatmapEl.replaceChildren();
  seatmapData = null;
  seatmapShow = m;
  editing = null;
  if (m.booking_link) seatmapBook.href = m.booking_link;
  updateSeatmapControls();
  if (inPanel()) {
    placeSeatmap();
    redrawShowtimes();
  } else {
    Sheets.open(seatmapOverlay);
  }
  loadSeatmap(m);
}

// On wide screens the panel shows a showtime from the start: the first
// changed one, else the first one that is on sale.
function selectFirstShow() {
  const mappable = MATCHES.filter(m => m.has_seat_map && !m.sale_upcoming && !m.sold);
  const first = mappable.find(m => m.changed) || mappable[0];
  if (first) openSeatmap(first);
}

// --- 7. Showtimes: the changes on top, then the list (by day or by free
// seats) or the calendar, both drawn from MATCHES with the same filters
// and the same row (showtimes.js). ---
let applyMatches = () => cxReload();
(function () {
  const listView = document.getElementById('list-view');
  const calendarView = document.getElementById('calendar-view');
  const noResults = document.getElementById('no-results');
  const viewListBtn = document.getElementById('view-list-btn');
  const viewCalendarBtn = document.getElementById('view-calendar-btn');
  const filterLowAvail = document.getElementById('filter-low-avail');
  const filterTimeFrom = document.getElementById('filter-time-from');
  const filterTimeTo = document.getElementById('filter-time-to');
  const weekdayCheckboxes = Array.from(document.querySelectorAll('.filter-weekday'));
  const showsUi = document.getElementById('shows-ui');
  const emptyState = document.getElementById('empty-state');
  const sortButtons = Array.from(document.querySelectorAll('[data-sort]'));
  const sortField = document.getElementById('sort-field');
  const filtersToggle = document.getElementById('filters-toggle');
  const filtersOverlay = document.getElementById('filters-overlay');
  const filtersPanel = document.getElementById('filters-panel');
  const filtersBadge = document.getElementById('filters-badge');
  const filtersDone = document.getElementById('filters-done');
  const dayJump = document.getElementById('day-jump');
  const calGrid = document.getElementById('calendar-grid');
  const calRangeLabel = document.getElementById('cal-range-label');
  const calPrev = document.getElementById('cal-prev');
  const calNext = document.getElementById('cal-next');
  const agenda = document.getElementById('agenda');
  const changes = document.getElementById('changes');
  const changesList = document.getElementById('changes-list');
  const changesCount = document.getElementById('changes-count');
  const changesMore = document.getElementById('changes-more');
  let audioCheckboxes = [];
  let formatCheckboxes = [];

  const LOW_AVAILABILITY_THRESHOLD = 0.3;
  const CHANGES_SHOWN = 3;
  const CAL_WEEKS = 5;
  const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const TIER_WORDS = {
    good: '50% free or more', fair: '15 to 50% free', low: 'under 15% free',
    sold: 'sold out', upcoming: 'booking not open yet', none: 'seats unknown',
  };
  let sortMode = 'date';
  // A push about this movie links here with ?changed=1: open all changes.
  const fromPush = new URLSearchParams(location.search).has('changed');
  let changesExpanded = fromPush;

  const row = (m, opts = {}) => showRow(m, {
    ...opts,
    bell: PICK_MODE ? { on: isWatched(m), onClick: () => toggleShowtime(m) } : null,
    onSeatMap: openSeatmap,
    selected: inPanel() && m.event_id === selectedEventId(),
  });

  // The day's light, from the server's tiers: its best showtime on sale.
  function dayTier(shows) {
    const open = shows.filter(m => !m.sale_upcoming && !m.sold && m.ratio != null);
    if (!open.length) return shows.some(m => m.sale_upcoming) ? 'upcoming' : 'sold';
    return open.reduce((a, b) => (b.ratio > a.ratio ? b : a)).tier;
  }

  function dayHead(key, count) {
    const head = el('div', 'day-head');
    const h = el('h3');
    h.append(el('span', 'day-long', dayTitle(key)), el('span', 'day-short', dayTitle(key, true)));
    head.appendChild(h);
    if (count != null) head.appendChild(el('span', 'day-count', `${count} showtime${count !== 1 ? 's' : ''}`));
    if (PICK_MODE) head.appendChild(dayBell(key));
    return head;
  }

  // --- what changed in the last 24 hours ---
  function renderChanges() {
    const items = MATCHES.filter(m => m.changed);
    changes.hidden = !items.length;
    if (!items.length) return;
    changesCount.textContent = `${items.length} showtime${items.length !== 1 ? 's' : ''}`;
    const shown = changesExpanded ? items : items.slice(0, CHANGES_SHOWN);
    changesList.replaceChildren(...shown.map(m => row(m, { withDate: true, why: true })));
    changesMore.hidden = items.length <= CHANGES_SHOWN;
    changesMore.textContent = changesExpanded ? 'Show fewer' : `Show all ${items.length}`;
  }
  changesMore.addEventListener('click', () => { changesExpanded = !changesExpanded; renderChanges(); });

  // --- filters (persisted per movie) ---
  const FILTERS_KEY = `cx-filters:${MOVIE_ID}`;
  const OLD_FILTERS_KEY = `cc-filters-v2:${MOVIE_ID}`;
  const TIME_RE = /^([01]\d|2[0-3]):([0-5]\d)$/;

  function saveFilterState() {
    storageSet(FILTERS_KEY, JSON.stringify({
      sort: sortMode,
      lowAvail: filterLowAvail.checked,
      timeFrom: filterTimeFrom.value,
      timeTo: filterTimeTo.value,
      weekdays: Object.fromEntries(weekdayCheckboxes.map(cb => [cb.dataset.day, cb.checked])),
      audioLanguages: Object.fromEntries(audioCheckboxes.map(cb => [cb.dataset.lang, cb.checked])),
      formats: Object.fromEntries(formatCheckboxes.map(cb => [cb.dataset.format, cb.checked])),
    }));
  }

  function savedFilterState() {
    try { return JSON.parse(storageGet(FILTERS_KEY, OLD_FILTERS_KEY)) || {}; } catch (e) { return {}; }
  }

  function loadFilterState() {
    const saved = savedFilterState();
    if (saved.sort === 'date' || saved.sort === 'seats') sortMode = saved.sort;
    if (typeof saved.lowAvail === 'boolean') filterLowAvail.checked = saved.lowAvail;
    for (const [select, value] of [[filterTimeFrom, saved.timeFrom], [filterTimeTo, saved.timeTo]]) {
      if (TIME_RE.test(value)) { ensureOption(select, value); select.value = value; }
    }
    for (const cb of weekdayCheckboxes) if (saved.weekdays && cb.dataset.day in saved.weekdays) cb.checked = saved.weekdays[cb.dataset.day];
  }

  function countActiveFilters() {
    let count = 0;
    if (filterLowAvail.checked) count++;
    if (filterTimeFrom.value !== '00:00' || filterTimeTo.value !== '23:59') count++;
    if (weekdayCheckboxes.some(cb => !cb.checked)) count++;
    if (audioCheckboxes.some(cb => !cb.checked)) count++;
    if (formatCheckboxes.some(cb => !cb.checked)) count++;
    return count;
  }

  function passesFilters(m) {
    if (!m.start_time) return false;
    if (filterLowAvail.checked && !m.sale_upcoming && (m.sold || (m.ratio != null && m.ratio < LOW_AVAILABILITY_THRESHOLD))) return false;
    const dt = new Date(m.start_time);
    const t = hhmm(dt);
    if (t < filterTimeFrom.value || t > filterTimeTo.value) return false;
    if (!weekdayCheckboxes.some(cb => cb.checked && +cb.dataset.day === dt.getDay())) return false;
    const lang = m.audio_language || 'Unknown';
    if (audioCheckboxes.length && !audioCheckboxes.some(cb => cb.checked && cb.dataset.lang === lang)) return false;
    const fmt = m.format || 'Unknown';
    if (formatCheckboxes.length && !formatCheckboxes.some(cb => cb.checked && cb.dataset.format === fmt)) return false;
    return true;
  }

  const filtered = () => MATCHES.filter(passesFilters);
  const byStart = (a, b) => (a.start_time || '').localeCompare(b.start_time || '');
  // Not on sale yet sorts after every bookable showtime, before sold out.
  const seatsValue = m => (m.sale_upcoming ? -0.5 : m.sold || m.ratio == null ? -1 : m.ratio);

  // --- list ---
  function renderList() {
    const items = filtered();
    listView.replaceChildren();
    if (sortMode === 'seats') {
      items.sort((a, b) => seatsValue(b) - seatsValue(a) || byStart(a, b));
      for (const m of items) listView.appendChild(row(m, { withDate: true }));
      renderDayJump(new Map());
      return items.length;
    }
    const groups = new Map();
    for (const m of items.sort(byStart)) {
      const key = dateKey(new Date(m.start_time));
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(m);
    }
    for (const [key, shows] of groups) {
      const section = el('section', 'day');
      section.id = `day-${key}`;
      section.appendChild(dayHead(key, shows.length));
      for (const m of shows) section.appendChild(row(m));
      listView.appendChild(section);
    }
    renderDayJump(groups);
    return items.length;
  }

  // A row of dates above the list; each jumps to its day.
  function renderDayJump(groups) {
    dayJump.replaceChildren();
    const show = view === 'list' && groups.size > 1;
    dayJump.hidden = !show;
    listView.classList.toggle('with-jump', show);
    if (!show) return;
    for (const [key, shows] of groups) {
      const d = new Date(`${key}T00:00:00`);
      const t = dayTier(shows);
      const b = el('a', 'jump-day');
      b.href = `#day-${key}`;
      b.dataset.day = key;
      b.append(
        el('span', 'jump-weekday', d.toLocaleDateString('en-GB', { weekday: 'short' })),
        el('span', 'jump-date', String(d.getDate())),
        el('i', `tier-bar ${t}`),
      );
      b.setAttribute('aria-label', `${dayTitle(key)}, ${shows.length} showtime${shows.length !== 1 ? 's' : ''}, best: ${TIER_WORDS[t]}`);
      b.addEventListener('click', (e) => {
        e.preventDefault();
        document.getElementById(`day-${key}`).scrollIntoView({ block: 'start', behavior: 'smooth' });
      });
      dayJump.appendChild(b);
    }
    markCurrentDay();
  }

  // The date chip of the day at the top of the screen is marked.
  let jumpFrame = 0;
  function markCurrentDay() {
    jumpFrame = 0;
    if (dayJump.hidden) return;
    const top = dayJump.getBoundingClientRect().bottom + 8;
    let current = null;
    for (const section of listView.querySelectorAll('.day')) {
      if (section.getBoundingClientRect().top <= top + 40) current = section.id.slice(4);
    }
    current = current || (listView.querySelector('.day') || { id: 'day-' }).id.slice(4);
    for (const chip of dayJump.children) {
      const on = chip.dataset.day === current;
      if (on && chip.getAttribute('aria-current') !== 'true') {
        dayJump.scrollTo({ left: chip.offsetLeft - dayJump.clientWidth / 2 + chip.offsetWidth / 2, behavior: 'smooth' });
      }
      if (on) chip.setAttribute('aria-current', 'true'); else chip.removeAttribute('aria-current');
    }
  }
  window.addEventListener('scroll', () => { if (!jumpFrame) jumpFrame = requestAnimationFrame(markCurrentDay); }, { passive: true });

  // --- calendar: five weeks at a time, starting with this week ---
  let calStart = null;
  let selectedDayKey = null;
  const mondayOf = d => { const m = new Date(d); m.setHours(0, 0, 0, 0); m.setDate(m.getDate() - ((m.getDay() + 6) % 7)); return m; };
  const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };

  function groupedByDay(items) {
    const map = new Map();
    for (const m of items) {
      const key = dateKey(new Date(m.start_time));
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(m);
    }
    return map;
  }

  function renderAgenda(byDay) {
    agenda.replaceChildren();
    if (!selectedDayKey || !byDay.has(selectedDayKey)) return;
    const section = el('section', 'day');
    section.appendChild(dayHead(selectedDayKey, byDay.get(selectedDayKey).length));
    for (const m of byDay.get(selectedDayKey).slice().sort(byStart)) section.appendChild(row(m));
    agenda.appendChild(section);
  }

  function rangeLabel(from, to) {
    const opts = { day: 'numeric', month: 'short' };
    const year = to.getFullYear() !== new Date().getFullYear() ? ` ${to.getFullYear()}` : '';
    return `${from.toLocaleDateString('en-GB', opts)} – ${to.toLocaleDateString('en-GB', opts)}${year}`;
  }

  function renderCalendar() {
    const items = filtered();
    const byDay = groupedByDay(items);
    const keys = Array.from(byDay.keys()).sort();
    const thisWeek = mondayOf(new Date());
    if (!calStart) {
      const first = keys[0] ? new Date(`${keys[0]}T00:00:00`) : new Date();
      calStart = mondayOf(first > thisWeek ? first : thisWeek);
    }
    const end = addDays(calStart, CAL_WEEKS * 7 - 1);
    const inWindow = k => k >= dateKey(calStart) && k <= dateKey(end);
    if (!selectedDayKey || !byDay.has(selectedDayKey) || !inWindow(selectedDayKey)) {
      selectedDayKey = keys.find(inWindow) || null;
    }
    calRangeLabel.textContent = rangeLabel(calStart, end);
    calPrev.disabled = calStart <= thisWeek;
    calNext.disabled = !keys.some(k => k > dateKey(end));
    calGrid.replaceChildren(...WEEKDAYS.map(w => el('div', 'cal-wd', w)));
    const todayKey = dateKey(new Date());
    for (let i = 0; i < CAL_WEEKS * 7; i++) {
      const d = addDays(calStart, i);
      const key = dateKey(d);
      const shows = byDay.get(key);
      const cell = el(shows ? 'button' : 'div', 'cal-day');
      const num = el('span', 'cal-num', String(d.getDate()));
      if (d.getDate() === 1) num.appendChild(el('span', 'cal-month', d.toLocaleDateString('en-GB', { month: 'short' })));
      cell.appendChild(num);
      if (key === todayKey) cell.classList.add('today');
      if (key < todayKey) cell.classList.add('past');
      if (shows) {
        const t = dayTier(shows);
        cell.type = 'button';
        cell.classList.add('has', t);
        cell.append(el('i', `tier-bar ${t}`), el('span', 'cal-count', String(shows.length)));
        cell.setAttribute('aria-label', `${dayTitle(key)}, ${shows.length} showtime${shows.length !== 1 ? 's' : ''}, best: ${TIER_WORDS[t]}`);
        cell.setAttribute('aria-pressed', String(key === selectedDayKey));
        if (key === selectedDayKey) cell.classList.add('selected');
        cell.addEventListener('click', () => {
          selectedDayKey = key;
          refresh();
          if (!mqPanel.matches) agenda.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        });
      }
      calGrid.appendChild(cell);
    }
    renderAgenda(byDay);
    return items.length;
  }

  // --- view switching (persisted per movie on reload; a fresh visit uses
  // the "Open movies in" setting) ---
  const VIEW_KEY = `cx-view:${MOVIE_ID}`;
  let view = 'list';

  function isReloadNavigation() {
    const [entry] = performance.getEntriesByType('navigation');
    if (entry && entry.type === 'reload') return true;
    try { return !!document.referrer && new URL(document.referrer).pathname === location.pathname; } catch (e) { return false; }
  }

  function initialView() {
    if (isReloadNavigation()) {
      const saved = storageGet(VIEW_KEY, `cc-view:${MOVIE_ID}`);
      if (saved === 'list' || saved === 'calendar') return saved;
    }
    return DATA.default_view === 'calendar' ? 'calendar' : 'list';
  }

  function refresh() {
    const isList = view === 'list';
    listView.hidden = !isList;
    calendarView.hidden = isList;
    sortField.hidden = !isList;
    viewListBtn.setAttribute('aria-pressed', String(isList));
    viewCalendarBtn.setAttribute('aria-pressed', String(!isList));
    for (const b of sortButtons) b.setAttribute('aria-pressed', String(b.dataset.sort === sortMode));
    renderChanges();
    const count = isList ? renderList() : renderCalendar();
    if (!isList) dayJump.hidden = true;
    noResults.hidden = count > 0;
    if (!isList) calendarView.hidden = count === 0;
    const active = countActiveFilters();
    filtersBadge.textContent = String(active);
    filtersBadge.hidden = active === 0;
    filtersDone.textContent = count ? `Show ${count} showtime${count !== 1 ? 's' : ''}` : 'No showtimes match';
  }

  function setView(v) {
    view = v;
    storageSet(VIEW_KEY, v);
    refresh();
  }

  function onFilterChange() {
    saveFilterState();
    refresh();
  }

  function resetFilters() {
    filterLowAvail.checked = false;
    filterTimeFrom.value = '00:00';
    filterTimeTo.value = '23:59';
    for (const cb of [...weekdayCheckboxes, ...audioCheckboxes, ...formatCheckboxes]) cb.checked = true;
    sortMode = 'date';
    onFilterChange();
  }

  // Filters: a bottom sheet on phones, a panel under the toolbar from 760 px.
  function setFiltersMode() {
    const sheet = cxPhone.matches;
    if (sheet) {
      filtersPanel.setAttribute('role', 'dialog');
      filtersPanel.setAttribute('aria-modal', 'true');
    } else {
      filtersPanel.removeAttribute('role');
      filtersPanel.removeAttribute('aria-modal');
      if (Sheets.isOpen(filtersOverlay)) Sheets.close(filtersOverlay);
    }
  }
  setFiltersMode();
  cxPhone.addEventListener('change', setFiltersMode);
  filtersToggle.addEventListener('click', () => {
    if (cxPhone.matches) {
      filtersToggle.setAttribute('aria-expanded', 'true');
      Sheets.open(filtersOverlay, { focus: filtersPanel });
      return;
    }
    const open = filtersOverlay.classList.toggle('open');
    filtersToggle.setAttribute('aria-expanded', String(open));
  });
  filtersOverlay.addEventListener('sheetclose', () => filtersToggle.setAttribute('aria-expanded', 'false'));
  for (const input of [filterLowAvail, ...weekdayCheckboxes, filterTimeFrom, filterTimeTo]) {
    input.addEventListener('change', onFilterChange);
  }

  // Audio and format chips list only values that occur in the showtimes
  // (a field with fewer than two values is hidden). A rebuild keeps each
  // chip's current state; a new value starts from the saved filter state,
  // or checked.
  function buildChips(field, container, values, cls, dataKey, savedKey, current) {
    const saved = savedFilterState()[savedKey] || {};
    const previous = Object.fromEntries(current.map(cb => [cb.dataset[dataKey], cb.checked]));
    container.replaceChildren();
    const boxes = values.map(value => {
      const label = el('label', 'chip');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = cls;
      cb.dataset[dataKey] = value;
      cb.checked = value in previous ? previous[value] : value in saved ? saved[value] : true;
      cb.addEventListener('change', onFilterChange);
      label.append(cb, ` ${value}`);
      container.appendChild(label);
      return cb;
    });
    field.hidden = values.length < 2;
    return values.length < 2 ? [] : boxes;
  }

  function rebuildChips() {
    const distinct = key => Array.from(new Set(MATCHES.map(m => m[key] || 'Unknown'))).sort();
    audioCheckboxes = buildChips(
      document.getElementById('audio-field'), document.getElementById('audio-chips'),
      distinct('audio_language'), 'filter-audio-language', 'lang', 'audioLanguages', audioCheckboxes);
    formatCheckboxes = buildChips(
      document.getElementById('format-field'), document.getElementById('format-chips'),
      distinct('format'), 'filter-format', 'format', 'formats', formatCheckboxes);
  }

  redrawShowtimes = refresh;
  applyMatches = (matches) => {
    MATCHES = matches;
    showsUi.hidden = !MATCHES.length;
    emptyState.hidden = !!MATCHES.length;
    // Keep the open seat map's showtime current (its Book link, sold out).
    if (seatmapShow) seatmapShow = MATCHES.find(m => m.event_id === seatmapShow.event_id) || seatmapShow;
    rebuildChips();
    refresh();
  };
  for (const b of sortButtons) b.addEventListener('click', () => { sortMode = b.dataset.sort; onFilterChange(); });
  document.getElementById('filters-reset').addEventListener('click', resetFilters);
  document.getElementById('no-results-reset').addEventListener('click', resetFilters);
  calPrev.addEventListener('click', () => { calStart = addDays(calStart, -CAL_WEEKS * 7); refresh(); });
  calNext.addEventListener('click', () => { calStart = addDays(calStart, CAL_WEEKS * 7); refresh(); });
  viewListBtn.addEventListener('click', () => setView('list'));
  viewCalendarBtn.addEventListener('click', () => setView('calendar'));

  loadFilterState();
  view = initialView();
  applyMatches(MATCHES);

  if (fromPush) {
    // Shown once: a reload of this page doesn't jump again.
    const url = new URL(location.href);
    url.searchParams.delete('changed');
    history.replaceState(history.state, '', url);
    if (!changes.hidden) changes.scrollIntoView({ block: 'start' });
  }
})();

placeSeatmap();
mqPanel.addEventListener('change', placeSeatmap);
