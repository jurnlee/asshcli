"""lib.redact：输出脱敏回归。

对应 docs/DESIGN.md P1（每段一个）：
- `pwd =` 形态的正常输出不再误伤；password / passwd 与已知值仍被替换
- I9：Redactor 按值替换的长度阈值是 8；cmd_run 用 vault 全量值
  （含项目共享密码）构造 Redactor，不再只装本机密码

运行：
    python tests/test_redact.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common  # noqa: E402  先注入路径、设环境，再 import scripts 下的模块

TMP = _common.init_data_dir("asshcli-redact-")

from _common import check, finish  # noqa: E402


def test_redact_pwd() -> None:
    """P1：`pwd =` 是 shell 内建/容器输出的正常形态，不得误伤。"""
    from lib.redact import Redactor

    print("\n== `pwd =` 是正常输出，不脱敏 ==")
    r = Redactor()
    out = r.clean("pwd = /home/deploy")
    check("pwd = /home/deploy 保留", out == "pwd = /home/deploy", out)
    out = r.clean("workdir=/app pwd=/app")
    check("容器输出里的 pwd=/app 保留", out == "workdir=/app pwd=/app", out)
    out = r.clean("PWD=/var/www")
    check("大写 PWD 保留", out == "PWD=/var/www", out)

    print("\n== password / passwd 仍被脱敏 ==")
    out = r.clean("password=abc123")
    check("password=abc123 被替换", "[REDACTED]" in out and "abc123" not in out, out)
    out = r.clean("passwd: hunter2")
    check("passwd: hunter2 被替换", "[REDACTED]" in out and "hunter2" not in out, out)
    out = r.clean("--password hunter2")
    check("--password hunter2 被替换", "[REDACTED]" in out and "hunter2" not in out, out)

    print("\n== 已知密码仍然按值替换（不管叫什么名字）==")
    r2 = Redactor(["pw-value-9f31"])
    out = r2.clean("echo pw-value-9f31")
    check("已知值被替换", "pw-value-9f31" not in out, out)


def test_redaction_values() -> None:
    """P1 I9：阈值 8；run 的脱敏值来自 vault 全量 + 项目共享。"""
    from lib.redact import Redactor

    print("\n== I9 Redactor 阈值 8 ==")
    r = Redactor(["short12", "eight-char-ok"])
    check("7 字符的值不做按值替换", "short12" in r.clean("x short12 y"))
    check("8 字符的值被替换", "eight-char-ok" not in r.clean("x eight-char-ok y"))
    r2 = Redactor(["1234567"])
    check("7 字符口令不误伤正常文本", r2.clean("size=1234567") == "size=1234567")

    print("\n== I9 cmd_run 用 vault 全量值构造 Redactor（含共享密码） ==")
    import inspect

    import asshcli as cli

    src = inspect.getsource(cli.cmd_run)
    check("run 的 redactor 来自 _redaction_values（vault 全量 + 项目共享）",
          "_redaction_values(vault_obj)" in src
          and "redact_values.append(password)" in src, src[:80])
    check("不再只装本机密码", "Redactor([password]" not in src)


def main() -> int:
    test_redact_pwd()
    test_redaction_values()
    return finish()


if __name__ == "__main__":
    sys.exit(main())
