"""lib.audit：审计卫生——秘密不落盘、展示分流、轮转与字段。

对应 docs/DESIGN.md 的四条线（每段一个）：
- P0 vault.open 审计不再落 argv 里的秘密；caller 字段删除
- P0 --stdin 只接受 `-`，取值改走 --stdin / --stdin-file（凭据不进 argv）
- P1 audit 展示层默认过滤 vault.open（--all 看全量）；tail 倒读跨块；
  轮转；audit 命令的 --all 与 -n 校验
- P1 审计记录带 auth 字段（不传就没有）

运行：
    python tests/test_audit.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common  # noqa: E402  先注入路径、设环境，再 import scripts 下的模块

TMP = _common.init_data_dir("asshcli-audit-")

from _common import check, finish, reset, run_cli  # noqa: E402


def test_audit_no_secret_leak() -> None:
    """P0：vault.open 把整串 argv 落盘，等于把「随手写在命令里的秘密」也落盘。"""

    print("\n== vault.open 不再落 argv 里的秘密 ==")
    reset()
    from lib import audit, paths, vault

    master = "p0-master-passphrase-7d1"
    vault_secret = "VaultedPw!2026"
    v = vault.Vault.create(paths.vault_file(), master)
    v.set("prod", vault_secret)
    os.environ["ASSHCLI_MASTER"] = master

    secret = "S3cretValue123"
    saved = sys.argv[:]
    # 复刻 issue 里的实测调用：密码既在 --stdin 的值里，也在远端命令里
    sys.argv = ["asshcli.py", "run", "prod", "--stdin", secret,
                "mysql", "-u", "root", "-p" + secret]
    try:
        vault.open_vault()
    finally:
        sys.argv = saved

    text = paths.audit_file().read_text(encoding="utf-8")
    check("argv 里的临时秘密不落盘", secret not in text, text[-400:])
    check("库里已有的密码也不落盘", vault_secret not in text, text[-400:])
    check("主密码不落盘", master not in text, text[-400:])

    # vault.open 在展示层被过滤，验「文件里有没有」用全保真视图
    opens = [r for r in audit.tail(10, include_internal=True)
             if r.get("action") == "vault.open"]
    last = opens[-1] if opens else {}
    command = str(last.get("command", ""))
    check("留下了 pid", f"pid={os.getpid()}" in command, command)
    check("留下了工作目录", "cwd=" in command, command)
    check("留下了可执行文件", "exe=" in command, command)
    check("只留前两个 argv 参数", "mysql" not in command, command)

    print("\n== caller 字段删除 ==")
    os.environ["ASSHCLI_CALLER"] = "随便填的-agent"
    audit.write("test", host="h")
    os.environ.pop("ASSHCLI_CALLER", None)
    recs = audit.tail(10)
    check("审计记录里没有 caller", all("caller" not in r for r in recs), str(recs[-1]))
    check("自填的调用方名没被写进去",
          '"随便填的-agent"' not in paths.audit_file().read_text(encoding="utf-8"))


def test_stdin_flag_gate() -> None:
    """P0：--stdin <值> 直接报错而不是默默把凭据送进 argv / 审计。"""
    print("\n== --stdin 只接受 - ==")
    import asshcli as cli

    value_flags = {"timeout", "stdin", "stdin-file"}
    opts, pos = cli._parse(["hk", "--stdin", "-", "mysql"], value_flags)
    check("--stdin - 解析成值", opts.get("stdin") == "-", str(opts))
    check("命令不受影响", pos == ["hk", "mysql"], str(pos))

    opts, _ = cli._parse(["hk", "--stdin-file", "a.sql", "mysql"], value_flags)
    check("--stdin-file 解析成路径", opts.get("stdin-file") == "a.sql", str(opts))

    def usage_error(opts_dict: dict) -> str | None:
        try:
            cli._stdin_data(opts_dict)
        except cli.UsageError as exc:
            return str(exc)
        return None

    secret = "S3cretValue123"
    detail = usage_error({"stdin": secret})
    check("--stdin <值> 直接报错而不是默默放行", detail is not None, str(detail))
    check("错误里指出会进审计日志", detail is not None and "审计" in detail, str(detail))

    stdin_file = TMP / "payload.sql"
    stdin_file.write_text("SELECT 1;\n", encoding="utf-8")
    check("--stdin-file 读得到内容",
          cli._stdin_data({"stdin-file": str(stdin_file)}) == "SELECT 1;\n")
    detail = usage_error({"stdin-file": str(TMP / "missing.sql")})
    check("--stdin-file 文件不存在时报错", detail is not None, str(detail))


def test_audit_streaming() -> None:
    """P1：审计分流——展示层默认滤 vault.open；tail 倒读；轮转。"""
    from lib import audit, paths

    print("\n== 展示层默认过滤 vault.open ==")
    reset()
    for i in range(5):
        audit.write("run", host="h", command=f"cmd-{i}")
    for i in range(3):
        audit.write("vault.open", command=f"pid={i} exe=python cwd=/x")
    audit.write("test", host="h2")

    biz = audit.tail(50)
    check("默认视图不含 vault.open",
          all(r.get("action") != "vault.open" for r in biz), str(biz))
    check("业务记录一条不少", len(biz) == 6, str(len(biz)))
    check("顺序保持（时间正序）",
          [r.get("command") for r in biz] == [f"cmd-{i}" for i in range(5)] + [None],
          str([r.get("command") for r in biz]))
    everything = audit.tail(50, include_internal=True)
    check("--all 看全量（含 3 条 vault.open）",
          sum(1 for r in everything if r.get("action") == "vault.open") == 3,
          str(len(everything)))

    print("\n== tail 倒读跨块正确 ==")
    reset()
    for i in range(2000):  # 约 180 KiB，跨 3 个 64 KiB 块
        audit.write("run", host="h", command=f"bulk-{i}")
    recs = audit.tail(5)
    check("跨块不丢行、只取 5 条", len(recs) == 5, str(len(recs)))
    check("正是最后 5 条",
          [r.get("command") for r in recs] == [f"bulk-{1995 + i}" for i in range(5)],
          str([r.get("command") for r in recs]))
    recs = audit.tail(2500, include_internal=True)
    check("大日志全量仍可读", len(recs) == 2000, str(len(recs)))

    print("\n== 轮转 ==")
    reset()
    orig = audit.ROTATE_BYTES
    audit.ROTATE_BYTES = 1024
    try:
        for i in range(60):
            audit.write("run", host="h", command=f"rotate-{i}-" + "x" * 40)
    finally:
        audit.ROTATE_BYTES = orig
    rotated = Path(str(paths.audit_file()) + ".1")
    check("轮转出 audit.log.1", rotated.exists(), str(rotated))
    check("当前文件回到上限以内", paths.audit_file().stat().st_size < 1024 * 2)
    recs = audit.tail(200)
    check("轮转后 tail 只读当前代且不崩",
          bool(recs) and all(str(r.get("command", "")).startswith("rotate-")
                             for r in recs), str(len(recs)))

    print("\n== audit 命令的 --all 与 -n 校验 ==")
    reset()
    audit.write("run", host="h", command="visible-cmd")
    audit.write("vault.open", command="pid=1 exe=x cwd=/y")
    code, out, err = run_cli(["audit", "-n", "20"])
    check("默认视图不显示 vault.open 记录", "pid=1 exe=x" not in out, out)
    check("提示默认隐藏与 --all", "--all" in out, out)
    check("业务记录可见", "visible-cmd" in out, out)
    code, out, err = run_cli(["audit", "-n", "20", "--all"])
    check("--all 显示 vault.open", "pid=1 exe=x" in out, out)
    code, out, err = run_cli(["audit", "-n", "abc"])
    check("-n 非数字报参数错误（2）", code == 2, f"{code} {err}")


def test_audit_auth_field() -> None:
    """P1：审计记录带 auth 字段（password / key-file），不传就没有。"""
    from lib import audit

    print("\n== 审计 auth 字段 ==")
    reset()
    audit.write("run", host="h", command="x", auth="password")
    rec = audit.tail(1)[0]
    check("auth=password 落盘", rec.get("auth") == "password", str(rec))
    audit.write("transfer.put", host="h2", command="y", auth="key-file")
    rec = audit.tail(1)[0]
    check("auth=key-file 落盘", rec.get("auth") == "key-file", str(rec))
    audit.write("test", host="h3", command="z")
    rec = audit.tail(1)[0]
    check("不传 auth 就没有该字段", "auth" not in rec, str(rec))


def main() -> int:
    test_audit_no_secret_leak()
    test_stdin_flag_gate()
    test_audit_streaming()
    test_audit_auth_field()
    return finish()


if __name__ == "__main__":
    sys.exit(main())
