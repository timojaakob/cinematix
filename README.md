# cinematix

Polls Cinema City's unofficial internal JSON feed for a given cinema and
notifies (via [ntfy](https://ntfy.sh)) when a matching showtime becomes
bookable. Built to monitor for IMAX tickets at Cinema City Praha Flora — the
only IMAX screen in Czechia — since high-demand releases there sell out
within minutes of going on sale.

This is a **monitor, not a bot**: it only sends a push notification when a
matching, non-sold-out showing appears. It does not automate purchases.

Two processes share one image and one `/data` volume:

- `scraper.py` — polls Cinema City on a loop, matches showtimes against your
  monitored movies, and sends up to two ntfy notifications per poll cycle:
  one when new matches appear, and one when seats free up in a known
  match (e.g. expired baskets after a drop). A push about a single showtime
  opens its booking page; a push about several opens the web UI.
- `webui.py` — a small local-only Flask app for searching [TMDb](https://www.themoviedb.org/)
  to add movies to monitor (poster/runtime/overview shown for each), and for
  each movie: which formats to check for (IMAX/3D/2D), how often, its
  Cinema City match text, and browsing its matching showtimes with
  clickable booking links.

## How it works

Cinema City has no official public API. The endpoint used here builds on
the reverse-engineering work in [ra100/cinema-loader](https://github.com/ra100/cinema-loader):

```
GET https://www.cinemacity.cz/cz/data-api-service/v1/quickbook/{siteId}/film-events/in-cinema/{cinemaId}/at-date/{YYYY-MM-DD}?attr=&lang=cs_CZ
```

It returns `body.films[]` (catalog) and `body.events[]` (showtimes, joined
on `filmId`). This is unofficial and unstable — Cinema City could change it
without notice.

Key findings baked into `scraper.py`:

- **Sold-out detection**: the `soldOut` flag is unreliable alone (observed
  `false` at <1% seat availability), so `availabilityRatio <= 0` is checked
  as a backup.
- **Format detection**: IMAX via `auditoriumTinyName == "IMAX"` (primary)
  or `"70-mm"` in `attributeIds` (secondary, for redundancy); 3D via the
  `"3d"` attribute tag; everything else is treated as 2D. These are the
  only formats observed at Praha Flora so far.
- **Booking links**: the raw `bookingLink` field points at
  `tickets.cinemacity.cz/api/order/{id}`, which 404s — the working link
  drops the `/api/` segment.
- Unpublished future dates return `HTTP 200` with empty arrays (not an
  error) — Cinema City publishes schedules weekly.
- **Booking open time**: showtimes are often published before booking
  opens. The feed doesn't say when it opens, but the booking site's own
  `GET https://tickets.cinemacity.cz/api/presentations/{eventId}` does
  (`ticketSaleStart`, Prague local time; also `ticketSaleStop` and
  `isMemberSaleOnly`). While a screening is locked, it returns only
  `{"error": {"error": "TICKETING_NOT_STARTED", "data": "2026-10-20 09:00"}}`.
  It always answers HTTP 200, even for errors, and needs no login or
  reCAPTCHA. The container must run with `TZ=Europe/Prague`. cinematix fetches it
  once per matched showtime, caches it in `state.json`, and rechecks
  upcoming times hourly until booking opens.

## Configuration

The monitored movie list — including each movie's required formats, check
frequency, and match text — plus global settings (TMDb API key, ntfy
endpoint, per-case notification toggles/priority, and the default check
frequency for newly added movies) live in `/data/config.json`, all managed
through the web UI, not through env vars (except as one-time seed values,
see below). Everything else is an environment variable:

| Variable | Default | Description |
|---|---|---|
| `CINEMA_ID` | `1052` | Cinema City venue ID (`1052` = Praha Flora) |
| `SITE_ID` | `10101` | Country/site code (`10101` = CZ) |
| `LANG_CODE` | `cs_CZ` | API response language |
| `MOVIE_QUERY` | `dune` | Seeds `config.json`'s monitored movie list *only* if it doesn't exist yet |
| `REQUIRE_IMAX` | `true` | One-time seed only: if `true`, movies get "IMAX" as their default required format when first created; each movie's required formats are then edited per-movie in the web UI |
| `DATE_RANGE_DAYS` | `21` | How many days ahead to poll |
| `POLL_INTERVAL_SECONDS` | `120` | Delay between poll cycles |
| `NTFY_TOPIC` | — | One-time seed only for the default ntfy endpoint (`https://ntfy.sh/{NTFY_TOPIC}`); pick something unguessable — topics are public. The live endpoint is set in the web UI's settings dialog |
| `NTFY_URL` | `https://ntfy.sh/{NTFY_TOPIC}` | One-time seed override for a self-hosted ntfy instance; the live endpoint is set in the web UI's settings dialog |
| `WEBUI_URL` | `https://cine.vfk.ovh` | Click-through link on ntfy notifications (point this at wherever the web UI is reachable) |
| `STATE_FILE` | `/data/state.json` | Dedup state so repeat polls don't re-notify |
| `TMDB_API_KEY` | — | One-time seed only, used by `webui.py` for movie search (free key from [themoviedb.org](https://www.themoviedb.org/settings/api)); the live key is set in the web UI's settings dialog |
| `AVAILABILITY_RISE_THRESHOLD` | `0` | A "seats freed up" push is sent when a showtime's free-seat ratio rises by more than this over its lowest value since the last push (`0` = any returned seat, `0.05` = 5 % of the hall) |
| `DEBUG` | `false` | Verbose logging, including raw API payloads |

## Running

Both processes are the same image; the web UI overrides the default command
and needs a `TMDB_API_KEY`. They share the `/data` volume.

```yaml
services:
  cinematix:
    build: .
    container_name: cinematix
    restart: unless-stopped
    environment:
      - TZ=Europe/Prague
      - CINEMA_ID=1052
      - SITE_ID=10101
      - MOVIE_QUERY=dune
      - REQUIRE_IMAX=true
      - DATE_RANGE_DAYS=21
      - POLL_INTERVAL_SECONDS=120
      - NTFY_TOPIC=your-topic-here
      - WEBUI_URL=http://your-lan-ip:8090
      - DEBUG=false
    volumes:
      - ./data:/data

  cinematix-web:
    build: .
    container_name: cinematix-web
    command: ["python", "-u", "webui.py"]
    restart: unless-stopped
    environment:
      - TZ=Europe/Prague
      - TMDB_API_KEY=your-tmdb-api-key
    ports:
      - 8090:5000  # LAN-only: no auth on this UI, don't expose it publicly
    volumes:
      - ./data:/data
```

## Web UI

- **Search** — type a title and pick from the TMDb results dropdown (arrow
  keys + Enter work too). This opens an "Add movie" dialog to choose which
  formats to check for before the movie is actually added — **no format
  selected means the movie matches nothing** until you pick at least one.
  It's added with its poster/runtime/overview and a default match string
  (the lowercased TMDb title).
- **Monitored movies grid** — fixed-size cards showing poster, title, runtime,
  release date, and a colored status dot summarizing that movie's current
  showtimes at a glance (no matches yet, sold out, or low/fair/good
  availability). Clicking the × on a card slides out a "Remove from
  Monitored?" button in place rather than popping a native confirm dialog.
- **Global settings** — a gear icon next to the title opens a settings
  dialog: dark/light mode (defaults to your OS preference, remembered via
  `localStorage`), the default check frequency for newly added movies, the
  default view (List/Calendar) for a movie's showtimes, your TMDb API key
  (masked by default, with a show/hide toggle), and notifications —
  independent enable/disable and ntfy priority for each case ("scraper
  started", "new showtimes", "seats freed up"), an ntfy endpoint field, and a **Test**
  button that sends a live test push to whatever's currently typed there,
  even if unsaved.
- **Per-movie settings** — a gear icon on each movie's page opens a dialog
  for its required formats (IMAX/3D/2D), check frequency (2 minutes to 1
  day, or off), match text, and removing the movie from the monitored list.
  Match text is editable because Cinema City listings are in Czech, so the
  auto-derived English title often won't match and needs manual tuning
  once the film shows up on the site. Saving any of these immediately
  triggers a recheck of just that movie instead of waiting out its check
  frequency.
- **Movie detail page** — shows the TMDb rating alongside runtime/release
  date, IMDb/TMDb links, a **Check now** button that forces an immediate
  poll for just this movie, and every currently matching showtime (format,
  audio/subtitle language as a code next to a dub/captions icon,
  availability %, or when booking opens if it hasn't yet, a working `Book`
  link), sortable by any column. A
  **List/Calendar** toggle switches to a month grid color-coded by that
  day's best availability, click a day for its agenda. Filters (hide low
  availability, time range, weekdays, audio, format) are built dynamically
  from whatever values actually appear in the results and apply to both
  views.

## Limitations

- Unofficial Cinema City API — no guarantee it keeps working.
- IDs (`CINEMA_ID`, `SITE_ID`) must be found manually per cinema; there is
  no directory endpoint (`/collections` and `/cinemas` do not exist on this
  API).
- Title matching is a plain substring match against the Czech listing
  title, which may differ from the film's English/TMDb title.
- The web UI has no authentication of its own — this deployment puts it
  behind Traefik + Authelia (see the repo root's `docker-compose.yml`), but
  if you run it standalone, only expose it on a trusted network or behind
  your own auth layer.

## Credits

The Cinema City data-api-service endpoint this project polls was
reverse-engineered by [ra100/cinema-loader](https://github.com/ra100/cinema-loader) —
this project wouldn't exist without that work.
