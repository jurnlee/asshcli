"""manage 分层：子命令分发 + 旧命令别名不破坏 + 主机改名。

对应 docs/DESIGN.md P1：
- manage policy / hosts / audit 子命令分发；未知子命令与无参数的用法错误
- 顶层 list / audit / setpass 别名照常可用
- manage rename：清单改名 + vault 凭据随迁 + 自动重签名 + 拒绝条件

运行：
    python tests/test_manage.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common  # noqa: E402  先注入路径、设环境，再 import scripts 下的模块

TMP = _common.init_data_dir("asshcli-manage-")

from _common import check, finish, reset, run_cli  # noqa: E402


def test_manage_dispatch() -> None:
    """P1：manage 子命令分发；旧命令别名不破坏。"""
    from lib import audit, paths, store, vault

    print("\n== manage 子命令分发 ==")
    reset()
    vault.Vault.create(paths.vault_file(), "p1-master-manage-1")
    os.environ["ASSHCLI_MASTER"] = "p1-master-manage-1"
    store.upsert_host(store.Host(name="prod", host="10.1.1.1", user="deploy",
                                 port=2222, description="生产"))
    store.set_policy("prod", [r"^df -h$"], [r"^rm -rf.*"],
                     allow_get=[r"^/var/log/"])

    code, out, err = run_cli(["manage", "policy", "prod"])
    check("manage policy 查看生效策略", code == 0 and "allow" in out, out)
    check("列出规则原文", "^df -h$" in out and "^/var/log/" in out, out)
    check("提示修改方式", "hosts.toml" in out, out)

    code, out, err = run_cli(["manage", "policy", "ghost"])
    check("manage policy 未知主机 → 3", code == 3, f"{code} {err}")

    code, out, err = run_cli(["manage", "hosts"])
    check("manage hosts 列出清单", code == 0 and "prod" in out, out)

    audit.write("run", host="prod", command="df -h")
    code, out, err = run_cli(["manage", "audit", "-n", "5"])
    check("manage audit 可用", code == 0 and "df -h" in out, out)

    code, out, err = run_cli(["manage", "bogus"])
    check("未知子命令 → 2", code == 2, f"{code} {err}")

    code, out, err = run_cli(["manage"])
    check("无参数 → 用法（2）", code == 2 and "manage" in err + out, err + out)

    print("\n== 旧命令别名不破坏 ==")
    code, out, err = run_cli(["list"])
    check("list 照常", code == 0 and "prod" in out, out)
    code, out, err = run_cli(["audit", "-n", "5"])
    check("顶层 audit 别名照常", code == 0 and "df -h" in out, out)
    code, out, err = run_cli(["setpass", "ghost-host"])
    check("顶层 setpass 别名照常", code == 3, str(code))
    os.environ.pop("ASSHCLI_MASTER", None)


def test_rename() -> None:
    """P1：manage rename 改名 + 凭据随迁 + 拒绝条件。"""
    from lib import audit, paths, store, vault

    print("\n== manage rename：改名与凭据随迁 ==")
    reset()
    vault.Vault.create(paths.vault_file(), "p1-master-rename")
    os.environ["ASSHCLI_MASTER"] = "p1-master-rename"
    store.upsert_host(store.Host(
        name="db1", host="10.2.2.2", user="op", port=22, description="旧名",
        settings=store.HostSettings(connect_timeout=30.0)))
    store.set_policy("db1", [r"^df -h$"], [], allow_get=[r"^/var/log/"])
    vault.write_secret("db1", "pw-db1-secret-0")
    v0 = vault.open_vault()
    v0.set("key:db1", "FAKE-KEY-PEM-db1")

    code, out, err = run_cli(["manage", "rename", "db1", "db2"])
    check("改名成功", code == 0, f"{code} {err}")
    hosts = store.load()  # 验签读取：签名失效会抛 StoreError
    check("清单已改名且签名有效", "db2" in hosts and "db1" not in hosts,
          str(sorted(hosts)))
    check("策略子段随条目迁移",
          hosts["db2"].policy.allow == [r"^df -h$"]
          and hosts["db2"].policy.allow_get == [r"^/var/log/"], "")
    check("settings 子段随条目迁移",
          hosts["db2"].settings is not None
          and hosts["db2"].settings.connect_timeout == 30.0, "")
    v = vault.open_vault()
    check("密码条目随迁", v.get("db2") == "pw-db1-secret-0", "")
    check("私钥条目随迁", v.get("key:db2") == "FAKE-KEY-PEM-db1", "")
    check("旧条目已删除", v.get("db1") is None and v.get("key:db1") is None, "")
    check("审计留痕", any(r.get("action") == "rename"
                          for r in audit.tail(5, include_internal=True)), "")

    print("\n== manage rename：用法与拒绝条件 ==")
    code, out, err = run_cli(["manage", "rename", "db2"])
    check("缺参数 → 2", code == 2, str(code))
    code, out, err = run_cli(["manage", "rename", "db2", "db2"])
    check("新旧同名 → 2", code == 2, str(code))
    code, out, err = run_cli(["manage", "rename", "ghost", "x"])
    check("未知主机 → 3", code == 3, str(code))

    store.upsert_host(store.Host(name="other", host="10.3.3.3", user="op"))
    code, out, err = run_cli(["manage", "rename", "db2", "other"])
    check("新名与用户级主机重名 → 3", code == 3, str(code))

    # 项目级：条目本身不能改，新名也不能撞项目主机
    proj = TMP / "proj" / ".asshcli"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "hosts.toml").write_text(
        '[hosts.shared1]\nhost = "10.4.4.4"\nuser = "dev"\n',
        encoding="utf-8")
    paths.set_project_override(proj / "hosts.toml")
    code, out, err = run_cli(["manage", "rename", "shared1", "x1"])
    check("项目级条目拒绝改名 → 3", code == 3 and "git" in err, f"{code} {err}")
    code, out, err = run_cli(["manage", "rename", "db2", "shared1"])
    check("新名与项目主机重名 → 3", code == 3, str(code))
    paths.set_project_override(None)

    # vault 孤儿条目：默认拒绝，--force 覆盖
    vault.write_secret("taken", "orphan-secret-12345")
    code, out, err = run_cli(["manage", "rename", "db2", "taken"])
    check("vault 孤儿条目冲突 → 3", code == 3, f"{code} {err}")
    code, out, err = run_cli(["manage", "rename", "db2", "taken", "--force"])
    check("--force 覆盖孤儿条目后成功", code == 0, f"{code} {err}")
    check("凭据迁到新名", vault.open_vault().get("taken") == "pw-db1-secret-0", "")

    # 签名失效：拒绝改名（先 reseal 的既定纪律），reseal 后恢复
    hosts_file = paths.hosts_file()
    text = hosts_file.read_text(encoding="utf-8")
    hosts_file.write_text(text.replace('user = "op"', 'user = "hacker"'),
                          encoding="utf-8")
    code, out, err = run_cli(["manage", "rename", "other", "other2"])
    check("签名失效拒绝改名 → 3", code == 3 and "reseal" in err, f"{code} {err}")
    code, out, err = run_cli(["manage", "reseal"])
    check("reseal 恢复签名", code == 0, f"{code} {err}")
    code, out, err = run_cli(["manage", "rename", "other", "other2"])
    check("reseal 后改名恢复可用", code == 0, f"{code} {err}")
    os.environ.pop("ASSHCLI_MASTER", None)


def main() -> int:
    test_manage_dispatch()
    test_rename()
    return finish()


if __name__ == "__main__":
    sys.exit(main())
