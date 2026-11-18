"""lib.vault：密码库加密格式、落盘纪律与主密码后端。

对应 docs/DESIGN.md P1（每段一个）：
- S7：新库用 canonical AAD 绑定 header；旧常量 AAD 文件可打开、写盘自动升级；
  主密码错误依旧被拒
- S8：落盘走唯一临时名（mkstemp + atomic replace），并发写不互踩、不残留
- headless：无钥匙串环境经 gpg 后端解锁；save_master_auto 的优先级
- vault `key:<host>` 命名空间：names() 过滤密钥条目，与 protocol.md 约定一致

运行：
    python tests/test_vault.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common  # noqa: E402  先注入路径、设环境，再 import scripts 下的模块

TMP = _common.init_data_dir("asshcli-vault-")

from _common import check, finish, reset  # noqa: E402


def test_vault_aad_and_tmp() -> None:
    """P1 S7/S8：AAD 绑定 header + 临时文件唯一命名。"""
    import base64 as b64
    import json as jsonlib
    import threading

    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from lib import paths, vault
    from lib.vault import _b64d, _b64e, _canonical_aad, _derive, _LEGACY_AAD

    def _key_of(header: dict) -> AESGCM:
        kdf = header["kdf"]
        key = _derive(master, _b64d(kdf["salt"]),
                      int(kdf["n"]), int(kdf["r"]), int(kdf["p"]))
        return AESGCM(key)

    print("\n== S7 新库用 canonical AAD 绑定 header ==")
    reset()
    master = "p1-master-aad-9f21"
    v = vault.Vault.create(paths.vault_file(), master)
    v.set("prod", "vault-pw-aad-1")
    header = jsonlib.loads(paths.vault_file().read_text(encoding="utf-8"))
    err = None
    try:
        _key_of(header).decrypt(_b64d(header["nonce"]),
                                _b64d(header["ct"]), _LEGACY_AAD)
    except InvalidTag:
        err = True
    check("新文件不再被旧常量 AAD 解开", err is not None)
    expect = jsonlib.dumps({"v": header["v"], "kdf": header["kdf"]},
                           sort_keys=True, separators=(",", ":")).encode()
    check("AAD 是 header 静态部分的 canonical JSON",
          _canonical_aad(header) == expect, str(_canonical_aad(header)))

    print("\n== S7 旧常量 AAD 的文件仍能打开，写盘时自动升级 ==")
    plain = jsonlib.dumps({"legacy": "legacy-pw-value"}, ensure_ascii=False).encode()
    nonce = bytes(range(12))
    ct = _key_of(header).encrypt(nonce, plain, _LEGACY_AAD)
    legacy_header = {"v": vault.VAULT_VERSION, "kdf": header["kdf"],
                     "nonce": _b64e(nonce), "ct": _b64e(ct)}
    paths.vault_file().write_text(jsonlib.dumps(legacy_header), encoding="utf-8")
    v2 = vault.Vault.open(paths.vault_file(), master=master, allow_keyring=False)
    check("旧文件解开（legacy 回落）", v2.get("legacy") == "legacy-pw-value")
    v2.set("prod", "vault-pw-aad-2")  # 触发升级落盘
    header2 = jsonlib.loads(paths.vault_file().read_text(encoding="utf-8"))
    err = None
    try:
        _key_of(header2).decrypt(_b64d(header2["nonce"]),
                                 _b64d(header2["ct"]), _LEGACY_AAD)
    except InvalidTag:
        err = True
    check("升级后旧 AAD 解不开", err is not None)
    upgraded = _key_of(header2).decrypt(_b64d(header2["nonce"]),
                                        _b64d(header2["ct"]),
                                        _canonical_aad(header2))
    check("升级后 canonical AAD 解得开",
          jsonlib.loads(upgraded.decode())["prod"] == "vault-pw-aad-2")

    print("\n== S7 主密码错误依旧被拒 ==")
    err = None
    try:
        vault.Vault.open(paths.vault_file(), master="wrong-master-pass-1",
                         allow_keyring=False)
    except vault.VaultError:
        err = True
    check("错误主密码 → VaultError", err is not None)

    print("\n== S8 临时文件唯一命名，并发写不互踩 ==")
    import inspect

    src = inspect.getsource(vault.Vault._flush)
    check("落盘走 atomic_write_text（mkstemp 唯一临时名）",
          "atomic_write_text" in src, src[:120])
    check("不再用固定名字 vault.tmp", 'with_suffix(".tmp")' not in src)

    v3 = vault.Vault.open(paths.vault_file(), master=master, allow_keyring=False)

    # 并发裸写（无锁）唯一的保证是「文件始终完好」：唯一临时名下，每次
    # os.replace 落盘的都是一个完整快照，谁最后完成谁获胜，不会出现混写
    from lib.lock import data_lock

    bare = [threading.Thread(target=lambda i=i: v3.set(f"bare-{i}", f"bare-{i}"))
            for i in range(6)]
    for t in bare:
        t.start()
    for t in bare:
        t.join()
    v4 = vault.Vault.open(paths.vault_file(), master=master, allow_keyring=False)
    check("并发裸写后文件完好可解", v4 is not None and bool(v4.names()),
          str(v4.names()))
    check("落盘的是完整快照（无混写损坏）",
          all(str(k).split("-")[0] in ("bare", "thread", "legacy", "prod")
              for k in v4.names()), str(v4.names()))

    # 生产路径经 data_lock 串行化：一条不丢
    def writer(i: int) -> None:
        with data_lock(timeout=30):
            v3.set(f"thread-{i}", f"thread-value-{i}")

    locked = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in locked:
        t.start()
    for t in locked:
        t.join()
    v5 = vault.Vault.open(paths.vault_file(), master=master, allow_keyring=False)
    check("走 data_lock 的并发写一条不丢",
          all(v5.get(f"thread-{i}") == f"thread-value-{i}" for i in range(6)),
          str(v5.names()))
    leftovers = [p.name for p in paths.data_dir().glob("*.tmp")]
    check("没有残留临时文件", not leftovers, str(leftovers))


def test_gpg_backend() -> None:
    """P1：无钥匙串（headless）环境的主密码 gpg 后端。"""
    import os

    from lib import paths, vault

    print("\n== gpg 指纹解析（-with-colons）==")
    sample = (
        "pub:u:3072:1:ABCD:1700000000:::u:::esc:::ed:::3072:\n"
        "fpr:::::::::AAAABBBBCCCC1111222233334444555566667777:\n"
        "uid:u::::1700000000::ABCDEF12::user (comment) <u@e>:\n"
        "ssb:u:3072:1:EFGH:1700000000::::::e:::\n"
        "fpr:::::::::DDDD0000EEEE1111FFFF22223333444455556666:\n"
        "sec:u:3072:1:IJKL:1700000005:::u:::sesc:::ed:::3072:\n"
        "fpr:::::::::9999888877776666555544443333222211110000:\n"
    )
    check("第一把 sec 的指纹胜出（pub/ssb 的不算）",
          vault._first_secret_fpr(sample) == "9999888877776666555544443333222211110000",
          str(vault._first_secret_fpr(sample)))
    check("只有公钥块返回 None",
          vault._first_secret_fpr("pub:u:3072:1:ABCD:0:\nfpr:::::::::XYZ:\n") is None)
    check("空输出返回 None", vault._first_secret_fpr("") is None)

    print("\n== Vault.open 在无钥匙串时经 gpg 后端解开 ==")
    reset()
    master = "p1-master-gpg-backend"
    vault.Vault.create(paths.vault_file(), master)
    os.environ.pop("ASSHCLI_MASTER", None)
    real_kl = vault.keyring_load
    real_gl = vault.gpg_load_master
    vault.keyring_load = lambda *a, **k: None  # 模拟无 Secret Service
    vault.gpg_load_master = lambda *a, **k: master
    try:
        v = vault.open_vault()
        check("gpg 后端提供的主密码解开了库", v is not None and v.names() == [])
    finally:
        vault.keyring_load = real_kl
        vault.gpg_load_master = real_gl

    print("\n== save_master_auto 的优先级 ==")
    real_ks = vault.keyring_save
    real_gs = vault.gpg_save_master
    try:
        vault.keyring_save = lambda *a, **k: True
        check("钥匙串成功 → keyring",
              vault.save_master_auto(paths.vault_file(), "m-12345678") == "keyring")
        vault.keyring_save = lambda *a, **k: False
        vault.gpg_save_master = lambda *a, **k: True
        check("钥匙串失败 → gpg",
              vault.save_master_auto(paths.vault_file(), "m-12345678") == "gpg")
        vault.gpg_save_master = lambda *a, **k: False
        check("全失败 → None",
              vault.save_master_auto(paths.vault_file(), "m-12345678") is None)
    finally:
        vault.keyring_save = real_ks
        vault.gpg_save_master = real_gs

    print("\n== 真 gpg round-trip（无 gpg / 无私钥自动跳过）==")
    if not vault.gpg_available():
        print("       本机无 gpg，跳过")
        check("gpg 缺席时 save 直接 False", vault.gpg_save_master(
            paths.vault_file(), "m-12345678") is False)
    elif vault._gpg_recipient(vault._gpg()) is None:
        print("       本机没有带私钥的 gpg 键，跳过")
        check("无私钥时 save 直接 False", vault.gpg_save_master(
            paths.vault_file(), "m-12345678") is False)
    else:
        reset()
        os.environ.pop("ASSHCLI_MASTER", None)
        check("保存成功", vault.gpg_save_master(paths.vault_file(), "real-gpg-master-1"))
        f = vault.gpg_file(paths.vault_file())
        check("master.gpg 存在且非明文",
              f.exists() and b"real-gpg-master-1" not in f.read_bytes())
        check("读回来一致", vault.gpg_load_master(paths.vault_file()) == "real-gpg-master-1")
        real_kl2 = vault.keyring_load
        vault.keyring_load = lambda *a, **k: None
        try:
            v = vault.open_vault()
            check("open_vault 经 gpg 自动解锁", v is not None)
        finally:
            vault.keyring_load = real_kl2


def test_key_namespace() -> None:
    """P1：vault `key:<host>` 命名空间；协议文档写明过滤约定。"""
    from lib import paths, vault

    print("\n== vault key:<host> 命名空间 ==")
    reset()
    v = vault.Vault.create(paths.vault_file(), "p1-master-keyns-1")
    v.set("prod", "host-password-1")
    v.set("key:prod", "private-key-material-1")
    check("names() 过滤 key: 条目",
          v.names() == ["prod"], str(v.names()))
    check("key_names() 只含 key: 条目", v.key_names() == ["key:prod"],
          str(v.key_names()))
    check("get('key:prod') 仍可取", v.get("key:prod") == "private-key-material-1")
    check("get('prod') 不受影响", v.get("prod") == "host-password-1")

    print("\n== protocol.md 写入约定 ==")
    proto = (Path(__file__).resolve().parent.parent
             / "references" / "protocol.md").read_text(encoding="utf-8")
    check("约定 key:<host> 命名空间", "key:<host>" in proto)
    check("说明 names() 过滤", "过滤" in proto and "key:" in proto)


def main() -> int:
    test_vault_aad_and_tmp()
    test_gpg_backend()
    test_key_namespace()
    return finish()


if __name__ == "__main__":
    sys.exit(main())
