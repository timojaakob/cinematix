# Changelog

## 1.0.0 - unreleased

First release that is set up fully in the web UI.

### Added
- A five-step tour after setup that introduces showtimes, the seat map, seating areas and pushes. It can be opened again from Settings.
- First-run setup wizard: cinema, TMDb key (with a live check), a generated private ntfy topic with QR code and test push, and an optional password.
- Every Cinema City cinema in Czechia, chosen in setup or settings. Changing it clears the old cinema's showtimes and notification history.
- Formats 4DX, Superscreen and VIP, next to IMAX, 3D and 2D.
- "Seats freed up" push when free seats rise in a known showtime, with its own toggle and priority. The push says how many seats came back and how many are free.
- Per movie, how many seats you need together: in one row for 2 or 3, or two blocks directly behind each other for 4 or more. A push needs such a group with at least one freed seat in it.
- Seating areas per hall (preferred and acceptable), drawn and edited on the seat map. Seats outside them never count. Preset for Flora's IMAX hall.
- Per movie, "seats freed up" pushes for all showtimes, or custom: rules with days, start times and date ranges ("Weekday evenings" and "Weekends" presets), plus bells on the movie page for single dates and showtimes.
- Seat counts on every showtime, counted inside your seating areas ("13 of 144 in your area") and without wheelchair spaces, a seat map with freed seats, your areas and where your group fits, and "+N freed" badges on showtimes and movie cards.
- When booking opens, for showtimes listed before sales start: in pushes, on the movie page, in the calendar and on the movie cards.
- A push about one showtime opens its booking page directly.
- Optional password login, and a same-origin check on every form, so other websites can't change settings.
- Prebuilt image for amd64 and arm64, a `docker-compose.yml`, a health check, and tests with fixtures captured from the live APIs.

- A movie page lists the new showtimes and freed seats of the last 24 hours at the top. A push about several showtimes of one movie opens that page.
- A row of dates above the showtime list jumps to a day.
- On wide screens (from 1200 px), the movie list shows each movie's next two weeks and next showtimes with **Book**, and the seat map stays open next to a movie's showtimes.
- The seat map can zoom to your seating areas (the default on phones), says what the seat under the pointer is, and its areas can be drawn by dragging with a mouse or with the keyboard.
- Theme choice "Auto", which follows the system setting.

### Changed
- The movie page on phones starts with a compact header; the movie's details fold away under **About this movie**.
- Movies with new showtimes or freed seats come first in the movie list.
- Filters open as a bottom sheet on phones and as a panel with the fields side by side on larger screens. Start times are chosen in 24-hour form.
- The calendar shows five weeks from this week, with the number of showtimes per day.
- The three seat lights also differ in shape (a long, middle or short bar), and "booking not open yet" is violet instead of orange. Status colours in the light theme meet WCAG contrast.
- Dialogs keep keyboard focus inside, return it to the button that opened them, and keep their save button in view.
- The settings dialog is grouped into sections, two columns wide on large screens.
- Settings that were env vars (`CINEMA_ID`, `TMDB_API_KEY`, `NTFY_TOPIC`, `NTFY_URL`, `WEBUI_URL`) are now set in the web UI. `MOVIE_QUERY`, `REQUIRE_IMAX`, `SITE_ID`, `LANG_CODE`, `POLL_INTERVAL_SECONDS` and the `IMAX_*` and file path variables are gone; `DATA_DIR` replaces the file paths.
- Times are compared in Prague time by the app itself, so the container no longer needs `TZ=Europe/Prague`.
- The app runs as a non-root user (`PUID`/`PGID`, default 1000) and is served by waitress instead of Flask's development server.
- Check intervals must be one of the offered values (one minute at the shortest).
- The last check time per movie moved from `config.json` to `state.json`, so the web UI and the scraper never write the same file.

### Upgrading from before 1.0
- Keep the old env vars for the first start. A config from before 1.0 imports `CINEMA_ID` (default Flora), `TMDB_API_KEY`, `NTFY_TOPIC`/`NTFY_URL` and `WEBUI_URL` once, so no setup wizard appears and no showtime is notified again. Remove the env vars afterwards.
- Movies that wanted "2D" no longer match 4DX, Superscreen or VIP showings, which used to count as 2D. Tick those formats in the movie's settings if you want them.
