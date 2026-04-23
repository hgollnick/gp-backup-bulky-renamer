# gp-backup-bulky-renamer

Renames Google Photos Takeout backup files to a **date-sortable** format and optionally embeds EXIF/GPS metadata directly into the media files.

## The problem

Google Takeout exports media files with their original device names (`IMG_20240101.jpg`, `000e5b3ea7d83c996d9e21d44493023d.mp4`, …) paired with a JSON sidecar that holds the real timestamp, GPS location, and description. File managers that sort by name end up with a scrambled timeline.

## What it does

For every media + sidecar pair it finds, the script:

1. **Renames** both files to `YYYYMMDD_HHMMSS_<original-name>.<ext>` (UTC time from the JSON).
2. **Updates the filesystem mtime** so applications that sort by date work correctly even without EXIF support.
3. **Embeds metadata** into the media file via `exiftool` (optional):
   - `DateTimeOriginal`, `CreateDate`, `ModifyDate`
   - GPS latitude / longitude / altitude (when non-zero)
   - `ImageDescription` / `Comment` (from the `description` field)
4. **Moves duplicate sidecars** (multiple JSONs for the same media file) into a `Duplicated/` sub-folder for manual review.

### Supported sidecar patterns

Every Google Photos JSON sidecar contains a `"title"` field with the exact original media filename. The script reads this field directly rather than guessing from the sidecar filename, so it handles all Takeout export variants regardless of how Windows or Takeout truncated the sidecar name:

```
photo.jpg.supplemental-metadata.json
photo.jpg.supplemental-met.json
photo.jpg.suppl.json
0B61F6A0-...-00000.json   ← iOS/iCloud exports
photo.jpg.json            ← older Takeout exports
```

## Requirements

- Python 3.8+
- [`exiftool`](https://exiftool.org/) *(optional — only needed for EXIF/GPS embedding)*

```bash
# Debian / Ubuntu / WSL
sudo apt install libimage-exiftool-perl
```

## Usage

```bash
# Preview — no files are changed
python3 renamer.py "/mnt/e/Google Photos" --dry-run

# Rename and embed EXIF + GPS metadata
python3 renamer.py "/mnt/e/Google Photos"

# Rename only, skip exiftool
python3 renamer.py "/mnt/e/Google Photos" --no-embed
```

### Options

| Flag | Description |
|------|-------------|
| `--dry-run` / `-n` | Preview every change without modifying anything |
| `--no-embed` | Skip EXIF/GPS embedding (faster, no exiftool needed) |

### Running from WSL against a Windows drive

Windows drives are accessible under `/mnt/<letter>/` in WSL:

```bash
python3 renamer.py "/mnt/e/Google Photos"
python3 renamer.py /mnt/d/Takeout/Photos
```

## Example output

```
17:10:06 [INFO ] Folder  : /mnt/e/Google Photos
17:10:06 [INFO ] Dry-run : False
17:10:06 [INFO ] Embed   : True

17:10:06 [INFO ] Scanning folder for sidecar files...
17:10:06 [INFO ] Found 1842 media+sidecar pair(s).

17:10:06 [INFO ] IMG_20240515_200050.jpg
17:10:06 [INFO ]   -> 20240515_200050_IMG_20240515_200050.jpg
17:10:06 [INFO ] mtime updated — IMG_20240515_200050.jpg -> 2024-05-15 20:00:50 UTC
17:10:06 [OK   ] Renamed IMG_20240515_200050.jpg
...
17:10:42 [INFO ] Embedding metadata into 1842 file(s) via exiftool...
17:10:58 [INFO ] Metadata embedded — 1842/1842 file(s) updated.

17:10:58 [INFO ] Done in 52.3s — 1842 renamed, 0 skipped, 0 errors.
```

## License

MIT — see [LICENSE](LICENSE).
