// cinematix - the first-run setup wizard (setup.html). Without JS every
// step stays visible and the form still works.
(function () {
  const form = document.getElementById('setup-form');
  const steps = Array.from(document.querySelectorAll('.step'));
  const dots = Array.from(document.querySelectorAll('[data-step-dot]'));
  const back = document.getElementById('step-back');
  const next = document.getElementById('step-next');
  const finish = document.getElementById('step-finish');
  let current = Number(form.dataset.firstStep) || 0;

  document.documentElement.classList.add('steps-js');
  function show(i) {
    current = i;
    steps.forEach((s, n) => { s.hidden = n !== i; });
    dots.forEach((d, n) => {
      d.classList.toggle('done', n < i);
      d.classList.toggle('active', n === i);
      if (n === i) d.setAttribute('aria-current', 'step'); else d.removeAttribute('aria-current');
    });
    back.hidden = i === 0;
    next.hidden = i === steps.length - 1;
    finish.hidden = i !== steps.length - 1;
    const first = steps[i].querySelector('select, input');
    if (first) first.focus({ preventScroll: true });
    window.scrollTo({ top: 0 });
  }

  // Each step checks only what the server would reject anyway, so a
  // mistake shows up before the last step.
  const cinema = document.getElementById('cinema_id');
  const tmdbKey = document.getElementById('tmdb_api_key');
  const tmdbStatus = document.getElementById('tmdb-status');
  const ntfy = document.getElementById('ntfy_endpoint');
  const ntfyStatus = document.getElementById('ntfy-status');
  function status(el, text, ok) {
    el.textContent = text;
    el.className = 'hint ' + (ok ? 'success' : 'error');
  }
  function validStep(i) {
    if (i === 0 && (!cinema || !cinema.value)) { status(document.getElementById('cinema-status'), 'Choose a cinema.', false); return false; }
    if (i === 1 && !tmdbKey.value.trim()) { status(tmdbStatus, 'Enter a TMDb API key.', false); return false; }
    if (i === 2 && !/^https?:\/\/[^/]+/.test(ntfy.value.trim())) { status(ntfyStatus, 'Enter an http(s) URL.', false); return false; }
    return true;
  }
  next.addEventListener('click', () => { if (validStep(current)) show(current + 1); });
  back.addEventListener('click', () => show(current - 1));

  async function postJson(url, body) {
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    return resp.json();
  }

  const tmdbBtn = document.getElementById('tmdb-check-btn');
  tmdbBtn.addEventListener('click', async () => {
    tmdbBtn.disabled = true;
    try {
      const data = await postJson(form.dataset.checkTmdbUrl, { key: tmdbKey.value });
      status(tmdbStatus, data.ok ? 'The key works.' : data.error, data.ok);
    } catch (e) {
      status(tmdbStatus, 'Could not reach the server.', false);
    }
    tmdbBtn.disabled = false;
  });

  // The QR code (desktop), the open link (phone) and the topic name follow
  // the URL field, so an edited topic stays in sync.
  const qr = document.getElementById('ntfy-qr');
  const open = document.getElementById('ntfy-open');
  function syncTopic() {
    const url = ntfy.value.trim();
    const name = url.split('/').filter(Boolean).pop() || '';
    for (const el of document.querySelectorAll('.ntfy-topic-name')) el.textContent = name;
    qr.src = `${form.dataset.qrUrl}?data=${encodeURIComponent(url)}`;
    open.href = url;
  }
  ntfy.addEventListener('change', syncTopic);
  syncTopic();

  const ntfyBtn = document.getElementById('ntfy-test-btn');
  ntfyBtn.addEventListener('click', async () => {
    ntfyBtn.disabled = true;
    try {
      const data = await postJson(form.dataset.testUrl, { ntfy_endpoint: ntfy.value });
      status(ntfyStatus, data.ok ? 'Test notification sent. Check your phone.' : data.error, data.ok);
    } catch (e) {
      status(ntfyStatus, 'Could not reach the server.', false);
    }
    ntfyBtn.disabled = false;
  });

  show(current);
})();
