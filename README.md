# cinemacity-scraper

Polls Cinema City's unofficial internal JSON feed for a given cinema and
notifies (via [ntfy](https://ntfy.sh)) when a matching showtime becomes
bookable. Built to watch for IMAX tickets at Cinema City Praha Flora — the
only IMAX screen in Czechia — since high-demand releases there sell out
within minutes of going on sale.

This is a **monitor, not a bot**: it only sends a push notification when a
matching, non-sold-out showing appears. It does not automate purchases.

Two processes share one image and one `/data` volume:

- `scraper.py` — polls Cinema City on a loop, matches showtimes against your
  watched movies, and sends one summary ntfy notification per poll cycle
  when new matches appear (deep-linking to the web UI, not to a single
  showtime).
- `webui.py` — a small local-only Flask app for searching [TMDb](https://www.themoviedb.org/)
  to add movies to watch (poster/runtime/overview shown for each), editing
  the Cinema City match text per movie, and browsing each movie's matching
  showtimes with clickable booking links.

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
- **IMAX detection**: `auditoriumTinyName == "IMAX"` (primary) or `"70-mm"`
  in `attributeIds` (secondary), used together for redundancy.
- **Booking links**: the raw `bookingLink` field points at
  `tickets.cinemacity.cz/api/order/{id}`, which 404s — the working link
  drops the `/api/` segment.
- Unpublished future dates return `HTTP 200` with empty arrays (not an
  error) — Cinema City publishes schedules weekly.

## Configuration

The watched movie list and IMAX toggle live in `/data/config.json`, managed
through the web UI — not through env vars (except as one-time seed values,
see below). Everything else is an environment variable:

| Variable | Default | Description |
|---|---|---|
| `CINEMA_ID` | `1052` | Cinema City venue ID (`1052` = Praha Flora) |
| `SITE_ID` | `10101` | Country/site code (`10101` = CZ) |
| `LANG_CODE` | `cs_CZ` | API response language |
| `MOVIE_QUERY` | `dune` | Seeds `config.json`'s watch list *only* if it doesn't exist yet |
| `REQUIRE_IMAX` | `true` | Only match IMAX auditoriums (also just a seed value after first run) |
| `DATE_RANGE_DAYS` | `21` | How many days ahead to poll |
| `POLL_INTERVAL_SECONDS` | `120` | Delay between poll cycles |
| `NTFY_TOPIC` | — | Your ntfy.sh topic (pick something unguessable — topics are public) |
| `NTFY_URL` | `https://ntfy.sh/{NTFY_TOPIC}` | Override for a self-hosted ntfy instance |
| `WEBUI_URL` | `http://10.0.0.32:8090` | Click-through link on ntfy notifications (point this at wherever the web UI is reachable) |
| `STATE_FILE` | `/data/state.json` | Dedup state so repeat polls don't re-notify |
| `TMDB_API_KEY` | — | Required by `webui.py` only, for movie search (free key from [themoviedb.org](https://www.themoviedb.org/settings/api)) |
| `DEBUG` | `false` | Verbose logging, including raw API payloads |

## Running

Both processes are the same image; the web UI overrides the default command
and needs a `TMDB_API_KEY`. They share the `/data` volume.

```yaml
services:
  cinemacity-scraper:
    build: .
    container_name: cinemacity-scraper
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

  cinemacity-scraper-web:
    build: .
    container_name: cinemacity-scraper-web
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

- **Search** — type a title, pick from the TMDb results dropdown; the movie
  is added immediately with its poster/runtime/overview and a default match
  string (the lowercased TMDb title).
- **Match text** — editable per movie on its detail page. Cinema City
  listings are in Czech, so the auto-derived English title often won't
  match and needs manual tuning once the film shows up on the site.
- **Movie detail page** — shows every currently matching showtime for that
  movie with availability % and a working `Book` link.

## Limitations

- Unofficial Cinema City API — no guarantee it keeps working.
- IDs (`CINEMA_ID`, `SITE_ID`) must be found manually per cinema; there is
  no directory endpoint (`/collections` and `/cinemas` do not exist on this
  API).
- Title matching is a plain substring match against the Czech listing
  title, which may differ from the film's English/TMDb title.
- The web UI has no authentication — only run it on a trusted LAN.

## Credits

The Cinema City data-api-service endpoint this project polls was
reverse-engineered by [ra100/cinema-loader](https://github.com/ra100/cinema-loader) —
this project wouldn't exist without that work.
