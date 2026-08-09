# claude-plex-defaults

A [Claude Code](https://claude.com/claude-code) **skill** that bulk-sets per-user default
audio and subtitle tracks across entire Plex libraries — e.g. "default everything to 5.1
surround if available, subtitles off for me, subtitles on for one family member" — safely,
with a dry-run-first workflow, a full audit log, and one-command revert.

Plex has no "prefer 5.1" setting; this works because Plex stores a per-account selected
stream for every item, settable through its HTTP API. Only Plex's database changes —
**media files are never touched**.

## What it does

- **Audio**: selects the best surround track per item (≥6 channels, preferred language,
  exact 5.1 before 7.1, never commentary tracks). Items with no surround track keep their
  existing selection.
- **Subtitles**: off by default — or on (`--subs-on <user>`) with the best full track in
  the preferred language (plain subs before SDH/CC, never forced tracks).
- **Per user**: handles the server owner, full shared/Home users, and managed child
  profiles (via the v2 home-user switch API), all from the owner's token — no one else's
  password needed.
- **Safely**: dry run by default; `--apply` to write; every change logged to CSV;
  `--revert <csv> --apply` restores the previous selections.

## The skill

[`plex-defaults/`](plex-defaults/) contains the skill (`SKILL.md`) and the standalone
script (`scripts/plex_set_defaults.py`). The `SKILL.md` documents the workflow and the
non-obvious Plex API gotchas (managed profiles missing from the legacy users API, uuid vs
id in the v2 switch endpoint, plex.tv tokens vs server access tokens, PIN-protected
profiles, what happens to newly added items).

The script is useful on its own, no Claude required:

```bash
pip install plexapi
export PLEX_TOKEN=...   # the server owner's X-Plex-Token

# dry run (writes nothing, logs the plan):
python plex-defaults/scripts/plex_set_defaults.py --baseurl http://myserver:32400 \
    --users alice bob carol --subs-on bob

# apply, then keep the CSV — it's the undo button:
python plex-defaults/scripts/plex_set_defaults.py --baseurl http://myserver:32400 \
    --users alice bob carol --subs-on bob --apply
python plex-defaults/scripts/plex_set_defaults.py --revert plex_defaults_log_XXXX.csv --apply
```

`--sections` limits which libraries are touched (default: every movie/show library),
`--limit`/`--title` scope test runs, `--langs` sets language preference (default `eng`).

## Install as a Claude Code skill

```bash
git clone https://github.com/mwadams/claude-plex-defaults.git
cp -r claude-plex-defaults/plex-defaults ~/.claude/skills/plex-defaults
```

Claude discovers it automatically and consults `SKILL.md` when you ask to change default
audio or subtitle behavior on a Plex server.

## Requirements

- Python 3.9+ and [`plexapi`](https://github.com/pkkid/python-plexapi)
- Network access to the Plex server and plex.tv
- The server owner's X-Plex-Token (Plex Web → any item → ⋮ → Get Info → View XML →
  `X-Plex-Token` in the URL)

See also [claude-disc-to-plex](https://github.com/mwadams/claude-disc-to-plex) — skills
that take a physical disc all the way to a Plex-ready library; this skill picks up where
those leave off.

## License

MIT — see [LICENSE](LICENSE).

---

*Built with [Claude Code](https://claude.com/claude-code).*
