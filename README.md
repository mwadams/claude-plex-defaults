# claude-plex-defaults

[Claude Code](https://claude.com/claude-code) **skills** for looking after a Plex library
from the outside — via Plex's HTTP API, never touching media files.

- [**plex-defaults**](plex-defaults/) — bulk-set per-user default audio and subtitle tracks.
- [**plex-subtitles**](plex-subtitles/) — audit which items lack subtitles, download
  matching SRTs with guards against wrong-film matches, and time-align them to the audio.

## plex-defaults

Bulk-sets per-user default
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

## plex-subtitles

[`plex-subtitles/`](plex-subtitles/) finds items with no usable **text** subtitle
(VOBSUB/PGS are image formats — they can't be resynced or restyled, so they count as
missing), searches Plex's subtitle provider, and aligns what it downloads to the audio.

```bash
pwsh -File plex-subtitles/scripts/install-tools.ps1     # ffsubsync + a working VAD
export PLEX_TOKEN=... PLEX_BASEURL=http://myserver:32400

python plex-subtitles/scripts/plex_subtitle_search.py --dry-run   # what's missing
python plex-subtitles/scripts/plex_subtitle_search.py             # search + download
python plex-subtitles/scripts/plex_subtitle_resync.py     --path-map /share/CACHEDEV1_DATA/=//nas/ --workers 2
```

Both phases are resumable and safe to interrupt. The interesting parts are the guards,
and `SKILL.md` explains why each exists:

- **Runtime alone picks the wrong film.** "A Murder of Quality" (1991) was matched to
  *A Murder of Crows* (1998), "Dirty Harry" to *The Dead Pool* — all fitting the runtime.
  Candidates must also look like the production by name, after release-scene noise is
  stripped, with an escape hatch for names carrying no title at all.
- **PAL rips need rescaling, not rejecting.** Subtitles 2–7.5% long are usually the right
  ones timed for a 23.976fps transfer; they're accepted and the framerate is corrected.
- **Alignment is corroborated, never trusted.** ffsubsync will confidently return a shift
  pinned to the edge of its search range when it can't align at all — it proposed −58s for
  subtitles that were already correct. Nothing is applied unless a second, independent
  measurement agrees, and originals are backed up before any upload, which cannot be undone.

## Install as a Claude Code skill

```bash
git clone https://github.com/mwadams/claude-plex-defaults.git
cp -r claude-plex-defaults/plex-defaults   ~/.claude/skills/plex-defaults
cp -r claude-plex-defaults/plex-subtitles  ~/.claude/skills/plex-subtitles
```

Claude discovers them automatically and consults the relevant `SKILL.md` when you ask to
change default audio/subtitle behaviour, or to add and fix subtitles, on a Plex server.

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
