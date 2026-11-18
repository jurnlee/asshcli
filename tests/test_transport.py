"""lib.transport：连接、主机指纹、sudo 通道与文件传输。

对应 docs/DESIGN.md 的四条线（每段一个）：
- P1：--sudo 与 --stdin 同用时，sudo 密码必须先于用户数据写入
- P0：known_hosts 损坏不再静默降级成「谁都信」——fail-closed、
  结构检查、按主机合并、并发写不损坏
- P1 S6：首连 TOFU 指纹进状态行（ASSHCLI_HOSTKEY: SHA256）
- P0：put / get 的本地路径、大小与超时约束

运行：
    python tests/test_transport.py
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common  # noqa: E402  先注入路径、设环境，再 import scripts 下的模块

TMP = _common.init_data_dir("asshcli-transport-")

from _common import SCRIPTS, check, finish, reset  # noqa: E402


# --------------------------------------------------------------------------
# --sudo 与 --stdin 同用时，sudo 密码必须排在用户数据之前
# --------------------------------------------------------------------------


class _FakeStdin:
    """记录 exec 通道 stdin 上写进来的内容。

    paramiko 的 ChannelFile 上 `.channel` 是属性不是方法，
    `stdin.channel.shutdown_write()` 走的是属性访问。
    """

    def __init__(self) -> None:
        self.written: list[str] = []
        self.shutdown = False
        outer = self

        class _Chan:
            @staticmethod
            def shutdown_write() -> None:
                outer.shutdown = True

        self.channel = _Chan()

    def write(self, data: str) -> int:
        self.written.append(data)
        return len(data)


class _FakeStream:
    def __init__(self, data: bytes = b"") -> None:
        self._data = data
        self.channel = _FakeChannel()

    def read(self) -> bytes:
        return self._data


class _FakeChannel:
    def recv_exit_status(self) -> int:
        return 0

    def settimeout(self, t) -> None:
        pass


class _FakeExecSsh:
    """最小 SSHClient：exec_command 记录命令与喂给 stdin 的数据。"""

    last: "_FakeExecSsh | None" = None

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.stdin = _FakeStdin()
        _FakeExecSsh.last = self

    def exec_command(self, command, timeout=None, get_pty=False):
        self.commands.append(command)
        self.stdin.written.clear()
        return (self.stdin, _FakeStream(b"out\n"), _FakeStream(b""))


def test_sudo_with_stdin() -> None:
    """P1：`run --sudo --stdin` 曾把用户数据当 sudo 密码认证，命令必挂。"""
    from lib.redact import Redactor
    from lib.store import Host, Settings
    from lib.transport import Client

    print("\n== sudo 密码先于 --stdin 数据 ==")
    client = Client(Host(name="box", host="127.0.0.1", user="root"),
                    "sudo-password-1", Settings(), Redactor())
    client._ssh = _FakeExecSsh()  # noqa: SLF001 - 直接注入，不真连

    client.run("cat > /tmp/x", stdin_data="user-data-line\n",
               timeout=10, use_sudo=True)

    ssh = _FakeExecSsh.last
    check("命令被包成 sudo -S", ssh.commands[0].startswith("sudo -S"),
          ssh.commands[0])
    written = "".join(ssh.stdin.written)
    lines = written.split("\n")
    check("第一行是 sudo 密码", lines[0] == "sudo-password-1", repr(lines[0]))
    check("用户数据在密码之后", "user-data-line" in written
          and written.index("sudo-password-1") < written.index("user-data-line"),
          repr(written))
    check("写完关写端（远端才能读到 EOF）", ssh.stdin.shutdown is True)

    print("\n== 无 --stdin 时只有密码一行 ==")
    client.run("systemctl restart nginx", timeout=10, use_sudo=True)
    written = "".join(_FakeExecSsh.last.stdin.written)
    check("只喂密码", written == "sudo-password-1\n", repr(written))


# --------------------------------------------------------------------------
# known_hosts：损坏指纹库 fail-closed、结构检查、按主机合并、并发写
# --------------------------------------------------------------------------


def test_known_hosts() -> None:
    """P0：损坏的 known_hosts 被吞掉 → 空库 → 任何主机密钥都接受。"""

    import subprocess

    import paramiko

    from lib import transport
    from lib.store import Host, Settings

    print("\n== 损坏的指纹库必须报错，不能静默当空库 ==")
    reset()
    from lib import paths

    known = paths.known_hosts_file()
    known.write_text("这不是指纹库 随便写的内容\n", encoding="utf-8")
    client = transport.Client(
        Host(name="bad", host="127.0.0.1", user="root", port=1),
        "pw", Settings(),
    )
    err: str | None = None
    try:
        client.connect()
    except transport.TransportError as exc:
        err = str(exc)
    check("损坏的指纹库让连接中止", err is not None, str(err))
    check("提示指纹库损坏", err is not None and "指纹库损坏" in err, str(err))
    check("说明没有发出凭据",
          err is not None and ("凭据" in err or "没有向该主机发送" in err), str(err))
    check("报错发生在建连之前（不是网络错误）",
          err is not None and "网络错误" not in err, str(err))

    print("\n== 结构检查能认出截断的行 ==")
    good_key = paramiko.RSAKey.generate(1024)
    line = f"1.1.1.1 {good_key.get_name()} {good_key.get_base64()}"
    check("完整行通过检查", transport.check_known_hosts_text(line + "\n") is None)
    check("注释与空行通过", transport.check_known_hosts_text("# c\n\n" + line) is None)
    truncated = transport.check_known_hosts_text(line[:40] + "\n")
    check("截断行被识别", truncated is not None, str(truncated))
    short = transport.check_known_hosts_text("1.1.1.1 ssh-rsa\n")
    check("缺 base64 的行被识别", short is not None, str(short))

    print("\n== 只替换该主机的条目，其它行原样保留 ==")
    old_key = paramiko.RSAKey.generate(1024)
    new_key = paramiko.RSAKey.generate(1024)
    original = (
        "# 手写注释\n"
        f"1.1.1.1 {old_key.get_name()} {old_key.get_base64()}\n"
        f"2.2.2.2 {good_key.get_name()} {good_key.get_base64()}\n"
        "\n"
        "# 尾巴\n"
    )
    merged = transport.merge_known_hosts(original, [("1.1.1.1", new_key)])
    check("注释保留", "# 手写注释" in merged and "# 尾巴" in merged, merged)
    check("其它主机保留", "2.2.2.2" in merged, merged)
    check("旧指纹已替换", old_key.get_base64() not in merged, merged)
    check("新指纹已写入", new_key.get_base64() in merged, merged)
    check("同一主机只剩一行",
          sum(1 for ln in merged.splitlines() if ln.startswith("1.1.1.1 ")) == 1,
          merged)
    check("合并结果结构合法", transport.check_known_hosts_text(merged) is None, merged)

    print("\n== 并发写下不会写坏指纹库 ==")
    known.unlink(missing_ok=True)
    worker = r"""
import sys, paramiko
sys.path.insert(0, sys.argv[1])
from lib import transport
key = paramiko.RSAKey.generate(1024)
print("OK" if transport.remember_host_keys([(sys.argv[2], key)]) else "FAIL")
"""
    hosts = [f"10.9.0.{i}" for i in range(6)]
    procs = [
        subprocess.Popen([sys.executable, "-c", worker, str(SCRIPTS), h],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8")
        for h in hosts
    ]
    results = [p.communicate(timeout=120)[0].strip() for p in procs]
    check("六个并发写入都成功", results == ["OK"] * len(hosts), str(results))

    text = known.read_text(encoding="utf-8")
    check("文件没被写坏（结构检查通过）",
          transport.check_known_hosts_text(text) is None, text)
    missing = [h for h in hosts if not any(
        ln.startswith(h + " ") for ln in text.splitlines())]
    check("六条指纹一条都没丢", not missing, f"缺失 {missing}\n{text}")

    keys = paramiko.HostKeys()
    keys.load(str(known))
    check("paramiko 能完整载入", len(keys) == len(hosts), str(list(keys)))
    check("载入后不再触发 missing_host_key",
          all(h in keys for h in hosts), str(list(keys)))


# --------------------------------------------------------------------------
# TOFU：首连指纹进状态行
# --------------------------------------------------------------------------


def test_tofu_hostkey_line() -> None:
    """P1 S6：首连输出 ASSHCLI_HOSTKEY: SHA256，记录失败也先给指纹。"""
    import base64
    import contextlib
    import hashlib
    import io

    import paramiko

    from lib.store import Host, Settings
    from lib.transport import Client, _hostkey_fingerprint

    print("\n== S6 首连输出 ASSHCLI_HOSTKEY: SHA256 ==")
    reset()
    from lib import paths as paths_mod

    key = paramiko.RSAKey.generate(1024)
    client = Client(Host(name="box", host="1.2.3.4", user="root"), "pw",
                    Settings())
    client._tofu.new_keys = [("1.2.3.4", key)]  # noqa: SLF001
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        client._save_new_keys()
    text = err.getvalue()
    expect = base64.b64encode(hashlib.sha256(key.asbytes()).digest()
                              ).decode().rstrip("=")
    check("状态行存在", "ASSHCLI_HOSTKEY: SHA256:" in text, text)
    check("指纹与 ssh-keygen 算法一致", f"SHA256:{expect}" in text, text)
    check("标注了首次连接的主机名", "首次连接 1.2.3.4" in text, text)
    check("记录成功后 new_keys 清空", not client._tofu.new_keys)
    check("指纹可复现（_hostkey_fingerprint）",
          _hostkey_fingerprint(key) == expect)

    print("\n== S6 记录失败时既给指纹也给警告 ==")
    client2 = Client(Host(name="box", host="1.2.3.4", user="root"), "pw",
                     Settings())
    client2._tofu.new_keys = [("1.2.3.4", key)]  # noqa: SLF001
    orig_kh = paths_mod.known_hosts_file
    # known_hosts 指到一个「必写不进」的位置：TMP 本身是目录，
    # os.replace 到目录上必然失败
    paths_mod.known_hosts_file = lambda: TMP
    try:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            client2._save_new_keys()
    finally:
        paths_mod.known_hosts_file = orig_kh
    text = err.getvalue()
    check("仍然输出指纹行", "ASSHCLI_HOSTKEY: SHA256:" in text, text)
    check("输出没记录上的警告", "没能记录主机指纹" in text, text)


# --------------------------------------------------------------------------
# put / get：大小与超时约束
# --------------------------------------------------------------------------


class _FakeRemoteFile:
    """最小可用的 SFTP 文件对象：够 transport 的分块读写用。"""

    def __init__(self, buffer, writer=None):
        self._buf = buffer
        self._writer = writer

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        return self._buf.read(size)

    def write(self, data):
        if self._writer is not None:
            self._writer.append(data)
        return len(data)

    def close(self):
        pass


class _FakeSftp:
    def __init__(self, data: bytes = b"", stat_size: int | None = None):
        self.data = data
        self.stat_size = len(data) if stat_size is None else stat_size
        self.written: list[bytes] = []
        self.removed: list[str] = []

    def stat(self, remote):
        import types

        return types.SimpleNamespace(st_size=self.stat_size)

    def open(self, path, mode):
        if "r" in mode:
            return _FakeRemoteFile(io.BytesIO(self.data))
        return _FakeRemoteFile(io.BytesIO(), self.written)

    def get_channel(self):
        return None

    def remove(self, remote):
        self.removed.append(remote)

    def close(self):
        pass


class _FakeSsh:
    def __init__(self, sftp: _FakeSftp):
        self._sftp = sftp

    def open_sftp(self) -> _FakeSftp:
        return self._sftp

    def close(self):
        pass


def test_transfer_limits() -> None:
    """P0：put/get 两端都要有大小与超时上限，超限要报错而不是挂死。"""

    from lib import transport
    from lib.store import Host, Settings

    def attach(client, sftp):
        """换掉底层连接（Client 会缓存 SFTP 客户端，一起重置）。"""
        client._ssh = _FakeSsh(sftp)  # noqa: SLF001
        client._sftp = None  # noqa: SLF001

    print("\n== get 的大小上限 ==")
    reset()
    host = Host(name="box", host="127.0.0.1", user="root")
    client = transport.Client(host, "pw", Settings(transfer_max_bytes=1024))
    attach(client, _FakeSftp(b"x" * 4096))

    target = TMP / "big.bin"
    err = None
    try:
        client.get_file("/remote/big.bin", str(target))
    except transport.TransportError as exc:
        err = str(exc)
    check("超过上限直接拒绝", err is not None and "上限" in err, str(err))
    check("提示怎么放宽", err is not None and "--max-bytes" in err, str(err))
    check("没有留下半截文件", not target.exists(), str(target))

    print("\n== 传输中途超限也不留半截文件 ==")
    attach(client, _FakeSftp(b"y" * 4096, stat_size=10))  # stat 撒谎：实际更大
    target2 = TMP / "sneaky.bin"
    err = None
    try:
        client.get_file("/remote/sneaky.bin", str(target2))
    except transport.TransportError as exc:
        err = str(exc)
    check("边传边算大小，超了立刻中止",
          err is not None and "上限" in err, str(err))
    check("半截文件被删掉", not target2.exists(), str(target2))

    print("\n== get 正常路径 ==")
    payload = b"hello world\n" * 10
    attach(client, _FakeSftp(payload))
    ok_target = TMP / "ok.bin"
    written = client.get_file("/remote/ok.bin", str(ok_target), max_bytes=0)
    check("返回写入字节数", written == len(payload), str(written))
    check("内容落盘", ok_target.read_bytes() == payload)

    print("\n== put 的大小上限 ==")
    big = TMP / "big-upload.bin"
    big.write_bytes(b"z" * 4096)
    attach(client, _FakeSftp())
    err = None
    try:
        client.put_file(str(big), "/tmp/big-upload.bin")
    except transport.TransportError as exc:
        err = str(exc)
    check("超过上限直接拒绝", err is not None and "上限" in err, str(err))

    small = TMP / "small.bin"
    small.write_bytes(b"ping\n")
    fake3 = _FakeSftp()
    attach(client, fake3)
    sent = client.put_file(str(small), "/tmp/small.txt")
    check("正常上传返回字节数", sent == small.stat().st_size, str(sent))
    check("内容真的发出去了", b"".join(fake3.written) == b"ping\n",
          str(fake3.written))

    print("\n== 超时与 sftp() 封装 ==")
    err = None
    try:
        transport._check_deadline(time.monotonic() - 1, "下载", "/x")
    except transport.TransportError as exc:
        err = str(exc)
    check("超时判定会抛 TransportError", err is not None and "超时" in err, str(err))

    fake4 = _FakeSftp()
    attach(client, fake4)
    check("Client.sftp() 返回 SFTP 客户端", client.sftp() is fake4)
    check("Client.sftp() 复用同一个连接", client.sftp() is fake4)

    import inspect

    src = inspect.getsource(__import__("asshcli")._transfer)
    check("CLI 不再摸 client._ssh", "_ssh" not in src, src[:200])
    check("CLI 走 Client.sftp/put_file/get_file",
          "put_file" in src and "get_file" in src)


def main() -> int:
    test_sudo_with_stdin()
    test_known_hosts()
    test_tofu_hostkey_line()
    test_transfer_limits()
    return finish()


if __name__ == "__main__":
    sys.exit(main())
