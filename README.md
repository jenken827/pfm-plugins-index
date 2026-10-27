# pfm-plugins-index

pfm 的**官方插件索引**（只放索引，不放插件二进制）。

pfm 客户端通过 `plugin.index_url` 拉这个仓库 release 里的 `index.json`，并用编译进
二进制的 ed25519 公钥校验同目录的 `index.json.sig`；通过后再按条目里的 `sha256`
校验下载到的插件包。安装流程与信任模型见 pfm 仓库 `DESIGN.md` §11.4.5.1。

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
      "name": "pfm-baidu",
      "driver_id": "baidu",
      "version": "1.0.0",
      "abi_version": 1,
      "platform": "linux-amd64",
      "url": "https://github.com/jenken827/pfm-plugin-baidu/releases/download/v1.0.0/pfm-baidu-1.0.0-linux-amd64.tar.gz",
      "sha256": "<包内 tar.gz 的 sha256>",
      "min_pfm_version": "",
      "released_at": "2026-10-27T00:00:00Z"
    }
  ]
}
```

- `platform` 用 `GOOS-GOARCH`（如 `linux-amd64`、`windows-amd64`、`darwin-arm64`），
  每个平台一条；客户端只挑当前平台。
- `seq` 只增不减：客户端本地记录过更大的值，就会拒绝更旧的索引。
- 插件包由**各插件自己的仓库**发布（`release/` 产物），不放在本仓库。

## 发布一条新条目

```bash
# 1) 在插件仓库构建发布包（会打印 sha256）
cd ../pfm-plugin-baidu
scripts/make-release.sh --version 1.0.0 --seq 1 \
  --url-prefix https://github.com/jenken827/pfm-plugin-baidu/releases/download/v1.0.0/ \
  --key <索引签名私钥>

# 2) 把 release/<pkg>.tar.gz 上传到插件仓库的 release（tag = v1.0.0）

# 3) 把条目并进本仓库的 index.json，seq 递增，重新签名
pfm plugin sign-index --key <索引签名私钥> index.json

# 4) 提交并打 release（把 index.json 与 index.json.sig 一起作为 release 资产）
```

⚠️ 签名私钥只在发布机上，**永远不要**提交到本仓库或插件仓库；
私钥泄露等于攻击者可以签名任意插件。

⚠️ 本索引只证明"包出自签名的发布流程"，**不证明代码安全**：插件与 pfm 宿主同权限运行，
能读宿主的 `config.yaml`（含全部 token）与 `data/`。
