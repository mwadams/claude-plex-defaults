---
name: plex-defaults
description: >-
  Set per-user default audio (prefer 5.1 surround) and subtitle (on/off) stream selections
  across entire Plex libraries via the Plex HTTP API — safely, with dry-run, audit log, and
  revert. Use this whenever the user wants Plex to "default to 5.1", change the default
  audio/subtitle track for a whole library, turn subtitles off (or on) by default for
  themselves or family members, apply different defaults per Plex Home user or managed
  child profile, or undo a previous bulk change. Prefer this over ad-hoc plexapi scripting
  for any bulk stream-selection job.
---

# Plex per-user stream defaults

Plex has no "prefer 5.1" setting, and its subtitle defaults are blunt. But Plex stores a
**per-account selected audio/subtitle stream for every media part**, settable via
`PUT /library/parts/<partId>?audioStreamID=<id>&subtitleStreamID=<id|0>`. This skill drives
`scripts/plex_set_defaults.py` (Python + `plexapi`) to bulk-set those selections: best
surround track for audio, subtitles off — or on, per user.

**Safety model** (explain this to the user up front):
- Media files are never touched; only Plex's database of per-account selections changes.
- Dry run is the default; `--apply` is required to write anything.
- Every change is logged to a CSV (old + new stream per item); `--revert <csv> --apply`
  restores the previous state. Copy the CSV somewhere durable after an apply run.

## Workflow

1. **Token, without exposure.** Needs the server **owner's** X-Plex-Token (Plex Web → any
   item → ⋮ → Get Info → View XML → `X-Plex-Token` in the URL). Never echo it into the
   transcript or store it in a repo. If the user has it on the clipboard, pipe it straight
   to a temp file (`Get-Clipboard -Raw | Set-Content ...`), validate shape only
   (`^[A-Za-z0-9_-]{15,40}$`), and pass it via the `PLEX_TOKEN` env var.

2. **Discover before assuming.** Library names are rarely literal "Movies"/"TV Shows" —
   list sections first (the script auto-discovers all movie/show sections if `--sections`
   is omitted, but confirm with the user which to include; servers often have special
   libraries that should be left alone). List the real usernames too (see gotchas — three
   different plex.tv APIs disagree about who exists).

3. **Smoke test**: dry run with `--limit 10`. Show the user the planned picks and confirm
   the policy (which users get `--subs-on`, which sections).

4. **Full dry run**, backgrounded for big libraries (it reloads every item once per user;
   thousands of API round-trips ≈ minutes to tens of minutes). Summarize planned change
   counts per user from the CSV.

5. **Apply only with the user's explicit go-ahead** — same command plus `--apply`,
   backgrounded, logged to a fresh CSV.

6. **Verify**: re-run a small dry run — it should plan **0 changes**. Then copy the apply
   CSV out of any temp directory (it is the undo button).

## Per-user model

- Selections are **per Plex account**. `--users alice bob carol` applies for each in turn;
  the owner's own username is auto-detected and uses the owner token directly.
- `--subs-on bob` flips bob's subtitle policy to ON (best full track in the preferred
  language, plain subs preferred over SDH/CC, never forced/commentary tracks). Everyone
  else gets subtitles off.
- Shared-user tokens come from plex.tv (`shared_servers` access tokens); managed Home
  profiles are switched into via the v2 API. All fetched with the owner token — users
  never need to hand over passwords.

## Gotchas (hard-won; read before debugging)

- **Three user APIs disagree.** Legacy `plex.tv/api/users` and
  `api/servers/<mid>/shared_servers` list full shared users but **omit managed Home
  profiles** (child profiles). `plex.tv/api/v2/home/users` lists everyone. The script
  checks all of them; when a name isn't found, list the v2 endpoint before concluding the
  user doesn't exist.
- **v2 profile switch keys on `uuid`, not the numeric id** (404 with the id). The v1 path
  `api/home/users/<id>/switch` takes the numeric id; token attribute is `authToken` (v2) or
  `authenticationToken` (v1). The script tries both.
- **A switch token is a plex.tv token, not automatically server access.** A managed
  profile that has never been granted access to (or signed into) the server gets **401
  from the PMS with every token**, and the server won't appear in its resources. That is
  an access problem, not a token problem: the profile needs library access granted in Plex
  Web (Manage Library Access) or a one-time sign-in on a device. Surface this to the user
  — don't try to force access via the sharing API without their explicit OK.
- **PIN-protected profiles** are not supported (the switch needs the PIN); the script
  exits with a clear message rather than guessing.
- **New library items are not covered** — they follow each account's plex.tv
  Audio & Subtitles auto-select settings until the script runs again. Suggest aligning
  those (subtitle mode "Manually selected" for subs-off users, "Always enabled" for
  subs-on users) and re-running after adding content; re-runs are cheap because correct
  items are skipped.
- **Clients can override.** TV apps with their own local audio/subtitle preferences may
  ignore the server-side selection; if one device misbehaves, check its settings.
- **5.1 policy**: exactly-6-channel tracks beat 7.1 (predictable behavior); commentary/
  descriptive tracks are never picked; items with no surround track are left untouched
  (their existing selection stands).

## Requirements

Python 3.9+, `pip install plexapi`, network reach to the PMS and plex.tv, and the server
owner's X-Plex-Token.
