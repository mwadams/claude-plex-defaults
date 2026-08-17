#!/usr/bin/env python3
"""Find and download English subtitles for Plex items that have none.

Scans movie/show sections, finds items with no usable text subtitle, searches
Plex's subtitle provider, and downloads the first candidate that passes the
guards below. Resumable: state is keyed by rating key, so re-running skips
everything already decided.

WHY THE GUARDS EXIST - matching on runtime alone picks the WRONG FILM. Real
examples, each of which fitted the runtime and was caught only by reading the
downloaded dialogue:

    "A Murder of Quality" (1991) -> A.Murder.of.Crows.1998
    "Blur, The Best Of"          -> Best of the Best 4 (a martial-arts film)
    "Dirty Harry" (1971)         -> Dirty.Harry.Dead.Pool.1988

So a candidate must ALSO look like this production by name. See --help.

Usage:
    python plex_subtitle_search.py --baseurl http://plex:32400 [--sections 1,2]
    python plex_subtitle_search.py --dry-run          # report only
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

# ---------------------------------------------------------------- guards ----

MIN_COVER, MAX_COVER, MIN_CUES = 0.85, 1.02, 20

# A PAL transfer runs 25/23.976 = 4.27% shorter than the film-rate version, so
# subtitles timed for the other transfer land just outside the normal band.
# Those are usually the RIGHT subtitles needing a rescale, which the resync
# script does. Accept them rather than discarding.
PAL_BANDS = ((1.021, 1.075), (0.930, 0.975))

MIN_TITLE_SIM = 0.6
# An exact SxxEyy lowers the name bar rather than removing it. 0.3 keeps the
# abbreviations this exists for ("DS9" scores 0.4 against "Deep Space Nine")
# while rejecting names that share nothing with the show OR the episode.
SE_TITLE_FLOOR = 0.3
BAD_TITLE = re.compile(r'commentary|karaoke|lyrics', re.I)

STOP = {'the', 'a', 'an', 'of', 'and', 'or', 'to', 'in', 'at', 'on', 'for',
        'with', 'from', 'part', 'disc', 'is', 'it', 'my', 'me', 'you', 'we',
        'they', 'he', 'she', 'his', 'her', 'their', 'episode'}

NOISE = set('''bluray blu ray brrip bdrip dvdrip dvd webrip web hdtv hdrip remux
uhd hdr sdr x264 x265 h264 h265 hevc xvid divx avc aac ac3 dts hdma ddp eac3
flac mp3 truehd atmos 1080p 720p 2160p 480p 576p 1080i 4k fps ntsc pal repack
proper extended uncut theatrical internal limited unrated remastered
anniversary edition cut version subs sub subtitles eng english en srt hi nonhi
sdh forced track disc disk cd yts yify rarbg ettv mess part chapter final
directors director special'''.split())

TEXT_CODECS = {'srt', 'ass', 'ssa', 'subrip', 'webvtt'}
TS = re.compile(r'(\d\d):(\d\d):(\d\d)[,.](\d\d\d)\s*-->\s*(\d\d):(\d\d):(\d\d)[,.](\d\d\d)')
SE = re.compile(r'[sS](\d{1,2})[ ._-]?[eExX](\d{1,3})')


def norm(s):
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower())


def sig_words(s):
    return [w for w in norm(s).split() if w not in STOP and len(w) >= 3]


def content_words(s):
    """Significant words with release-scene metadata stripped out."""
    out = []
    for w in sig_words(s):
        if w in NOISE or re.fullmatch(r'\d{3,4}', w) or re.fullmatch(r'[\dhms_.]+', w):
            continue
        out.append(w)
    return out


def usable_title(t):
    """Does this title carry words of its own, or is it a placeholder?

    Plex names untitled episodes "Episode 7". Stripped of stopwords and digits
    that leaves nothing, and an empty anchor matches every candidate perfectly
    - "Episode 7" scored 1.00 against "Fresh.Fried.and.Crispy.S01E07". Such a
    title must not be allowed to vouch for anything.
    """
    if not t:
        return False
    return any(w not in STOP and w not in NOISE and not w.isdigit()
               for w in re.findall(r"[a-z0-9']+", t.lower()))


def title_sim(want, cand):
    """Fraction of the media title's significant words present in the candidate.

    Returns None when the candidate name has no content words left after
    stripping release noise (e.g. 'English_ 23_975 fps_ 1h57m36s'). There is
    nothing to judge there, so callers fall back to trusting the provider match
    plus the duration check - that name was correct for Blade Runner.
    """
    ws = sig_words(want)
    if not ws:
        return 1.0
    if not content_words(cand):
        return None
    flat = norm(cand).replace(' ', '')
    return sum(1 for w in ws if w in flat) / len(ws)


def cover_ok(cover):
    if MIN_COVER <= cover <= MAX_COVER:
        return 'fit'
    for lo, hi in PAL_BANDS:
        if lo <= cover <= hi:
            return 'rescale'
    return None


def cue_end(text):
    t = TS.findall(text)
    if not t:
        return None
    end = max(int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7]) / 1000 for g in t)
    return end, len(t)


# ------------------------------------------------------------------ plex ----

class Plex:
    def __init__(self, baseurl, token):
        self.b = baseurl.rstrip('/')
        self.s = requests.Session()
        self.s.headers.update({'X-Plex-Token': token, 'Accept': 'application/json'})

    def req(self, method, path, **kw):
        for attempt in range(4):
            try:
                r = self.s.request(method, self.b + path, timeout=180, **kw)
                if r.status_code < 500:
                    return r
            except requests.RequestException:
                pass
            time.sleep(3 * (attempt + 1))
        return None

    def get(self, path, **params):
        r = self.req('GET', path, params=params)
        r.raise_for_status()
        return r.json().get('MediaContainer', {})

    def page(self, path, typ):
        out, start = [], 0
        while True:
            mc = self.get(path, type=typ, **{'X-Plex-Container-Start': start,
                                             'X-Plex-Container-Size': 300})
            md = mc.get('Metadata', [])
            out += md
            start += len(md)
            if not md or start >= mc.get('totalSize', 0):
                break
        return out

    def sub_streams(self, rk):
        mc = self.get(f'/library/metadata/{rk}')
        d = mc['Metadata'][0]
        return d, [s for m in d.get('Media', []) for p in m.get('Part', [])
                   for s in p.get('Stream', []) if s.get('streamType') == 3]


# ------------------------------------------------------------------ main ----

def needs_subs(streams, lang):
    for s in streams:
        code = (s.get('languageCode') or s.get('language') or '').lower()
        if code.startswith(lang) and not s.get('forced') and (s.get('codec') or '') in TEXT_CODECS:
            return False
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--baseurl', default=os.environ.get('PLEX_BASEURL', 'http://localhost:32400'))
    ap.add_argument('--token', default=os.environ.get('PLEX_TOKEN'))
    ap.add_argument('--sections', help='comma-separated section keys (default: all movie/show)')
    ap.add_argument('--language', default='en', help='ISO 639-1 language (default en)')
    ap.add_argument('--workdir', default='.', help='where state/log files live')
    ap.add_argument('--max-candidates', type=int, default=6)
    ap.add_argument('--dry-run', action='store_true', help='report what lacks subtitles, download nothing')
    ap.add_argument('--blocklist', help='JSON {label: [substrings]} of candidates to never accept')
    ap.add_argument('--keys', help='comma-separated rating keys: search ONLY these items. '
                                   'Implies --retry for them, since you are naming them '
                                   'deliberately (e.g. after deleting a wrong-episode track).')
    args = ap.parse_args()

    if not args.token:
        sys.exit('No token: pass --token or set PLEX_TOKEN')

    plex = Plex(args.baseurl, args.token)
    state_path = os.path.join(args.workdir, 'subtitle_state.json')
    log_path = os.path.join(args.workdir, 'subtitle_log.jsonl')
    state = json.load(open(state_path)) if os.path.exists(state_path) else {}
    block = json.load(open(args.blocklist)) if args.blocklist else {}

    def rec(**kw):
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(kw) + '\n')

    # ---- discover items
    only = set(k.strip() for k in args.keys.split(',')) if args.keys else None
    dirs = plex.get('/library/sections').get('Directory', [])
    keys = args.sections.split(',') if args.sections else None
    items = []
    if only:
        # Named explicitly: fetch just those, skip the whole-library sweep. Drop
        # any prior verdict so a rerun actually retries them - the usual reason
        # for naming keys is that the previous answer was wrong.
        for rk in sorted(only):
            d, _ = plex.sub_streams(rk)
            items.append((d.get('librarySectionTitle') or '?', {'ratingKey': rk}))
            state.pop(str(rk), None)
        print(f'targeting {len(items)} named item(s)', file=sys.stderr)
    for d in [] if only else dirs:
        if d['type'] not in ('movie', 'show'):
            continue
        if keys and d['key'] not in keys:
            continue
        typ = 1 if d['type'] == 'movie' else 4      # 4 = episode
        md = plex.page(f"/library/sections/{d['key']}/all", typ)
        print(f"{d['title']}: {len(md)}", file=sys.stderr)
        items += [(d['title'], m) for m in md]

    # ---- find the ones lacking subtitles (bulk listings omit Stream data)
    def probe(arg):
        section, m = arg
        try:
            d, streams = plex.sub_streams(m['ratingKey'])
        except Exception:
            return None
        # When keys are named explicitly the caller has already decided these
        # need attention, so do not second-guess them on existing streams.
        if not only and not needs_subs(streams, args.language):
            return None
        label = d.get('title') if d.get('type') == 'movie' else \
            f"{d.get('grandparentTitle')} S{d.get('parentIndex')}E{d.get('index')} {d.get('title')}"
        return {'rk': str(m['ratingKey']), 'label': label, 'section': section,
                'type': d.get('type'), 'show': d.get('grandparentTitle'),
                'season': d.get('parentIndex'), 'ep': d.get('index'),
                'ep_title': d.get('title'),
                'duration': (d.get('duration') or 0) / 1000}

    with ThreadPoolExecutor(max_workers=12) as ex:
        todo = [r for r in ex.map(probe, items) if r]
    print(f'{len(todo)} items lack a usable {args.language} text subtitle', file=sys.stderr)

    if args.dry_run:
        for t in todo:
            print(f"  {t['section']:<18} {t['label']}")
        return

    # ---- search, verify, download
    consec_fail = 0
    for n, it in enumerate(todo, 1):
        rk, label = it['rk'], it['label']
        if rk in state:
            continue
        dur = it['duration']
        if dur < 60:
            state[rk] = {'r': 'skip_no_duration', 'label': label}
            continue

        r = plex.req('GET', f'/library/metadata/{rk}/subtitles',
                     params={'language': args.language, 'hearingImpaired': 0, 'forced': 3})
        if r is None or r.status_code != 200:
            consec_fail += 1
            rec(event='search_fail', label=label)
            if consec_fail >= 15:
                rec(event='ABORT', why='15 consecutive search failures')
                break
            continue

        cands = [c for c in r.json()['MediaContainer'].get('Stream', [])
                 if (c.get('format') == 'srt' or c.get('codec') == 'srt')
                 and not BAD_TITLE.search(c.get('title') or '')]
        for pat in block.get(label, []) + block.get(rk, []):
            cands = [c for c in cands if pat.lower() not in (c.get('title') or '').lower()]

        anchor = it['show'] or label if it['type'] == 'episode' else label
        ep_title = it.get('ep_title') if it['type'] == 'episode' else None
        if not usable_title(ep_title):
            ep_title = None    # "Episode 7" has no words of its own and would
                               # score 1.00 against anything at all
        se = (int(it['season']), int(it['ep'])) if it.get('season') is not None \
            and it.get('ep') is not None else None

        keep = []
        for c in cands:
            ct = c.get('title') or ''
            se_ok = False
            if se:
                found = SE.findall(ct)
                if found:
                    if not any((int(a), int(b)) == se for a, b in found):
                        continue                      # explicitly a different episode
                    se_ok = True                      # explicitly THIS episode
            sim = title_sim(anchor, ct)
            # Score the EPISODE title too: releases legitimately drop the show
            # name but keep the episode ("S03E01 - Aftermath.eng").
            sim_ep = title_sim(ep_title, ct) if ep_title else None
            best = max([s for s in (sim, sim_ep) if s is not None], default=None)
            # An exact SxxEyy hit LOWERS the bar - release names abbreviate show
            # names ("DS9" scores 0.4 against "Deep Space Nine") - but it must
            # not remove the bar. Accepting SxxEyy on its own let 225 wrong-show
            # subtitles into one library: Ace of Wands took Record of Ragnarok
            # and Elena of Avalor, Beasts took Beast Games. Each matched the
            # episode number and a plausible runtime, and nothing else.
            floor = SE_TITLE_FLOOR if se_ok else MIN_TITLE_SIM
            if best is not None and best < floor:
                continue
            c['_sim'] = best
            keep.append(c)
        keep.sort(key=lambda c: -int(c.get('score') or 0))

        if not keep:
            state[rk] = {'r': 'no_candidates', 'label': label}
            rec(event='none', label=label)
            consec_fail = 0
            json.dump(state, open(state_path, 'w'))
            continue

        _, cur = plex.sub_streams(rk)
        before = {s['id'] for s in cur}
        outcome = {'r': 'no_match', 'label': label, 'tried': []}
        for c in keep[:args.max_candidates]:
            plex.req('PUT', f'/library/metadata/{rk}/subtitles', params={'key': c['key']})
            new = None
            for _ in range(25):                        # download is asynchronous
                time.sleep(2)
                _, cur = plex.sub_streams(rk)
                fresh = [s for s in cur if s['id'] not in before]
                if fresh:
                    new = fresh[-1]
                    break
            if not new:
                outcome['tried'].append({'t': c.get('title'), 'why': 'download_failed'})
                consec_fail += 1
                if consec_fail >= 15:
                    break
                continue
            consec_fail = 0
            before.add(new['id'])
            tr = plex.req('GET', new['key'])
            span = cue_end(tr.text) if tr is not None and tr.status_code == 200 else None
            cover = (span[0] / dur) if span else 0
            kind = cover_ok(cover) if span and span[1] >= MIN_CUES else None
            outcome['tried'].append({'t': c.get('title'), 'score': c.get('score'),
                                     'sim': c.get('_sim'), 'cover': round(cover, 3),
                                     'cues': span[1] if span else 0, 'kind': kind})
            if kind:
                outcome.update(r='matched', kept=c.get('title'), cover=round(cover, 3),
                               cues=span[1], kind=kind)
                break
            plex.req('DELETE', new['key'])

        state[rk] = outcome
        json.dump(state, open(state_path, 'w'))
        rec(event=outcome['r'], label=label, dur=round(dur),
            **{k: outcome[k] for k in ('kept', 'cover', 'cues', 'kind') if k in outcome})
        print(f"[{n}/{len(todo)}] {outcome['r']:<14} {label[:64]}", flush=True)
        if consec_fail >= 15:
            rec(event='ABORT', why='15 consecutive download failures (provider quota?)')
            break

    json.dump(state, open(state_path, 'w'))
    print('DONE', flush=True)


if __name__ == '__main__':
    main()
