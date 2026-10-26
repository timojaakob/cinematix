// cinematix - the movie list (index.html): searching TMDb, the add-movie
// sheet, and keeping the list fresh.

const INDEX = JSON.parse(document.getElementById('index-data').textContent);
const searchInput = document.getElementById('movie-search');
const searchClearBtn = document.getElementById('search-clear');
const dropdown = document.getElementById('search-dropdown');
let debounceTimer = null;
let searchSeq = 0;
let currentResults = [];
let activeIndex = -1;

// Movies already being monitored (by TMDb id), so the search dropdown and
// add-movie sheet can flag a re-add before it happens - re-adding silently
// resets that movie's check interval, formats, and history (storage.add_movie
// overwrites by id).
const EXISTING_TMDB_IDS = new Set(INDEX.existing_tmdb_ids);

// --- Add-movie sheet: opened when a search result is picked, lets the
// user choose which formats to check for before the movie is actually
// added. The formats start as last time (all of them the first time),
// since leaving them unchecked means the movie never matches anything. ---
const FORMATS_KEY = 'cx-add-formats';
const addOverlay = document.getElementById('add-movie-overlay');
const addTitle = document.getElementById('add-movie-title');
const addWarning = document.getElementById('add-movie-warning');
const addConfirmBtn = document.getElementById('add-movie-confirm-btn');
const addError = document.getElementById('add-movie-error');
const formatCheckboxes = Array.from(document.querySelectorAll('.add-movie-format'));
let pendingMovie = null;

function lastFormats() {
  try {
    const saved = JSON.parse(localStorage.getItem(FORMATS_KEY));
    if (Array.isArray(saved) && saved.length) return new Set(saved);
  } catch (e) { /* none saved */ }
  return null;
}

function openAddDialog(result) {
  pendingMovie = result;
  addTitle.textContent = result.year ? `${result.title} (${result.year})` : result.title;
  const formats = lastFormats();
  for (const cb of formatCheckboxes) cb.checked = !formats || formats.has(cb.value);
  addError.hidden = true;
  const alreadyAdded = EXISTING_TMDB_IDS.has(result.tmdb_id);
  addWarning.textContent = alreadyAdded
    ? 'You already monitor this movie. Adding it again resets its check frequency, formats and history.'
    : '';
  addWarning.hidden = !alreadyAdded;
  addConfirmBtn.textContent = alreadyAdded ? 'Add again' : 'Add movie';
  addConfirmBtn.disabled = false;
  closeDropdown();
  Sheets.open(addOverlay);
}

addOverlay.addEventListener('sheetclose', () => { pendingMovie = null; });
addConfirmBtn.addEventListener('click', () => {
  if (!pendingMovie) return;
  const formats = formatCheckboxes.filter(cb => cb.checked).map(cb => cb.value);
  try { localStorage.setItem(FORMATS_KEY, JSON.stringify(formats)); } catch (e) { /* private mode */ }
  addMovie(pendingMovie.tmdb_id, formats);
});

async function addMovie(tmdbId, requiredFormats) {
  addError.hidden = true;
  addConfirmBtn.disabled = true;
  addConfirmBtn.textContent = 'Adding…';
  let resp;
  try {
    resp = await fetch(addConfirmBtn.dataset.url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tmdb_id: tmdbId, required_formats: requiredFormats }),
    });
  } catch (e) {
    resp = null;
  }
  if (resp && resp.ok) {
    const data = await resp.json();
    try { sessionStorage.setItem('cx-just-added', data.movie.id); } catch (e) { /* storage unavailable */ }
    cxReload({ keepScroll: false });
    return;
  }
  const data = resp ? await resp.json().catch(() => ({})) : {};
  addError.textContent = resp ? (data.error || 'Could not add the movie. Try again.') : 'Could not reach the server. Check your connection and try again.';
  addError.hidden = false;
  addConfirmBtn.disabled = false;
  addConfirmBtn.textContent = 'Add movie';
}

// --- Search: a combobox. Arrow keys move through the results, the
// highlighted one is announced through aria-activedescendant. ---
function openDropdown() {
  dropdown.classList.add('open');
  searchInput.setAttribute('aria-expanded', 'true');
}
function closeDropdown() {
  dropdown.classList.remove('open');
  searchInput.setAttribute('aria-expanded', 'false');
  searchInput.removeAttribute('aria-activedescendant');
}

function updateActive() {
  const rows = dropdown.querySelectorAll('.search-row');
  rows.forEach((row, i) => {
    const isActive = i === activeIndex;
    row.classList.toggle('active', isActive);
    row.setAttribute('aria-selected', String(isActive));
    if (isActive) row.scrollIntoView({ block: 'nearest' });
  });
  if (activeIndex >= 0) searchInput.setAttribute('aria-activedescendant', `search-option-${activeIndex}`);
  else searchInput.removeAttribute('aria-activedescendant');
}

function renderResults(results) {
  currentResults = results;
  activeIndex = -1;
  dropdown.replaceChildren();
  if (results.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'search-empty';
    empty.textContent = 'No movies found. Check the spelling or try the original title.';
    dropdown.appendChild(empty);
  }
  results.forEach((r, i) => {
    const row = document.createElement('div');
    row.className = 'search-row';
    row.id = `search-option-${i}`;
    row.setAttribute('role', 'option');
    row.setAttribute('aria-selected', 'false');
    if (r.poster_thumb) {
      const img = document.createElement('img');
      img.src = r.poster_thumb; img.alt = ''; img.loading = 'lazy'; img.width = 36; img.height = 54;
      row.appendChild(img);
    } else {
      const ph = document.createElement('span');
      ph.className = 'thumb-placeholder';
      row.appendChild(ph);
    }
    const meta = document.createElement('span');
    meta.className = 'meta';
    const title = document.createElement('span');
    title.className = 'title';
    title.textContent = r.title;
    const year = document.createElement('span');
    year.className = 'year';
    year.textContent = r.year || '';
    meta.append(title, year);
    row.appendChild(meta);
    if (EXISTING_TMDB_IDS.has(r.tmdb_id)) {
      const tag = document.createElement('span');
      tag.className = 'already';
      tag.textContent = 'Monitored';
      row.appendChild(tag);
    }
    row.addEventListener('click', () => openAddDialog(r));
    dropdown.appendChild(row);
  });
  openDropdown();
}

function renderSearchError() {
  currentResults = [];
  activeIndex = -1;
  dropdown.replaceChildren();
  const box = document.createElement('div');
  box.className = 'search-empty';
  box.textContent = 'Search failed. Check the TMDb API key in Settings, then try again.';
  dropdown.appendChild(box);
  openDropdown();
}

searchInput.addEventListener('input', () => {
  searchClearBtn.hidden = searchInput.value.length === 0;
  clearTimeout(debounceTimer);
  const q = searchInput.value.trim();
  if (!q) {
    closeDropdown();
    return;
  }
  debounceTimer = setTimeout(async () => {
    // Drop responses that arrive after a newer search was started, so a
    // slow early request can't overwrite the results for what's typed now.
    const seq = ++searchSeq;
    try {
      const resp = await fetch(`${searchInput.dataset.url}?q=${encodeURIComponent(q)}`);
      if (!resp.ok) throw new Error('search request failed');
      const results = await resp.json();
      if (seq === searchSeq) renderResults(results);
    } catch (e) {
      if (seq === searchSeq) renderSearchError();
    }
  }, 250);
});

searchClearBtn.addEventListener('click', () => {
  searchInput.value = '';
  searchClearBtn.hidden = true;
  closeDropdown();
  searchInput.focus();
});

searchInput.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { closeDropdown(); return; }
  if (!dropdown.classList.contains('open') || currentResults.length === 0) return;
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    activeIndex = Math.min(activeIndex + 1, currentResults.length - 1);
    updateActive();
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    activeIndex = Math.max(activeIndex - 1, -1);
    updateActive();
  } else if (e.key === 'Enter') {
    e.preventDefault();
    openAddDialog(currentResults[Math.max(activeIndex, 0)]);
  }
});

document.addEventListener('click', (e) => {
  if (!e.target.closest('.search')) closeDropdown();
});

// The loading shell (shell.html) draws this many placeholder rows.
try { localStorage.setItem('cx-film-count', String(document.querySelectorAll('.film').length || 1)); } catch (e) {}

// Coming back to the tab (e.g. from an ntfy notification) should show
// fresh statuses, not whatever was loaded hours ago.
let hiddenAt = null;
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { hiddenAt = Date.now(); return; }
  if (hiddenAt && Date.now() - hiddenAt > 60000 && !Sheets.top()) cxReload();
});

// --- Acknowledge a just-added movie with a brief highlight: addMovie()
// stashes the new id before reloading, so it survives the reload. ---
(function highlightJustAdded() {
  let justAddedId;
  try {
    justAddedId = sessionStorage.getItem('cx-just-added');
    sessionStorage.removeItem('cx-just-added');
  } catch (e) {
    return;
  }
  if (!justAddedId) return;
  const card = document.querySelector(`.film[data-movie-id="${CSS.escape(justAddedId)}"]`);
  if (!card) return;
  card.scrollIntoView({ block: 'center' });
  card.classList.add('just-added');
  card.addEventListener('animationend', () => card.classList.remove('just-added'), { once: true });
})();
