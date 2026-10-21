# Changelog

## 1.0.0 - unreleased

First release that is set up fully in the web UI.

### Added
- First-run setup wizard: cinema, TMDb key (with a live check), a generated private ntfy topic with QR code and test push, and an optional password.
- Every Cinema City cinema in Czechia, chosen in setup or settings. Changing it clears the old cinema's showtimes and notification history.
- Formats 4DX, Superscreen and VIP, next to IMAX, 3D and 2D.
- "Seats freed up" push when free seats rise in a known showtime, with its own toggle and priority.
- When booking opens, for showtimes listed before sales start: in pushes, on the movie page, in the calendar and on the movie cards.
- A push about one showtime opens its booking page directly.
- Optional password login, and a same-origin check on every form, so other websites can't change settings.
- Prebuilt image for amd64 and arm64, a `docker-compose.yml`, a health check, and tests with fixtures captured from the live APIs.

### Changed
- Settings that were env vars (`CINEMA_ID`, `TMDB_API_KEY`, `NTFY_TOPIC`, `NTFY_URL`, `WEBUI_URL`) are now set in the web UI. `MOVIE_QUERY`, `REQUIRE_IMAX`, `SITE_ID`, `LANG_CODE`, `POLL_INTERVAL_SECONDS` and the `IMAX_*` and file path variables are gone; `DATA_DIR` replaces the file paths.
- Times are compared in Prague time by the app itself, so the container no longer needs `TZ=Europe/Prague`.
- The app runs as a non-root user (`PUID`/`PGID`, default 1000) and is served by waitress instead of Flask's development server.
- Check intervals must be one of the offered values (one minute at the shortest).
- The last check time per movie moved from `config.json` to `state.json`, so the web UI and the scraper never write the same file.

### Upgrading from before 1.0
- Keep the old env vars for the first start. A config from before 1.0 imports `CINEMA_ID` (default Flora), `TMDB_API_KEY`, `NTFY_TOPIC`/`NTFY_URL` and `WEBUI_URL` once, so no setup wizard appears and no showtime is notified again. Remove the env vars afterwards.
- Movies that wanted "2D" no longer match 4DX, Superscreen or VIP showings, which used to count as 2D. Tick those formats in the movie's settings if you want them.
