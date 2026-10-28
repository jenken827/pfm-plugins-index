# pfm-plugins-index

pfm 的**官方插件索引**（只放索引，不放插件二进制）。

pfm 客户端通过 `plugin.index_url` 拉这个仓库 release 里的 `index.json`，并用编译进
二进制的 ed25519 公钥校验同目录的 `index.json.sig`；通过后再按条目里的 `sha256`
校验下载到的插件包。安装流程见 pfm 仓库的 `internal/plugin`（`pfm plugin install`）。

```
index.json       签名索引（seq 单调递增，防镜像回退）
index.json.sig   base64 的 ed25519 签名，覆盖 index.json 原始字节
```

## 条目格式

```json
{
  "seq": 1,
  "plugins": [
    {
      "kind": "driver",
      "name": "pfm-plugin-driver-baidu",
      "driver_id": "baidu",
      "version": "1.0.0",
      "abi_version": 1,
      "platform": "linux-amd64",
      "url": "https://github.com/jenken827/pfm-plugin-driver-baidu/releases/download/v1.0.0/pfm-plugin-driver-baidu-1.0.0-linux-amd64.tar.gz",
      "sha256": "<包内 tar.gz 的 sha256>",
      "min_pfm_version": "",
      "released_at": "2026-10-27T00:00:00Z"
    }
  ]
}
```

- `kind` 是插件类型，目前有两种：`driver`（存储驱动，身份字段 `driver_id`）与
  `downloader`（离线下载后端，身份字段 `downloader_id`）。身份字段按类型二选一，必须与包内
  `plugin.json` 逐字一致，宿主只在 `downloader.plugin` 等于 `downloader_id` 时挂载该后端。
  下载器条目的样子（其余字段相同）：

  ```json
  {
    "kind": "downloader",
    "name": "pfm-plugin-downloader-aria2",
    "downloader_id": "aria2",
    "version": "1.0.0",
    "abi_version": 1,
    "platform": "linux-amd64",
    "url": "https://github.com/jenken827/pfm-plugin-downloader-aria2/releases/download/v1.0.0/pfm-plugin-downloader-aria2-1.0.0-linux-amd64.tar.gz",
    "sha256": "<包内 tar.gz 的 sha256>",
    "min_pfm_version": "",
    "released_at": "2026-10-27T00:00:00Z"
  }
  ```

- `platform` 用 `GOOS-GOARCH`（如 `linux-amd64`、`windows-amd64`、`darwin-arm64`），
  每个平台一条；客户端只挑当前平台。
- `seq` 只增不减：客户端本地记录过更大的值，就会拒绝更旧的索引。
- 插件包由**各插件自己的仓库**发布（`release/` 产物），不放在本仓库。

## 索引怎么更新（自动）

`plugins.json` 是来源清单：每行一个插件仓库。同步工作流 `.github/workflows/sync.yml`
（push 到 main、手动、以及每小时一次）会：

1. 读每个仓库的版本 tag，取版本号最大的那个（带 `-rc` 一类后缀的排在正式版之后）；
2. 取该 tag 的 Release 里的 `index-entries.json` 资产（插件仓库自己的发布工作流产出的）；
3. 校验条目：包名要形如 `pfm-plugin-<kind>-<id>`，`kind` / 身份字段 / `version` /
   `abi_version` 必须与包名和 tag 一致，`url` 必须指向该 Release 的
   `<name>-<version>-<platform>.tar.gz`，`sha256` 必须是 64 位小写十六进制；
4. 重建整个 `plugins` 数组——索引完全由清单决定，**不要手改 index.json**；
5. 内容有变化才 `seq + 1`，用 secret `INDEX_SIGNING_KEY` 签名，提交推送；
6. 发一个 tag 为 `seq-<N>` 的 Release（`index.json` + `index.json.sig`）并标成 latest，
   客户端的默认地址就是 `https://github.com/jenken827/pfm-plugins-index/releases/latest/download/index.json`。

几条约定：

- 索引只收各插件的**当前版本**（每个仓库的 latest release）。回滚靠客户端本地的
  `.versions/` 归档，不靠索引里的旧条目。
- 原本有条目、这次却同步不出来的插件，会让工作流**失败且不写文件**（发布被删、条目校验
  回归都会命中），不会静默把插件从索引里抹掉。
- 仓库不存在 / 未公开 / 还没有版本 tag、或者 tag 的 Release 里没有 `index-entries.json`，
  都会被跳过并打一行日志，不影响其它插件——插件仓库是逐个上线的。
- 私钥只放在 CI secret 里；`plugins.json` 的 `signing_public_key` 是 pfm 内置的那把公钥，
  配错私钥会在签名前报「私钥与期望公钥不匹配」。

### 一次性配置

在仓库 `Settings → Secrets and variables → Actions` 加 secret `INDEX_SIGNING_KEY`，
内容就是 `pfm plugin keygen` 写出的私钥文件（base64，32 字节种子或 64 字节完整私钥）。

### 手工流程（没有 CI 时，或要临时插一条）

```bash
# 1) 在插件仓库打 tag，等它自己的发布工作流出包（会带上 index-entries.json）

# 2) 本地重建索引（读各仓库最新 Release）并签名
python3 scripts/sync.py --config plugins.json --index index.json
python3 scripts/sign.py index.json --key <索引签名私钥> \
  --expect-public-key "$(python3 -c 'import json;print(json.load(open("plugins.json"))["signing_public_key"])')"

# 3) 提交，并把 index.json 与 index.json.sig 一起传成 Release 资产（标 latest）
```

签名与 `pfm plugin sign-index` 的产物等价（同一个私钥文件格式、同样覆盖 index.json 的原始字节）。

⚠️ 签名私钥只在发布机与 CI secret 里，**永远不要**提交到本仓库或插件仓库；
私钥泄露等于攻击者可以签名任意插件。

⚠️ 本索引只证明"包出自签名的发布流程"，**不证明代码安全**：插件与 pfm 宿主同权限运行，
能读宿主的 `config.yaml`（含全部 token）与 `data/`。
