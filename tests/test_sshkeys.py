"""lib.sshkeys：密钥托管（keygen / keyimport / keyshow）与密钥优先登录。

对应 docs/DESIGN.md P2（每段一个）：
- keygen：生成、入 vault `key:<host>`、公钥可派生；重复生成被拒、--force 轮换
- keyimport：PKCS8 / PKCS1 / OpenSSH 三种封装归一化后入库；口令私钥与
  非 PEM 拒绝；省略路径时用遗留 key_file
- keyshow：只显示公钥与指纹，私钥材料不出 vault、不进输出、不进审计
- 密钥优先登录与回落留痕：真实本地 SSH 服务端验证 auth=key /
  fallback=key 的审计与 stderr 双留痕；key_file 托管旁路封堵；
  list 认证列反映 vault 状态

运行：
    python tests/test_sshkeys.py
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import paramiko

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common  # noqa: E402  先注入路径、设环境，再 import scripts 下的模块

TMP = _common.init_data_dir("asshcli-sshkeys-")

MASTER = "p2-master-passphrase-11a4"
os.environ["ASSHCLI_MASTER"] = MASTER

from _common import check, finish, reset, run_cli  # noqa: E402
from lib import audit, paths, store, vault  # noqa: E402

_USER = "tester"
_SSH_PASSWORD = "Sup3r-Secret-Pw!<&>"


def _audit_raw() -> str:
    try:
        return paths.audit_file().read_text(encoding="utf-8")
    except OSError:
        return ""


def _same_key_material(original: str, stored: str) -> bool:
    """比较导入前后的密钥材料：ed/ec 比公钥 blob，RSA 比 n/e。

    不能比 PEM 文本 —— 归一化本来就会改封装（这正是它的职责）。
    """
    import paramiko
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

    from lib import sshkeys

    def _s(b: bytes) -> bytes:
        return len(b).to_bytes(4, "big") + b

    def _mpint(n: int) -> bytes:
        b = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
        if b[0] & 0x80:
            b = b"\x00" + b
        if n == 0:
            b = b""
        return _s(b)

    try:
        key = serialization.load_pem_private_key(original.strip().encode(),
                                                 password=None)
        orig_kind = "crypto"
    except ValueError:
        # OpenSSH 容器输入：cryptography 读不了，原始侧也走 paramiko
        pk_orig = sshkeys.load_pkey(original)
        orig_kind = "paramiko"
    pk = sshkeys.load_pkey(stored)
    if orig_kind == "paramiko":
        # 输入本就是 paramiko 可读形态：归一化应原样保留（blob 逐字节一致）
        return (pk.get_name() == pk_orig.get_name()
                and pk.asbytes() == pk_orig.asbytes())
    if isinstance(key, ed25519.Ed25519PrivateKey):
        pub = key.public_key().public_bytes(serialization.Encoding.Raw,
                                            serialization.PublicFormat.Raw)
        return (pk.get_name() == "ssh-ed25519"
                and pk.asbytes() == _s(b"ssh-ed25519") + _s(pub))
    if isinstance(key, ec.EllipticCurvePrivateKey):
        curve = {"secp256r1": "nistp256"}.get(key.curve.name, key.curve.name)
        point = key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return (pk.get_name() == f"ecdsa-sha2-{curve}"
                and pk.asbytes() == (_s(f"ecdsa-sha2-{curve}".encode())
                                     + _s(curve.encode()) + _s(point)))
    if isinstance(key, rsa.RSAPrivateKey):
        nums = key.public_key().public_numbers()
        blob = _s(b"ssh-rsa") + _mpint(nums.e) + _mpint(nums.n)
        return pk.get_name() == "ssh-rsa" and pk.asbytes() == blob
    return False


def test_key_management() -> None:
    """P2：keygen / keyimport / keyshow 全流程。"""
    from lib import sshkeys

    print("\n== keygen：生成、入 vault、公钥可派生 ==")
    reset()
    _setup_sealed_host()
    code, out, err = run_cli(["manage", "keygen", "prod"])
    check("keygen 退出码 0", code == 0, f"{out}{err}")
    check("输出含公钥行", "ssh-ed25519 " in out, out)
    check("输出含指纹", "SHA256:" in out, out)

    v = vault.open_vault()
    pem = v.get("key:prod")
    check("私钥入 vault key:<host>", bool(pem))
    check("names() 不含密钥条目",
          "prod" in v.names() and "key:prod" not in v.names(), str(v.names()))
    pk = sshkeys.load_pkey(pem)
    check("paramiko 能从内存加载生成的密钥", pk.get_name() == "ssh-ed25519")
    check("私钥材料不进输出", "BEGIN" not in out, out[:200])
    check("私钥材料不进审计", "BEGIN" not in _audit_raw())
    actions = [r.get("action") for r in audit.tail(5, include_internal=True)]
    check("审计记录 key.gen", "key.gen" in actions, str(actions))

    print("\n== keygen：重复生成被拒，--force 轮换 ==")
    code, out, err = run_cli(["manage", "keygen", "prod"])
    check("已有密钥时退出码 3", code == 3, f"{out}{err}")
    old_fp = sshkeys.sha256_fingerprint(pem)
    code, out, err = run_cli(["manage", "keygen", "prod", "--force"])
    check("--force 轮换成功", code == 0, f"{out}{err}")
    pem2 = vault.open_vault().get("key:prod")
    check("轮换后私钥更换", pem2 != pem)
    check("轮换后指纹变化", sshkeys.sha256_fingerprint(pem2) != old_fp)

    print("\n== keyimport：三种封装归一化后入库 ==")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

    key_dir = Path(tempfile.mkdtemp(prefix="asshcli-keys-"))
    cases = {}
    cases["ed25519-pkcs8"] = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    cases["ecdsa-pkcs8"] = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    cases["rsa-pkcs1"] = rsa.generate_private_key(
        public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()).decode()
    # OpenSSH 容器是用户手里最常见的形态（ssh-keygen 产出），必须原样通过
    cases["ed25519-openssh"] = sshkeys.generate_ed25519_pem(comment="existing")

    for label, original in cases.items():
        src = key_dir / f"{label}.pem"
        src.write_text(original, encoding="utf-8")
        code, out, err = run_cli(["manage", "keyimport", "prod", str(src)])
        check(f"{label} 导入退出码 0", code == 0, f"{out}{err}")
        stored = vault.open_vault().get("key:prod")
        check(f"{label} 入库", bool(stored))
        try:
            sshkeys.load_pkey(stored)
            loadable = True
        except Exception:
            loadable = False
        check(f"{label} 归一化产物 paramiko 可加载", loadable)
        check(f"{label} 密钥材料一致（仅封装改变）",
              _same_key_material(original, stored))
    check("私钥材料不进审计", "BEGIN" not in _audit_raw())
    actions = [r.get("action") for r in audit.tail(10, include_internal=True)]
    check("审计记录 key.import", "key.import" in actions, str(actions))

    print("\n== keyimport：拒绝与兜底 ==")
    enc_src = key_dir / "encrypted.pem"
    enc_src.write_text(rsa.generate_private_key(
        public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.BestAvailableEncryption(b"a-passphrase-1")).decode(),
        encoding="utf-8")
    code, out, err = run_cli(["manage", "keyimport", "prod", str(enc_src)])
    check("带口令私钥被拒（退出码 3）", code == 3, f"{out}{err}")
    check("报错给出口令处理指引", "口令" in err, err)

    junk = key_dir / "junk.pem"
    junk.write_text("PuTTY-User-Key-File-3: ssh-ed25519\nnot-a-pem\n",
                    encoding="utf-8")
    code, out, err = run_cli(["manage", "keyimport", "prod", str(junk)])
    check("非 PEM 文件被拒", code == 3, f"{out}{err}")

    missing = run_cli(["manage", "keyimport", "prod", str(key_dir / "nope.pem")])
    check("文件不存在退出码 3", missing[0] == 3, f"{missing[1]}{missing[2]}")

    print("\n== keyimport：省略路径时用遗留 key_file ==")
    reset()
    _setup_sealed_host()
    legacy_src = key_dir / "legacy.pem"
    legacy_src.write_text(cases["ed25519-pkcs8"], encoding="utf-8")
    store.upsert_host(store.Host(name="prod", host="127.0.0.1", user="root",
                                 key_file=str(legacy_src)))
    code, out, err = run_cli(["manage", "keyimport", "prod"])
    check("自动使用 key_file 路径", code == 0, f"{out}{err}")
    check("输出含 legacy 路径与删除提示",
          str(legacy_src) in out and "key_file" in out, out)

    print("\n== keyshow：只显示公钥 ==")
    code, out, err = run_cli(["manage", "keyshow", "prod"])
    check("keyshow 退出码 0", code == 0, f"{out}{err}")
    check("输出含公钥行", "ssh-ed25519 " in out, out)
    check("输出含指纹", "SHA256:" in out, out)
    check("不含私钥材料", "BEGIN" not in out, out)
    code, out, err = run_cli(["manage", "keyshow", "ghost"])
    check("未知主机退出码 3", code == 3, f"{out}{err}")


def _setup_sealed_host() -> None:
    """建库 + 建一台带签名的 prod 主机（密钥用例不关心命令策略）。"""
    v = vault.Vault.create(paths.vault_file(), MASTER)
    store.upsert_host(store.Host(name="prod", host="127.0.0.1", user="root",
                                 port=22))
    v.set("prod", "prod-password-9")
    from lib import integrity

    assert integrity.mac_key() is not None


# --------------------------------------------------------------------------
# 密钥优先登录与回落留痕（本地 SSH 服务端）
# --------------------------------------------------------------------------


class _AuthServer(paramiko.ServerInterface):
    """公钥 + 密码双认证的迷你服务端：check_auth_publickey 比对授权公钥，
    相当于完成了一次真实的签名验证。"""

    def __init__(self, authorized_pub_b64: str) -> None:
        self.authorized = authorized_pub_b64
        self.event = threading.Event()
        self.command = b""

    def get_allowed_auths(self, username):
        return "publickey,password"

    def check_auth_publickey(self, username, key):
        if username == _USER and key.get_base64() == self.authorized:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_password(self, username, password):
        if username == _USER and password == _SSH_PASSWORD:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return (paramiko.OPEN_SUCCEEDED if kind == "session"
                else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED)

    def check_channel_exec_request(self, channel, command):
        self.command = command
        self.event.set()
        return True


def _handle_auth(transport, server) -> None:
    try:
        while True:
            chan = transport.accept(20)
            if chan is None:
                return
            if not server.event.wait(20):
                chan.close()
                return
            server.event.clear()
            chan.sendall(("(ran) "
                          + server.command.decode("utf-8", "replace") + "\n").encode())
            chan.send_exit_status(0)
            chan.close()
    except Exception:
        pass
    finally:
        try:
            transport.close()
        except Exception:
            pass


def _start_auth_server(authorized_pub_b64: str) -> tuple[int, threading.Event]:
    host_key = paramiko.RSAKey.generate(2048)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(20)
    port = sock.getsockname()[1]
    stop = threading.Event()

    def loop() -> None:
        sock.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except (socket.timeout, OSError):
                continue
            t = paramiko.Transport(conn)
            t.add_server_key(host_key)
            srv = _AuthServer(authorized_pub_b64)
            try:
                t.start_server(server=srv)
            except Exception:
                t.close()
                continue
            threading.Thread(target=_handle_auth, args=(t, srv),
                             daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return port, stop


def _last_run_record() -> dict | None:
    for rec in reversed(audit.tail(10, include_internal=True)):
        if rec.get("action") == "run":
            return rec
    return None


def test_key_first_auth() -> None:
    """P2：密钥优先登录、回落密码留痕、key_file 旁路封堵。"""
    from lib import sshkeys

    print("\n== 密钥优先：合法密钥直接通过 ==")
    reset()
    pem_auth = sshkeys.generate_ed25519_pem(comment="asshcli-test")
    authorized = sshkeys.load_pkey(pem_auth).get_base64()
    pem_bad = sshkeys.generate_ed25519_pem(comment="not-installed")
    port, stop = _start_auth_server(authorized)
    try:
        v = vault.Vault.create(paths.vault_file(), MASTER)
        store.upsert_host(store.Host(name="local", host="127.0.0.1",
                                     user=_USER, port=port))
        v.set("local", _SSH_PASSWORD)
        v.set("key:local", pem_auth)

        code, out, err = run_cli(["run", "local", "echo hi"])
        check("密钥认证 run 成功", code == 0, f"{out}{err}")
        rec = _last_run_record()
        check("审计 auth=key", rec is not None and rec.get("auth") == "key",
              str(rec))
        check("无回落标记", rec is not None and "fallback" not in rec, str(rec))
        check("没有回落提示", "回落" not in err, err)

        print("\n== 密钥被拒 → 回落密码，审计与 stderr 双留痕 ==")
        v.set("key:local", pem_bad)
        code, out, err = run_cli(["run", "local", "echo hi"])
        check("回落后 run 成功", code == 0, f"{out}{err}")
        rec = _last_run_record()
        check("审计 auth=password", rec is not None
              and rec.get("auth") == "password", str(rec))
        check("审计 fallback=key", rec is not None
              and rec.get("fallback") == "key", str(rec))
        check("stderr 有回落提示", "回落" in err, err)

        print("\n== 只有被拒密钥、没有密码 → 明确失败 ==")
        v.delete("local")
        code, out, err = run_cli(["run", "local", "echo hi"])
        check("退出码 4（网络/认证失败）", code == 4, f"{out}{err}")
        check("报错说明密钥被拒", "密钥认证被服务器拒绝" in err, err)
        rec = _last_run_record()
        check("失败记录 auth=key", rec is not None and rec.get("auth") == "key",
              str(rec))

        print("\n== 仅密码主机行为不变 ==")
        v.set("local", _SSH_PASSWORD)
        v.delete("key:local")
        code, out, err = run_cli(["run", "local", "echo hi"])
        check("密码认证 run 成功", code == 0, f"{out}{err}")
        rec = _last_run_record()
        check("审计 auth=password 且无 fallback",
              rec is not None and rec.get("auth") == "password"
              and "fallback" not in rec, str(rec))

        print("\n== key_file 旁路封堵==")
        store.upsert_host(store.Host(name="legacy", host="127.0.0.1",
                                     user=_USER, port=port,
                                     key_file="~/.ssh/id_should_not_be_read"))
        code, out, err = run_cli(["run", "legacy", "echo hi"])
        check("仅 key_file 的主机退出码 3", code == 3, f"{out}{err}")
        check("报错指向 manage keyimport", "keyimport" in err, err)

        # key_file + 密码并存：密码照常可用（key_file 被忽略，不再从盘上读私钥）
        v.set("legacy", _SSH_PASSWORD)
        code, out, err = run_cli(["run", "legacy", "echo hi"])
        check("补了密码后 legacy 主机可用", code == 0, f"{out}{err}")
        rec = _last_run_record()
        check("legacy 用密码认证", rec is not None
              and rec.get("auth") == "password", str(rec))

        print("\n== list 认证列反映 vault 状态 ==")
        v.set("key:local", pem_auth)
        # 有密码的 legacy 主机：key_file 惰性，正常显示密码
        store.upsert_host(store.Host(name="legacy2", host="127.0.0.1",
                                     user=_USER, port=port,
                                     key_file="~/.ssh/another"))
        code, out, err = run_cli(["list"])
        check("list 显示密钥（local 密钥+密码）", "密钥+密码" in out, out)
        check("list 标记无凭据的 legacy2", "key_file*" in out, out)
        check("list 提示迁移", "keyimport" in out, out)
    finally:
        stop.set()


def main() -> int:
    test_key_management()
    test_key_first_auth()
    return finish()


if __name__ == "__main__":
    sys.exit(main())
