# Moving civic_media to a New Machine

## Status

**DB migration: DONE (2026-03-06)**
All 5,816 path rows converted from absolute (`E:\0-Automated-Apps\civic_media\...`)
to relative (`media/{id}/file.mp4`). The DB is now fully portable.

Backup of the pre-migration DB:
  `database/civic_media.db.bak-premigration`

---

## When you're ready to move

### 1. Copy everything to the new machine
```bash
robocopy "E:\0-Automated-Apps\civic_media" "D:\new-path\civic_media" /E /COPYALL
# or on Linux/WSL:
rsync -av /mnt/e/0-Automated-Apps/civic_media/ /new/path/civic_media/
```
Include the `database/`, `media/`, `documents/`, `tv_news/`, `ocr_text/` folders.
The venv can be rebuilt fresh on the new machine — no need to copy it.

### 2. Tell the app where the files are

Open `run.sh` on the new machine and set this line to the new path:
```bash
export CIVIC_MEDIA_STORAGE_ROOT="/new/path/to/civic_media"
```
If the app directory path is the same on the new machine, leave it blank —
it defaults to the directory the code lives in.

### 3. Start and verify
```bash
./run.sh

# Quick sanity check — all should return 0:
sqlite3 database/civic_media.db "SELECT COUNT(*) FROM media_files WHERE file_path LIKE '/%' OR file_path LIKE '_:\%';"
sqlite3 database/civic_media.db "SELECT COUNT(*) FROM meetings WHERE media_directory LIKE '/%' OR media_directory LIKE '_:\%';"

# Spot-check a few paths look right:
sqlite3 database/civic_media.db "SELECT file_path FROM media_files LIMIT 3;"
```

### 4. Rollback (if needed)
```bash
# Restore the pre-migration DB backup:
cp database/civic_media.db.bak-premigration database/civic_media.db
# Then revert the code changes (the old code still works with absolute paths)
```

---

## How the portability works

All file paths stored in the DB are relative to `STORAGE_ROOT`.
Two helper functions in `app/paths.py` handle the conversion:

- `to_relative(path)` — called on every DB write, strips the `STORAGE_ROOT` prefix
- `to_absolute(path)` — called on every file I/O read, prepends `STORAGE_ROOT`

Both are idempotent so old absolute paths and new relative paths both work correctly.

`STORAGE_ROOT` is set in `app/config.py`:
```python
STORAGE_ROOT = Path(os.environ.get("CIVIC_MEDIA_STORAGE_ROOT", str(BASE_DIR))).resolve()
```
Default (no env var set) = the directory the code lives in.

## Shasta-PRA-Backup

Same pattern — already stored relative paths, just needs the env var on the new machine:
```bash
export PRA_STORAGE_ROOT="/new/path/to/Shasta-PRA-Backup"
```
Configured in `Shasta-PRA-Backup/app/config.py`.
