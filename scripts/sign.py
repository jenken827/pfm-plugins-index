#!/usr/bin/env python3
"""给 index.json 签名，生成 index.json.sig（base64 的 ed25519 签名，覆盖索引原始字节）。

与 `pfm plugin sign-index` 的产物逐字节等价：私钥文件是 `pfm plugin keygen` 写出的
那种 base64 文本（32 字节种子，或 64 字节完整私钥），签名用 openssl 的 ed25519
（`pkeyutl -rawin`）算，不依赖 pfm 二进制，CI 里不用编译宿主。

用法：
    python3 scripts/sign.py index.json --key <私钥文件> [--expect-public-key <base64>]

`--expect-public-key` 会先从私钥推导公钥再比对（推荐：防止把不匹配的私钥配进 CI，
签出来的索引客户端一个都不认）。签名后还会自检一遍。
"""

from __future__ import annotations

import argparse
import base64
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# PKCS#8 包装一个 ed25519 私钥种子的固定前缀
PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")


def die(msg: str) -> None:
    print(f"错误：{msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str]) -> bytes:
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        die(f"{cmd[0]} 执行失败：{proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def pem_from_seed(seed: bytes) -> str:
    body = base64.b64encode(PKCS8_PREFIX + seed).decode()
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return "-----BEGIN PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END PRIVATE KEY-----\n"


def read_seed(path: str) -> bytes:
    raw = Path(path).read_text().strip()
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception as err:  # noqa: BLE001 - 私钥格式错误统一报错
        die(f"私钥不是合法的 base64：{err}")
    if len(decoded) == 64:
        decoded = decoded[:32]
    if len(decoded) != 32:
        die(f"私钥长度不对：{len(decoded)} 字节（应为 32 字节种子或 64 字节完整私钥）")
    return decoded


def main() -> int:
    parser = argparse.ArgumentParser(description="给索引签名（与 pfm plugin sign-index 等价）")
    parser.add_argument("index", help="要签名的 index.json")
    parser.add_argument("--key", required=True, help="私钥文件（base64）")
    parser.add_argument("--expect-public-key", default="",
                        help="期望的公钥（base64）；给了就校验私钥是否匹配")
    args = parser.parse_args()

    seed = read_seed(args.key)
    payload = Path(args.index).read_bytes()

    workdir = tempfile.mkdtemp(prefix="pfm-index-sign-")
    try:
        key_pem = Path(workdir, "key.pem")
        key_pem.write_text(pem_from_seed(seed))
        key_pem.chmod(0o600)

        public = run(["openssl", "pkey", "-in", str(key_pem), "-pubout", "-outform", "DER"])[-32:]
        public_b64 = base64.b64encode(public).decode()
        if args.expect_public_key:
            expected = args.expect_public_key.strip()
            if expected != public_b64:
                die(f"私钥与期望公钥不匹配：\n  期望 {expected}\n  实际 {public_b64}")

        signature = run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(key_pem),
                         "-in", args.index])

        # 自检一遍，避免写出客户端验不过的签名
        sig_path = Path(workdir, "sig.bin")
        sig_path.write_bytes(signature)
        pub_pem = Path(workdir, "pub.pem")
        pub_pem.write_bytes(run(["openssl", "pkey", "-in", str(key_pem), "-pubout"]))
        verify = subprocess.run(["openssl", "pkeyutl", "-verify", "-rawin", "-pubin",
                                 "-inkey", str(pub_pem), "-in", args.index, "-sigfile", str(sig_path)],
                                capture_output=True)
        if verify.returncode != 0:
            die("签名自检失败（openssl 验不过）")

        sig_file = Path(args.index + ".sig")
        sig_file.write_text(base64.b64encode(signature).decode() + "\n")
        print(f"已签名：{sig_file}（公钥 {public_b64}）")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
