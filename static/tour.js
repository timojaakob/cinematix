// cinematix - the introduction tour on the home page (_tour.html).
(function () {
  const overlay = document.getElementById('tour-overlay');
  const modal = overlay.querySelector('.modal');
  const steps = Array.from(overlay.querySelectorAll('.tour-step'));
  const title = document.getElementById('tour-title');
  const text = document.getElementById('tour-text');
  const count = document.getElementById('tour-count');
  const back = document.getElementById('tour-back-btn');
  const next = document.getElementById('tour-next-btn');
  let index = 0;
  let focusSearch = false;

  // Step 2's example: an evening IMAX showtime with few seats left.
  const now = new Date().toISOString();
  document.getElementById('tour-show').appendChild(showRow({
    event_id: 'tour', start_time: '2026-12-18T20:30:00', auditorium: 'IMAX VOLVO',
    tier: 'low', lit: 2, seat_text: '13 of 144 in your area', has_seat_map: true,
    is_new: true, fetched_at: now, freed: 2, freed_at: now, booking_link: '#',
  }, { onSeatMap: () => {} }));

  // The visible step's text moves into the shared title and description,
  // so the dialog's aria-labelledby/-describedby always read the current step.
  function show(i) {
    index = i;
    steps.forEach((s, n) => { s.hidden = n !== i; });
    title.textContent = steps[i].dataset.title;
    text.replaceChildren(steps[i].querySelector('.tour-text').cloneNode(true));
    count.textContent = `${i + 1} of ${steps.length}`;
    back.hidden = i === 0;
    next.textContent = i === steps.length - 1 ? 'Add a movie' : 'Next';
  }

  // Closing in any way (the button, Escape, the backdrop) marks the tour seen.
  overlay.addEventListener('sheetclose', () => {
    fetch(modal.dataset.seenUrl, { method: 'POST' }).catch(() => {});
    if (location.search.includes('tour=1')) history.replaceState(null, '', location.pathname);
    if (focusSearch) document.getElementById('movie-search').focus();
  });

  back.addEventListener('click', () => show(Math.max(index - 1, 0)));
  next.addEventListener('click', () => {
    if (index < steps.length - 1) { show(index + 1); return; }
    focusSearch = true;
    Sheets.close(overlay);
  });
  overlay.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowRight' && index < steps.length - 1) show(index + 1);
    if (e.key === 'ArrowLeft' && index > 0) show(index - 1);
  });

  show(0);
  Sheets.open(overlay);
})();
