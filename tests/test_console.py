"""console_setup / lib.console：交互向导与无窗口环境的引导文案。

对应 docs/DESIGN.md 的三条线（每段一个）：
- P0：setpass 复用 add_host 流程时不再丢 sudo / key_file / description；
  reset_password 不重问地址端口；新主机仍按输入创建
- P1：非 Windows 首跑（无窗口可等）的指引——指向 config --here，
  不再提「窗口可能还开着」；.no-window 标记同款指引；config --here
  在无 tty 环境依旧拒绝并解释
- P1：status 的 headless 指引——说明无钥匙串、指向 gpg 后端与 README 矩阵

运行：
    python tests/test_console.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common  # noqa: E402  先注入路径、设环境，再 import scripts 下的模块

TMP = _common.init_data_dir("asshcli-console-")

from _common import check, finish, reset, run_cli  # noqa: E402


def test_setpass_keeps_fields() -> None:
    """P0：setpass 复用 add_host 流程，旧实现整段重写 Host → sudo / key_file 被清空。"""

    import contextlib
    import io
    import os

    import console_setup

    from lib import paths, store, vault

    print("\n== setpass 不丢 sudo / key_file / description ==")
    reset()
    master = "p0-master-setpass-3c9"
    os.environ["ASSHCLI_MASTER"] = master
    v = vault.Vault.create(paths.vault_file(), master)
    v.set("prod", "old-password-1")
    store.upsert_host(store.Host(
        name="prod", host="10.1.2.3", user="deploy", port=2222,
        description="生产", sudo=True,
        key_file="/home/deploy/.ssh/id_ed25519", allow_shell=True,
    ))
    store.set_policy("prod", [r"^df -h$"], [r"^rm -rf.*"])

    asked: list[str] = []
    orig = (console_setup.ask, console_setup.ask_port, console_setup.ask_secret)
    # 模拟用户「一路回车」：ask 返回默认值，密码输入新的
    console_setup.ask = lambda prompt, default="", required=True: (
        asked.append(prompt), default)[1]
    console_setup.ask_port = lambda prompt, default="22": (
        asked.append(prompt), int(default))[1]
    console_setup.ask_secret = lambda prompt, confirm=False: "new-password-2"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            console_setup.do_add_host({"host_name": "prod", "reset_password": True})
    finally:
        console_setup.ask, console_setup.ask_port, console_setup.ask_secret = orig

    h = store.get("prod")
    check("sudo 保留", h is not None and h.sudo is True, str(h))
    check("key_file 保留", h is not None and h.key_file
          == "/home/deploy/.ssh/id_ed25519", str(h))
    check("description 保留", h is not None and h.description == "生产", str(h))
    check("端口保留", h is not None and h.port == 2222, str(h))
    check("allow_shell 保留", h is not None and h.allow_shell is True, str(h))
    check("命令策略保留", h is not None and h.policy.allow == [r"^df -h$"], str(h))
    check("密码已更新", vault.open_vault().get("prod") == "new-password-2")
    check("reset_password 不再重问地址 / 端口",
          not any(("地址" in a) or ("端口" in a) for a in asked), str(asked))

    print("\n== 走完整编辑流程（config）也不丢字段 ==")
    asked.clear()
    console_setup.ask = lambda prompt, default="", required=True: (
        asked.append(prompt), default)[1]
    console_setup.ask_port = lambda prompt, default="22": (
        asked.append(prompt), int(default))[1]
    console_setup.ask_secret = lambda prompt, confirm=False: "new-password-3"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            console_setup.do_add_host({"host_name": "prod"})
    finally:
        console_setup.ask, console_setup.ask_port, console_setup.ask_secret = orig

    h = store.get("prod")
    check("一路回车后字段仍然在", h is not None and h.sudo is True
          and h.key_file == "/home/deploy/.ssh/id_ed25519"
          and h.description == "生产" and h.port == 2222, str(h))
    check("地址没被清空", h is not None and h.host == "10.1.2.3", str(h))
    check("这次确实问了地址和端口",
          any("地址" in a for a in asked) and any("端口" in a for a in asked),
          str(asked))

    print("\n== 新主机仍按输入创建 ==")
    answers = {"名称": "fresh", "地址": "9.9.9.9", "用户名": "root",
               "备注（可留空）": "新机器"}
    console_setup.ask = lambda prompt, default="", required=True: (
        answers.get(prompt, default))
    console_setup.ask_port = lambda prompt, default="22": 2200
    console_setup.ask_secret = lambda prompt, confirm=False: "fresh-password"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            console_setup.do_add_host({})
    finally:
        console_setup.ask, console_setup.ask_port, console_setup.ask_secret = orig

    fresh = store.get("fresh")
    check("新主机创建成功", fresh is not None, str(fresh))
    check("字段按输入落盘",
          fresh is not None and fresh.host == "9.9.9.9"
          and fresh.user == "root" and fresh.port == 2200
          and fresh.description == "新机器", str(fresh))
    check("新主机不会被凭空加上 sudo / 密钥",
          fresh is not None and fresh.sudo is False and fresh.key_file is None,
          str(fresh))
    check("新主机密码入 vault",
          vault.open_vault().get("fresh") == "fresh-password")
    os.environ.pop("ASSHCLI_MASTER", None)


def test_non_windows_first_run_guidance() -> None:
    """P1：没有窗口可等时，文案不再提「窗口」，指向 config --here。"""
    import io as _io
    import sys as _sys

    from lib import console, paths, protocol

    real_launch, real_available = console.launch, console.available
    try:
        print("\n== 无弹窗环境（非 nt / 非 tty）：hint 指向 config --here，不提窗口 ==")
        reset()
        # 模拟 console.available() 在非 Windows 非 tty 下的返回：False。
        # （不 patch os.name —— pathlib 在 Windows 上切到 PosixPath 会直接炸）
        console.available = lambda: False
        code, _, err = run_cli(["run", "box", "echo hi"])
        check("返回 12（契约不变）", code == protocol.EXIT_TIMEOUT, str(code))
        check("提示 config --here", "config --here" in err, err)
        check("文案不提「窗口可能还开着」", "窗口可能还开着" not in err, err)
        check("文案说明这里没有窗口", "没有可用的交互窗口" in err, err)
        check("状态行带 run 的 nonce",
              "ASSHCLI_STATUS[" in err and "ASSHCLI_INVOCATION" in err, err[:200])

        print("\n== .no-window 标记：复用 gone 同款指引 ==")
        reset()
        paths.ensure_data_dir()
        paths.no_window_marker().write_text("测试标记", encoding="utf-8")
        code, _, err = run_cli(["config"])
        check("返回 12", code == protocol.EXIT_TIMEOUT, str(code))
        check("指向自己终端里的 --here", "--here" in err, err)
        check("不再出现误导文案", "窗口可能还开着" not in err, err)
        paths.no_window_marker().unlink()

        print("\n== config --here 在无 tty 环境依旧拒绝并解释 ==")
        reset()
        # 与 test_flow 同款：伪造非 tty 的 stdin，否则真终端里会进交互向导
        real_stdin = _sys.stdin
        _sys.stdin = _io.StringIO()
        try:
            code, _, err = run_cli(["config", "--here"])
        finally:
            _sys.stdin = real_stdin
        check("返回 3（配置错误）", code == protocol.EXIT_CONFIG, str(code))
        check("提示到真实终端执行", "真实" in err or "终端" in err, err)
    finally:
        console.launch, console.available = real_launch, real_available


def test_status_headless_guidance() -> None:
    """P1：status 在无钥匙串环境给出 gpg / README 矩阵的指引。"""
    import os

    from lib import vault

    print("\n== status 的 headless 指引 ==")
    reset()
    from lib import paths

    vault.Vault.create(paths.vault_file(), "p1-master-status-guide-1")
    os.environ.pop("ASSHCLI_MASTER", None)
    real_kr = vault.keyring_available
    real_ga = vault.gpg_available
    real_kl = vault.keyring_load
    vault.keyring_load = lambda *a, **k: None  # 让解锁失败
    try:
        vault.keyring_available = lambda: False
        vault.gpg_available = lambda: True
        code, out, err = run_cli(["status"])
        check("status 仍返回 0", code == 0, f"{code} {err}")
        check("说明无钥匙串", "没有可用的系统凭据库" in out, out)
        check("指引 gpg 后端", "master.gpg" in out, out)

        vault.gpg_available = lambda: False
        code, out, err = run_cli(["status"])
        check("无 gpg 时指向 README 矩阵", "主密码后端" in out, out)
        check("如实声明 ASSHCLI_MASTER 可被 agent 读到", "printenv" in out, out)
    finally:
        vault.keyring_available = real_kr
        vault.gpg_available = real_ga
        vault.keyring_load = real_kl


def test_add_host_project_scope() -> None:
    """P2.5 补充：add_host 向导可选项目级——必问项目根（默认识别目录，
    相对路径按 payload cwd 解析）；密码同样问但可留空，留空提示补录。"""
    import contextlib
    import io
    import os

    import console_setup
    from lib import paths, store, vault

    orig = (console_setup.ask, console_setup.ask_port, console_setup.ask_secret)

    def _capped(fn, cap=10):
        n = [0]

        def wrap(*a, **k):
            n[0] += 1
            if n[0] > cap:
                raise RuntimeError("向导陷入重复提问（测试中止）")
            return fn(*a, **k)

        return wrap

    try:
        print("\n== ① 项目级：相对根目录 + 留空密码 ==")
        reset()
        proj_root = TMP / "teamproj"
        proj_root.mkdir(parents=True, exist_ok=True)
        answers = {"配置级别": "2", "地址": "10.2.3.4", "用户名": "deploy",
                   "备注（可留空）": "团队共享"}
        root_seq = iter(["teamproj"])
        console_setup.ask = lambda prompt, default="", required=True: (
            next(root_seq) if prompt == "项目根目录"
            else answers.get(prompt, default))
        console_setup.ask_port = lambda prompt, default="22": 2222
        console_setup.ask_secret = _capped(lambda prompt, confirm=False,
                                           allow_empty=False: "")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            console_setup.do_add_host({"host_name": "proj-x",
                                       "cwd": str(TMP)})
        text = out.getvalue()
        manifest = proj_root / ".asshcli" / "hosts.toml"
        check("项目清单已写入", manifest.exists(), text)
        raw = manifest.read_text(encoding="utf-8") if manifest.exists() else ""
        check("主机进了项目清单", "[hosts.proj-x]" in raw and "10.2.3.4" in raw,
              raw)
        if raw:
            if sys.version_info >= (3, 11):
                import tomllib as _tomllib
            else:
                import tomli as _tomllib  # type: ignore[import-not-found,no-redef]
            item = _tomllib.loads(raw)["hosts"]["proj-x"]
            check("清单不含密码字段",
                  not any("password" in k for k in item), str(item))
        check("提示首连补录", "补录" in text, text)
        check("提示共享密码路径", "sharepw" in text, text)
        check("提示 git 检查/提交", "git" in text, text)
        check("留空密码不触发解锁（vault 未创建）", not paths.vault_file().exists(),
              str(paths.vault_file()))
        check("摘要标注项目级", "项目级" in text, text)

        print("\n== ② 根目录不存在时重问 ==")
        reset()
        proj2 = TMP / "teamproj2"
        proj2.mkdir(parents=True, exist_ok=True)
        answers2 = {"配置级别": "2", "地址": "10.2.3.5", "用户名": "deploy",
                    "备注（可留空）": ""}
        root_seq2 = iter(["nope-dir", "teamproj2"])
        console_setup.ask = lambda prompt, default="", required=True: (
            next(root_seq2) if prompt == "项目根目录"
            else answers2.get(prompt, default))
        console_setup.ask_port = lambda prompt, default="22": 22
        console_setup.ask_secret = _capped(lambda prompt, confirm=False,
                                           allow_empty=False: "")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            console_setup.do_add_host({"host_name": "proj-w",
                                       "cwd": str(TMP)})
        text = out.getvalue()
        m2 = proj2 / ".asshcli" / "hosts.toml"
        raw2 = m2.read_text(encoding="utf-8") if m2.exists() else ""
        check("重问后写入存在的目录", "[hosts.proj-w]" in raw2 and "10.2.3.5" in raw2,
              raw2)
        check("重问提示目录不存在", "不存在" in text, text)

        print("\n== ③ 填了密码 → 进个人 vault，不进清单 ==")
        reset()
        master = "proj-master-9"
        vault.Vault.create(paths.vault_file(), master)
        os.environ["ASSHCLI_MASTER"] = master
        proj3 = TMP / "teamproj3"
        proj3.mkdir(parents=True, exist_ok=True)
        answers3 = {"配置级别": "2", "地址": "10.2.3.6", "用户名": "root",
                    "备注（可留空）": ""}
        root_seq3 = iter(["teamproj3"])
        console_setup.ask = lambda prompt, default="", required=True: (
            next(root_seq3) if prompt == "项目根目录"
            else answers3.get(prompt, default))
        console_setup.ask_port = lambda prompt, default="22": 22
        console_setup.ask_secret = _capped(lambda prompt, confirm=False,
                                           allow_empty=False: "proj-pw-3")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            console_setup.do_add_host({"host_name": "proj-y",
                                       "cwd": str(TMP)})
        text = out.getvalue()
        v = vault.open_vault()
        check("密码进了个人密码库", v.get("proj-y") == "proj-pw-3", str(v.names()))
        m3 = proj3 / ".asshcli" / "hosts.toml"
        raw3 = m3.read_text(encoding="utf-8") if m3.exists() else ""
        check("密码值不进项目清单", "proj-pw-3" not in raw3, raw3)
        check("说明密码存的是个人库", "个人" in text and "密码库" in text, text)
        os.environ.pop("ASSHCLI_MASTER", None)

        print("\n== ④ 个人同名条目优先的提示 ==")
        reset()
        store.upsert_host(store.Host(name="dup", host="1.1.1.1", user="u"))
        proj4 = TMP / "teamproj4"
        proj4.mkdir(parents=True, exist_ok=True)
        answers4 = {"配置级别": "2", "地址": "10.2.3.7", "用户名": "deploy",
                    "备注（可留空）": ""}
        root_seq4 = iter(["teamproj4"])
        console_setup.ask = lambda prompt, default="", required=True: (
            next(root_seq4) if prompt == "项目根目录"
            else answers4.get(prompt, default))
        console_setup.ask_port = lambda prompt, default="22": 22
        console_setup.ask_secret = _capped(lambda prompt, confirm=False,
                                           allow_empty=False: "")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            console_setup.do_add_host({"host_name": "dup", "cwd": str(TMP)})
        text = out.getvalue()
        check("提示个人条目优先", "优先" in text, text)

        print("\n== ⑤ 共享档：团队口令已存则复用，明文被清掉 ==")
        reset()
        master5 = "proj-master-10"
        vault.Vault.create(paths.vault_file(), master5)
        os.environ["ASSHCLI_MASTER"] = master5
        vault.open_vault().set("teamkey:platform", "team-pass-5")
        proj5 = TMP / "teamproj5"
        m5 = _write_proj_manifest(
            proj5, "[hosts.shr]\nhost = \"10.2.3.8\"\n"
                   "user = \"deploy\"\nshared_password = \"old-plain-pw\"\n")
        answers5 = {"配置级别": "2", "地址": "10.2.3.8", "用户名": "deploy",
                    "备注（可留空）": "", "密码存到哪": "2", "团队 ID": "platform"}
        root_seq5 = iter(["teamproj5"])
        console_setup.ask = lambda prompt, default="", required=True: (
            next(root_seq5) if prompt == "项目根目录"
            else answers5.get(prompt, default))
        console_setup.ask_port = lambda prompt, default="22": 22

        def _secret5(prompt, confirm=False, allow_empty=False):
            assert not prompt.startswith("团队口令"), "团队口令应复用 teamkey，不应再问"
            return "shr-pw-5"

        console_setup.ask_secret = _capped(_secret5)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            console_setup.do_add_host({"host_name": "shr", "cwd": str(TMP)})
        text = out.getvalue()
        raw5 = m5.read_text(encoding="utf-8")
        blob5 = _extract_shared_enc(raw5)
        check("密文写进项目清单", blob5 is not None, raw5)
        if blob5:
            check("密文带团队 ID", vault.parse_blob(blob5)[0] == "platform", blob5)
            check("团队口令能解开",
                  vault.decrypt_blob(blob5, "team-pass-5") == "shr-pw-5")
        item5 = _item_of(raw5, "shr")
        check("旧明文 shared_password 被清掉",
              item5 is not None and "shared_password" not in item5, str(item5))
        v5 = vault.open_vault()
        check("共享档不写个人条目", v5.get("shr") is None, str(v5.names()))
        check("提示成员 teamkey 存口令", "teamkey" in text, text)
        os.environ.pop("ASSHCLI_MASTER", None)

        print("\n== ⑥ 共享档：口令未存则询问，向导不代存 ==")
        reset()
        master6 = "proj-master-11"
        vault.Vault.create(paths.vault_file(), master6)
        os.environ["ASSHCLI_MASTER"] = master6
        proj6 = TMP / "teamproj6"
        proj6.mkdir(parents=True, exist_ok=True)
        answers6 = {"配置级别": "2", "地址": "10.2.3.9", "用户名": "deploy",
                    "备注（可留空）": "", "密码存到哪": "2", "团队 ID": "beta"}
        root_seq6 = iter(["teamproj6"])
        console_setup.ask = lambda prompt, default="", required=True: (
            next(root_seq6) if prompt == "项目根目录"
            else answers6.get(prompt, default))
        console_setup.ask_port = lambda prompt, default="22": 22

        def _secret6(prompt, confirm=False, allow_empty=False):
            if prompt.startswith("团队口令"):
                return "fresh-team-pw"
            return "shr-pw-6"

        console_setup.ask_secret = _capped(_secret6)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            console_setup.do_add_host({"host_name": "shr2", "cwd": str(TMP)})
        m6 = proj6 / ".asshcli" / "hosts.toml"
        raw6 = m6.read_text(encoding="utf-8") if m6.exists() else ""
        blob6 = _extract_shared_enc(raw6)
        check("口令未存时询问并加密", blob6 is not None, raw6)
        if blob6:
            check("新口令能解开密文",
                  vault.decrypt_blob(blob6, "fresh-team-pw") == "shr-pw-6")
        v6 = vault.open_vault()
        check("向导不代存团队口令", v6.get("teamkey:beta") is None,
              str(v6.names()))
        os.environ.pop("ASSHCLI_MASTER", None)
    finally:
        console_setup.ask, console_setup.ask_port, console_setup.ask_secret = orig


def _write_proj_manifest(root: Path, text: str) -> Path:
    d = root / ".asshcli"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "hosts.toml"
    f.write_text(text, encoding="utf-8")
    return f


def _item_of(raw: str, name: str):
    if sys.version_info >= (3, 11):
        import tomllib as _tomllib
    else:
        import tomli as _tomllib  # type: ignore[import-not-found,no-redef]
    try:
        return _tomllib.loads(raw)["hosts"].get(name)
    except Exception:
        return None


def _extract_shared_enc(raw: str):
    import re as _re

    m = _re.search(r'shared_password_enc = "([^"]+)"', raw)
    return m.group(1) if m else None


def main() -> int:
    test_setpass_keeps_fields()
    test_non_windows_first_run_guidance()
    test_status_headless_guidance()
    test_add_host_project_scope()
    return finish()


if __name__ == "__main__":
    sys.exit(main())
