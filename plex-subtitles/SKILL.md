---
name: plex-subtitles
description: Find, verify and time-align English subtitles across a whole Plex library - audit which items have none, download matching SRTs through Plex's subtitle provider with guards against wrong-film matches, and resync them to the actual audio with ffsubsync. Use this whenever the user wants subtitles added to a Plex/Jellyfin library, mentions subtitles being out of sync, drifting, or ahead of the audio, asks about SRT vs VOBSUB/PGS, subtitle offset, PAL/framerate subtitle mismatches, OpenSubtitles, or wants a bulk/hands-off subtitle pass over films or TV. Prefer this over ad-hoc Plex API scripting for any library-wide subtitle job.
---

# Plex subtitles: find, verify, align

Three phases, each resumable and safe to interrupt:

1. **Audit + search** - `plex_subtitle_search.py` finds items with no usable
   text subtitle, searches Plex's provider, and downloads the first candidate
   that passes the guards.
2. **Resync** - `plex_subtitle_resync.py` aligns each subtitle to the audio.
3. **Review** - inspect what was held back rather than applied.

## Setup

```powershell
pwsh -File scripts/install-tools.ps1          # silero (recommended)
pwsh -File scripts/install-tools.ps1 -Vad auditok   # no torch download
```

Set `PLEX_BASEURL` and `PLEX_TOKEN` (or pass `--baseurl` / `--token`).
The resync phase needs **direct file access** to the media, so pass
`--path-map` to translate the server's paths onto paths this machine can read:

```
--path-map /share/CACHEDEV1_DATA/=//nas/
```

## Running it

```powershell
python scripts/plex_subtitle_search.py --dry-run        # what lacks subtitles
python scripts/plex_subtitle_search.py                  # search + download
python scripts/plex_subtitle_resync.py --path-map ... --workers 2
```

Keep `--workers` low (2 is a good default). Each worker streams a whole audio
track off the NAS; three concurrent workers produced spurious alignment
failures that vanished at two.

## What counts as "has subtitles"

Only **non-forced text** subtitles (srt/ass) in the target language count.
VOBSUB and PGS are image formats - they cannot be resynced, searched, or
restyled, so treat them as missing if the goal is usable SRT. On a
DVD/Blu-ray-ripped library this typically triples the work: in one 4,280-item
library, 2,501 items had no subtitle at all and a further 1,652 had only
image-based ones.

## The guards, and why each exists

**Matching on runtime alone picks the wrong film.** Every one of these fitted
the runtime and was caught only by reading the downloaded dialogue:

| Library item | What was downloaded |
|---|---|
| A Murder of Quality (1991) | `A.Murder.of.Crows.1998` |
| Blur, The Best Of | `Best of the Best 4` (a martial-arts film) |
| Dirty Harry (1971) | `Dirty.Harry.Dead.Pool.1988` |

So a candidate must also *look* like the production:

- **SRT only**, and drop titles matching `commentary|karaoke|lyrics` - the
  top-scoring result is very often a commentary track.
- **Title similarity >= 0.6** against the film title or show name, after
  stripping release-scene noise (codecs, resolutions, fps, years, group tags).
- **Escape hatch**: if the candidate name has *no* content words left after
  stripping noise (`English_ 23_975 fps_ 1h57m36s`), skip the title test -
  there is nothing to judge, and that name was correct for Blade Runner.
- **For episodes**, an exact `SxxEyy` match satisfies the guard on its own;
  release names abbreviate show names ("DS9" scores 0.4 against "Deep Space
  Nine"). A *mismatched* `SxxEyy` rejects outright.
- **Coverage**: last cue at 85-102% of runtime, >= 20 cues.

**Known residual risk:** superset titles ("Dirty Harry" vs "Dirty Harry Dead
Pool") score 1.00 and slip through. Keep a `--blocklist` JSON of
`{label: [substrings]}` for those.

**Verify a suspect match** by fetching the stream and reading its first cues -
that is the only reliable test, and it is quick.

## PAL and framerate

A PAL transfer runs 25/23.976 = 4.27% shorter than the film-rate version, so
subtitles for the other transfer land 2-7.5% long. Those are usually the
*right* subtitles: the search script accepts them as `rescale`, and the resync
script fixes the framerate. Proved on 12 Monkeys (scale 0.959, both anchors
agreeing to 0.02s) and The Long Arm (scale 0.96).

## Why alignment is corroborated, not trusted

ffsubsync will confidently return a shift pinned to the edge of its +/-60s
search range when it cannot align at all. On sketch comedy with laughter
tracks it proposed -58s, +37.6s and -48.9s for subtitles that were verifiably
correct. Offsets clustered at +/-60 are the signature.

So the resync script **applies nothing that a second independent measurement
does not confirm**: it re-aligns the first and last thirds separately, and
requires those two anchors to agree. That slope is also the exact framerate
correction - a single global scale is not accurate enough, being perfect at the
midpoint yet unwatchable by the end of a feature.

Anchor passes must run with `--no-fix-framerate --max-offset-seconds 15`;
unconstrained they latch onto spurious alignments (-57.8s on a file whose true
residual was ~2.4s).

### Agreement is not corroboration — check each anchor is a real measurement

**A railed anchor is a failure report, not a number.** Two anchors that both
failed come back pinned at the same bound (`-14.99`, `-14.99`), so they *agree
perfectly* and sail through an agreement test. An audit of 127 applied fixes
found 15 resting on exactly this: +37.6s applied to a Fry & Laurie episode,
+27.2s to a Derren Brown episode whose anchors were both precisely -14.99.

So reject any anchor within `ANCHOR_RAIL_MARGIN` of `+/-ANCHOR_MAX_OFFSET`
*before* comparing them, and apply the same rule to the global pass at its own
±60 bound.

**Do not add a magnitude test on `oa`/`ob`.** They are residuals of the
already-shifted text, measured *before* the fit, and the fit passes exactly
through both — so both anchors end at ~0 by construction. A large but real
residual is what the refinement exists to remove, not evidence of a fault. Only
a railed anchor invalidates the result. One such test was added on a misreading
of these fields and would have held valid fixes.

Audit the applied set from the log rather than trusting it: `oa`/`ob` in each
`resynced` record cost nothing to re-read, and a railed pair is unmistakable.

## When no shift or scale can work: a different cut

Measure the error at **two** landmarks pinned in the VIDEO by frame-scanning
(`ffmpeg -ss N -t 40 -vf "fps=1,tile=8x5"`), then find the cue carrying each
line. Sketch title cards and hard scene cuts are exact to the second.

| both landmarks | meaning |
|---|---|
| near zero | correct |
| off by the same amount | pure shift — safe to apply |
| **errors differ** | **the subtitle is for a different cut** |

On *A Bit of Fry & Laurie* S02E04 the attached track measured `+0.4s` at 3:28
and `-17.8s` at 7:27 — the video's dinner scene runs ~18s longer than the
subtitle's. No offset or scale can fix that; a different release is needed.
ffsubsync had returned `-32.96s`, the wrong *sign* from what the user heard,
at a score indistinguishable from its correct answers. Any single correlation
number against a differently-cut episode is meaningless, so when a reported
offset and a measured one disagree in sign, suspect the cut before the timing.

## Reaching an episode the agent has mis-numbered

Plex searches the provider using the *agent's* episode identity, so when the
agent's episode list is wrong, the right subtitle is unreachable by default —
every candidate offered comes back for the wrong episode.

Pass a **`title` parameter** to the search endpoint to override the query with
free text:

```
/library/metadata/{rk}/subtitles?language=en&hearingImpaired=0&forced=3
                                &title=A Bit of Fry and Laurie S04E04
```

Only `title` works; `query`, `searchTitle`, `episode` and `year` are ignored.
The candidates it returns download and attach normally, onto whichever item you
searched from.

On *A Bit of Fry & Laurie* series 4 the agent omits the episode broadcast
5 March 1995 and lists the 19 March one twice, so slots 4-6 all searched one
episode ahead. The override retrieved the correct subtitle for slot 4, verified
by content and by two landmarks. **Correcting and locking the local metadata
does not help** — the provider keys on the upstream guid, not the local title.

If a download silently no-ops here, suspect quota before concluding the
identity is at fault: both fail the same way, and quota was the real cause once
when identity was blamed.

## The download quota is the real constraint

Candidates cannot be read before downloading, so testing them consumes the
provider quota. Exhausting it is silent: `PUT` returns 200, no stream ever
appears, and no activity registers. Batch candidate tests, and when downloads
start no-oping, stop — retrying burns nothing but time. Verification work
(landmarks, log audits, re-reading attached subtitles) needs no quota at all.

**A constant offset is undetectable from the file alone.** Title and duration
checks catch wrong films and wrong transfers; only listening catches a
correctly-shaped file sitting uniformly early. When the user reports one, ask
them to check the **end** specifically - errors concentrate there, and their
estimate of the *magnitude* is often well out (one reported "~500ms" for what
measured 2.5s).

## Choosing a VAD

`--vad auditok,silero` (default) tries the cheap detector first and escalates
only when it fails or returns a boundary value.

- **auditok** - fast, but roughly half of alignments fail. Only in use because
  `webrtcvad` has no wheel for Python 3.13+.
- **silero** - neural, far more reliable, several times slower. Needs torch.

## Metadata mismatches masquerade as sync problems

A large offset that no anchor confirms often means the *item is mis-matched in
Plex*, not that the subtitle is mistimed. A library item titled "1984" (1984)
turned out to be the 1954 BBC production; once re-matched, a correct subtitle
was found immediately and needed no alignment at all. Symptoms: `year`
disagreeing with `originallyAvailableAt`, a runtime far from the matched
title's, or a folder name unlike the title. Fix the match first.

## Recovering from a bad upload

Plex uploads **replace** the current subtitle irreversibly, which is why
originals are backed up to `backups/` as raw bytes before any upload. To
restore, re-upload the backup. If no backup exists, re-download the candidate
recorded as `kept` in the search state file.

## Plex API notes

- Bulk section listings omit `Stream` data - per-item `/library/metadata/{rk}`
  is required (concurrency ~12 is fine).
- Search results are **not fetchable** until downloaded (`/library/streams/{id}`
  404s), so verifying length means downloading first.
- Downloads are asynchronous - poll for a new stream after the `PUT`.
- Upload: `POST .../subtitles?title=X.srt&format=srt`, raw body,
  `Accept: text/plain, */*`. **500s unless the title ends `.srt`**; adding a
  `language` param or `Content-Type` header also 500s; a BOM in the body 406s.
- Downloaded/uploaded subtitles report no `file` path - they live in Plex's
  metadata store, not as sidecars, so removing them touches no media.
- Plex serves subtitles with **no charset**, so `requests` falls back to
  ISO-8859-1. Decoding with that and re-encoding as UTF-8 corrupts every
  non-ASCII character. Decode explicitly.
