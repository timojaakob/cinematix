// cinematix - the showtime row, drawn the same way on the movie page and
// in the tour. Everything it shows about seats comes from the server
// (webui.showtime_view: tier, lit, seat_text, is_new, freed, ...), so no
// threshold lives here.

const pad2 = n => String(n).padStart(2, '0');
const hhmm = d => `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
const dateKey = d => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
const shortDate = d => d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });

// "Today, Monday 26 October", or "Today, Mon 26 Oct" with short.
function dayTitle(key, short = false) {
  const d = new Date(`${key}T00:00:00`);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const diff = Math.round((d - today) / 86400000);
  const opts = short
    ? { weekday: 'short', day: 'numeric', month: 'short' }
    : { weekday: 'long', day: 'numeric', month: 'long' };
  if (d.getFullYear() !== today.getFullYear()) opts.year = 'numeric';
  const label = d.toLocaleDateString('en-GB', opts);
  return diff === 0 ? `Today, ${label}` : diff === 1 ? `Tomorrow, ${label}` : label;
}

const ICONS = {
  bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>',
  seats: '<svg class="seat-map-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 4c6-2 12-2 18 0"/><rect x="4" y="9" width="4" height="4" rx="1"/><rect x="10" y="9" width="4" height="4" rx="1"/><rect x="16" y="9" width="4" height="4" rx="1"/><rect x="4" y="16" width="4" height="4" rx="1"/><rect x="10" y="16" width="4" height="4" rx="1"/><rect x="16" y="16" width="4" height="4" rx="1"/></svg>',
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

// The seat strip: 20 seat-backs, the lit ones are free seats. The same
// drawing as the seats() macro in _macros.html.
function seatStrip(m) {
  const strip = el('span', `seats ${m.tier}`);
  for (let i = 0; i < 20; i++) strip.appendChild(el('i', i < m.lit ? 'on' : ''));
  strip.setAttribute('aria-hidden', 'true');
  return strip;
}

function bellButton(on, label, onClick) {
  const b = el('button', 'icon-btn bell' + (on ? ' on' : ''));
  b.type = 'button';
  b.innerHTML = ICONS.bell;
  b.setAttribute('aria-pressed', String(on));
  b.setAttribute('aria-label', label);
  b.title = on ? 'Freed seats send a push' : 'Freed seats send no push';
  b.addEventListener('click', onClick);
  return b;
}

// One showtime. Options:
//   withDate  - show the date above the time (rows not grouped by day)
//   why       - add a line that says what changed and when
//   bell      - { on, onClick } for the freed-seats bell, or null
//   onSeatMap - called when the seat count is clicked, or null
//   selected  - mark the row whose seat map is open
function showRow(m, { withDate = false, why = false, bell = null, onSeatMap = null, selected = false } = {}) {
  const dt = new Date(m.start_time);
  const row = el('article', 'show' + (m.sold ? ' sold' : '') + (withDate ? ' dated' : '') + (selected ? ' selected' : ''));
  row.dataset.eventId = m.event_id;

  const time = el('div', 'show-time');
  if (withDate) time.appendChild(el('span', 'show-date', shortDate(dt)));
  time.append(hhmm(dt));

  const info = el('div', 'show-info');
  info.appendChild(el('span', 'hall', m.auditorium || 'Unknown hall'));
  if (m.is_new) {
    const badge = el('span', 'badge-new', 'New');
    badge.title = `First seen ${formatRelative(m.fetched_at)}`;
    info.appendChild(badge);
  }
  if (m.freed) {
    const badge = el('span', 'badge-freed', `+${m.freed} freed`);
    badge.title = `${m.freed} seat${m.freed !== 1 ? 's' : ''} freed up in the last 24 hours, the last ${formatRelative(m.freed_at)}`;
    info.appendChild(badge);
  }
  const tags = [];
  if (m.format) tags.push([m.format, `${m.format} screening`]);
  if (m.audio_language_code) tags.push([m.audio_language_code.toUpperCase(), `${m.audio_language} audio`]);
  if (m.subtitle_language_code) tags.push([`${m.subtitle_language_code.toUpperCase()} subs`, `${m.subtitle_language} subtitles`]);
  if (m.member_only) tags.push(['Members only', 'Only Cinema City members can book this showtime']);
  for (const [text, title] of tags) {
    const tag = el('span', 'tag', text);
    tag.title = title;
    info.appendChild(tag);
  }

  const seats = el('div', 'show-seats');
  // Seat counts mean nothing before booking opens - the opening time
  // gets the strip's room instead.
  if (m.tier !== 'upcoming') seats.appendChild(seatStrip(m));
  const canMap = !!onSeatMap && m.has_seat_map && m.tier !== 'upcoming';
  const label = el(canMap ? 'button' : 'span', `seat-label tier-${m.tier}`, m.seat_text);
  if (m.tier === 'upcoming') label.title = 'Booking opens at this time. The booking page can be opened before.';
  if (canMap) {
    label.type = 'button';
    label.classList.add('seat-map-btn');
    label.insertAdjacentHTML('afterbegin', ICONS.seats);
    label.setAttribute('aria-label', `${m.seat_text}. Show the seat map`);
    label.addEventListener('click', () => onSeatMap(m));
  }
  seats.appendChild(label);

  const book = el('div', 'show-book');
  if (!m.sold && m.booking_link) {
    // Before booking opens the page can be opened, but it isn't the
    // action that matters yet: a quiet button.
    const a = el('a', m.sale_upcoming ? 'btn btn-quiet' : 'btn', 'Book');
    a.href = m.booking_link;
    a.target = '_blank';
    a.rel = 'noopener';
    a.setAttribute('aria-label', `Book ${hhmm(dt)} on ${dt.toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' })}`);
    book.appendChild(a);
  }
  row.append(time, info, seats, book);

  if (bell && !m.sold) {
    const b = bellButton(bell.on, `Push freed seats for ${hhmm(dt)}`, bell.onClick);
    b.classList.add('show-bell');
    row.appendChild(b);
  }

  if (why) {
    const parts = [];
    if (m.is_new) parts.push(`New, first seen ${formatRelative(m.fetched_at)}`);
    if (m.freed) parts.push(`${m.freed} seat${m.freed !== 1 ? 's' : ''} freed, the last ${formatRelative(m.freed_at)}`);
    row.appendChild(el('p', 'show-why', parts.join('. ') + '.'));
  }
  return row;
}
