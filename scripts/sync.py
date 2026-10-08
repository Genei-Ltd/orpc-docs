#!/usr/bin/env python3
"""Refresh the documentation files described by sync.json."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import fcntl
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET


RESERVED = {".git", ".gitignore", ".snapshot", "scripts", "README.md", "sync.json", "snapshot.json"}
IMAGE_EXTENSIONS = {".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".ico"}


def safe_path(root, relative):
    path = PurePosixPath(relative)
    if not path.parts or path.is_absolute() or ".." in path.parts or any(part.startswith(".") for part in path.parts):
        raise ValueError(f"Unsafe documentation path: {relative}")
    if path.parts[0] in RESERVED:
        raise ValueError(f"Reserved repository path: {relative}")
    output = root / relative
    if not output.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Documentation path escapes the repository: {relative}")
    if any(parent.is_symlink() for parent in (output, *output.parents) if parent != root):
        raise ValueError(f"Documentation path contains a symlink: {relative}")
    return output


def fetch(url):
    for attempt in range(4):
        try:
            with urlopen(Request(url, headers={"User-Agent": "Genei-documentation-sync/1.0"}), timeout=45) as response:
                data = response.read()
                content_type = response.headers.get("Content-Type", "")
            if not data.strip():
                raise ValueError(f"Empty response: {url}")
            if "text/html" in content_type or data.lstrip().lower().startswith((b"<!doctype html", b"<html")):
                raise ValueError(f"Expected documentation, received HTML: {url}")
            return data, content_type
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            if isinstance(error, HTTPError) and error.code not in (408, 429, 500, 502, 503, 504):
                raise
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def links(text, base):
    for target in re.findall(r"\[[^\]]*\]\(([^\s)]+)", text):
        parsed = urlsplit(urljoin(base, target))
        yield urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def relative_url(url, base):
    parsed, source = urlsplit(url), urlsplit(base)
    if (parsed.scheme, parsed.netloc) != (source.scheme, source.netloc) or not parsed.path.startswith(source.path):
        return None
    return unquote(parsed.path[len(source.path):])


def route(url):
    return url.removesuffix(".md").rstrip("/").removesuffix("/index")


def validate_file(relative, data):
    suffix = PurePosixPath(relative).suffix
    if suffix in (".md", ".txt", ".json", ".xml") or relative == "LICENSE":
        text = data.decode("utf-8")
        if suffix == ".json":
            document = json.loads(text)
            if "openapi" in relative and not (isinstance(document, dict) and "paths" in document and ("openapi" in document or "swagger" in document)):
                raise ValueError(f"Invalid OpenAPI document: {relative}")
        elif suffix == ".xml":
            ET.fromstring(text)


def synchronize(root, config):
    root = root.resolve()
    metadata = root / ".snapshot"
    metadata.mkdir(exist_ok=True)
    with (metadata / "sync.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return refresh(root, config, metadata)


def refresh(root, config, metadata):
    manifest_path = root / "snapshot.json"
    previous = json.loads(manifest_path.read_text())["files"] if manifest_path.exists() else {}
    for relative, record in previous.items():
        file = safe_path(root, relative)
        if file.exists() and sha256(file.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError(f"Local changes in {relative}; preserve or revert them before syncing")

    base = config["base_url"].rstrip("/") + "/"
    include_prefixes = tuple(config.get("include_prefixes", [""]))

    def included(relative):
        return relative is not None and relative.startswith(include_prefixes)

    records, pages, indexes, sitemap_pages = {}, {}, {}, set()
    source_commit = None
    with TemporaryDirectory(prefix="sync-", dir=metadata) as directory:
        staging = Path(directory)

        def save(relative, url, data, content_type):
            destination = safe_path(staging, relative)
            safe_path(root, relative)
            validate_file(relative, data)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            records[relative] = {"url": url, "bytes": len(data), "sha256": sha256(data).hexdigest()}

        pending = [urljoin(base, "llms.txt")]
        while pending:
            url = pending.pop()
            if url in indexes:
                continue
            relative = relative_url(url, base)
            if relative is None:
                raise ValueError(f"Index outside the documentation site: {url}")
            data, content_type = fetch(url)
            indexes[url] = data
            for linked in links(data.decode("utf-8"), url):
                path = relative_url(linked, base)
                if path is None:
                    continue
                if path.startswith("_llms/") and path.endswith((".md", ".txt")):
                    pending.append(linked)
                elif included(path):
                    if not PurePosixPath(path).suffix:
                        path = path.rstrip("/") + ".md"
                    if path.endswith((".md", ".json")):
                        pages[path] = {"url": urljoin(base, path)}

        pending_maps, seen_maps = [urljoin(base, "sitemap.xml")], set()
        while pending_maps:
            url = pending_maps.pop()
            if url in seen_maps:
                continue
            seen_maps.add(url)
            relative = relative_url(url, base)
            if relative is None:
                raise ValueError(f"Sitemap outside the documentation site: {url}")
            data, content_type = fetch(url)
            save(relative, url, data, content_type)
            xml = ET.fromstring(data)
            locations = [element.text for element in xml.iter() if element.tag.rsplit("}", 1)[-1] == "loc"]
            if xml.tag.rsplit("}", 1)[-1] == "sitemapindex":
                pending_maps.extend(locations)
            else:
                sitemap_pages.update(url for url in locations if included(relative_url(url, base)))

        indexed_routes = {route(record["url"]) for record in pages.values()}
        for url in sorted(sitemap_pages):
            if route(url) in indexed_routes:
                continue
            markdown_url = url.rstrip("/") if url.endswith(".md") else url.rstrip("/") + ".md"
            relative = relative_url(markdown_url, base)
            if relative is None:
                raise ValueError(f"Sitemap page outside the documentation site: {url}")
            pages[relative] = {"url": markdown_url}
        if not pages or not sitemap_pages:
            raise ValueError("The documentation index or sitemap is empty")

        if config.get("source_repository"):
            repository = config["source_repository"]
            api = f"https://api.github.com/repos/{repository}"
            commit_data, _ = fetch(api + "/commits/main")
            source_commit = json.loads(commit_data)["sha"]
            tree_data, _ = fetch(api + f"/git/trees/{source_commit}?recursive=1")
            tree = json.loads(tree_data)
            if tree["truncated"]:
                raise ValueError("The source repository inventory is truncated")
            raw_base = f"https://raw.githubusercontent.com/{repository}/{source_commit}/"
            source_directory = config.get("source_directory", "docs/").rstrip("/") + "/"
            for entry in tree["tree"]:
                path = entry["path"]
                if path.startswith(source_directory) and path.endswith((".md", ".mdx")):
                    relative = path[len(source_directory):].rsplit(".", 1)[0] + ".md"
                    if included(relative) and relative not in pages:
                        pages[relative] = {"url": urljoin(base, relative), "fallback": raw_base + path}
            pages["LICENSE"] = {"url": raw_base + "LICENSE"}

        pages["llms-full.txt"] = {"url": urljoin(base, "llms-full.txt")}
        for relative, url in config.get("extra_files", {}).items():
            pages[relative] = {"url": url}

        def download(item):
            relative, source = item
            try:
                data, content_type = fetch(source["url"])
                url = source["url"]
            except HTTPError as error:
                if error.code != 404 or "fallback" not in source:
                    raise
                url = source["fallback"]
                data, content_type = fetch(url)
            validate_file(relative, data)
            return relative, url, data, content_type

        def download_batch(targets):
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = [pool.submit(download, item) for item in sorted(targets.items())]
                for count, future in enumerate(as_completed(futures), 1):
                    save(*future.result())
                    if count % 25 == 0 or count == len(futures):
                        print(f"Downloaded {count}/{len(futures)} files", flush=True)

        download_batch(pages)

        assets = {}
        asset_hosts = set(config.get("asset_hosts", []))
        for relative in list(records):
            if not asset_hosts or not relative.endswith(".md"):
                continue
            for url in re.findall(r"https?://[^\s<>\)\]\"'`]+", (staging / relative).read_text()):
                try:
                    parsed = urlsplit(url)
                except ValueError:
                    continue
                if parsed.hostname not in asset_hosts or PurePosixPath(parsed.path).suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
                local = "assets/" + parsed.netloc + unquote(parsed.path)
                assets[local] = {"url": url}
        if assets:
            download_batch(assets)

        for url in indexes.keys() | seen_maps:
            latest, _ = fetch(url)
            original = indexes[url] if url in indexes else (staging / relative_url(url, base)).read_bytes()
            if latest != original:
                raise ValueError("The documentation inventory changed during sync; run the script again")

        markdown = sorted(relative for relative in records if relative.endswith(".md"))
        documents = sorted(relative for relative in records if relative.endswith((".md", ".json")))
        index = "# Documentation index\n\nAll links point to local documentation, relative to this file.\n\n"
        index += "\n".join(f"- [{relative}]({relative})" for relative in documents) + "\n"
        save("llms.txt", None, index.encode(), "text/plain")
        for relative in records:
            if relative not in previous and safe_path(root, relative).exists():
                raise ValueError(f"Unmanaged file would be overwritten: {relative}")
        for relative in previous:
            file = safe_path(root, relative)
            if file.exists() and sha256(file.read_bytes()).hexdigest() != previous[relative]["sha256"]:
                raise ValueError(f"Local changes appeared during sync: {relative}")

        snapshot = {
            "source": base,
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "source_commit": source_commit,
            "markdown_files": len(markdown),
            "sitemap_pages": len(sitemap_pages),
            "indexes": sorted(indexes),
            "files": dict(sorted(records.items())),
        }
        for url, data in indexes.items():
            original = safe_path(metadata / "upstream", relative_url(url, base))
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_bytes(data)
        for relative in records:
            destination = safe_path(root, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / relative, destination)
        for relative in previous.keys() - records.keys():
            safe_path(root, relative).unlink(missing_ok=True)
        manifest = staging / "snapshot.json"
        manifest.write_text(json.dumps(snapshot, indent=2) + "\n")
        os.replace(manifest, root / "snapshot.json")
        print(f"Synced {len(records)} files, including {len(markdown)} Markdown files", flush=True)
        return snapshot


if __name__ == "__main__":
    repository_root = Path(__file__).resolve().parents[1]
    try:
        synchronize(repository_root, json.loads((repository_root / "sync.json").read_text()))
    except (OSError, ValueError, KeyError, ET.ParseError) as error:
        print(f"Sync failed: {error}", file=sys.stderr)
        raise SystemExit(1)
