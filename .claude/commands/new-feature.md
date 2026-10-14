# /new-feature — Add a New Feature

Before writing a single line of code, go through this checklist. It saves debugging time.

## 1. Understand exactly what's wanted
Ask Husain:
- What should the user experience look like? (exact commands, messages, outputs)
- Who triggers it? (any user, only the group, only managers, only in a specific topic?)
- Should it be announced publicly in the group when done, or silently available?
- Arabic or English or bilingual? (default: Arabic-first, casual young-adult tone — not corporate, not childish)

## 2. Read the relevant parts of forest_bot.py first
- Find existing similar commands for reference (e.g. `/me`, `/compare`, `/stats`)
- Check if there's already a DB table or column for the data you need — don't create a duplicate
- Use `week_start_for(date)` for any weekly logic — NOT `date - timedelta(days=date.weekday())`
- Use `display_name` from the DB — NOT `user.first_name` (privacy bug, already fixed once)
- Save any state that must survive restarts to the SQLite `settings` table, not `bot_data`

## 3. Database changes
- If adding a new table/column: write a migration that runs at startup (`CREATE TABLE IF NOT EXISTS` or `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`)
- Never assume a fresh DB — there are 100+ real users with existing data
- Test the migration with the existing schema in mind

## 4. Write the feature
- Match the existing code style (async handlers, ConversationHandler patterns, etc.)
- For any new public-facing message mixing Arabic + English tokens: add `‎` (LRM) before LTR tokens inside Arabic sentences
- For topic routing: check which `TOPIC_*_ID` env var the message belongs in
- Manager-only commands: check `user.id in MANAGER_IDS` at the top of the handler

## 5. Test plan (tell Husain exactly what to test)
Write out the test steps as a numbered list so he can run them himself in the live group or a test group.
Never say "this should work" — show the expected output for each step.

## 6. Deployment
Use `/deploy` after the feature is ready.
