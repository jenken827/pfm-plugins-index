#!/usr/bin/env python3
"""从各插件仓库的最新 Release 重建 index.json（未签名）。

索引内容完全由 plugins.json 的 sources 决定：每次同步都重建整个 plugins 数组，
条目的 url / sha256 直接取自插件仓库 Release 里的 index-entries.json 资产。
内容没变就不动 seq（幂等）；变了 seq + 1。

索引只收各插件的**当前版本**（每个仓库的 latest release）；回滚靠客户端本地的
.versions/ 归档，不靠索引里的旧条目。

用法：
    python3 scripts/sync.py --config plugins.json --index index.json
    （可选）--summary "$GITHUB_STEP_SUMMARY" 把结果写进 Actions 摘要
    （可选）--token "$GH_TOKEN" 提 API 速率上限（公开仓库不需要）

退出码：0 = 成功（有或没有变化都算成功）；1 = 有插件条目校验失败。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"

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


def http_get(url: str, token: str | None, *, accept: str) -> bytes:
    """带一次重试的 GET；404 直接抛 HTTPError 交给调用方判断。"""
    req = urllib.request.Request(url, headers={
        "Accept": accept,
        "User-Agent": "pfm-plugins-index-sync",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    last: Exception | None = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as err:
            if err.code == 404:
                raise
            last = err
        except (urllib.error.URLError, TimeoutError) as err:
            last = err
    raise RuntimeError(f"请求失败：{url}（{last}）")


def fetch_json(url: str, token: str | None):
    return json.loads(http_get(url, token, accept="application/vnd.github+json"))


def fetch_asset(url: str, token: str | None) -> bytes:
    return http_get(url, token, accept="application/octet-stream")


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
    # 落到固定字段顺序（omitempty 的另一个身份字段不出现）
    return {k: entry[k] for k in ENTRY_FIELDS if k in entry}


def load_source(source: dict, token: str | None, problems: list[str]) -> list[dict]:
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
        release = fetch_json(f"{API}/repos/{repo}/releases/latest", token)
    except urllib.error.HTTPError as err:
        if err.code == 404:
            print(f"跳过 {repo}：还没有 Release（最新一次都没有发布过）")
            return []
        raise

    tag = release.get("tag_name", "")
    if release.get("draft") or release.get("prerelease"):
        problems.append(f"{repo}：latest release {tag} 是 draft 或 prerelease")
        return []
    version = tag[1:] if tag.startswith("v") else tag
    if not VERSION_RE.match(version):
        problems.append(f"{repo}：tag {tag!r} 应为 v<版本号>")
        return []

    asset = next((a for a in release.get("assets", []) if a.get("name") == "index-entries.json"), None)
    if asset is None:
        problems.append(f"{repo}：release {tag} 里没有 index-entries.json 资产")
        return []

    raw_entries = json.loads(fetch_asset(asset["browser_download_url"], token))
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


def managed_names(sources: list[dict]) -> list[tuple[str, str]]:
    """清单里合法的插件包名（用于判断「条目不翼而飞」）。"""
    names = []
    for source in sources:
        base = source.get("repo", "").partition("/")[2]
        if REPO_NAME_RE.match(base):
            names.append((base, source.get("repo", "")))
    return names


def render(index: dict) -> str:
    return json.dumps(index, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="从各插件仓库的最新 Release 重建索引")
    parser.add_argument("--config", default="plugins.json", help="插件仓库清单")
    parser.add_argument("--index", default="index.json", help="要重建的索引文件")
    parser.add_argument("--token", default=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"),
                        help="GitHub API token（公开仓库可省略）")
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
        entries.extend(load_source(source, args.token, problems))

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

    # 仍然在清单里的插件，其条目不允许凭空消失（发布被删、校验回归都会命中），
    # 命中就不写文件，交给人处理。
    managed = {name for name, _ in managed_names(sources)}
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
    payload = render({"seq": old_seq, "plugins": entries})
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
        print("\n有插件被跳过（条目已失效，索引里没有它们）：", file=sys.stderr)
        for item in problems:
            print(f"  - {item}", file=sys.stderr)

    if args.summary:
        lines = [f"### 索引同步", "",
                 f"- 结果：{'已更新，seq ' + str(seq) if changed else '无变化，seq ' + str(seq)}",
                 f"- 条目：{len(entries)} 条"]
        if problems:
            lines += ["", "跳过的来源："] + [f"- {item}" for item in problems]
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
