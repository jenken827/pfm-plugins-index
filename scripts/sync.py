#!/usr/bin/env python3
"""从各插件仓库的最新发布重建 index.json（未签名）。

索引内容完全由 plugins.json 的 sources 决定：每次同步都重建整个 plugins 数组，
条目的 url / sha256 直接取自插件仓库 Release 里的 index-entries.json 资产。

「最新发布」= 仓库里版本号最大的 tag（不带 pre-release 后缀的优先）；对应 Release
里没有 index-entries.json 资产就跳过这个仓库。这里只走 git 与 Releases 下载地址，
不碰 GitHub API，所以不受 token 作用域和 API 速率限制影响。

内容没变就不动 seq（幂等）；变了 seq + 1。

用法：
    python3 scripts/sync.py --config plugins.json --index index.json
    （可选）--summary "$GITHUB_STEP_SUMMARY" 把结果写进 Actions 摘要

退出码：0 = 成功；1 = 有条目凭空消失（发布被删 / 校验回归），此时不写文件。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

# 包名约定：pfm-plugin-<kind>-<id>，条目里的身份字段必须与包名一致
REPO_NAME_RE = re.compile(r"^pfm-plugin-(driver|downloader)-([a-z0-9][a-z0-9_]*)$")
# 每种类型的插件协议版本（与宿主的 ABI 对齐）
KIND_ABI = {"driver": 1, "downloader": 1}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PLATFORM_RE = re.compile(r"^[a-z0-9]+-[a-z0-9]+$")
VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+)*(-[0-9A-Za-z.]+)?$")
RELEASED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# 条目字段顺序固定，保证同样的输入产出逐字节相同的 index.json
ENTRY_FIELDS = ("name", "kind", "driver_id", "downloader_id", "version", "abi_version",
                "platform", "url", "sha256", "min_pfm_version", "released_at")


def die(msg: str) -> None:
    print(f"错误：{msg}", file=sys.stderr)
    sys.exit(1)


def version_of(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


def version_key(tag: str) -> tuple:
    """按版本号排序；带 pre-release 后缀的排在同一个正式版之后。"""
    version = version_of(tag)
    core, _, pre = version.partition("-")
    parts = []
    for chunk in core.split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return (tuple(parts), 0 if pre else 1, pre)


def list_tags(repo: str) -> list[str]:
    proc = subprocess.run(["git", "ls-remote", "--tags", "--refs",
                           f"https://github.com/{repo}.git"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git ls-remote 失败")
    return [line.split("refs/tags/", 1)[1] for line in proc.stdout.splitlines()
            if "refs/tags/" in line]


def latest_tag(repo: str) -> str | None:
    tags = [tag for tag in list_tags(repo) if VERSION_RE.match(version_of(tag))]
    if not tags:
        return None
    return max(tags, key=version_key)


def download(url: str) -> bytes | None:
    """下载资产；404 返回 None（这个 tag 还没发布 Release 资产）。"""
    req = urllib.request.Request(url, headers={"User-Agent": "pfm-plugins-index-sync"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise
    except (urllib.error.URLError, TimeoutError) as err:
        raise RuntimeError(f"下载失败：{url}（{err}）") from err


def normalize_entry(raw: dict, *, repo: str, tag: str, version: str, kind: str,
                    plugin_id: str, name: str) -> dict:
    """校验一条索引条目并归一化成固定字段顺序；不合法就抛 ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError("条目不是对象")

    if raw.get("name") != name:
        raise ValueError(f"name={raw.get('name')!r}，应为包名 {name}")
    if raw.get("kind") != kind:
        raise ValueError(f"kind={raw.get('kind')!r}，应为 {kind}")
    identity = "driver_id" if kind == "driver" else "downloader_id"
    if raw.get(identity) != plugin_id:
        raise ValueError(f"{identity}={raw.get(identity)!r}，应为 {plugin_id}")
    other = "downloader_id" if kind == "driver" else "driver_id"
    if raw.get(other):
        raise ValueError(f"{kind} 类型不该带 {other}")

    if raw.get("version") != version:
        raise ValueError(f"version={raw.get('version')!r}，应为 {version}（tag {tag}）")
    if raw.get("abi_version") != KIND_ABI[kind]:
        raise ValueError(f"abi_version={raw.get('abi_version')!r}，{kind} 应为 {KIND_ABI[kind]}")

    platform = raw.get("platform")
    if not isinstance(platform, str) or not PLATFORM_RE.match(platform):
        raise ValueError(f"platform={platform!r} 应为 GOOS-GOARCH（如 linux-amd64）")

    url = raw.get("url")
    want_prefix = f"https://github.com/{repo}/releases/download/{tag}/"
    want_file = f"{name}-{version}-{platform}.tar.gz"
    if not isinstance(url, str) or not url.startswith(want_prefix) or url.rsplit("/", 1)[-1] != want_file:
        raise ValueError(f"url={url!r} 应指向 {want_prefix}{want_file}")

    sha = raw.get("sha256")
    if not isinstance(sha, str) or not SHA256_RE.match(sha):
        raise ValueError(f"sha256={sha!r} 应为 64 位小写十六进制")

    min_pfm = raw.get("min_pfm_version", "")
    if not isinstance(min_pfm, str):
        raise ValueError(f"min_pfm_version={min_pfm!r} 应为字符串")

    released_at = raw.get("released_at")
    if not isinstance(released_at, str) or not RELEASED_AT_RE.match(released_at):
        raise ValueError(f"released_at={released_at!r} 应为 UTC 的 RFC3339（如 2026-10-28T01:00:00Z）")

    entry = {
        "name": name,
        "kind": kind,
        identity: plugin_id,
        "version": version,
        "abi_version": KIND_ABI[kind],
        "platform": platform,
        "url": url,
        "sha256": sha,
        "min_pfm_version": min_pfm,
        "released_at": released_at,
    }
    return {k: entry[k] for k in ENTRY_FIELDS if k in entry}


def load_source(source: dict, problems: list[str]) -> list[dict]:
    repo = source.get("repo", "")
    owner, _, base = repo.partition("/")
    if not owner or not base:
        problems.append(f"{repo!r} 不是 owner/name")
        return []
    matched = REPO_NAME_RE.match(base)
    if not matched:
        problems.append(f"{repo}：包名要形如 pfm-plugin-<driver|downloader>-<id>")
        return []
    kind, plugin_id = matched.group(1), matched.group(2)

    try:
        tag = latest_tag(repo)
    except RuntimeError as err:
        # 仓库还不存在 / 还没公开都是常态（插件仓库逐个上线），不算错误；
        # 真要是网络抽风导致条目丢失，下面的「凭空消失」检查会兜住。
        first_line = (str(err).splitlines() or [""])[0]
        print(f"跳过 {repo}：读不到 tag（仓库不存在或未公开）：{first_line}")
        return []
    if not tag:
        print(f"跳过 {repo}：还没有版本 tag")
        return []

    version = version_of(tag)
    asset_url = f"https://github.com/{repo}/releases/download/{tag}/index-entries.json"
    payload = download(asset_url)
    if payload is None:
        print(f"跳过 {repo}@{tag}：该 tag 还没有发布 Release 资产")
        return []

    raw_entries = json.loads(payload)
    if not isinstance(raw_entries, list) or not raw_entries:
        problems.append(f"{repo}：index-entries.json 应为非空数组")
        return []

    entries, seen = [], set()
    for raw in raw_entries:
        try:
            entry = normalize_entry(raw, repo=repo, tag=tag, version=version, kind=kind,
                                    plugin_id=plugin_id, name=base)
        except ValueError as err:
            problems.append(f"{repo}@{tag}：{err}")
            continue
        key = (entry["kind"], entry["name"], entry["platform"])
        if key in seen:
            problems.append(f"{repo}@{tag}：平台 {entry['platform']} 重复")
            continue
        seen.add(key)
        entries.append(entry)
    if entries:
        print(f"同步 {repo}@{tag}：{len(entries)} 个平台")
    return entries


def managed_names(sources: list[dict]) -> set[str]:
    """清单里合法的插件包名（用于判断「条目不翼而飞」）。"""
    names = set()
    for source in sources:
        base = source.get("repo", "").partition("/")[2]
        if REPO_NAME_RE.match(base):
            names.add(base)
    return names


def render(index: dict) -> str:
    return json.dumps(index, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="从各插件仓库的最新发布重建索引")
    parser.add_argument("--config", default="plugins.json", help="插件仓库清单")
    parser.add_argument("--index", default="index.json", help="要重建的索引文件")
    parser.add_argument("--summary", default="", help="把结果追加进这个文件（Actions 摘要）")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as fh:
        config = json.load(fh)
    sources = config.get("sources") or []
    if not sources:
        die(f"{args.config} 里没有 sources")

    problems: list[str] = []
    entries: list[dict] = []
    for source in sources:
        entries.extend(load_source(source, problems))
    entries.sort(key=lambda e: (e["kind"], e["name"], e["platform"]))

    old_payload = ""
    old_seq = 0
    old_pairs: set[tuple[str, str, str]] = set()
    if os.path.exists(args.index):
        with open(args.index, encoding="utf-8") as fh:
            old_payload = fh.read()
        try:
            old_index = json.loads(old_payload)
            old_seq = int(old_index.get("seq") or 0)
        except (json.JSONDecodeError, ValueError):
            die(f"{args.index} 不是合法 JSON，拒绝覆盖")
        old_pairs = {(e.get("kind"), e.get("name"), e.get("platform"))
                     for e in old_index.get("plugins") or []}

    # 仍然在清单里的插件，条目不允许凭空消失（发布被删、校验回归都会命中）；
    # 命中就不写文件，交给人处理。
    managed = managed_names(sources)
    new_pairs = {(e["kind"], e["name"], e["platform"]) for e in entries}
    lost = sorted((old_pairs - new_pairs), key=str)
    lost = [item for item in lost if item[1] in managed]
    if lost:
        print("错误：以下条目原本在索引里，这次同步却没有了（发布被删或校验失败）：",
              file=sys.stderr)
        for kind, name, platform in lost:
            print(f"  - {kind}/{name}/{platform}", file=sys.stderr)
        print("拒绝写入，修好来源后重跑。", file=sys.stderr)
        return 1

    seq = old_seq
    payload = render({"seq": seq, "plugins": entries})
    changed = payload != old_payload
    if changed:
        seq = old_seq + 1 if old_seq else 1
        payload = render({"seq": seq, "plugins": entries})
        tmp = args.index + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, args.index)
        print(f"索引已更新：seq {old_seq} → {seq}，{len(entries)} 条")
    else:
        print(f"索引没有变化（seq {seq}，{len(entries)} 条）")

    if problems:
        print("\n有插件被跳过（索引里没有它们）：", file=sys.stderr)
        for item in problems:
            print(f"  - {item}", file=sys.stderr)

    if args.summary:
        lines = ["### 索引同步", "",
                 f"- 结果：{'已更新，seq ' + str(seq) if changed else '无变化，seq ' + str(seq)}",
                 f"- 条目：{len(entries)} 条"]
        if problems:
            lines += ["", "跳过的来源："] + [f"- {item}" for item in problems]
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
