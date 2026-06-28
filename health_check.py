#!/usr/bin/env python3
"""IPTV quality filter.

Filters upstream cn.m3u:
- CCTV / 卫视 always pass text filter regardless of resolution
- Others: skip [Not 24/7]/[Geo-blocked], require >= 1080p tag
- If a channel has 1080p+ sources, drop all <1080p sources
- If a channel has only <1080p sources, keep only the highest res one
- Output → cn_hd.m3u (with safety guards)
"""

import os
import re
import shutil
import sys
import tempfile
import urllib.request

UPSTREAM_URL = (
    "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/cn.m3u"
)
MIN_HEIGHT = 1080
OUTPUT = "cn_hd.m3u"
BACKUP = OUTPUT + ".bak"
GUARD_MIN_ABSOLUTE = 20
GUARD_MIN_RATIO = 0.5

RES_RE = re.compile(r"\((\d+)p\)")
TAG_BAD_RE = re.compile(r"\[Not 24/7\]|\[Geo-blocked\]")
PRIORITY_RE = re.compile(r'tvg-id="CCTV|tvg-name="CCTV|,CCTV-|卫视|Satellite')
CCTV_RE = re.compile(r'tvg-id="CCTV|,CCTV-')
WS_RE = re.compile(r'卫视|Satellite')
CN_RE = re.compile(r'[\u4e00-\u9fff]')
GROUP_RE = re.compile(r'group-title="([^"]*)"')


def is_priority(info: str) -> bool:
    return bool(PRIORITY_RE.search(info))


def classify_channel(info: str) -> str:
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


def cctv_sort_key(name: str):
    """Sort CCTV: 8K > 4K > numbered 1..17."""
    m = re.search(r'CCTV[- ]?(\d+)', name)
    if not m:
        return (9, 0)
    num = int(m.group(1))
    if '8K' in name.upper():
        return (0, 0)
    if '4K' in name.upper():
        return (0, 1)
    return (1, num)


def format_name(info: str, res: int) -> str:
    idx = info.rfind(",")
    if idx == -1:
        return info
    prefix = info[:idx]
    name = info[idx + 1:]
    name_clean = re.sub(r"\s*[\[\(]\d+p[\]\)]", "", name).strip()
    return f"{prefix},{name_clean} [{res}p]"


def build_extinf(info: str, group: str, res: int) -> str:
    if GROUP_RE.search(info):
        tagged = GROUP_RE.sub(f'group-title="{group}"', info)
    else:
        idx = info.rfind(",")
        tagged = info[:idx] + f' group-title="{group}"' + info[idx:] if idx != -1 else info
    return format_name(tagged, res)


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


def fetch_upstream() -> str | None:
    try:
        req = urllib.request.Request(UPSTREAM_URL)
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                print(f"[fetch_upstream] HTTP {resp.status}, abort")
                return None
            return resp.read().decode("utf-8")
    except Exception as e:
        print(f"[fetch_upstream] Error: {e}")
        return None


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


def dedup_by_channel(entries):
    """Deduplicate per channel:
    - If channel has any 1080p+ source → keep only 1080p+ entries
    - If all sources < 1080p → keep only the single highest res entry
    """
    groups = {}
    for info, url in entries:
        name = info.split(",")[-1].strip() if "," in info else info
        m = RES_RE.search(info)
        res = int(m.group(1)) if m else 0
        groups.setdefault(name, []).append((res, info, url, is_priority(info)))

    result = []
    for name, items in groups.items():
        items.sort(key=lambda x: -x[0])  # highest res first
        max_res = items[0][0]
        is_prio = items[0][3]

        if max_res >= MIN_HEIGHT:
            filtered = [x for x in items if x[0] >= MIN_HEIGHT]
            limit = 3 if is_prio else 2
            for res, info, url, _ in filtered[:limit]:
                result.append((info, url, res))
        else:
            res, info, url, _ = items[0]
            result.append((info, url, res))

    print(f"[dedup] {len(entries)} → {len(result)}")
    return result


def main():
    if os.path.isfile(OUTPUT):
        shutil.copy2(OUTPUT, BACKUP)
        print(f"[backup] {OUTPUT} → {BACKUP}")

    print("[filter] Fetching upstream...")
    content = fetch_upstream()
    if content is None:
        print("[filter] Upstream fetch failed, keeping existing output")
        sys.exit(1)

    entries = parse_m3u(content)
    print(f"[filter] Raw entries: {len(entries)}")

    candidates = text_filter(entries)
    candidates = dedup_by_channel(candidates)
    if not candidates:
        print("[filter] No candidates after filter, keeping existing output")
        sys.exit(1)

    healthy = []
    groups_count = {"CCTV": 0, "卫视台": 0, "地方台": 0, "其他": 0}
    for info, url, res in candidates:
        name = info.split(",")[-1].strip() if "," in info else ""
        group = classify_channel(info)
        extinf = build_extinf(info, group, res)
        healthy.append((group, -res, name, extinf, url))
        groups_count[group] += 1
        print(f"  [OK] [{group}] {name} [{res}p]")

    healthy.sort(key=lambda x: (
        x[0],                              # group
        x[1],                              # -res (higher first)
        cctv_sort_key(x[2]) if x[0] == "CCTV" else (0, x[2])  # CCTV numeric sort
    ))

    lines = ["#EXTM3U\n"]
    for _, _, _, extinf, url in healthy:
        lines.append(f"{extinf}\n{url}\n")
    healthy_count = len(healthy)

    print(f"\n[filter] Output: {healthy_count} streams")
    for g in ["CCTV", "卫视台", "地方台", "其他"]:
        print(f"  {g}: {groups_count[g]}")

    if not guard_check(healthy_count):
        if os.path.isfile(BACKUP):
            shutil.copy2(BACKUP, OUTPUT)
            print(f"[rollback] restored {OUTPUT} from {BACKUP}")
        sys.exit(1)

    atomic_write("".join(lines), OUTPUT)
    print("[filter] Done")


if __name__ == "__main__":
    main()
