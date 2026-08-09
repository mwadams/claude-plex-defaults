#!/usr/bin/env python3
"""Set default audio to 5.1 surround (when available) and turn subtitles
on or off, per user, for every item in the given Plex libraries.

Only Plex's per-account stream selections are changed (via the Plex HTTP API);
media files are never touched. Dry run by default -- pass --apply to write.
Every change is logged to a CSV so it can be reverted with --revert.

Stream selections are per Plex account. Pass --users to apply for specific
Plex Home / shared users (their tokens are fetched from plex.tv using the
owner token you supply, so --token must be the server owner's token).
Subtitles are turned OFF by default; users listed in --subs-on instead get
the best full subtitle track (in the preferred language) selected.

Usage:
  export PLEX_TOKEN=...             # or:  set PLEX_TOKEN=...  /  --token ...
  python plex_set_defaults.py --baseurl http://myserver:32400 --limit 5
  python plex_set_defaults.py --baseurl http://myserver:32400 \
      --users alice bob carol --subs-on bob            # full dry run
  python plex_set_defaults.py --baseurl http://myserver:32400 \
      --users alice bob carol --subs-on bob --apply    # do it
  python plex_set_defaults.py --revert plex_defaults_log_XXXX.csv --apply
"""
import argparse
import csv
import os
import re
import sys
import time
from urllib.parse import urlencode

try:
    from plexapi.server import PlexServer
except ImportError:
    sys.exit("plexapi is not installed. Run:  pip install plexapi")

COMMENTARY_MARKERS = ("commentary", "descriptive", "description", "narration")
OWNER_LABEL = "(token account)"


def describe(stream):
    if stream is None:
        return "(none)"
    lang = stream.languageCode or "und"
    ch = stream.channels if getattr(stream, "channels", None) else "?"
    codec = stream.codec or "?"
    title = stream.title or stream.displayTitle or ""
    return f"#{stream.id} {codec} {ch}ch {lang} {title}".strip()


def is_commentary(stream):
    text = " ".join(
        filter(None, [stream.title, stream.displayTitle,
                      getattr(stream, "extendedDisplayTitle", None)])
    ).lower()
    return any(m in text for m in COMMENTARY_MARKERS)


def pick_audio(streams, prefer_langs):
    """Best surround track: >=6 channels, not commentary.
    Rank: preferred language first, exactly 5.1 (6ch) before 7.1, then bitrate.
    Returns None if the item has no surround track (leave selection alone)."""
    candidates = [s for s in streams
                  if (getattr(s, "channels", 0) or 0) >= 6 and not is_commentary(s)]
    if not candidates:
        return None

    def rank(s):
        lang = (s.languageCode or "").lower()
        lang_rank = prefer_langs.index(lang) if lang in prefer_langs else len(prefer_langs)
        exact_51 = 0 if s.channels == 6 else 1
        return (lang_rank, exact_51, -(getattr(s, "bitrate", 0) or 0))

    return sorted(candidates, key=rank)[0]


def pick_subtitle(streams, prefer_langs):
    """Best full subtitle track: preferred language (unknown-language as a
    last resort), not forced, not commentary; plain subs ranked before
    SDH/CC. Returns None if there is no suitable track (leave alone)."""
    def lang_rank(s):
        lang = (s.languageCode or "").lower()
        if lang in prefer_langs:
            return prefer_langs.index(lang)
        if lang in ("", "und"):
            return len(prefer_langs)
        return None  # some other language -- not a candidate

    def is_sdh(s):
        if getattr(s, "hearingImpaired", False):
            return True
        text = " ".join(filter(None, [s.title, s.displayTitle])).lower()
        return bool({"sdh", "cc", "hearing"} & set(re.split(r"[^a-z0-9]+", text)))

    candidates = [s for s in streams
                  if lang_rank(s) is not None
                  and not getattr(s, "forced", False)
                  and not is_commentary(s)]
    if not candidates:
        return None
    return sorted(candidates, key=lambda s: (lang_rank(s), 1 if is_sdh(s) else 0))[0]


def put_streams(server, part_id, audio_id=None, subtitle_id=None):
    """Raw PUT so this works across plexapi versions. subtitle_id=0 means off."""
    params = {"allParts": 1}
    if audio_id is not None:
        params["audioStreamID"] = audio_id
    if subtitle_id is not None:
        params["subtitleStreamID"] = subtitle_id
    server.query(f"/library/parts/{part_id}?{urlencode(params)}",
                 method=server._session.put)


def home_user_token(account, name):
    """Token for a managed (restricted) Plex Home profile via the v2 API --
    these don't appear in the legacy users list. None if no title matches.

    NOTE: a profile that has NEVER signed in on this server may still get
    401 from the PMS -- that means it has no server access yet; fix that in
    Plex Web (Manage Library Access), not here."""
    data = account.query("https://plex.tv/api/v2/home/users")
    for el in data.iter("user"):
        attrs = el.attrib
        if (attrs.get("title") or "").lower() != name.lower():
            continue
        if attrs.get("protected") == "1":
            sys.exit(f"Home user '{name}' is PIN-protected; this script does "
                     "not support PIN switching.")
        # v2 switch wants the uuid; some deployments only have the v1 path.
        urls = []
        if attrs.get("uuid"):
            urls.append(f"https://plex.tv/api/v2/home/users/{attrs['uuid']}/switch")
        urls.append(f"https://plex.tv/api/home/users/{attrs['id']}/switch")
        last_err = None
        for url in urls:
            try:
                resp = account.query(url, method=account._session.post)
            except Exception as e:
                last_err = e
                continue
            token = (resp.attrib.get("authToken")
                     or resp.attrib.get("authenticationToken"))
            if token:
                return token
        sys.exit(f"Could not switch to home user '{name}': {last_err}")
    return None


def resolve_servers(args, usernames):
    """Return {username: PlexServer} where each connection uses that user's
    own access token for this server. --token must be the owner's token."""
    token = args.token or os.environ.get("PLEX_TOKEN")
    if not token:
        sys.exit("No token. Set PLEX_TOKEN or pass --token.")
    owner_server = PlexServer(args.baseurl, token)
    if not usernames:
        return owner_server, {OWNER_LABEL: owner_server}

    from plexapi.myplex import MyPlexAccount
    account = MyPlexAccount(token=token)
    owner_names = {n.lower() for n in
                   (account.username, account.title, account.email) if n}
    servers = {}
    for name in usernames:
        if name == OWNER_LABEL or name.lower() in owner_names:
            servers[name] = owner_server
            continue
        try:
            user = account.user(name)
        except Exception:
            user = None
        if user:
            user_token = user.get_token(owner_server.machineIdentifier)
            if not user_token:
                sys.exit(f"Could not get an access token for '{name}' on "
                         f"'{owner_server.friendlyName}' -- do they have "
                         "access to this server?")
            servers[name] = PlexServer(args.baseurl, user_token)
            continue
        managed_token = home_user_token(account, name)
        if managed_token:
            servers[name] = PlexServer(args.baseurl, managed_token)
            continue
        try:
            known = ", ".join(u.title for u in account.users())
        except Exception:
            known = "(could not list)"
        sys.exit(f"User '{name}' not found on this Plex account "
                 f"(checked shared users and Home profiles). "
                 f"Known shared users: {known}")
    return owner_server, servers


def iter_videos(section, title_filter=None):
    if section.type == "movie":
        videos = section.all()
    elif section.type == "show":
        videos = section.search(libtype="episode")
    else:
        print(f"  Skipping section '{section.title}' (type {section.type})")
        return
    for v in videos:
        if title_filter:
            names = [v.title or "", getattr(v, "grandparentTitle", "") or ""]
            if not any(title_filter.lower() in n.lower() for n in names):
                continue
        yield v


def label(video):
    if video.type == "episode":
        return (f"{video.grandparentTitle} - "
                f"S{video.parentIndex or 0:02d}E{video.index or 0:02d} - {video.title}")
    return f"{video.title} ({getattr(video, 'year', '?')})"


def process_user(user, server, args, prefer_langs, want_subs, writer):
    """Walk the sections as one user; returns (examined, changes, errors)."""
    examined = changes = errors = 0
    section_names = args.sections
    if not section_names:
        section_names = [s.title for s in server.library.sections()
                         if s.type in ("movie", "show")]
        print(f"\n{user}: auto-discovered sections: {', '.join(section_names)}")
    for section_name in section_names:
        try:
            section = server.library.section(section_name)
        except Exception:
            print(f"  WARNING: {user} cannot see section '{section_name}' "
                  "-- skipping")
            continue
        print(f"\n=== {user}: {section_name} ===")
        for video in iter_videos(section, args.title):
            if args.limit and examined >= args.limit:
                return examined, changes, errors
            examined += 1
            try:
                video.reload()
                for media in video.media:
                    for part in media.parts:
                        audio = part.audioStreams()
                        subs = part.subtitleStreams()
                        cur_audio = next((s for s in audio if s.selected), None)
                        cur_sub = next((s for s in subs if s.selected), None)

                        target = pick_audio(audio, prefer_langs)
                        new_audio_id = (target.id if target and
                                        (cur_audio is None or cur_audio.id != target.id)
                                        else None)
                        if want_subs:
                            target_sub = pick_subtitle(subs, prefer_langs)
                            new_sub_id = (target_sub.id if target_sub and
                                          (cur_sub is None or cur_sub.id != target_sub.id)
                                          else None)
                        else:
                            target_sub = None
                            new_sub_id = 0 if cur_sub is not None else None

                        if new_audio_id is None and new_sub_id is None:
                            continue

                        actions = []
                        if new_audio_id is not None:
                            actions.append(f"audio -> {describe(target)}")
                        if new_sub_id == 0:
                            actions.append(f"subs off (was {describe(cur_sub)})")
                        elif new_sub_id is not None:
                            actions.append(f"subs -> {describe(target_sub)}")
                        name = label(video)
                        prefix = "APPLY" if args.apply else "DRY"
                        print(f"  [{prefix}] {name}: " + "; ".join(actions))

                        writer.writerow([
                            user, section_name, name, part.id,
                            cur_audio.id if cur_audio else "", describe(cur_audio),
                            target.id if new_audio_id is not None else "",
                            describe(target) if new_audio_id is not None else "",
                            cur_sub.id if cur_sub else "", describe(cur_sub),
                            "" if new_sub_id is None else new_sub_id,
                            ("" if new_sub_id is None else
                             "(off)" if new_sub_id == 0 else describe(target_sub)),
                            "; ".join(actions),
                        ])
                        changes += 1
                        if args.apply:
                            put_streams(server, part.id, new_audio_id, new_sub_id)
            except Exception as e:  # keep going; one bad item shouldn't stop the run
                errors += 1
                print(f"  [ERROR] {label(video)}: {e}")
            if examined % 100 == 0:
                print(f"  ... {examined} items examined, {changes} changes so far")
    return examined, changes, errors


def run(args):
    owner_server, servers = resolve_servers(args, args.users)
    print(f"Connected to '{owner_server.friendlyName}'; "
          f"applying for: {', '.join(servers)}")

    prefer_langs = [l.strip().lower() for l in args.langs.split(",") if l.strip()]
    subs_on = {n.lower() for n in (args.subs_on or [])}
    unknown = subs_on - {u.lower() for u in servers}
    if unknown:
        print(f"WARNING: --subs-on names not in --users: {', '.join(unknown)}")

    log_path = args.log or f"plex_defaults_log_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    total_examined = total_changes = total_errors = 0

    with open(log_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["user", "section", "item", "part_id",
                         "old_audio_id", "old_audio", "new_audio_id", "new_audio",
                         "old_sub_id", "old_sub", "new_sub_id", "new_sub", "action"])
        for user, server in servers.items():
            examined, changes, errors = process_user(
                user, server, args, prefer_langs, user.lower() in subs_on, writer)
            total_examined += examined
            total_changes += changes
            total_errors += errors
            print(f"\n--- {user}: {examined} items examined, {changes} changes, "
                  f"{errors} errors ---")

    mode = "applied" if args.apply else "planned (dry run -- nothing written)"
    print(f"\nDone: {total_examined} items examined, {total_changes} changes {mode}, "
          f"{total_errors} errors. Log: {log_path}")
    if not args.apply and total_changes:
        print("Re-run with --apply to make these changes.")


def revert(args):
    with open(args.revert, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for row in rows:  # tolerate logs from older versions with no user column
        row["user"] = row.get("user") or OWNER_LABEL
    usernames = sorted({row["user"] for row in rows})
    _, servers = resolve_servers(args, usernames)

    print(f"Reverting {len(rows)} rows from {args.revert} "
          f"({'APPLY' if args.apply else 'DRY RUN'})")
    for row in rows:
        audio_id = row["old_audio_id"] or None
        # Restore the old subtitle state only if the run changed it.
        sub_id = (row["old_sub_id"] or 0) if "subs" in row["action"] else None
        if audio_id is None and sub_id is None:
            continue
        print(f"  [{row['user']}] {row['item']}: audio -> {row['old_audio']}"
              + (f"; subs -> {row['old_sub']}" if sub_id is not None else ""))
        if args.apply:
            put_streams(servers[row["user"]], row["part_id"], audio_id, sub_id)
    if not args.apply:
        print("Dry run only. Re-run with --apply to revert for real.")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--baseurl", default="http://localhost:32400",
                   help="Plex server URL (default: %(default)s)")
    p.add_argument("--token", help="Server owner's X-Plex-Token "
                   "(default: PLEX_TOKEN env var)")
    p.add_argument("--users", nargs="+", metavar="NAME",
                   help="Plex Home/shared usernames to apply selections for; "
                        "may include the owner's own username. Omit to apply "
                        "only for the token's account.")
    p.add_argument("--subs-on", nargs="+", metavar="NAME", default=[],
                   help="Users who get subtitles selected ON by default "
                        "instead of off (e.g. --subs-on bob)")
    p.add_argument("--sections", nargs="+", metavar="NAME",
                   help="Library names to process (default: every movie and "
                        "show library the user can see)")
    p.add_argument("--langs", default="eng",
                   help="Preferred audio/subtitle languages, comma-separated "
                        "ISO codes (default: %(default)s)")
    p.add_argument("--limit", type=int,
                   help="Stop after examining N items per user (for testing)")
    p.add_argument("--title", help="Only items whose title/show contains this text")
    p.add_argument("--apply", action="store_true",
                   help="Actually write changes (default is dry run)")
    p.add_argument("--log", help="CSV log path (default: timestamped file)")
    p.add_argument("--revert", metavar="CSV",
                   help="Revert a previous run from its CSV log")
    args = p.parse_args()
    if args.revert:
        revert(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
