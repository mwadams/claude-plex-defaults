#!/usr/bin/env python3
"""Align a Plex library's subtitles to the actual audio using ffsubsync.

For each item carrying a downloaded/uploaded subtitle: export it, align it
against the video's audio, and upload the corrected file.

THREE THINGS THIS SCRIPT EXISTS TO GET RIGHT
--------------------------------------------
1. A Plex upload REPLACES the current subtitle and cannot be undone from the
   server side, so every original is backed up locally BEFORE uploading.

2. VAD mis-latches. On laughter tracks and sparse dialogue, ffsubsync happily
   returns a shift pinned to the edge of its +/-60s search range - it proposed
   -58s for subtitles that were already correct. So no correction is applied
   unless a second, independent measurement agrees with it (see refine()).

3. A single global framerate scale is not accurate enough over a feature-length
   runtime. Aligning the first and last thirds separately and deriving the
   scale from the difference fixes drift that is inaudible at the midpoint but
   unwatchable by the end.

Usage:
    python plex_subtitle_resync.py --baseurl http://plex:32400 \
        --path-map /share/CACHEDEV1_DATA/=//nas/ --workers 2
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import requests

MIN_FIX = 0.15            # seconds; below this leave the subtitle alone
MAX_TRUST = 30.0          # never apply a shift larger than this
SCALE_TOL = 0.0002
MAX_ANCHOR_DIFF = 5.0     # sanity bound on drift measured between anchors
ANCHOR_MAX_OFFSET = 15    # anchors measure a residual, so bound their search
ANCHOR_RAIL_MARGIN = 0.6  # within this of +/-ANCHOR_MAX_OFFSET = failed, not measured
MAX_ANCHOR_RESIDUAL = 2.0 # both anchors agreeing but this far out = bad placement
GLOBAL_RAIL = 60.0        # the main pass's search bound; same rail logic applies
GLOBAL_RAIL_MARGIN = 3.0

OFF = re.compile(r'offset seconds:\s*(-?[\d.]+)')
SCL = re.compile(r'framerate scale factor:\s*([\d.]+)')
TS = re.compile(r'(\d\d):(\d\d):(\d\d),(\d\d\d) --> (\d\d):(\d\d):(\d\d),(\d\d\d)')

lock = threading.Lock()


def secs(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def fmt(t):
    t = max(t, 0.0)
    h, m, s = int(t // 3600), int(t % 3600 // 60), int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        s, ms = s + 1, 0
    return '%02d:%02d:%02d,%03d' % (h, m, s, ms)


def cue_times(text):
    return [secs(*g[:4]) for g in TS.findall(text)]


def blocks(text):
    """Split an SRT into (start, end, raw_block) triples.

    Anchored on the timestamp lines rather than blank-line separators: files
    using CRLF, missing trailing blanks, or an index on the same line all parse
    identically this way. Splitting on blank lines silently returned almost
    nothing for some providers' files, surfacing as a bogus 'too_few_cues' on a
    729-cue subtitle.
    """
    ms = list(TS.finditer(text))
    out = []
    for i, m in enumerate(ms):
        ls = text.rfind('\n', 0, m.start())
        bstart = text.rfind('\n', 0, ls) + 1 if ls > 0 else 0
        bend = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        if i + 1 < len(ms):
            ls2 = text.rfind('\n', 0, bend)
            bend = text.rfind('\n', 0, ls2) + 1 if ls2 > 0 else bend
        out.append((secs(*m.groups()[:4]), secs(*m.groups()[4:]),
                    text[bstart:bend].strip('\n')))
    return out


def subset_srt(bl, lo, hi):
    """Renumbered SRT of only the cues starting within [lo, hi)."""
    keep = [b for b in bl if lo <= b[0] < hi]
    parts = []
    for i, (_, _, blk) in enumerate(keep, 1):
        body = blk.split('\n')
        j = next(k for k, l in enumerate(body) if '-->' in l)
        parts.append(str(i) + '\n' + '\n'.join(body[j:]))
    return '\n\n'.join(parts) + '\n', len(keep)


def apply_linear(text, scale, shift):
    def sub(m):
        a = secs(*m.groups()[:4]) * scale + shift
        b = secs(*m.groups()[4:]) * scale + shift
        return f'{fmt(a)} --> {fmt(b)}'
    return TS.sub(sub, text)


class Aligner:
    def __init__(self, vads):
        self.vads = vads          # tried in order; first plausible result wins

    def run(self, video, fin, fout, vad, anchor=False, timeout=3600):
        cmd = [sys.executable, '-c',
               'import sys; from ffsubsync import main; '
               'sys.argv=["ffsubsync"]+sys.argv[1:]; sys.exit(main())',
               video, '-i', fin, '-o', fout, '--vad', vad,
               '--skip-sync-on-low-quality']
        if anchor:
            # An anchor measures a small residual over a subset of cues. Letting
            # ffsubsync re-fit framerate there makes it latch onto spurious
            # far-off alignments, so pin the scale and bound the search.
            cmd += ['--no-fix-framerate', '--max-offset-seconds', str(ANCHOR_MAX_OFFSET)]
        try:
            # its rich output is UTF-8; Windows would otherwise decode cp1252
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                               encoding='utf-8', errors='replace')
        except subprocess.TimeoutExpired:
            return None, None, 'timeout'
        out = (p.stdout or '') + (p.stderr or '')
        mo, ms = OFF.search(out), SCL.search(out)
        if p.returncode != 0 or not mo:
            return None, None, f'rc={p.returncode} ' + (out.strip()[-300:] or '<no output>')
        return float(mo.group(1)), float(ms.group(1)) if ms else 1.0, None

    def align(self, video, fin, fout, anchor=False):
        """Try each VAD in turn; escalate only when a cheap one gives nothing usable.

        The cheap detector (auditok) handles most files in a fraction of the
        time; the neural one (silero) is several times slower and is only worth
        paying for when the cheap one fails or returns a boundary value.
        """
        last = (None, None, 'no vad ran')
        for vad in self.vads:
            off, scale, err = self.run(video, fin, fout, vad, anchor=anchor)
            last = (off, scale, err)
            if off is not None and abs(off) < MAX_TRUST:
                return off, scale, None, vad
        return last[0], last[1], last[2], self.vads[-1]


class Plex:
    def __init__(self, baseurl, token):
        self.b = baseurl.rstrip('/')
        self.s = requests.Session()
        self.s.headers.update({'X-Plex-Token': token})

    def meta(self, rk):
        r = self.s.get(f'{self.b}/library/metadata/{rk}',
                       headers={'Accept': 'application/json'}, timeout=90)
        r.raise_for_status()
        return r.json()['MediaContainer']['Metadata'][0]

    def sub_streams(self, rk):
        d = self.meta(rk)
        return d, [s for m in d.get('Media', []) for p in m.get('Part', [])
                   for s in p.get('Stream', []) if s.get('streamType') == 3 and s.get('key')]

    def fetch_sub(self, key):
        """Return (text, raw_bytes).

        Plex serves subtitles with no charset, so requests falls back to
        ISO-8859-1. Decoding with that and re-encoding as UTF-8 corrupts every
        non-ASCII character, so decode explicitly and keep the original bytes.
        """
        r = self.s.get(self.b + key, timeout=90)
        raw = r.content
        for enc in ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1'):
            try:
                return raw.decode(enc).lstrip('﻿'), raw
            except UnicodeDecodeError:
                continue
        return raw.decode('latin-1', errors='replace').lstrip('﻿'), raw

    def upload(self, rk, title, text):
        # Plex 500s unless the title ends .srt, and a stray BOM in the body 406s.
        return self.s.post(f'{self.b}/library/metadata/{rk}/subtitles',
                           params={'title': f'{title}.srt', 'format': 'srt'},
                           data=text.lstrip('﻿').encode('utf-8'),
                           headers={'Accept': 'text/plain, */*'}, timeout=180)


def refine(aligner, video, tmp, synced_text, dur):
    """Two-anchor linear fit -> (scale, shift, info) or (None, None, why).

    Aligning the first and last thirds separately gives two residual offsets;
    the slope between them is the exact framerate correction, and their
    agreement is the corroboration that the whole alignment is real.
    """
    bl = blocks(synced_text)
    if len(bl) < 80:
        return None, None, 'too_few_cues'
    res = {}
    for tag, (lo, hi) in (('a', (0.0, dur * 0.30)), ('b', (dur * 0.70, dur))):
        txt, n = subset_srt(bl, lo, hi)
        if n < 30:
            return None, None, f'sparse_{tag}'
        p_in = os.path.join(tmp, f'{tag}.srt')
        p_out = os.path.join(tmp, f'{tag}_o.srt')
        open(p_in, 'w', encoding='utf-8').write(txt)
        off, _, err, _ = aligner.align(video, p_in, p_out, anchor=True)
        if off is None:
            return None, None, f'anchor_{tag}_failed'
        times = cue_times(txt)
        res[tag] = (off, sum(times) / len(times))
    (oa, ta), (ob, tb) = res['a'], res['b']
    # An anchor pinned to the edge of its search window is not a measurement -
    # it is ffsubsync reporting that it could not align at all. Two such
    # failures both railed at -14.99 AGREE PERFECTLY, so the agreement test
    # below cannot catch them: it was passing them as corroborated. Reject any
    # railed anchor before comparing them. This was applying confident,
    # wholly unfounded shifts (+37.6s on one, +27.2s on another whose anchors
    # were both exactly -14.99).
    for tag, val in (('a', oa), ('b', ob)):
        if abs(abs(val) - ANCHOR_MAX_OFFSET) < ANCHOR_RAIL_MARGIN:
            return None, None, f'anchor_{tag}_railed'
    if abs(ob - oa) > MAX_ANCHOR_DIFF or tb - ta < 300:
        return None, None, 'anchors_inconsistent'
    # oa/ob are residuals of the ALREADY-SHIFTED text, measured before this
    # fit. The line below passes exactly through both, so both anchors end at
    # ~0 by construction: a large but REAL residual here is what the
    # refinement exists to remove, not a fault. Only a railed (unmeasurable)
    # anchor invalidates the result. Do not add a magnitude test here - one was
    # added on a misreading of these fields and would have held valid fixes.
    scale = 1.0 + (ob - oa) / (tb - ta)
    shift = oa - (scale - 1.0) * ta
    return scale, shift, {'oa': round(oa, 3), 'ob': round(ob, 3),
                          'ta': round(ta), 'tb': round(tb)}


def process(plex, aligner, rk, label, state, opts):
    try:
        d = plex.meta(rk)
    except Exception as e:
        return {'event': 'meta_fail', 'label': label, 'err': str(e)[:150]}

    med = d['Media'][0]
    dur = med['duration'] / 1000
    video = opts['map'](med['Part'][0].get('file'))
    if not video or not os.path.exists(video):
        state[rk] = {'r': 'no_video_path', 'label': label}
        return {'event': 'no_video_path', 'label': label, 'path': med['Part'][0].get('file')}

    _, subs = plex.sub_streams(rk)
    if not subs:
        state[rk] = {'r': 'no_subtitle', 'label': label}
        return {'event': 'no_subtitle', 'label': label}

    # Pick deterministically. An item can carry more than one downloaded
    # subtitle, and taking subs[-1] made the same file measure +56.66s on one
    # run and +1.98s on the next. Prefer the fullest non-forced track.
    cands = []
    for s in subs:
        if s.get('forced'):
            continue
        t, rw = plex.fetch_sub(s['key'])
        cands.append((len(TS.findall(t)), s['id'], s, t, rw))
    if not cands:
        state[rk] = {'r': 'no_usable_sub', 'label': label}
        return {'event': 'no_usable_sub', 'label': label}
    cands.sort(key=lambda c: (c[0], c[1]), reverse=True)
    _, _, src, text, raw = cands[0]
    dupes = [c[2] for c in cands[1:]]

    tmp = tempfile.mkdtemp(prefix='resync_')
    t0 = time.time()
    try:
        fin, fout = os.path.join(tmp, 'in.srt'), os.path.join(tmp, 'out.srt')
        open(fin, 'w', encoding='utf-8').write(text)
        offset, scale, err, vad = aligner.align(video, fin, fout)
        if offset is None:
            state[rk] = {'r': 'align_fail', 'label': label}
            return {'event': 'align_fail', 'label': label, 'tail': err}
        # Same rail logic as the anchors: a global offset sitting on the
        # search bound means ffsubsync failed, not that the file is a minute
        # out. Check this before MAX_TRUST so the log says WHY it was held.
        if abs(abs(offset) - GLOBAL_RAIL) < GLOBAL_RAIL_MARGIN:
            state[rk] = {'r': 'align_railed', 'label': label, 'offset': offset}
            return {'event': 'align_railed', 'label': label,
                    'offset': round(offset, 2), 'scale': scale, 'vad': vad}
        if abs(offset) > MAX_TRUST:
            state[rk] = {'r': 'needs_review', 'label': label, 'offset': offset}
            return {'event': 'needs_review', 'label': label,
                    'offset': round(offset, 2), 'scale': scale, 'vad': vad}

        final = open(fout, encoding='utf-8').read()

        # Nothing worth changing: skip the refinement cost entirely.
        if abs(offset) < MIN_FIX and abs(scale - 1.0) <= SCALE_TOL:
            state[rk] = {'r': 'already_aligned', 'label': label, 'offset': offset}
            return {'event': 'already_aligned', 'label': label, 'offset': offset,
                    'secs': round(time.time() - t0)}

        # A correction IS proposed, so corroborate it before touching anything.
        rs, sh, inf = refine(aligner, video, tmp, final, dur)
        if rs is None:
            state[rk] = {'r': 'needs_review', 'label': label, 'offset': offset, 'why': inf}
            return {'event': 'needs_review', 'label': label, 'offset': round(offset, 2),
                    'scale': scale, 'why': inf, 'secs': round(time.time() - t0)}
        final = apply_linear(final, rs, sh)

        if opts['dry_run']:
            state[rk] = {'r': 'would_resync', 'label': label, 'offset': offset}
            return {'event': 'would_resync', 'label': label, 'offset': round(offset, 3),
                    'scale': scale, 'scale2': round(rs, 6), 'shift2': round(sh, 3)}

        # Back up the ORIGINAL BYTES first - the upload replaces it irreversibly.
        os.makedirs(opts['backup'], exist_ok=True)
        safe = re.sub(r'[^A-Za-z0-9 _.-]', '_', label)[:80]
        with open(os.path.join(opts['backup'], f'{rk}_{safe}.orig.srt'), 'wb') as f:
            f.write(raw)

        # Keep the SOURCE name in the title. Naming the output after the
        # library item destroys the provenance: a wrong-show subtitle that gets
        # resynced then appears in Plex as a correctly-named English SRT, which
        # is how 37 Queer Eye and Private Eyes tracks sat on Public Eye looking
        # legitimate. The source name is the only evidence of where it came
        # from, and it survives nothing else.
        src_name = (src.get('title') or '').strip()
        out_name = f'{src_name} (synced)' if src_name else f'{label} (synced)'
        r = plex.upload(rk, out_name, final)
        if r.status_code >= 300:
            state[rk] = {'r': 'upload_fail', 'label': label, 'code': r.status_code}
            return {'event': 'upload_fail', 'label': label, 'code': r.status_code}

        # Clear the tracks this one supersedes, but only once the replacement
        # is confirmed present.
        removed = []
        if opts.get('cleanup'):
            _, now = plex.sub_streams(rk)
            stale = {s['id'] for s in ([src] + dupes)}
            if any(s['id'] not in stale for s in now):
                for s in now:
                    if s['id'] in stale:
                        dr = plex.s.delete(plex.b + s['key'], timeout=90)
                        removed.append(f"{s.get('title')} ({dr.status_code})")

        state[rk] = {'r': 'resynced', 'label': label, 'offset': offset, 'scale': scale}
        return {'event': 'resynced', 'label': label, 'offset': round(offset, 3),
                'removed': removed or None,
                'scale': scale, 'vad': vad, 'scale2': round(rs, 6),
                'shift2': round(sh, 3), 'secs': round(time.time() - t0), **inf}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--baseurl', default=os.environ.get('PLEX_BASEURL', 'http://localhost:32400'))
    ap.add_argument('--token', default=os.environ.get('PLEX_TOKEN'))
    ap.add_argument('--state', default='subtitle_state.json',
                    help='state file written by plex_subtitle_search.py')
    ap.add_argument('--workdir', default='.')
    ap.add_argument('--path-map', action='append', default=[],
                    help='PLEXPREFIX=LOCALPREFIX, repeatable; maps the server\'s '
                         'file paths onto paths this machine can read')
    ap.add_argument('--vad', default='auditok,silero',
                    help='comma-separated, tried in order. auditok is fast but '
                         'fails often; silero is far better and several times '
                         'slower, so the default escalates only when needed.')
    ap.add_argument('--workers', type=int, default=2,
                    help='keep low: each worker streams a whole audio track')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--keys', help='comma-separated rating keys: resync ONLY these, and '
                                   'redo them even if a previous verdict exists. Use after '
                                   'attaching a subtitle by hand, which the sweep state '
                                   'knows nothing about.')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--no-cleanup', action='store_true',
                    help='keep the subtitle tracks a resync supersedes '
                         '(default removes them once the replacement is confirmed)')
    args = ap.parse_args()

    if not args.token:
        sys.exit('No token: pass --token or set PLEX_TOKEN')

    maps = []
    for m in args.path_map:
        a, _, b = m.partition('=')
        maps.append((a, b.replace('/', os.sep)))

    def mapper(path):
        if not path:
            return None
        for a, b in maps:
            if path.startswith(a):
                return b + path[len(a):].replace('/', os.sep)
        return path if os.path.exists(path) else None

    plex = Plex(args.baseurl, args.token)
    aligner = Aligner(args.vad.split(','))
    rstate_path = os.path.join(args.workdir, 'resync_state.json')
    log_path = os.path.join(args.workdir, 'resync_log.jsonl')
    sweep = json.load(open(args.state))
    state = json.load(open(rstate_path)) if os.path.exists(rstate_path) else {}
    opts = {'map': mapper, 'backup': os.path.join(args.workdir, 'backups'),
            'dry_run': args.dry_run, 'cleanup': not args.no_cleanup}

    if args.keys:
        # Named explicitly: bypass the sweep state entirely. A hand-attached
        # subtitle never went through the search, so it has no 'matched'
        # record, and any stale verdict must not suppress the rerun.
        todo = []
        for rk in [k.strip() for k in args.keys.split(',') if k.strip()]:
            state.pop(rk, None)
            d = plex.meta(rk)
            todo.append((rk, f"{d.get('grandparentTitle')} S{d.get('parentIndex')}"
                             f"E{d.get('index')} {d.get('title')}"))
    else:
        todo = [(rk, v['label']) for rk, v in sweep.items()
                if isinstance(v, dict) and v.get('r') == 'matched' and rk not in state]
    todo.sort(key=lambda x: x[1])
    if args.limit:
        todo = todo[:args.limit]
    print(f'{len(todo)} items to resync, {args.workers} workers, vad={args.vad}', flush=True)

    idx = [0]

    def worker():
        while True:
            with lock:
                if idx[0] >= len(todo):
                    return
                i = idx[0]
                idx[0] += 1
            rk, label = todo[i]
            try:
                out = process(plex, aligner, rk, label, state, opts)
            except Exception as e:
                out = {'event': 'error', 'label': label, 'err': str(e)[:200]}
                state[rk] = {'r': 'error', 'label': label}
            with lock:
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(out) + '\n')
                json.dump(state, open(rstate_path, 'w'))
            print(f"[{i+1}/{len(todo)}] {out['event']:<16} {label[:58]}", flush=True)

    ts = [threading.Thread(target=worker, daemon=True) for _ in range(args.workers)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    json.dump(state, open(rstate_path, 'w'))
    print('RESYNC DONE', flush=True)


if __name__ == '__main__':
    main()
