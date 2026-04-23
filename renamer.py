#!/usr/bin/env python3
"""
Google Photos Backup Renamer

Renames every media file (and its JSON sidecar) in a folder to a
date-sortable name:

    YYYYMMDD_HHMMSS_<original-filename>.<ext>
    YYYYMMDD_HHMMSS_<original-filename>.<ext>.json   ← renamed sidecar

The timestamp comes from the "photoTakenTime" field of the Google Photos
JSON sidecar (falls back to "creationTime").  The pairing is done via the
"title" field inside the JSON, which always holds the original media
filename regardless of how Takeout or Windows truncated the sidecar name.

Media files with no matching sidecar are moved to an Orphan/ sub-folder.
Duplicate sidecars (multiple JSONs for the same media file) are moved to
a Duplicated/ sub-folder.

Optionally embeds DateTimeOriginal, CreateDate, GPS coordinates, and
description into the media file using a single batched exiftool call.
Also updates each file's filesystem mtime to the photo timestamp.

Usage:
    python renamer.py <folder> [--dry-run] [--no-embed]

    # Preview every change without touching any file
    python renamer.py "/mnt/e/Google Photos B" --dry-run

    # Rename + embed EXIF/GPS metadata
    python renamer.py "/mnt/e/Google Photos B"

    # Rename only, skip exiftool
    python renamer.py "/mnt/e/Google Photos B" --no-embed

Requirements:
    Python 3.8+
    exiftool  (sudo apt install libimage-exiftool-perl)   ← optional
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MEDIA_EXTENSIONS: frozenset[str] = frozenset({
    # Images
    ".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif",
    ".webp", ".bmp", ".tiff", ".tif", ".raw", ".dng",
    # Videos
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".3gp",
    ".m4v", ".flv", ".webm", ".mts", ".m2ts",
})

# Detects files already renamed by this script: YYYYMMDD_HHMMSS_<anything>.
# Used to skip sidecar JSONs that were already processed in a previous run.
_ALREADY_RENAMED_RE = re.compile(r"^\d{8}_\d{6}_", re.IGNORECASE)

# Suffixes that Google Photos appends to edited/processed media variants.
# These files share the original's sidecar (which references the base name).
_EDIT_SUFFIXES: tuple[str, ...] = ("-editada", "-edited", "-edit")

# Extracts YYYYMMDD_HHMMSS from anywhere in a filename stem.
_FILENAME_DATE_RE = re.compile(r"(\d{8})_(\d{6})")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(level: str, msg: str) -> None:
    """Print a timestamped log line: HH:MM:SS [LEVEL] message."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{ts} [{level:<5}] {msg}")


def _get_taken_time(data: dict) -> datetime | None:
    """Return the best available datetime from Google Photos JSON metadata."""
    for key in ("photoTakenTime", "creationTime"):
        entry = data.get(key)
        if isinstance(entry, dict) and isinstance(entry.get("timestamp"), str):
            try:
                ts = int(entry["timestamp"])
                if ts > 0:
                    return datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, OverflowError, OSError):
                pass
    return None


def _resolve_unique_names(
    existing: set[str], media_name: str, json_name: str
) -> tuple[str, str]:
    """
    Return (media_name, json_name) guaranteed not to collide with *existing*
    (case-insensitive in-memory set).  Appends _01, _02, … when needed.
    No filesystem calls.
    """
    if media_name.lower() not in existing and json_name.lower() not in existing:
        return media_name, json_name
    stem = Path(media_name).stem
    ext = Path(media_name).suffix
    counter = 1
    while True:
        nm = f"{stem}_{counter:02d}{ext}"
        nj = f"{nm}.json"
        if nm.lower() not in existing and nj.lower() not in existing:
            return nm, nj
        counter += 1


def _fuzzy_sidecar_lookup(
    orphan: Path, title_to_data: dict[str, dict]
) -> dict | None:
    """
    Secondary sidecar matching for files that have no exact-title match.

    Tries three strategies in order:
      1. Edit-suffix stripping  — 'X-editada.jpg' -> look for title 'X.jpg'
      2. Counter suffix          — 'X(1).jpg'      -> look for title 'X.jpg'
      3. Prefix match            — 'X_S.jpg'        -> look for title starting
                                   with 'X_S' (handles Takeout filename truncation)
    """
    stem = orphan.stem
    ext = orphan.suffix

    # 1. Edit suffix stripping
    for sfx in _EDIT_SUFFIXES:
        if stem.lower().endswith(sfx):
            base_title = (stem[: -len(sfx)] + ext).lower()
            if base_title in title_to_data:
                return title_to_data[base_title]

    # 2. Counter suffix stripping: 'name(1).jpg' -> 'name.jpg'
    base_stem = re.sub(r"\(\d+\)$", "", stem)
    if base_stem != stem:
        base_title = (base_stem + ext).lower()
        if base_title in title_to_data:
            return title_to_data[base_title]

    # 3. Prefix match: orphan stem (or its counter-stripped base) is a truncated
    #    prefix of a full sidecar title (handles Takeout filename truncation).
    for candidate in dict.fromkeys([stem.lower(), base_stem.lower()]):
        if len(candidate) >= 10:  # avoid accidental short-name false positives
            for title_lower, data in title_to_data.items():
                dot = title_lower.rfind(".")
                title_stem = title_lower[:dot] if dot != -1 else title_lower
                if title_stem.startswith(candidate) and len(title_stem) > len(candidate):
                    return data

    # 4. Reverse prefix: orphan stem extends a known base title stem.
    #    Handles Takeout-truncated edit suffixes, e.g. 'NAME-editad.JPG' whose
    #    sidecar references the base 'NAME.JPG'.
    orphan_stem_lower = stem.lower()
    if len(orphan_stem_lower) >= 10:
        for title_lower, data in title_to_data.items():
            dot = title_lower.rfind(".")
            title_stem = title_lower[:dot] if dot != -1 else title_lower
            if (
                len(title_stem) >= 10
                and orphan_stem_lower.startswith(title_stem)
                and len(orphan_stem_lower) > len(title_stem)
            ):
                return data

    return None


def _check_exiftool() -> bool:
    """Return True if exiftool is available on PATH."""
    try:
        subprocess.run(
            ["exiftool", "-ver"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _build_exiftool_args(path: Path, data: dict, dt: datetime) -> list[str]:
    """Return the exiftool argument lines for one file (excluding the -execute marker)."""
    dt_str = dt.strftime("%Y:%m:%d %H:%M:%S")
    args = [
        "-overwrite_original",
        f"-DateTimeOriginal={dt_str}",
        f"-CreateDate={dt_str}",
        f"-ModifyDate={dt_str}",
    ]
    geo: dict = data.get("geoData") or {}
    if not (geo.get("latitude") or geo.get("longitude")):
        geo = data.get("geoDataExif") or geo
    lat = float(geo.get("latitude", 0.0))
    lon = float(geo.get("longitude", 0.0))
    alt = float(geo.get("altitude", 0.0))
    if lat != 0.0 or lon != 0.0:
        args += [
            f"-GPSLatitude={abs(lat)}",
            f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
            f"-GPSLongitude={abs(lon)}",
            f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}",
        ]
        if alt != 0.0:
            args += [
                f"-GPSAltitude={abs(alt)}",
                f"-GPSAltitudeRef={'0' if alt >= 0 else '1'}",
            ]
    desc = data.get("description", "").strip()
    if desc:
        args += [f"-ImageDescription={desc}", f"-Comment={desc}"]
    args.append(str(path))
    return args


def _embed_all_batch(tasks: list[tuple[Path, dict, datetime]]) -> None:
    """
    Embed metadata into every file in *tasks* using a single exiftool process
    via the -stay_open protocol, avoiding per-file subprocess spawn overhead.
    """
    if not tasks:
        return
    log("INFO", f"Embedding metadata into {len(tasks)} file(s) via exiftool...")
    lines: list[str] = []
    for path, data, dt in tasks:
        lines.extend(_build_exiftool_args(path, data, dt))
        lines.append("-execute")
    lines += ["-stay_open", "False"]
    stdin_data = "\n".join(lines) + "\n"
    try:
        result = subprocess.run(
            ["exiftool", "-stay_open", "True", "-@", "-"],
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log("WARN", "exiftool timed out during batch embedding.")
        return
    successes = result.stdout.count("{ready}")
    log("INFO", f"Metadata embedded — {successes}/{len(tasks)} file(s) updated.")
    for line in result.stderr.splitlines():
        line = line.strip()
        if line:
            log("WARN", f"exiftool: {line}")


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def _move_to_duplicated(json_file: Path, folder: Path, dry_run: bool) -> None:
    """Move a duplicate sidecar JSON into <folder>/Duplicated/."""
    dup_dir = folder / "Duplicated"
    dest = dup_dir / json_file.name
    log("DUPL", f"Duplicate sidecar — {'would move' if dry_run else 'moving'} to Duplicated/{json_file.name}")
    if dry_run:
        return
    try:
        dup_dir.mkdir(exist_ok=True)
        # Avoid overwriting if a file with that name already exists there
        counter = 1
        while dest.exists():
            dest = dup_dir / f"{json_file.stem}_{counter:02d}.json"
            counter += 1
        json_file.rename(dest)
    except OSError as exc:
        log("ERROR", f"Could not move duplicate sidecar: {exc}")


def _collect_pairs(
    folder: Path, dry_run: bool = False
) -> tuple[list[tuple[Path, Path, dict]], set[str], list[Path]]:
    """
    Scan *folder* (single iterdir call) and return a 3-tuple:
      - pairs:          (media_file, json_file, json_data) for every matched pair
      - existing_names: lowercase filename set for in-memory conflict detection
      - orphans:        media files with no matching sidecar

    Pairing is done via the JSON 'title' field, which Google Photos always
    sets to the original media filename, regardless of how Takeout or Windows
    truncated the sidecar filename on disk.
    """
    # Build a case-insensitive name → Path index in one pass
    name_index: dict[str, Path] = {}
    for entry in folder.iterdir():
        if entry.is_file():
            name_index[entry.name.lower()] = entry

    existing_names: set[str] = set(name_index.keys())

    pairs: list[tuple[Path, Path | None, dict]] = []
    seen_media: set[Path] = set()
    title_to_data: dict[str, dict] = {}

    for name_lower in sorted(name_index):
        if not name_lower.endswith(".json"):
            continue
        json_file = name_index[name_lower]

        # Skip sidecars already renamed by this script to avoid double-prefixing
        if _ALREADY_RENAMED_RE.match(json_file.name):
            continue

        # Parse JSON first — the 'title' field is the authoritative media name
        try:
            data: dict = json.loads(json_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        # Must be a Google Photos metadata file
        if "photoTakenTime" not in data and "creationTime" not in data:
            continue

        media_name: str | None = data.get("title") or None
        if not media_name:
            log("WARN", f"No 'title' in sidecar: {json_file.name}")
            continue

        # Record every valid sidecar title for secondary (fuzzy) matching later
        title_to_data[media_name.lower()] = data

        # Locate the media file via O(1) case-insensitive lookup
        media_file = name_index.get(media_name.lower())
        if media_file is None or not media_file.is_file():
            log("WARN", f"Media file not found for sidecar: {json_file.name} (title: {media_name!r})")
            continue

        if media_file.suffix.lower() not in MEDIA_EXTENSIONS:
            continue

        if media_file in seen_media:
            _move_to_duplicated(json_file, folder, dry_run)
            continue

        seen_media.add(media_file)
        pairs.append((media_file, json_file, data))

    # Collect media files with no matched sidecar
    orphans: list[Path] = [
        path
        for name_lower, path in name_index.items()
        if (
            not _ALREADY_RENAMED_RE.match(path.name)
            and Path(name_lower).suffix in MEDIA_EXTENSIONS
            and path not in seen_media
        )
    ]

    # Secondary pass: fuzzy-match remaining orphans via edit suffixes, counter
    # variants, or filename-prefix truncation.
    remaining_orphans: list[Path] = []
    for orphan in orphans:
        borrowed = _fuzzy_sidecar_lookup(orphan, title_to_data)
        if borrowed is not None:
            log("INFO", f"Fuzzy-matched sidecar data for orphan: {orphan.name}")
            pairs.append((orphan, None, borrowed))
        else:
            remaining_orphans.append(orphan)

    return pairs, existing_names, remaining_orphans


def process(folder: Path, *, dry_run: bool, embed: bool) -> None:
    log("INFO", f"Folder  : {folder}")
    log("INFO", f"Dry-run : {dry_run}")
    log("INFO", f"Embed   : {embed}")
    print()

    start = time.monotonic()

    log("INFO", "Scanning folder for sidecar files...")
    pairs, existing_names, orphans = _collect_pairs(folder, dry_run=dry_run)

    log("INFO", f"Found {len(pairs)} media+sidecar pair(s), {len(orphans)} orphan(s).")
    print()

    counts = {"renamed": 0, "skipped": 0, "errors": 0, "orphaned": 0}
    embed_queue: list[tuple[Path, dict, datetime]] = []

    for media_file, json_file, data in pairs:
        dt = _get_taken_time(data)
        if dt is None:
            log("SKIP", f"No valid timestamp in sidecar — {media_file.name}")
            counts["skipped"] += 1
            continue

        prefix = dt.strftime("%Y%m%d_%H%M%S")
        new_media_name = f"{prefix}_{media_file.name}"
        new_json_name = f"{new_media_name}.json"

        # Idempotency: already correctly named
        if media_file.name == new_media_name:
            log("OK", f"Already correct — {media_file.name}")
            counts["skipped"] += 1
            continue

        # In-memory conflict detection — no Path.exists() calls
        new_media_name, new_json_name = _resolve_unique_names(
            existing_names, new_media_name, new_json_name
        )
        new_media_path = folder / new_media_name
        new_json_path = folder / new_json_name

        log("INFO", f"{media_file.name}")
        log("INFO", f"  -> {new_media_name}")
        if json_file is not None and json_file.name != new_json_name:
            log("INFO", f"{json_file.name}")
            log("INFO", f"  -> {new_json_name}")

        if dry_run:
            counts["renamed"] += 1
            continue

        # ── 1. Update filesystem mtime so file managers sort correctly ────────
        ts = dt.timestamp()
        try:
            os.utime(media_file, (ts, ts))
            log("INFO", f"mtime updated — {media_file.name} -> {dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        except OSError as exc:
            log("WARN", f"Could not update mtime for {media_file.name}: {exc}")

        # ── 2. Rename media (and sidecar when one exists) ─────────────────────
        try:
            media_file.rename(new_media_path)
        except OSError as exc:
            log("ERROR", f"Could not rename media: {exc}")
            counts["errors"] += 1
            continue

        if json_file is not None:
            try:
                json_file.rename(new_json_path)
            except OSError as exc:
                log("ERROR", f"Could not rename sidecar: {exc}")
                try:
                    new_media_path.rename(media_file)
                except OSError:
                    pass
                counts["errors"] += 1
                continue

        # Keep the in-memory set consistent so future conflict checks are correct
        existing_names.discard(media_file.name.lower())
        if json_file is not None:
            existing_names.discard(json_file.name.lower())
        existing_names.add(new_media_name.lower())
        existing_names.add(new_json_name.lower())

        log("OK", f"Renamed {media_file.name}")
        counts["renamed"] += 1

        if embed:
            embed_queue.append((new_media_path, data, dt))

    # ── 3. Process remaining orphans (no sidecar) ────────────────────────────
    if orphans:
        print()
        log("INFO", f"Processing {len(orphans)} orphan file(s) (no sidecar)...")
        orphan_dir = folder / "Orphan"
        for media_file in sorted(orphans, key=lambda p: p.name):
            # Try to extract a usable timestamp from the filename itself.
            m = _FILENAME_DATE_RE.search(media_file.stem)
            if m:
                prefix = f"{m.group(1)}_{m.group(2)}"
                new_media_name = f"{prefix}_{media_file.name}"
                # Avoid the degenerate 'YYYYMMDD_HHMMSS_YYYYMMDD_HHMMSS.ext' when
                # the stem IS already exactly the timestamp.
                if media_file.stem == f"{m.group(1)}_{m.group(2)}":
                    log("OK", f"Already date-named — {media_file.name}")
                    counts["skipped"] += 1
                    continue
                new_media_name, _ = _resolve_unique_names(
                    existing_names, new_media_name, new_media_name + ".json"
                )
                new_media_path = folder / new_media_name
                log("INFO", f"{media_file.name} (no sidecar, date from filename)")
                log("INFO", f"  -> {new_media_name}")
                if dry_run:
                    counts["renamed"] += 1
                    continue
                try:
                    media_file.rename(new_media_path)
                    existing_names.discard(media_file.name.lower())
                    existing_names.add(new_media_name.lower())
                    log("OK", f"Renamed {media_file.name}")
                    counts["renamed"] += 1
                except OSError as exc:
                    log("ERROR", f"Could not rename {media_file.name}: {exc}")
                    counts["errors"] += 1
            else:
                # Last resort: use the file's own mtime as the date prefix.
                try:
                    mtime = media_file.stat().st_mtime
                    dt_mtime = datetime.fromtimestamp(mtime, tz=timezone.utc)
                    prefix = dt_mtime.strftime("%Y%m%d_%H%M%S")
                    new_media_name = f"{prefix}_{media_file.name}"
                    new_media_name, _ = _resolve_unique_names(
                        existing_names, new_media_name, new_media_name + ".json"
                    )
                    new_media_path = folder / new_media_name
                    log("INFO", f"{media_file.name} (no sidecar, date from file mtime)")
                    log("INFO", f"  -> {new_media_name}")
                    if dry_run:
                        counts["renamed"] += 1
                    else:
                        media_file.rename(new_media_path)
                        existing_names.discard(media_file.name.lower())
                        existing_names.add(new_media_name.lower())
                        log("OK", f"Renamed {media_file.name}")
                        counts["renamed"] += 1
                except OSError as exc:
                    log("SKIP", f"No sidecar, no date — {'would move' if dry_run else 'moving'} {media_file.name} -> Orphan/")
                    if dry_run:
                        counts["orphaned"] += 1
                        continue
                    try:
                        orphan_dir.mkdir(exist_ok=True)
                        dest = orphan_dir / media_file.name
                        counter = 1
                        while dest.exists():
                            dest = orphan_dir / f"{media_file.stem}_{counter:02d}{media_file.suffix}"
                            counter += 1
                        media_file.rename(dest)
                        counts["orphaned"] += 1
                    except OSError as exc2:
                        log("ERROR", f"Could not move orphan {media_file.name}: {exc2}")
                        counts["errors"] += 1

    # ── 4. Embed metadata — ONE exiftool process for all files ────────────────
    if embed_queue:
        exiftool_ok = _check_exiftool()
        if not exiftool_ok:
            log("INFO", "exiftool not found — skipping metadata embedding.")
            log("INFO", "Install with: sudo apt install libimage-exiftool-perl")
        else:
            try:
                _embed_all_batch(embed_queue)
            except Exception as exc:  # noqa: BLE001
                log("WARN", f"Batch metadata embedding failed: {exc}")

    elapsed = time.monotonic() - start
    print()
    log("INFO", (
        f"Done in {elapsed:.1f}s — "
        f"{counts['renamed']} renamed, "
        f"{counts['orphaned']} orphaned, "
        f"{counts['skipped']} skipped, "
        f"{counts['errors']} errors."
    ))
    if dry_run:
        log("INFO", "Dry-run: no files were changed.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Rename Google Photos backup files to a date-sortable format "
            "(YYYYMMDD_HHMMSS_<original>) and embed EXIF metadata via exiftool."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "folder",
        help="Path to the folder containing Google Photos backup files",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Preview changes without modifying any files",
    )
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="Skip embedding metadata with exiftool",
    )
    args = parser.parse_args()

    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        sys.exit(f"Error: not a directory — {folder}")

    process(folder, dry_run=args.dry_run, embed=not args.no_embed)


if __name__ == "__main__":
    main()
