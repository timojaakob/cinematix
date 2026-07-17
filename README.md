# cinemacity-scraper

Polls Cinema City's unofficial internal JSON feed for a given cinema and
notifies (via [ntfy](https://ntfy.sh)) when a matching showtime becomes
bookable. Built to watch for IMAX tickets at Cinema City Praha Flora — the
only IMAX screen in Czechia — since high-demand releases there sell out
within minutes of going on sale.

This is a **monitor, not a bot**: it only sends a push notification when a
matching, non-sold-out showing appears. It does not automate purchases.

## How it works

Cinema City has no official public API. The endpoint used here was
reverse-engineered from the site's own frontend requests:

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

All configuration is via environment variables:

| Variable | Default | Description |
|---|---|---|
| `CINEMA_ID` | `1052` | Cinema City venue ID (`1052` = Praha Flora) |
| `SITE_ID` | `10101` | Country/site code (`10101` = CZ) |
| `LANG_CODE` | `cs_CZ` | API response language |
| `MOVIE_QUERY` | `dune` | Case-insensitive substring match against the film title |
| `REQUIRE_IMAX` | `true` | Only notify for IMAX auditoriums |
| `DATE_RANGE_DAYS` | `21` | How many days ahead to poll |
| `POLL_INTERVAL_SECONDS` | `120` | Delay between poll cycles |
| `NTFY_TOPIC` | — | Your ntfy.sh topic (pick something unguessable — topics are public) |
| `NTFY_URL` | `https://ntfy.sh/{NTFY_TOPIC}` | Override for a self-hosted ntfy instance |
| `STATE_FILE` | `/data/state.json` | Dedup state so repeat polls don't re-notify |
| `DEBUG` | `false` | Verbose logging, including raw API payloads |

## Running

```bash
docker build -t cinemacity-scraper .
docker run -d --name cinemacity-scraper \
  -e NTFY_TOPIC=your-topic-here \
  -e MOVIE_QUERY=dune \
  -v ./data:/data \
  cinemacity-scraper
```

Or with `docker-compose.yml`:

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
      - DEBUG=false
    volumes:
      - ./data:/data
```

## Limitations

- Unofficial API — no guarantee it keeps working.
- IDs (`CINEMA_ID`, `SITE_ID`) must be found manually per cinema; there is
  no directory endpoint (`/collections` and `/cinemas` do not exist on this
  API).
- Title matching is a plain substring match against the Czech listing
  title, which may differ from the film's English title.
