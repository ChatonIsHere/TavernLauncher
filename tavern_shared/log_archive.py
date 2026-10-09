"""
Archives unity-log.csv the same way the game's own NLog config does:
moves it into an Archives/ folder next to itself, sequence-numbered,
capped at 20 files.
"""
import os
import re

MAX_ARCHIVE_FILES = 20
DEFAULT_SIZE_THRESHOLD_BYTES = 10 * 1000 * 1000  # matches the game's own 10MB threshold


def archives_dir_for(log_path):
    return os.path.join(os.path.dirname(log_path), "Archives")


def _existing_archive_numbers(log_path):
    """[(number, path), ...] for existing unity-log.N.csv files, oldest first."""
    stem, ext = os.path.splitext(os.path.basename(log_path))
    archives = archives_dir_for(log_path)
    pattern = re.compile(re.escape(stem) + r"\.(\d+)" + re.escape(ext) + r"$")
    found = []
    if os.path.isdir(archives):
        for name in os.listdir(archives):
            m = pattern.match(name)
            if m:
                found.append((int(m.group(1)), os.path.join(archives, name)))
    found.sort(key=lambda pair: pair[0])
    return found


def archive_log_now(log_path, max_files=MAX_ARCHIVE_FILES):
    """Moves the log into Archives/ and leaves a fresh empty file behind.
    Prunes the oldest archive if this would exceed max_files.

    Returns (True, archive_path, None) on success, or
    (False, message, locked). locked=True means another process has the
    file open (usually the game) -- retrying won't help until it's closed.
    """
    if not os.path.isfile(log_path):
        return False, "No active log file found to archive.", False

    stem, ext = os.path.splitext(os.path.basename(log_path))
    archives = archives_dir_for(log_path)
    try:
        os.makedirs(archives, exist_ok=True)
    except OSError as e:
        return False, f"Couldn't create the Archives folder: {e}", False

    existing = _existing_archive_numbers(log_path)
    while len(existing) >= max_files:
        _, oldest_path = existing.pop(0)
        try:
            os.remove(oldest_path)
        except OSError:
            pass

    next_num = (existing[-1][0] + 1) if existing else 0
    archive_path = os.path.join(archives, f"{stem}.{next_num}{ext}")

    try:
        os.rename(log_path, archive_path)
    except OSError as e:
        locked = getattr(e, "winerror", None) == 32  # ERROR_SHARING_VIOLATION
        return False, (f"Couldn't move the log file — {e}. Something has it "
                        "open; close the game (or stop the server) and try again."), locked

    # Leave a fresh file in place so the log viewer doesn't show a gap.
    try:
        open(log_path, "a", encoding="utf-8").close()
    except OSError:
        pass

    return True, archive_path, None


def should_auto_archive(log_path, threshold_bytes=DEFAULT_SIZE_THRESHOLD_BYTES):
    try:
        return os.path.getsize(log_path) >= threshold_bytes
    except OSError:
        return False
