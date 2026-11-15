#!/usr/bin/env python3
"""Download original FuOverflow attachments and create a pixel-lossless PDF."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps, UnidentifiedImageError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

THREAD_URL = "https://fuoverflow.com/threads/mas291-su26-re.7211/"
PROJECT = "MAS291_SU26_RE"
IMAGE_EXTENSIONS = {".webp", ".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".bmp"}
QUESTION_RE = re.compile(r"(?i)(?:^|[^a-z0-9])q(?:uestion)?[\s._-]*(\d+)\s*\.(webp|jpe?g|png|gif|tiff?|bmp)(?:$|[?#])")
QUESTION_LOOSE_RE = re.compile(r"(?i)(?:^|[^a-z0-9])q(?:uestion)?[\s._-]*(\d+)(?:[^0-9]|$)")
THUMB_RE = re.compile(r"(?i)(?:/|[_-])(thumb|thumbnail|small|preview)(?:/|[_.-]|$)|[?&](?:width|w)=\d+")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"


@dataclass
class Candidate:
    url: str
    name: str = ""
    source: str = "dom"
    score: int = 0
    pixel_area: int = 0
    byte_size: int = 0
    width: int = 0
    height: int = 0


@dataclass
class Result:
    name: str
    url: str
    ok: bool
    status: int | None = None
    content_type: str = ""
    size: int = 0
    dimensions: tuple[int, int] | None = None
    sha256: str = ""
    error: str = ""
    duplicate_of: str = ""
    replaced_preview: str = ""


def natural_key(value: str) -> tuple:
    return tuple(int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", value))


def question_number(value: str) -> int | None:
    m = QUESTION_LOOSE_RE.search(unquote(value))
    return int(m.group(1)) if m else None


def normalize_name(name: str, url: str, content_type: str = "") -> str:
    name = unquote(name).strip().replace("/", "_").replace("\\", "_")
    match = QUESTION_RE.search(name) or QUESTION_RE.search(unquote(urlparse(url).path))
    if match:
        return f"Q{int(match.group(1))}.{match.group(2).lower().replace('jpeg', 'jpg')}"
    q = question_number(name) or question_number(url)
    ext = Path(urlparse(url).path).suffix.lower()
    if ext not in IMAGE_EXTENSIONS:
        ext = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) or ".img"
        if ext == ".jpe":
            ext = ".jpg"
    return f"Q{q}{ext}" if q is not None else (name or f"attachment{ext}")


def load_cookies(session: requests.Session, path: Path | None) -> int:
    if not path:
        return 0
    if not path.is_file():
        raise FileNotFoundError(f"Cookie file not found: {path}")
    count = 0
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        for c in data if isinstance(data, list) else data.get("cookies", []):
            domain = c.get("domain", "fuoverflow.com")
            session.cookies.set(c["name"], c["value"], domain=domain, path=c.get("path", "/"))
            count += 1
    else:
        jar = MozillaCookieJar(str(path))
        jar.load(ignore_discard=True, ignore_expires=True)
        session.cookies.update(jar)
        count = len(jar)
    return count


def session_for(cookie_path: Path | None) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.7"})
    s.max_redirects = 10
    retry = Retry(
        total=4, connect=3, read=3, status=3, backoff_factor=0.8,
        status_forcelist=(408, 425, 429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}), respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    load_cookies(s, cookie_path)
    return s


def is_blocked(response: requests.Response) -> str:
    text = response.text[:10000].lower() if "html" in response.headers.get("Content-Type", "").lower() else ""
    if response.status_code in (401, 403, 429):
        return f"HTTP {response.status_code}"
    if any(x in text for x in ("cf-chl-", "captcha", "just a moment", "checking your browser")):
        return "anti-bot challenge"
    if "đăng nhập" in text and not re.search(r"(?i)q\d+\.(?:webp|jpe?g|png)", text):
        return "login required"
    return ""


def add_candidate(out: dict[str, Candidate], url: str, name: str, source: str, base: str) -> None:
    if not url or url.startswith(("data:", "javascript:", "mailto:")):
        return
    url = urljoin(base, url).split("#", 1)[0]
    combined = f"{name} {url}"
    q = question_number(combined)
    lower_url = url.lower()
    direct_attachment = "/attachments/" in lower_url and "/data/attachments/" not in lower_url
    attachmentish = direct_attachment or any(p in lower_url for p in ("/media/", "/data/attachments/"))
    imageish = Path(urlparse(url).path).suffix.lower() in IMAGE_EXTENSIONS or bool(QUESTION_RE.search(combined))
    if q is None or not (attachmentish or imageish):
        return
    score = 100 if direct_attachment else (10 if attachmentish else 5)
    score += 25 if source.endswith(":href") else 0
    score += 20 if QUESTION_RE.search(name) else 0
    score += 15 if any(x in url.lower() for x in ("/full", "original", "download")) else 0
    score -= 80 if THUMB_RE.search(url) else 0
    candidate = Candidate(url=url, name=normalize_name(name, url), source=source, score=score)
    old = out.get(url)
    if old is None or candidate.score > old.score:
        out[url] = candidate


def candidates_from_html(html: str, base: str, source: str = "html") -> list[Candidate]:
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, Candidate] = {}
    attrs = ("href", "src", "data-src", "data-url", "data-original", "data-full", "data-download-url")
    for tag in soup.find_all(True):
        label = " ".join(filter(None, [tag.get("download", ""), tag.get("alt", ""), tag.get("title", ""), tag.get_text(" ", strip=True)]))
        for attr in attrs:
            add_candidate(out, tag.get(attr, ""), label, f"{source}:{attr}", base)
        for attr in ("srcset", "data-srcset"):
            for item in tag.get(attr, "").split(","):
                add_candidate(out, item.strip().split(" ")[0], label, f"{source}:{attr}", base)
    return list(out.values())


def collect_lightpanda(url: str, work: Path, binary: Path, cookie_path: Path | None) -> tuple[str, list[Candidate], str]:
    if not binary.is_file():
        return "", [], f"Lightpanda binary missing: {binary}"
    node = shutil.which("node")
    if not node:
        return "", [], "Node.js is required for the CDP collector"
    output = work / "lightpanda.json"
    log = work / "lightpanda.log"
    env = os.environ.copy()
    env.update({"LIGHTPANDA_DISABLE_TELEMETRY": "true", "LIGHTPANDA_DISABLE_CORE_DUMP": "1"})
    with log.open("w", encoding="utf-8") as log_file:
        server = subprocess.Popen(
            [str(binary), "serve", "--host", "127.0.0.1", "--port", "9222", "--load-resources", "image", "--log-format", "pretty", "--log-level", "info"],
            stdout=log_file, stderr=subprocess.STDOUT, env=env,
        )
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            if server.poll() is not None:
                return "", [], f"Lightpanda exited with code {server.returncode}; see {log}"
            try:
                with socket.create_connection(("127.0.0.1", 9222), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            return "", [], f"Lightpanda CDP did not start; see {log}"
        command = [node, str(Path(__file__).with_name("lightpanda_collect.mjs")), url, str(output)]
        if cookie_path:
            command.append(str(cookie_path))
        proc = subprocess.run(command, text=True, capture_output=True, timeout=90, env=env)
        if proc.returncode:
            return "", [], f"CDP collector failed: {(proc.stderr or proc.stdout).strip()}"
        data = json.loads(output.read_text(encoding="utf-8"))
        dom = data.get("dom", {})
        html = dom.get("html", "")
        out = {c.url: c for c in candidates_from_html(html, dom.get("url", url), "lightpanda")}
        for item in data.get("network", []):
            mime = str(item.get("mimeType", ""))
            if mime.startswith("image/"):
                add_candidate(out, item.get("url", ""), "", "lightpanda:network", url)
        return html, list(out.values()), ""
    except subprocess.TimeoutExpired:
        return "", [], "Lightpanda CDP collector timed out"
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


def resolve_candidates(session: requests.Session, initial: Iterable[Candidate], report_notes: list[str]) -> list[Candidate]:
    """Resolve attachment/media HTML pages to direct image responses without saving thumbnails."""
    resolved: list[Candidate] = []
    seen: set[str] = set()
    for candidate in sorted(initial, key=lambda c: (-c.score, natural_key(c.name))):
        if candidate.url in seen or THUMB_RE.search(candidate.url):
            continue
        seen.add(candidate.url)
        try:
            r = session.get(candidate.url, timeout=(15, 45), allow_redirects=True)
            ctype = r.headers.get("Content-Type", "").lower()
            if r.ok and ctype.startswith("image/"):
                candidate.url = r.url
                candidate.score += 50
                candidate.byte_size = len(r.content)
                try:
                    with Image.open(io.BytesIO(r.content)) as probe:
                        candidate.width, candidate.height = probe.size
                        candidate.pixel_area = probe.width * probe.height
                except (OSError, UnidentifiedImageError):
                    candidate.pixel_area = 0
                resolved.append(candidate)
            elif r.ok and "html" in ctype:
                nested = candidates_from_html(r.text, r.url, "attachment-page")
                direct = [x for x in nested if not THUMB_RE.search(x.url)]
                if direct:
                    resolved.extend(direct)
                else:
                    report_notes.append(f"No original image link found on attachment page: {candidate.url}")
            else:
                report_notes.append(f"Candidate rejected: {candidate.url} -> HTTP {r.status_code}, {ctype or 'unknown type'}")
        except requests.RequestException as exc:
            report_notes.append(f"Candidate request failed: {candidate.url} -> {exc}")
    best: dict[int, Candidate] = {}
    for c in resolved:
        q = question_number(c.name) or question_number(c.url)
        quality = (c.pixel_area, c.byte_size, c.score)
        old_quality = (best[q].pixel_area, best[q].byte_size, best[q].score) if q in best else (-1, -1, -1)
        if q is not None and (q not in best or quality > old_quality):
            best[q] = c
    originals: list[Candidate] = []
    for q in sorted(best):
        chosen = best[q]
        # FuOverflow's grid previews are typically 400x150 or 150x400. Never
        # silently feed those into the final PDF; the lightbox attachment is
        # 1920px wide for this exam.
        if chosen.width and chosen.height and max(chosen.width, chosen.height) <= 500:
            report_notes.append(
                f"Rejected Q{q} thumbnail candidate: {chosen.width}x{chosen.height}, {chosen.url}"
            )
            continue
        originals.append(chosen)
    return originals


def content_disposition_filename(value: str) -> str:
    if not value:
        return ""
    msg = Message()
    msg["content-disposition"] = value
    return msg.get_filename() or ""


def validate_image(path: Path) -> tuple[tuple[int, int], str]:
    with Image.open(path) as im:
        fmt = im.format or ""
        im.verify()
    with Image.open(path) as im:
        im.load()
        dimensions = im.size
    return dimensions, fmt


def download_images(session: requests.Session, candidates: list[Candidate], image_dir: Path) -> list[Result]:
    image_dir.mkdir(parents=True, exist_ok=True)
    results: list[Result] = []
    hashes: dict[str, str] = {}
    for c in candidates:
        result = Result(name=c.name, url=c.url, ok=False)
        try:
            with session.get(c.url, timeout=(15, 90), stream=True, allow_redirects=True) as r:
                result.status = r.status_code
                result.url = r.url
                result.content_type = r.headers.get("Content-Type", "").split(";", 1)[0].lower()
                r.raise_for_status()
                if not result.content_type.startswith("image/"):
                    raise ValueError(f"Content-Type is not image/*: {result.content_type or 'missing'}")
                cd_name = content_disposition_filename(r.headers.get("Content-Disposition", ""))
                name = normalize_name(cd_name or c.name, r.url, result.content_type)
                if question_number(name) is None:
                    raise ValueError(f"Cannot determine question number for {name!r}")
                result.name = name
                final = image_dir / name
                temp = image_dir / f".{name}.part"
                digest = hashlib.sha256()
                size = 0
                with temp.open("wb") as f:
                    for chunk in r.iter_content(1024 * 256):
                        if chunk:
                            f.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                if size == 0:
                    raise ValueError("empty response body")
                dims, _ = validate_image(temp)
                sha = digest.hexdigest()
                duplicate = hashes.get(sha, "")
                if duplicate:
                    temp.unlink(missing_ok=True)
                    result.duplicate_of = duplicate
                    raise ValueError(f"duplicate binary of {duplicate}")
                if final.exists():
                    existing_sha = hashlib.sha256(final.read_bytes()).hexdigest()
                    if existing_sha != sha:
                        existing_dims, _ = validate_image(final)
                        existing_area = existing_dims[0] * existing_dims[1]
                        new_area = dims[0] * dims[1]
                        if new_area > existing_area:
                            rejected_dir = image_dir.parent / "rejected_thumbnails"
                            rejected_dir.mkdir(parents=True, exist_ok=True)
                            backup = rejected_dir / final.name
                            if backup.exists():
                                backup = rejected_dir / f"{final.stem}-{existing_sha[:12]}{final.suffix}"
                            os.replace(final, backup)
                            os.replace(temp, final)
                            result.replaced_preview = str(backup)
                        else:
                            temp.unlink(missing_ok=True)
                            raise ValueError(
                                f"refusing to overwrite existing {existing_dims[0]}x{existing_dims[1]} image "
                                f"with non-larger {dims[0]}x{dims[1]} response: {final}"
                            )
                    else:
                        temp.unlink(missing_ok=True)
                else:
                    os.replace(temp, final)
                hashes[sha] = name
                result.ok = True
                result.size = size
                result.dimensions = dims
                result.sha256 = sha
        except (requests.RequestException, OSError, ValueError, UnidentifiedImageError) as exc:
            result.error = str(exc)
            (image_dir / f".{result.name}.part").unlink(missing_ok=True)
        results.append(result)
    return results


class PdfWriter:
    def __init__(self) -> None:
        self.objects: list[bytes] = []

    def add(self, value: bytes) -> int:
        self.objects.append(value)
        return len(self.objects)

    def reserve(self) -> int:
        return self.add(b"")

    def set(self, number: int, value: bytes) -> None:
        self.objects[number - 1] = value

    def write(self, path: Path, root: int) -> None:
        data = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for i, obj in enumerate(self.objects, 1):
            offsets.append(len(data))
            data.extend(f"{i} 0 obj\n".encode())
            data.extend(obj)
            data.extend(b"\nendobj\n")
        xref = len(data)
        data.extend(f"xref\n0 {len(self.objects)+1}\n0000000000 65535 f \n".encode())
        for offset in offsets[1:]:
            data.extend(f"{offset:010d} 00000 n \n".encode())
        data.extend(f"trailer\n<< /Size {len(self.objects)+1} /Root {root} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_bytes(data)
        os.replace(temp, path)


def pdf_stream(dictionary: str, payload: bytes) -> bytes:
    return f"<< {dictionary} /Length {len(payload)} >>\nstream\n".encode() + payload + b"\nendstream"


def create_lossless_pdf(images: list[Path], output: Path) -> int:
    """Embed decoded pixels with lossless Flate compression; never crop or upscale."""
    writer = PdfWriter()
    catalog = writer.reserve()
    pages_obj = writer.reserve()
    page_objects: list[int] = []
    for image_path in images:
        with Image.open(image_path) as original:
            oriented = ImageOps.exif_transpose(original)
            if oriented.mode in ("RGBA", "LA") or "transparency" in oriented.info:
                rgba = oriented.convert("RGBA")
                rgb = rgba.convert("RGB")
                alpha = rgba.getchannel("A")
            else:
                rgb = oriented.convert("RGB")
                alpha = None
            width, height = rgb.size
            smask = 0
            if alpha is not None:
                compressed_alpha = zlib.compress(alpha.tobytes(), 9)
                smask = writer.add(pdf_stream(
                    f"/Type /XObject /Subtype /Image /Width {width} /Height {height} "
                    "/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode",
                    compressed_alpha,
                ))
            compressed = zlib.compress(rgb.tobytes(), 9)
            mask_ref = f" /SMask {smask} 0 R" if smask else ""
            image_obj = writer.add(pdf_stream(
                f"/Type /XObject /Subtype /Image /Width {width} /Height {height} "
                f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode{mask_ref}",
                compressed,
            ))
            content = f"q\n{width} 0 0 {height} 0 0 cm\n/Im0 Do\nQ\n".encode()
            content_obj = writer.add(pdf_stream("", content))
            page = writer.add(
                f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 {width} {height}] "
                f"/Resources << /XObject << /Im0 {image_obj} 0 R >> >> /Contents {content_obj} 0 R >>".encode()
            )
            page_objects.append(page)
    writer.set(pages_obj, f"<< /Type /Pages /Count {len(page_objects)} /Kids [{' '.join(f'{p} 0 R' for p in page_objects)}] >>".encode())
    writer.set(catalog, f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode())
    writer.write(output, catalog)
    return len(page_objects)


def write_report(path: Path, mode: str, candidates: list[Candidate], results: list[Result], notes: list[str], pdf_pages: int, source: str) -> None:
    good = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    lines = [
        f"MAS291 download report",
        f"Generated (UTC): {datetime.now(timezone.utc).isoformat()}",
        f"Source: {source}",
        f"Collection mode: {mode}",
        f"Attachment candidates detected: {len(candidates)}",
        f"Images downloaded and validated: {len(good)}",
        f"Images failed/rejected: {len(failed)}",
        f"PDF pages: {pdf_pages}",
        f"Page/image count match: {'YES' if good and pdf_pages == len(good) else 'NO (no validated images/PDF)' if not good else 'NO'}",
        "",
        "Successful images:",
    ]
    for r in sorted(good, key=lambda x: natural_key(x.name)):
        replacement = f", replaced_preview={r.replaced_preview}" if r.replaced_preview else ""
        lines.append(f"- {r.name}: {r.size} bytes, {r.dimensions[0]}x{r.dimensions[1]}, {r.content_type}, sha256={r.sha256}, url={r.url}{replacement}")
    lines.extend(["", "Failed/rejected images:"])
    for r in failed:
        lines.append(f"- {r.name or '(unknown)'}: {r.error}; status={r.status}; url={r.url}")
    lines.extend(["", "Notes:"])
    lines.extend(f"- {n}" for n in notes)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    root = Path(args.output).resolve()
    image_dir = root / "original_images"
    work = root / ".work"
    root.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    cookie_path = Path(args.cookies).resolve() if args.cookies else None
    session = session_for(cookie_path)
    notes: list[str] = []
    html = ""
    candidates: list[Candidate] = []
    used_mode = "none"

    if args.mode in ("auto", "lightpanda"):
        html, candidates, error = collect_lightpanda(args.url, work, Path(args.lightpanda_bin).resolve(), cookie_path)
        if error:
            notes.append(error)
        else:
            used_mode = "lightpanda-cdp"
            if not candidates:
                lowered = html.lower()
                reason = "login/challenge page" if any(x in lowered for x in ("captcha", "cf-chl-", "just a moment", "đăng nhập")) else "DOM contained no recognizable Q-numbered attachments"
                notes.append(f"Lightpanda loaded the page but found no attachments: {reason}")

    if not candidates and args.mode in ("auto", "http"):
        try:
            response = session.get(args.url, timeout=(15, 60), allow_redirects=True)
            blocked = is_blocked(response)
            if blocked:
                notes.append(f"Direct HTTP access blocked: {blocked}; final URL={response.url}")
            elif response.ok:
                html = response.text
                candidates = candidates_from_html(html, response.url, "http")
                used_mode = "direct-http"
            else:
                notes.append(f"Direct HTTP failed: HTTP {response.status_code}; final URL={response.url}")
        except requests.RequestException as exc:
            notes.append(f"Direct HTTP failed: {exc}")

    (work / "thread.html").write_text(html, encoding="utf-8")
    discovered = {q for c in candidates if (q := question_number(c.name) or question_number(c.url)) is not None}
    candidates = resolve_candidates(session, candidates, notes) if candidates else []
    resolved_numbers = {q for c in candidates if (q := question_number(c.name) or question_number(c.url)) is not None}
    unresolved = sorted(discovered - resolved_numbers)
    if unresolved:
        notes.append('Attachments not resolved to original images: ' + ', '.join(f'Q{q}' for q in unresolved))
    numbers = sorted({q for c in candidates if (q := question_number(c.name) or question_number(c.url)) is not None})
    if numbers:
        missing = [q for q in range(numbers[0], numbers[-1] + 1) if q not in numbers]
        if missing:
            notes.append("Missing question numbers within detected range: " + ", ".join(f"Q{q}" for q in missing))
    results = download_images(session, candidates, image_dir) if candidates else []
    valid_paths = sorted((image_dir / r.name for r in results if r.ok), key=lambda p: natural_key(p.name))
    pdf = root / f"{root.name}.pdf"
    complete = bool(valid_paths) and not unresolved and len(valid_paths) == len(candidates)
    pages = create_lossless_pdf(valid_paths, pdf) if complete else 0
    if valid_paths and not complete:
        notes.append('Some attachments failed; no new PDF was created. An existing PDF may be from an earlier run.')
    if not valid_paths:
        notes.append("No valid original images were downloaded; PDF was not created.")
    write_report(root / "download_report.txt", used_mode, candidates, results, notes, pages, args.url)
    print(f"Detected questions: {len(discovered)} | originals: {len(valid_paths)} | failed/unresolved: {len(results)-len(valid_paths)+len(unresolved)} | PDF pages: {pages}")
    print(f"Report: {root / 'download_report.txt'}")
    return 0 if complete and pages == len(valid_paths) else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="FuOverflow thread URL")
    parser.add_argument("--output", required=True, help="Output directory named for this exam")
    parser.add_argument("--mode", choices=("auto", "lightpanda", "http"), default="auto")
    parser.add_argument("--lightpanda-bin", default="tools/lightpanda/lightpanda")
    parser.add_argument("--cookies", help="JSON export containing only fuoverflow.com cookies")
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(run(parse_args()))

