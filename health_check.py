#!/usr/bin/env python3
"""IPTV health check & high-quality filter.

Phase 1 — Text filter: strip non-1080p/4K/8K & non-24/7 sources
          CCTV / 卫视 always pass regardless of resolution tag
Phase 2 — ffprobe probe: verify resolution + codec on survivors
Output → cn_healthy.m3u  (with safety guards against bad overwrites)
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

UPSTREAM_URL = (
    "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/cn.m3u"
)
MIN_HEIGHT = 1080
TIMEOUT = 15
CONCURRENCY = 15
OUTPUT = "cn_hd.m3u"
BACKUP = OUTPUT + ".bak"
GUARD_MIN_ABSOLUTE = 20
GUARD_MIN_RATIO = 0.5

RES_RE = re.compile(r"\((\d+)p\)")
TAG_BAD_RE = re.compile(r"\[Not 24/7\]|\[Geo-blocked\]")
# Priority channels: CCTV series + 卫视 (satellite TV)
PRIORITY_RE = re.compile(r'tvg-id="CCTV|tvg-name="CCTV|,CCTV-|卫视|Satellite')

# Classification patterns
CCTV_RE = re.compile(r'tvg-id="CCTV|,CCTV-')
WS_RE = re.compile(r'卫视|Satellite')
CN_RE = re.compile(r'[\u4e00-\u9fff]')  # Chinese characters

# group-title injection
GROUP_RE = re.compile(r'group-title="([^"]*)"')


def is_priority(info: str) -> bool:
    """CCTV / 卫视 channels are always kept regardless of resolution."""
    return bool(PRIORITY_RE.search(info))


def classify_channel(info: str) -> str:
    """Assign group-title based on channel type only."""
    is_cctv = bool(CCTV_RE.search(info))
    is_weishi = bool(WS_RE.search(info))
    is_chinese = bool(CN_RE.search(info))

    if is_cctv:
        return "CCTV"
    if is_weishi:
        return "卫视台"
    if is_chinese:
        return "地方台"
    return "其他"


def format_name(info: str, height: int) -> str:
    """Rebuild name part of EXTINF with [resolution] tag.

    Input:  ...,CCTV-1 (1080p)
    Output: ...,CCTV-1 [1080p]
    """
    idx = info.rfind(",")
    if idx == -1:
        return info
    prefix = info[:idx]
    name = info[idx + 1:]

    # Remove existing (Np) or [Np] from name, then append clean tag
    name_clean = re.sub(r"\s*[\[\(]\d+p[\]\)]", "", name).strip()
    new_name = f"{name_clean} [{height}p]"
    return f"{prefix},{new_name}"


def build_extinf(info: str, group: str, height: int) -> str:
    """Build final EXTINF line with group-title and formatted name."""
    # Inject or replace group-title
    if GROUP_RE.search(info):
        tagged = GROUP_RE.sub(f'group-title="{group}"', info)
    else:
        idx = info.rfind(",")
        tagged = info[:idx] + f' group-title="{group}"' + info[idx:] if idx != -1 else info
    # Format name with resolution
    return format_name(tagged, height)


def count_entries(path: str) -> int:
    if not os.path.isfile(path):
        return 0
    count = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#EXTINF:"):
                count += 1
    return count


def guard_check(healthy_count: int) -> bool:
    prev = count_entries(OUTPUT)
    if healthy_count < GUARD_MIN_ABSOLUTE:
        print(f"[guard] BLOCKED: healthy_count={healthy_count} < min_absolute={GUARD_MIN_ABSOLUTE}")
        return False
    if prev > 0 and healthy_count < prev * GUARD_MIN_RATIO:
        print(f"[guard] BLOCKED: healthy_count={healthy_count} < prev_count={prev} * {GUARD_MIN_RATIO}")
        return False
    print(f"[guard] passed  (healthy={healthy_count}, prev={prev})")
    return True


def atomic_write(content: str, path: str):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        shutil.move(tmp, path)
        print(f"[write] {path} ({len(content)} bytes)")
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


async def fetch_upstream(session) -> str:
    async with session.get(UPSTREAM_URL, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        return await resp.text()


def parse_m3u(content: str):
    entries = []
    info = None
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            info = line
        elif line.startswith("http") and info:
            entries.append((info, line))
            info = None
    return entries


def text_filter(entries):
    """Phase 1 — text-only filter.

    Priority channels (CCTV/卫视): always pass (no tag or res filtering).
    Others:         skip [Not 24/7]/[Geo-blocked], require >= 1080p tag.
    """
    kept = []
    dropped = {"bad_tag": 0, "low_res": 0, "no_res": 0, "priority_kept": 0}
    for info, url in entries:
        prio = is_priority(info)
        if prio:
            kept.append((info, url))
            dropped["priority_kept"] += 1
            continue
        if TAG_BAD_RE.search(info):
            dropped["bad_tag"] += 1
            continue
        m = RES_RE.search(info)
        if not m:
            dropped["no_res"] += 1
            continue
        res = int(m.group(1))
        if res < MIN_HEIGHT:
            dropped["low_res"] += 1
            continue
        kept.append((info, url))

    print(f"[text_filter] kept={len(kept)} "
          f"(priority={dropped['priority_kept']}, "
          f"other={len(kept) - dropped['priority_kept']}), "
          f"dropped_bad_tag={dropped['bad_tag']}, "
          f"dropped_low_res={dropped['low_res']}, "
          f"dropped_no_res={dropped['no_res']}")
    return kept


def dedup_by_channel(entries, max_per_channel=2, max_per_priority=3):
    """Deduplicate; priority channels get more slots."""
    groups = {}
    for info, url in entries:
        name = info.split(",")[-1].strip() if "," in info else info
        m = RES_RE.search(info)
        res = int(m.group(1)) if m else 0
        groups.setdefault(name, []).append((res, info, url, is_priority(info)))

    result = []
    for name, items in groups.items():
        items.sort(key=lambda x: -x[0])  # highest res first
        limit = max_per_priority if items[0][3] else max_per_channel
        for res, info, url, _ in items[:limit]:
            result.append((info, url))
    print(f"[dedup] {len(entries)} → {len(result)} "
          f"(max {max_per_channel}/channel, {max_per_priority}/priority)")
    return result


async def probe_stream(session, url: str, prio: bool):
    """Phase 2 — ffprobe probe.

    Priority channels: pass if alive at any resolution.
    Others:           require >= MIN_HEIGHT.
    """
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            if resp.status != 200:
                return {"alive": False, "height": 0, "codec": ""}
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-rw_timeout", str(TIMEOUT * 1000000), url],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
        if result.returncode != 0:
            return {"alive": False, "height": 0, "codec": ""}
        data = json.loads(result.stdout)
        height = 0
        codec = ""
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                h = s.get("height", 0)
                if h > height:
                    height = h
                    codec = s.get("codec_name", "")
        if prio:
            alive = height > 0  # any resolution is OK
        else:
            alive = height >= MIN_HEIGHT
        return {"alive": alive, "height": height, "codec": codec}
    except Exception:
        return {"alive": False, "height": 0, "codec": ""}


async def main():
    import aiohttp

    if os.path.isfile(OUTPUT):
        shutil.copy2(OUTPUT, BACKUP)
        print(f"[backup] {OUTPUT} → {BACKUP}")

    print("[health_check] Fetching upstream...")
    connector = aiohttp.TCPConnector(limit=CONCURRENCY)
    async with aiohttp.ClientSession(connector=connector) as session:
        content = await fetch_upstream(session)
        entries = parse_m3u(content)
        print(f"[health_check] Raw entries: {len(entries)}")

        candidates = text_filter(entries)
        candidates = dedup_by_channel(candidates, max_per_channel=2, max_per_priority=3)
        if not candidates:
            print("[health_check] No candidates after text filter, keeping existing output")
            sys.exit(1)

        print(f"[health_check] Probing {len(candidates)} streams...")
        tasks = [probe_stream(session, url, is_priority(info)) for info, url in candidates]
        results = await asyncio.gather(*tasks)

    # Collect healthy entries with sort info
    healthy = []
    groups = {"CCTV": 0, "卫视台": 0, "地方台": 0, "其他": 0}
    for (info, url), probe in zip(candidates, results):
        name = info.split(",")[-1].strip() if "," in info else ""
        prio = is_priority(info)
        if probe["alive"]:
            group = classify_channel(info)
            extinf = build_extinf(info, group, probe["height"])
            healthy.append((group, -probe["height"], name, extinf, url))
            groups[group] += 1
            tag = "[P]" if prio else ""
            print(f"  [OK]{tag} [{group}] {name} | {probe['height']}p {probe['codec']}")
        else:
            reason = "unreachable" if not probe["height"] else f"{probe['height']}p"
            print(f"  [--] {name} | {reason}")

    healthy.sort()  # by group, then -height (higher res first)

    lines = ["#EXTM3U\n"]
    for _, _, _, extinf, url in healthy:
        lines.append(f"{extinf}\n{url}\n")
    healthy_count = len(healthy)

    print(f"\n[health_check] Healthy: {healthy_count}/{len(candidates)}")
    for g in ["CCTV", "卫视台", "地方台", "其他"]:
        print(f"  {g}: {groups[g]}")

    if not guard_check(healthy_count):
        if os.path.isfile(BACKUP):
            shutil.copy2(BACKUP, OUTPUT)
            print(f"[rollback] restored {OUTPUT} from {BACKUP}")
        sys.exit(1)

    atomic_write("".join(lines), OUTPUT)
    print("[health_check] Done")


if __name__ == "__main__":
    asyncio.run(main())
