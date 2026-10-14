# /website — Update the Public Website

The website lives at: **kauforest.github.io/kau-forest-bot**
Source file: `docs/index.html` (single self-contained HTML/CSS/JS file)
Hosted via GitHub Pages from the `/docs` folder on the `main` branch.

## Before making any UI changes:
1. Read `docs/index.html` fully to understand the current structure
2. The color theme is **navy / teal / amber** — do not restyle without a specific reason from Husain
3. The site connects to the Railway WebSocket for live updates (`wss://...`) — never break that connection
4. The site also loads `docs/data.json` as a static fallback on initial render — check if that file needs updating too

## Common website tasks:

### Update static leaderboard data (docs/data.json)
- `ENABLE_GITHUB_DATA_SYNC` is OFF by default — so `data.json` is not auto-updated
- To manually push fresh data: read the current DB state and update `docs/data.json` accordingly
- Commit and push — GitHub Pages updates within ~60 seconds

### Fix a display bug
- Read `docs/index.html`, find the relevant section, fix it
- Test: open the file locally in a browser (or use the in-app browser) to verify the fix before pushing

### Add a new section / view
- Match the existing tab structure (daily/weekly/monthly/all-time + per-batch views)
- Keep Arabic/RTL-safe — test with Arabic usernames in the data
- Keep it mobile-friendly (most users are on phones)

### Check if the site is live
- Visit kauforest.github.io/kau-forest-bot
- Check browser console for WebSocket errors
- Confirm GitHub Pages is still set to serve from `/docs` on `main` (repo Settings → Pages)

## After any change to docs/:
Commit with a clear message and push to `main`. GitHub Pages will update automatically in ~1 minute.
