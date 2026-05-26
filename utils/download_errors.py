"""Shared helpers for error.txt catalog gap / missing-track logging."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


PLATFORM_TRACK_URLS = {
    'tidal': 'https://tidal.com/browse/track/{id}',
    'spotify': 'https://open.spotify.com/track/{id}',
    'qobuz': 'https://www.qobuz.com/track/{id}',
    'deezer': 'https://www.deezer.com/track/{id}',
    'applemusic': 'https://music.apple.com/song/{id}',
    'apple music': 'https://music.apple.com/song/{id}',
    'youtube': 'https://music.youtube.com/watch?v={id}',
    'soundcloud': 'https://soundcloud.com/tracks/{id}',
    'amazonmusic': 'https://music.amazon.com/tracks/{id}',
    'amazon music': 'https://music.amazon.com/tracks/{id}',
}

PLATFORM_ALBUM_URLS = {
    'tidal': 'https://tidal.com/browse/album/{id}',
    'spotify': 'https://open.spotify.com/album/{id}',
    'qobuz': 'https://www.qobuz.com/album/{id}',
    'deezer': 'https://deezer.com/album/{id}',
    'applemusic': 'https://music.apple.com/album/{id}',
    'apple music': 'https://music.apple.com/album/{id}',
    'youtube': 'https://music.youtube.com/playlist?list={id}',
    'soundcloud': 'https://soundcloud.com/{id}',
    'amazonmusic': 'https://music.amazon.com/albums/{id}',
    'amazon music': 'https://music.amazon.com/albums/{id}',
    'beatport': 'https://www.beatport.com/release/{id}',
    'beatsource': 'https://www.beatsource.com/release/{id}',
}

PLATFORM_PLAYLIST_URLS = {
    'tidal': 'https://tidal.com/browse/playlist/{id}',
    'spotify': 'https://open.spotify.com/playlist/{id}',
    'qobuz': 'https://www.qobuz.com/playlist/{id}',
    'deezer': 'https://www.deezer.com/playlist/{id}',
    'applemusic': 'https://music.apple.com/playlist/{id}',
    'apple music': 'https://music.apple.com/playlist/{id}',
    'youtube': 'https://music.youtube.com/playlist?list={id}',
    'soundcloud': 'https://soundcloud.com/{id}',
    'amazonmusic': 'https://music.amazon.com/playlists/{id}',
    'amazon music': 'https://music.amazon.com/playlists/{id}',
    'beatport': 'https://www.beatport.com/chart/{id}',
    'beatsource': 'https://www.beatsource.com/chart/{id}',
}


def _normalize_service_key(service_name: Optional[str]) -> str:
    return (service_name or '').strip().lower()


def platform_track_url(service_name: Optional[str], track_id) -> Optional[str]:
    if track_id is None or str(track_id).strip() == '':
        return None
    key = _normalize_service_key(service_name)
    template = PLATFORM_TRACK_URLS.get(key)
    if not template:
        return None
    tid = str(track_id).split(':')[-1]
    return template.format(id=tid)


def platform_album_url(service_name: Optional[str], album_id) -> Optional[str]:
    if album_id is None or str(album_id).strip() == '':
        return None
    key = _normalize_service_key(service_name)
    template = PLATFORM_ALBUM_URLS.get(key)
    if not template:
        return None
    return template.format(id=str(album_id))


def platform_playlist_url(service_name: Optional[str], playlist_id) -> Optional[str]:
    if playlist_id is None or str(playlist_id).strip() == '':
        return None
    key = _normalize_service_key(service_name)
    template = PLATFORM_PLAYLIST_URLS.get(key)
    if not template:
        return None
    return template.format(id=str(playlist_id))


def _artists_from_dict(track_dict: dict) -> List[str]:
    artists = track_dict.get('artists')
    if isinstance(artists, list):
        names = []
        for artist in artists:
            if isinstance(artist, dict):
                name = artist.get('name')
                if name:
                    names.append(str(name))
            elif artist:
                names.append(str(artist))
        if names:
            return names
    artist = track_dict.get('artist')
    if isinstance(artist, dict) and artist.get('name'):
        return [str(artist['name'])]
    if isinstance(artist, str) and artist.strip():
        return [artist.strip()]
    if track_dict.get('ART_NAME'):
        return [str(track_dict['ART_NAME'])]
    if track_dict.get('primaryArtistName'):
        return [str(track_dict['primaryArtistName'])]
    return []


def _track_title_from_dict(track_dict: dict) -> Optional[str]:
    for key in ('title', 'name', 'titleText', 'SNG_TITLE'):
        value = track_dict.get(key)
        if value:
            title = str(value)
            version = track_dict.get('version') or track_dict.get('titleVersion')
            if version and str(version) not in title:
                title = f'{title} ({version})'
            return title
    return None


def _coerce_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _lookup_track_dict(track_id: str, data_cache: dict, extra: dict) -> Optional[dict]:
    if track_id in data_cache and isinstance(data_cache[track_id], dict):
        return data_cache[track_id]
    if track_id in extra and isinstance(extra[track_id], dict):
        return extra[track_id]
    return None


def _build_data_cache(extra: dict) -> dict:
    if not isinstance(extra, dict):
        return {}
    data = extra.get('data')
    if isinstance(data, dict):
        return data
    cache = extra.get('cache')
    if isinstance(cache, dict) and isinstance(cache.get('data'), dict):
        return cache['data']
    return {}


def collect_track_rows_from_album(album_info) -> List[dict]:
    """Extract per-track metadata from AlbumInfo for gap detection."""
    rows: List[dict] = []
    extra = getattr(album_info, 'track_extra_kwargs', None) or {}
    data_cache = _build_data_cache(extra)

    for track_item in getattr(album_info, 'tracks', None) or []:
        track_id = None
        name = None
        artists = None
        track_number = None
        disc_number = None

        if hasattr(track_item, 'id'):
            track_id = str(track_item.id)
            name = getattr(track_item, 'name', None)
            artists = getattr(track_item, 'artists', None)
            tags = getattr(track_item, 'tags', None)
            if tags:
                track_number = getattr(tags, 'track_number', None)
                disc_number = getattr(tags, 'disc_number', None)
        else:
            track_id = str(track_item)

        track_dict = _lookup_track_dict(track_id, data_cache, extra) if track_id else None
        if isinstance(track_dict, dict):
            name = name or _track_title_from_dict(track_dict)
            if not artists:
                artists = _artists_from_dict(track_dict)
            track_number = track_number or track_dict.get('trackNumber')
            track_number = track_number or track_dict.get('track_number')
            track_number = track_number or track_dict.get('number')
            track_number = track_number or track_dict.get('TRACK_NUMBER')
            disc_number = disc_number or track_dict.get('volumeNumber')
            disc_number = disc_number or track_dict.get('disc_number')
            disc_number = disc_number or track_dict.get('discNumber')
            disc_number = disc_number or track_dict.get('volume_number')
            disc_number = disc_number or track_dict.get('DISK_NUMBER')

        if not track_id:
            continue

        rows.append({
            'id': track_id,
            'name': name,
            'artists': artists or [],
            'track_number': _coerce_int(track_number),
            'disc_number': _coerce_int(disc_number) or 1,
        })

    return rows


def _title_from_probe_payload(payload: dict) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    if payload.get('error'):
        return None
    title = payload.get('title') or payload.get('name')
    if title:
        version = payload.get('version') or payload.get('titleVersion')
        if version and str(version) not in str(title):
            return f'{title} ({version})'
        return str(title)
    return _track_title_from_dict(payload)


def detect_per_disc_track_gaps(
    track_rows: List[dict],
    probe_callback: Optional[Callable[[str], Any]] = None,
) -> List[dict]:
    """
    Find missing tracks from internal gaps in per-disc trackNumber sequences.
    Optionally probe numeric catalog IDs between sequential neighbors.
    """
    by_disc: Dict[int, List[Tuple[int, str, dict]]] = defaultdict(list)
    for row in track_rows:
        track_no = row.get('track_number')
        if track_no is None:
            continue
        disc = int(row.get('disc_number') or 1)
        by_disc[disc].append((int(track_no), str(row['id']), row))

    exclusions: List[dict] = []

    for disc in sorted(by_disc):
        entries = sorted(by_disc[disc], key=lambda item: item[0])
        if len(entries) < 2:
            continue
        by_num = {num: (tid, row) for num, tid, row in entries}

        for gap_num in range(min(by_num), max(by_num) + 1):
            if gap_num in by_num:
                continue

            prev_entry = by_num.get(gap_num - 1)
            next_entry = by_num.get(gap_num + 1)
            guessed_id = None
            title = None
            artists: List[str] = []
            reason = (
                f'Missing from download list (disc {disc} track {gap_num} — '
                f'not returned by service API)'
            )

            if prev_entry and next_entry:
                prev_id, prev_row = prev_entry
                next_id, next_row = next_entry
                prev_title = prev_row.get('name') or 'previous track'
                next_title = next_row.get('name') or 'next track'
                title = f'Between "{prev_title}" and "{next_title}"'

                try:
                    prev_int = int(prev_id)
                    next_int = int(next_id)
                    if next_int - prev_int == 2:
                        guessed_id = str(prev_int + 1)
                except (TypeError, ValueError):
                    pass

            if guessed_id and probe_callback:
                try:
                    payload = probe_callback(guessed_id)
                    if isinstance(payload, dict):
                        if payload.get('error'):
                            err = str(payload['error']).lower()
                            if 'region' in err or 'not found' in err:
                                reason = (
                                    f'Region-locked or unavailable for your account (catalog id {guessed_id})'
                                )
                        else:
                            probed_title = _title_from_probe_payload(payload)
                            if probed_title:
                                title = probed_title
                            probed_artists = _artists_from_dict(payload)
                            if probed_artists:
                                artists = probed_artists
                            reason = (
                                f'Region-locked or unavailable for your account (catalog id {guessed_id})'
                            )
                except Exception as exc:
                    msg = str(exc).lower()
                    if 'region' in msg or 'not found' in msg:
                        reason = f'Region-locked or unavailable for your account (catalog id {guessed_id})'

            exclusions.append({
                'id': guessed_id,
                'name': title,
                'artists': artists,
                'track_number': gap_num,
                'disc_number': disc,
                'reason': reason,
            })

    return exclusions


def _exclusion_key(entry: dict) -> Tuple:
    entry_id = entry.get('id')
    if entry_id is not None and str(entry_id).strip():
        return ('id', str(entry_id))
    disc = entry.get('disc_number')
    track_no = entry.get('track_number')
    if disc is not None and track_no is not None:
        return ('pos', int(disc), int(track_no))
    name = entry.get('name') or entry.get('title')
    return ('name', str(name or ''), str(entry.get('reason') or ''))


def dedupe_exclusions(exclusions: List[dict]) -> List[dict]:
    seen: Set[Tuple] = set()
    merged: List[dict] = []
    for entry in exclusions:
        if not isinstance(entry, dict):
            continue
        key = _exclusion_key(entry)
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    return merged


def merge_album_exclusions(album_info, probe_callback: Optional[Callable[[str], Any]] = None) -> List[dict]:
    """Combine module-provided exclusions with generic per-disc gap detection."""
    exclusions = list(getattr(album_info, 'excluded_tracks', None) or [])
    rows = collect_track_rows_from_album(album_info)
    if rows:
        exclusions.extend(detect_per_disc_track_gaps(rows, probe_callback=probe_callback))
    return dedupe_exclusions(exclusions)


def catalog_summary_url(service_name: Optional[str], context_type: str, context_id: str) -> Optional[str]:
    if not context_id:
        return None
    if context_type == 'playlist':
        return platform_playlist_url(service_name, context_id)
    return platform_album_url(service_name, context_id)
