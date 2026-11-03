#!/usr/bin/env python3
"""Build SomaFM playlists (M3U, M3U8, PLS, AIMP) from the official channel list.

The channel list comes from https://api.somafm.com/channels.json.
Only the Python standard library is used (Python 3.9+).
See README.md for the options in config.json.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

USER_AGENT = "soma-playlists/1.0 (+https://github.com/blackgur/Soma.fm)"
AIMP_PLAYLIST_ID = "{E9EC43D0-9C9C-47BC-897E-6C3349EF1749}"
ORDER_MODES = ("keep", "listeners", "alpha", "api")
EOL = "\r\n"

DEFAULTS = {
    "api_url": "https://api.somafm.com/channels.json",
    "output_dir": "Playlists",
    "basename": "SomaFM",
    "scheme": "https",
    "host": "ice1.somafm.com",
    "default_bitrate": 128,
    "bitrate_overrides": {},
    "exclude": [],
    "exclude_genres": [],
    "order": "keep",
    "min_channels": 20,
}


class GenerationError(Exception):
    """Raised when the output must not be written (bad config, bad data)."""


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def load_config(path: Path | None) -> tuple[dict, Path]:
    """Return (config, base_dir). Relative paths in the config are resolved
    against base_dir, which is the directory of the config file."""
    cfg = dict(DEFAULTS)
    base = Path(__file__).resolve().parent
    if path is not None:
        if not path.is_file():
            raise GenerationError(f"config file not found: {path}")
        base = path.resolve().parent
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise GenerationError(f"{path}: invalid JSON: {exc}") from exc
        if not isinstance(user, dict):
            raise GenerationError(f"{path}: top level must be an object")
        unknown = sorted(set(user) - set(DEFAULTS))
        if unknown:
            raise GenerationError(f"{path}: unknown option(s): {', '.join(unknown)}")
        cfg.update(user)
    validate_config(cfg)
    return cfg, base


def valid_bitrate(value: object) -> bool:
    """A bitrate setting is a positive integer (kbps) or the word "max"."""
    if value == "max":
        return True
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate_config(cfg: dict) -> None:
    if cfg["scheme"] not in ("http", "https"):
        raise GenerationError("scheme must be 'http' or 'https'")
    if cfg["order"] not in ORDER_MODES:
        raise GenerationError(f"order must be one of: {', '.join(ORDER_MODES)}")
    if not valid_bitrate(cfg["default_bitrate"]):
        raise GenerationError('default_bitrate must be a positive integer or "max"')
    overrides = cfg["bitrate_overrides"]
    if not isinstance(overrides, dict) or not all(
        isinstance(k, str) and valid_bitrate(v) for k, v in overrides.items()
    ):
        raise GenerationError('bitrate_overrides must map channel ids to positive integers or "max"')
    for key in ("exclude", "exclude_genres"):
        if not isinstance(cfg[key], list) or not all(isinstance(x, str) for x in cfg[key]):
            raise GenerationError(f"{key} must be a list of strings")
    if not isinstance(cfg["min_channels"], int) or cfg["min_channels"] < 0:
        raise GenerationError("min_channels must be a non-negative integer")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", str(cfg["basename"])):
        raise GenerationError("basename may only contain letters, digits, '.', '_' and '-'")


# --------------------------------------------------------------------------
# channel data
# --------------------------------------------------------------------------

def fetch_json(url: str, retries: int = 3, timeout: int = 30) -> object:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (OSError, ValueError, http.client.HTTPException) as exc:
            last = exc
            if attempt < retries:
                time.sleep(2 * attempt)
    raise GenerationError(f"could not fetch {url}: {last}")


def mp3_bitrates(channel: dict) -> set[int]:
    """MP3 bitrates a channel offers, derived from its playlist file names:
    <id>.pls = 128k, <id>256.pls = 256k, <id>320.pls = 320k."""
    cid = str(channel.get("id", ""))
    rates: set[int] = set()
    for pl in channel.get("playlists") or []:
        if not isinstance(pl, dict) or pl.get("format") != "mp3":
            continue
        m = re.search(r"/([^/]+)\.pls$", str(pl.get("url", "")))
        if not m or not m.group(1).startswith(cid):
            continue
        suffix = m.group(1)[len(cid):]
        if suffix == "":
            rates.add(128)
        elif suffix.isdigit():
            rates.add(int(suffix))
    return rates


def pick_bitrate(rates: set[int], wanted: int | str) -> int | None:
    """"max": the highest offered bitrate. A number: exact match if offered,
    else the closest lower one, else the lowest."""
    if not rates:
        return None
    if wanted == "max":
        return max(rates)
    if wanted in rates:
        return wanted
    lower = [r for r in rates if r <= wanted]
    return max(lower) if lower else min(rates)


def listeners_of(channel: dict) -> int:
    try:
        return int(str(channel.get("listeners", "0")).strip() or 0)
    except ValueError:
        return 0


_ASCII_MAP = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2026": "...",
})


def clean_title(title: str) -> str:
    title = "".join(ch for ch in str(title) if ch.isprintable() or ch.isspace())
    title = re.sub(r"\s+", " ", title).strip()
    return title.replace("|", "-")  # '|' separates fields in AIMP playlists


def ascii_title(title: str) -> str:
    title = unicodedata.normalize("NFKD", title.translate(_ASCII_MAP))
    return title.encode("ascii", "ignore").decode("ascii").strip()


@dataclass
class Changes:
    """Differences between the previous playlist and the new one."""
    added: list = field(default_factory=list)    # new entries
    removed: list = field(default_factory=list)  # previous entries
    renamed: list = field(default_factory=list)  # (previous, new)
    bitrate: list = field(default_factory=list)  # (previous, new)
    address: list = field(default_factory=list)  # (previous, new): same bitrate, other URL

    def __bool__(self) -> bool:
        return any((self.added, self.removed, self.renamed, self.bitrate, self.address))


@dataclass
class Result:
    entries: list = field(default_factory=list)
    files: dict = field(default_factory=dict)
    changes: Changes = field(default_factory=Changes)
    skipped: list = field(default_factory=list)


def build_entries(channels: list, cfg: dict) -> tuple[list, list]:
    exclude = set(cfg["exclude"])
    excluded_genres = {g.lower() for g in cfg["exclude_genres"]}
    entries, skipped = [], []
    for ch in channels:
        if not isinstance(ch, dict):
            skipped.append("?")
            continue
        cid = str(ch.get("id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_]+", cid):
            skipped.append(cid or "?")
            continue
        if cid in exclude:
            continue
        genres = {g.strip().lower() for g in re.split(r"[|/]", str(ch.get("genre", ""))) if g.strip()}
        if genres & excluded_genres:
            continue
        rate = pick_bitrate(
            mp3_bitrates(dict(ch, id=cid)),
            cfg["bitrate_overrides"].get(cid, cfg["default_bitrate"]),
        )
        if rate is None:
            skipped.append(cid)
            continue
        entries.append({
            "id": cid,
            "title": clean_title(ch.get("title", "")) or cid,
            "bitrate": rate,
            "listeners": listeners_of(ch),
            "url": f"{cfg['scheme']}://{cfg['host']}/{cid}-{rate}-mp3",
        })
    return entries, skipped


def order_entries(entries: list, mode: str, existing_ids: list[str]) -> list:
    by_popularity = sorted(entries, key=lambda e: (-e["listeners"], e["title"].lower(), e["id"]))
    if mode == "listeners":
        return by_popularity
    if mode == "alpha":
        return sorted(entries, key=lambda e: (e["title"].lower(), e["id"]))
    if mode == "api":
        return list(entries)
    # "keep": existing order first, new channels (most popular first) at the end
    position = {cid: i for i, cid in enumerate(existing_ids)}
    kept = sorted((e for e in entries if e["id"] in position), key=lambda e: position[e["id"]])
    new = [e for e in by_popularity if e["id"] not in position]
    return kept + new


STREAM_RE = re.compile(r"/([A-Za-z0-9_]+)-(\d+)-mp3$")


def read_existing(path: Path) -> list[dict]:
    """Entries of the previous playlist (id, title, bitrate, url), in file order."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    entries: list[dict] = []
    seen: set[str] = set()
    title = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            title = line.split(",", 1)[1].strip() if "," in line else ""
        elif line and not line.startswith("#"):
            m = STREAM_RE.search(line)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                entries.append({"id": m.group(1), "title": title or m.group(1),
                                "bitrate": int(m.group(2)), "url": line})
            title = ""
    return entries


def read_existing_ids(path: Path) -> list[str]:
    return [e["id"] for e in read_existing(path)]


def diff_entries(old: list[dict], new: list[dict]) -> Changes:
    old_by_id = {e["id"]: e for e in old}
    new_ids = {e["id"] for e in new}
    changes = Changes(
        added=[e for e in new if e["id"] not in old_by_id],
        removed=[e for e in old if e["id"] not in new_ids],
    )
    for e in new:
        o = old_by_id.get(e["id"])
        if o is None:
            continue
        if (ascii_title(e["title"]) or e["id"]) != o["title"]:  # the old file is ASCII
            changes.renamed.append((o, e))
        if o["bitrate"] != e["bitrate"]:
            changes.bitrate.append((o, e))
        elif o["url"] != e["url"]:
            changes.address.append((o, e))
    return changes


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def render_m3u(entries: list, title_of) -> str:
    lines = ["#EXTM3U"]
    for e in entries:
        lines += [f"#EXTINF:-1,{title_of(e)}", e["url"]]
    return EOL.join(lines) + EOL


def render_pls(entries: list) -> str:
    lines = ["[playlist]"]
    for i, e in enumerate(entries, 1):
        lines += [f"File{i}={e['url']}", f"Length{i}=-1", f"Title{i}={ascii_title(e['title']) or e['id']}"]
    lines += [f"NumberOfEntries={len(entries)}", "Version=2"]
    return EOL.join(lines) + EOL


def render_aimp(entries: list) -> str:
    lines = [
        "#-----SUMMARY-----#",
        f"ID={AIMP_PLAYLIST_ID}",
        "Name=SomaFM",
        "NameIsAutoSet=0",
        f"ContentFiles={len(entries)}",
        "PlaybackCursor=0",
        "",
        "#-----SETTINGS-----#",
        "Flags=2047",
        "FormatMainLine=%IF(%Artist,%Artist - %Title,%Title)",
        "FormatSecondLine=%FileFormat :: %SampleRate, %BitRate, %FileSize",
        "GroupFormatLine=%FileDir",
        "",
        "#-----CONTENT-----#",
        "-Radio",
    ]
    for i, e in enumerate(entries):
        lines.append(f"{e['url']}|{e['title']}||||||||||0|0|0|0|0|0|1|{i}||")
    return EOL.join(lines) + EOL


def render_all(entries: list, basename: str) -> dict:
    m3u_ascii = render_m3u(entries, lambda e: ascii_title(e["title"]) or e["id"])
    m3u_utf8 = render_m3u(entries, lambda e: e["title"])
    return {
        f"{basename}.m3u": m3u_ascii.encode("ascii"),
        f"{basename}.m3u8": m3u_utf8.encode("utf-8"),  # UTF-8 without BOM
        f"{basename}.pls": render_pls(entries).encode("ascii"),
        f"{basename}.aimppl4": b"\xff\xfe" + render_aimp(entries).encode("utf-16-le"),
    }


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------

def generate(data: object, cfg: dict, existing: list[dict]) -> Result:
    if not isinstance(data, dict) or not isinstance(data.get("channels"), list):
        raise GenerationError("unexpected channel list format (no 'channels' array)")
    channels = data["channels"]
    existing_ids = [e["id"] for e in existing]
    entries, skipped = build_entries(channels, cfg)

    if len(entries) < cfg["min_channels"]:
        raise GenerationError(
            f"only {len(entries)} usable channels (min_channels={cfg['min_channels']}); "
            "refusing to overwrite the playlists"
        )
    if len(skipped) > max(3, len(channels) // 10):
        raise GenerationError(
            f"{len(skipped)} of {len(channels)} channels have no usable MP3 stream "
            f"({', '.join(skipped[:8])}...); the API format may have changed"
        )
    if existing_ids and len(entries) < len(existing_ids) / 2:
        raise GenerationError(
            f"channel count dropped from {len(existing_ids)} to {len(entries)}; "
            "refusing to overwrite the playlists"
        )

    entries = order_entries(entries, cfg["order"], existing_ids)
    return Result(
        entries=entries,
        files=render_all(entries, cfg["basename"]),
        changes=diff_entries(existing, entries),
        skipped=skipped,
    )


def write_if_changed(path: Path, data: bytes) -> bool:
    try:
        if path.read_bytes() == data:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


# --------------------------------------------------------------------------
# stream check (advisory)
# --------------------------------------------------------------------------

def probe(url: str, timeout: float = 10.0) -> tuple[bool, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Icy-MetaData": "0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if not 200 <= resp.status < 300:
                return False, f"HTTP {resp.status}"
            if not resp.read(256):
                return False, "empty response"
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except http.client.BadStatusLine as exc:
        if str(getattr(exc, "line", "")).startswith("ICY 200"):
            return True, "ICY 200"
        return False, f"bad status line: {exc}"
    except (OSError, http.client.HTTPException) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def check_streams(entries: list, workers: int = 8) -> list[tuple[dict, str]]:
    failed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for entry, (ok, detail) in zip(entries, pool.map(lambda e: probe(e["url"]), entries)):
            if not ok:
                failed.append((entry, detail))
    return failed


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def _name(entry: dict) -> str:
    return f"{entry['title']} (`{entry['id']}`)"


def render_release_notes(res: Result, changed_files: list[str], date: str) -> str:
    ch = res.changes
    lines = [
        f"## SomaFM playlists, {date}",
        "",
        f"{len(res.entries)} channels in each playlist. Changes since the previous version:",
    ]

    def section(title: str, items: list) -> None:
        if items:
            lines.extend(["", f"**{title}**"] + items)

    section("Added", [f"- {_name(e)}, {e['bitrate']} kbps" for e in res.changes.added])
    section("Removed", [f"- {_name(e)}" for e in ch.removed])
    section("Renamed", [f"- {o['title']} \u2192 {n['title']} (`{n['id']}`)" for o, n in ch.renamed])
    section("Bitrate changed",
            [f"- {_name(n)}: {o['bitrate']} \u2192 {n['bitrate']} kbps" for o, n in ch.bitrate])
    section("Stream address changed", [f"- {_name(n)}" for _, n in ch.address])
    if not ch:
        lines += ["", "No channels were added, removed or renamed; the files were regenerated "
                      "(formatting or settings changed)."]
    lines += ["", f"Updated files: {', '.join(changed_files)}.", ""]
    return "\n".join(lines)


def write_step_summary(res: Result, changed: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "### SomaFM playlists",
        "",
        f"{len(res.entries)} channels. Changed files: {', '.join(changed) if changed else 'none'}.",
    ]
    if res.changes.added:
        lines.append(f"- Added: {', '.join(_name(e) for e in res.changes.added)}")
    if res.changes.removed:
        lines.append(f"- Removed: {', '.join(_name(e) for e in res.changes.removed)}")
    if res.changes.renamed or res.changes.bitrate or res.changes.address:
        lines.append(f"- Renamed: {len(res.changes.renamed)}, bitrate changed: {len(res.changes.bitrate)}, "
                     f"address changed: {len(res.changes.address)}")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate SomaFM playlists from the official channel list.")
    p.add_argument("--config", type=Path, default=None,
                   help="path to config.json (default: config.json next to this script, if present)")
    p.add_argument("--input", type=Path, default=None,
                   help="read the channel list from a local JSON file instead of the API")
    p.add_argument("--output-dir", type=Path, default=None,
                   help="where to write the playlists (overrides output_dir from the config)")
    p.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    p.add_argument("--notes-file", type=Path, default=None,
                   help="when playlist files changed, write release notes (Markdown) to this file")
    p.add_argument("--check", action="store_true",
                   help="also test every stream URL (advisory: failures are reported, exit code stays 0)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config_path = args.config
        if config_path is None:
            default = Path(__file__).resolve().parent / "config.json"
            config_path = default if default.is_file() else None
        cfg, base = load_config(config_path)
        out_dir = args.output_dir if args.output_dir is not None else base / cfg["output_dir"]

        if args.input is not None:
            try:
                data = json.loads(args.input.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise GenerationError(f"cannot read {args.input}: {exc}") from exc
            source = str(args.input)
        else:
            data = fetch_json(cfg["api_url"])
            source = cfg["api_url"]

        existing = read_existing(out_dir / f"{cfg['basename']}.m3u")
        res = generate(data, cfg, existing)
    except GenerationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Source: {source}")
    ch = res.changes
    print(f"Playlist: {len(res.entries)} channels"
          f" (+{len(ch.added)} added, -{len(ch.removed)} removed)")
    for e in ch.added:
        print(f"  added:   {e['title']} ({e['id']}), {e['bitrate']} kbps")
    for e in ch.removed:
        print(f"  removed: {e['title']} ({e['id']})")
    for o, n in ch.renamed:
        print(f"  renamed: {o['title']} -> {n['title']} ({n['id']})")
    for o, n in ch.bitrate:
        print(f"  bitrate: {n['title']} ({n['id']}): {o['bitrate']} -> {n['bitrate']} kbps")
    for _, n in ch.address:
        print(f"  address: {n['title']} ({n['id']})")
    for cid in res.skipped:
        print(f"  skipped (no MP3 stream listed): {cid}")

    changed = []
    for name, data_bytes in res.files.items():
        path = out_dir / name
        if args.dry_run:
            same = path.is_file() and path.read_bytes() == data_bytes
            changed_now = not same
        else:
            changed_now = write_if_changed(path, data_bytes)
        if changed_now:
            changed.append(name)
        print(f"  {path}: {'updated' if changed_now else 'unchanged'}"
              f"{' (dry run)' if args.dry_run and changed_now else ''}")
    write_step_summary(res, changed)

    if args.notes_file is not None and changed:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        args.notes_file.parent.mkdir(parents=True, exist_ok=True)
        args.notes_file.write_text(render_release_notes(res, changed, date), encoding="utf-8")
        print(f"Release notes written to {args.notes_file}")

    if args.check:
        failed = check_streams(res.entries)
        in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
        for entry, detail in failed:
            msg = f"stream not reachable: {entry['title']} ({entry['url']}): {detail}"
            print(f"::warning::{msg}" if in_actions else f"  WARNING {msg}")
        print(f"Stream check: {len(res.entries) - len(failed)}/{len(res.entries)} reachable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
