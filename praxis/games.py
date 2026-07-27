"""Local game scanning and metadata extraction.

Scans a directory (e.g. C:\Games or a torrent folder) for subdirectories or game installers,
extracts the game title by cleaning up the directory name, and yields them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterator

def _clean_game_title(name: str) -> str:
    """Cleans up release group tags, version numbers, and scene markings."""
    # Remove bracketed/parenthetical release tags e.g. [GOG], (FitGirl Repack)
    name = re.sub(r"\[.*?\]", "", name)
    name = re.sub(r"\(.*?\)", "", name)
    
    # Remove version numbers e.g. v1.0, v.1.2.3, Update 5
    name = re.sub(r"(?i)\bv\d+(\.\d+)*\b", "", name)
    name = re.sub(r"(?i)update\s*\d+", "", name)
    name = re.sub(r"(?i)build\s*\d+", "", name)
    name = re.sub(r"(?i)dlc", "", name)
    
    # Remove common scene words
    scene_words = [
        "repack", "crack", "plaza", "codex", "skidrow", "reloaded", "prophet", "dodi",
        "elamigos", "multi", "eng", "multi5", "multi12", "edition", "goty", "remastered"
    ]
    for w in scene_words:
        name = re.sub(rf"(?i)\b{w}\b", "", name)
    
    # Convert dots and underscores to spaces (often used in torrent names)
    name = name.replace(".", " ").replace("_", " ")
    
    # Collapse multiple spaces and trim
    name = re.sub(r"\s+", " ", name).strip()
    return name

def scan_games_dir(directory: str) -> Iterator[dict[str, Any]]:
    """Yields parsed game metadata from a folder containing games."""
    root = Path(directory)
    if not root.is_dir():
        return
        
    for item in root.iterdir():
        yield {"type": "progress", "file": item.name}
        
        title = None
        if item.is_dir():
            title = _clean_game_title(item.name)
        elif item.is_file() and item.suffix.lower() in {".exe", ".iso", ".zip", ".rar", ".7z"}:
            title = _clean_game_title(item.stem)
            
        if title and len(title) > 1:
            yield {
                "type": "game",
                "data": {
                    "title": title,
                    "type": "game",
                }
            }
