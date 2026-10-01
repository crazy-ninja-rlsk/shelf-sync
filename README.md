# shelf-sync

Scheduled job: picks up the newest snapshot from a storage folder, reads stock per location and updates a spreadsheet.

Configuration comes from repository secrets:

- `CONFIG_JSON` — ids, sheet layout, queries, schedule
- `GOOGLE_KEY_JSON` — service account key

Local run: put the same JSON into `config.local.json` and run `python sync.py` (dry run) or `python sync.py --write`.

## digest

`digest.py` sends scheduled chat messages: a daily sales summary with a PDF per location, and an account balance note.
Schedules, texts and queries come from the `DIGEST_JSON` secret; tokens from `TELEGRAM_TOKEN` and `BALANCE_TOKEN`.
Local run: `python digest.py` (dry run), `python digest.py --send`, `python digest.py --balance`.
