"""Shared resolution helpers for local and Google Drive Desktop analysis datasets."""
import os
import string
from pathlib import Path
from typing import List, Optional

ENV_ANALYSIS_KEYS = ('VENUE_DATA_DIR', 'ANALYSIS_DATA_DIR')
GOOGLE_DRIVE_PROJECT_PARTS = (
    ('내 드라이브', '0. MLops2026', 'Chapter4 mission'),
    ('My Drive', '0. MLops2026', 'Chapter4 mission'),
)


def _iter_mounted_windows_drives() -> List[Path]:
    """Return non-system mounted Windows drive roots without probing disconnected shares."""
    if os.name != 'nt':
        return []
    roots: List[Path] = []
    try:
        import ctypes

        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        letters = [
            letter
            for idx, letter in enumerate(string.ascii_uppercase)
            if (bitmask & (1 << idx)) and letter != 'C'
        ]
    except Exception:
        letters = ['G']
    # Check G: before other external drives when present.
    ordered = (['G'] if 'G' in letters else []) + [l for l in letters if l != 'G']
    for letter in ordered:
        root = Path(f'{letter}:/')
        if root.exists():
            roots.append(root)
    return roots


def _append_unique_path(paths: List[Path], seen: set[str], candidate: Optional[Path]) -> None:
    if candidate is None:
        return
    try:
        key = os.path.normcase(str(candidate.resolve()))
    except OSError:
        key = os.path.normcase(str(candidate))
    if key not in seen:
        seen.add(key)
        paths.append(candidate)


def discover_analysis_dirs(
    app_root: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> List[Path]:
    """Return ordered candidate directories that may hold local or Google Drive datasets."""
    if app_root is None:
        app_root = Path(__file__).resolve().parents[2]
    if repo_root is None:
        repo_root = app_root.parent

    candidates: List[Path] = []
    seen: set[str] = set()

    for env_key in ENV_ANALYSIS_KEYS:
        raw = os.environ.get(env_key, '').strip()
        if raw:
            _append_unique_path(candidates, seen, Path(raw).expanduser())

    _append_unique_path(candidates, seen, app_root / 'analysis')
    _append_unique_path(candidates, seen, repo_root / 'analysis')
    if repo_root.parent != repo_root:
        _append_unique_path(candidates, seen, repo_root.parent / 'analysis')

    drive_roots = _iter_mounted_windows_drives()
    home = Path.home()
    for mount_root in drive_roots + [home / 'Google Drive', home / '내 드라이브', home / 'My Drive']:
        if not mount_root.exists():
            continue
        for rel_parts in GOOGLE_DRIVE_PROJECT_PARTS:
            project_dir = mount_root.joinpath(*rel_parts)
            if project_dir.exists():
                _append_unique_path(candidates, seen, project_dir / 'app' / 'analysis')
                _append_unique_path(candidates, seen, project_dir / 'analysis')

    return candidates


def resolve_analysis_subpath(
    *subparts: str,
    app_root: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> Path:
    """Resolve the first existing analysis subpath across local and Google Drive locations."""
    candidates = discover_analysis_dirs(app_root=app_root, repo_root=repo_root)
    for base in candidates:
        target = base.joinpath(*subparts)
        if target.exists():
            return target
    fallback_base = candidates[0] if candidates else Path(__file__).resolve().parents[2] / 'analysis'
    return fallback_base.joinpath(*subparts)
