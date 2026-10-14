# /deploy — Deploy to Railway

Check if there are any uncommitted local changes first, then walk through deploying the latest code to Railway.

## Steps to follow:

1. Run `git status` to show any uncommitted changes in the repo.
2. If there are changes, ask Husain if he wants them committed before deploying, then commit + push.
3. Remind him of the Railway deployment checklist:
   - Railway auto-deploys when code is pushed to `main` on GitHub (via the GitHub webhook).
   - If it doesn't trigger, check Railway → Settings → Source — the repo path may be stale after the GitHub account rename. Reconnect if needed.
   - If pushed code isn't taking effect after a confirmed webhook trigger, edit `Dockerfile` (bump a comment like `# rebuild v2`) to force a fresh Docker layer rebuild — Docker caching can serve stale code.
4. Show the current git log (last 5 commits) so Husain knows exactly what's live vs. what's pending.
5. After confirming a push is complete, remind him to tail Railway logs for 60 seconds to verify the bot came back up and connected to Telegram successfully.

## Key reminders:
- `DB_PATH=/data/forest.db` must remain set in Railway env vars — changing this loses the live database.
- The Docker build installs `tesseract-ocr` via apt — this is why Dockerfile is required (not Railpack).
- Real users (100+) are active. Every deploy is a brief outage. Warn Husain if deploying during peak hours (evenings, Riyadh time).
