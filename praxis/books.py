"""Ebook scanning and metadata extraction.

Uses PyMuPDF (fitz) to read EPUB, MOBI, PDF, etc., and extracts titles, authors,
and cover images. Falls back to filename parsing for AZW3.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any, Iterator
import zipfile
import xml.etree.ElementTree as ET

from .config import DATA_DIR

COVERS_DIR = DATA_DIR / "covers"
COVERS_DIR.mkdir(parents=True, exist_ok=True)

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

# Supported extensions
_FITZ_EXTS = {".epub", ".mobi", ".pdf", ".fb2", ".cbz", ".xps"}
_FALLBACK_EXTS = {".azw3"}


def _parse_filename(filepath: Path) -> tuple[str, str | None]:
    """Fallback: try to guess 'Author - Title' from filename."""
    name = filepath.stem
    # Remove bracketed tags like [Arkady Renko 01] or (epub)
    name = re.sub(r"\[.*?\]", "", name)
    name = re.sub(r"\(.*?\)", "", name)
    name = name.strip()
    
    if " - " in name:
        parts = name.split(" - ", 1)
        return parts[1].strip(), parts[0].strip()
    return name.strip(), None

def _parse_epub(filepath: Path) -> tuple[str | None, str | None, bytes | None]:
    title = None
    author = None
    cover_bytes = None
    try:
        with zipfile.ZipFile(filepath) as z:
            # 1. Try to find the OPF to get exact metadata & cover
            if "META-INF/container.xml" in z.namelist():
                container = ET.fromstring(z.read("META-INF/container.xml"))
                ns = {"n": "urn:oasis:names:tc:opendocument:xmlns:container"}
                rootfile = container.find(".//n:rootfile", ns)
                if rootfile is not None:
                    opf_path = rootfile.attrib.get("full-path")
                    if opf_path and opf_path in z.namelist():
                        opf = ET.fromstring(z.read(opf_path))
                        # Namespaces can be tricky, we'll strip them for easy searching
                        # Or just search with wildcards
                        for elem in opf.iter():
                            if elem.tag.endswith("title") and not title:
                                title = elem.text
                            elif elem.tag.endswith("creator") and not author:
                                author = elem.text
                        
                        # Find cover ID
                        cover_id = None
                        for meta in opf.findall(".//*[@name='cover']"):
                            cover_id = meta.attrib.get("content")
                            break
                        
                        if cover_id:
                            for item in opf.findall(".//*[@id]"):
                                if item.attrib.get("id") == cover_id:
                                    href = item.attrib.get("href")
                                    base = Path(opf_path).parent
                                    # Resolve relative href (e.g. Images/cover.jpg)
                                    img_path = (base / href).as_posix()
                                    # cleanup path like OEBPS/../Images -> Images
                                    img_path = os.path.normpath(img_path).replace("\\", "/")
                                    if img_path in z.namelist():
                                        cover_bytes = z.read(img_path)
                                        break

            # 2. Naive fallback for cover if OPF didn't have it
            if not cover_bytes:
                for name in z.namelist():
                    low = name.lower()
                    if "cover" in low and (low.endswith(".jpg") or low.endswith(".jpeg") or low.endswith(".png")):
                        cover_bytes = z.read(name)
                        break
                        
    except Exception as e:
        print(f"Failed to parse EPUB {filepath.name}: {e}")
        
    return title, author, cover_bytes


def scan_directory_stream(directory: str) -> Iterator[dict[str, Any]]:
    """Scan a folder for ebook files and yield progress and results."""
    if not fitz:
        yield {"type": "error", "message": "PyMuPDF is not installed. Run: pip install pymupdf"}
        return

    base_path = Path(directory)
    if not base_path.is_dir():
        yield {"type": "error", "message": f"Not a valid directory: {directory}"}
        return
    
    for root, _, files in os.walk(base_path):
        for file in files:
            ext = Path(file).suffix.lower()
            if ext not in _FITZ_EXTS and ext not in _FALLBACK_EXTS:
                continue
            
            
            yield {"type": "progress", "file": file}

            filepath = Path(root) / file
            title = None
            author = None
            thumb_name = None

            if ext == ".epub":
                ep_title, ep_author, cover_bytes = _parse_epub(filepath)
                if ep_title: title = ep_title
                if ep_author: author = ep_author
                if cover_bytes:
                    thumb_name = f"{uuid.uuid4().hex[:16]}.jpg"
                    (COVERS_DIR / thumb_name).write_bytes(cover_bytes)
            elif ext in _FITZ_EXTS:
                try:
                    with fitz.open(filepath) as doc:
                        meta = doc.metadata or {}
                        title = meta.get("title")
                        author = meta.get("author")
                        
                        if len(doc) > 0:
                            pix = doc.load_page(0).get_pixmap(dpi=72)
                            if pix:
                                thumb_name = f"{uuid.uuid4().hex[:16]}.png"
                                pix.save(str(COVERS_DIR / thumb_name))
                except Exception as e:
                    print(f"Failed to parse {file} with PyMuPDF: {e}")
            
            # Fallback if PyMuPDF/EPUB failed or it's AZW3
            if not title:
                title, guessed_author = _parse_filename(filepath)
                if not author:
                    author = guessed_author
            
            # Try to see if there's a cover.jpg sidecar (Calibre style)
            if not thumb_name:
                for sidecar in ["cover.jpg", "cover.jpeg"]:
                    side_path = filepath.parent / sidecar
                    if side_path.exists():
                        thumb_name = f"{uuid.uuid4().hex[:16]}.jpg"
                        (COVERS_DIR / thumb_name).write_bytes(side_path.read_bytes())
                        break
            
            # Clean up title/author formatting
            if title:
                title = re.sub(r"\s+", " ", title.strip())
            if author:
                author = re.sub(r"\s+", " ", author.strip())

            yield {"type": "book", "data": {
                "title": title or filepath.stem,
                "author": author,
                "type": "book",
                "thumb": f"local:{thumb_name}" if thumb_name else None,
                "genres": [],
            }}
