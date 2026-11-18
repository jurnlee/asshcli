"""harden --check：远端加固条件的只读探测接口。

对应 docs/DESIGN.md P1（加固接口）：
- 只读探测（ssh 版本 / shell 可用 / authorized_keys 可写）全部通过返回 0，
  有阻塞项返回 1；非 OpenSSH 识别；不带 --check 拒绝；白名单主机探测被拒
- 探测脚本经 stdin 喂入（bash -s）；写 harden.check 审计且带 auth 字段

运行：
    python tests/test_harden.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common  # noqa: E402  先注入路径、设环境，再 import scripts 下的模块

TMP = _common.init_data_dir("asshcli-harden-")

from _common import check, finish, reset, run_cli  # noqa: E402


class _ProbeClient:
    """harden 探测的替身：banner 可配，run 返回预置的探测结果。"""

    script: str | None = None
    banner_text = "SSH-2.0-OpenSSH_9.6p1 Debian-3"
    probe_line = "ASSHCLI_PROBE dir=1 dirw=1 keyw=1\n"

    def __init__(self, host, password, settings, redactor=None, key_pem=None):
        self.auth_used = "password"
        self.auth_fallback = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def banner(self) -> str:
        return _ProbeClient.banner_text

    def run(self, command, stdin_data=None, timeout=None, use_sudo=False):
        from lib.transport import Result

        _ProbeClient.script = stdin_data
        return Result(stdout=_ProbeClient.probe_line, stderr="",
                      exit_code=0, duration_ms=5)


def test_harden_check() -> None:
    """P1：harden --check 只读探测的判定与审计。"""
    import os

    from lib import audit, paths, store, vault
    from lib import transport

    real_client = transport.Client
    transport.Client = _ProbeClient
    try:
        print("\n== harden --check 只读探测（全部通过）==")
        reset()
        vault.Vault.create(paths.vault_file(), "p1-master-harden-1")
        os.environ["ASSHCLI_MASTER"] = "p1-master-harden-1"
        store.upsert_host(store.Host(name="prod", host="10.0.0.1", user="root"))

        code, out, err = run_cli(["harden", "prod", "--check"])
        check("全部通过返回 0", code == 0, f"{code} {err}")
        check("识别 OpenSSH 版本", "OpenSSH 9.6p1" in out, out)
        check("shell 可用", "bash 可用" in out, out)
        check("authorized_keys 可写", "可写或可创建" in out, out)
        check("结论可加固", "具备可加固条件" in out, out)
        check("给出 restrict 行模板",
              'restrict,command="$HOME/bin/asshcli-agent-shell"' in out, out)
        check("声明不改动", "只读" in out, out)
        check("探测脚本经 stdin 喂入（bash -s）",
              _ProbeClient.script is not None and "$HOME/.ssh" in _ProbeClient.script)
        recs = [r for r in audit.tail(5, include_internal=True)
                if r.get("action") == "harden.check"]
        check("写了 harden.check 审计", bool(recs), str(audit.tail(5)))
        check("审计带 auth=password",
              bool(recs) and recs[-1].get("auth") == "password", str(recs[-1]))

        print("\n== harden --check 存在阻塞项 ==")
        _ProbeClient.probe_line = "ASSHCLI_PROBE dir=0 dirw=0 keyw=0\n"
        code, out, err = run_cli(["harden", "prod", "--check"])
        check("有阻塞返回 1", code == 1, f"{code} {err}")
        check("结论提示阻塞", "阻塞" in out, out)

        print("\n== 非 OpenSSH 的 sshd ==")
        _ProbeClient.probe_line = "ASSHCLI_PROBE dir=1 dirw=1 keyw=1\n"
        _ProbeClient.banner_text = "SSH-2.0-Dropbear_2020.79"
        code, out, err = run_cli(["harden", "prod", "--check"])
        check("识别为非 OpenSSH", "非 OpenSSH" in out, out)
        check("返回 1", code == 1, str(code))
        _ProbeClient.banner_text = "SSH-2.0-OpenSSH_9.6p1 Debian-3"

        print("\n== harden 不带 --check 拒绝 ==")
        code, out, err = run_cli(["harden", "prod"])
        check("提示只支持 --check", code == 2 and "--check" in err, f"{code} {err}")

        print("\n== 白名单主机探测被拒 ==")
        store.set_policy("prod", ["^df -h$"], [])
        code, out, err = run_cli(["harden", "prod", "--check"])
        check("白名单下返回 1", code == 1, f"{code} {err}")
        check("提示探测命令被策略拒绝", "探测命令被策略拒绝" in err, err)
        store.set_policy("prod", [], [])
    finally:
        transport.Client = real_client


def main() -> int:
    test_harden_check()
    return finish()


if __name__ == "__main__":
    sys.exit(main())
