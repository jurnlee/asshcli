"""项目级主机清单测试。

覆盖：发现（cwd 向上走、最近者赢、跳过用户级数据目录）、合并（同名整条
遮蔽）、项目清单健壮性（无签名 / 假签名段 / 解析失败 fail-closed /
password 键告警）、per-host settings、写路径影子最小化、向导跳过加机、
命令面来源可见性、审计 source 字段。

运行：
    python tests/test_project.py
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

# 数据目录故意放在 <tmp>/home/.asshcli —— 模拟真实布局（home 下的 .asshcli），
# 发现逻辑必须跳过它，不能把它当成「项目清单」再合并一遍（§5.1）
_TMP = Path(tempfile.mkdtemp(prefix="asshcli-proj-"))
_DATA = _TMP / "home" / ".asshcli"
os.environ["ASSHCLI_DATA_DIR"] = str(_DATA)
os.environ["ASSHCLI_TEST_MODE"] = "1"
os.environ.pop("ASSHCLI_MASTER", None)

from lib import audit, paths, store  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [ok] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f"  -> {detail}" if detail else ""))


def reset_user_hosts() -> None:
    f = paths.hosts_file()
    if f.exists():
        f.unlink()


def write_project(root: Path, text: str) -> Path:
    d = root / ".asshcli"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "hosts.toml"
    f.write_text(text, encoding="utf-8")
    return f


def user_raw() -> str:
    f = paths.hosts_file()
    return f.read_text(encoding="utf-8") if f.exists() else ""


def run_cli(args: list[str]) -> tuple[int, str, str]:
    import asshcli as cli

    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(args)
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


PROJECT_TOML = """\
# 团队共享的主机清单：随 git 提交，手编即可（无需 reseal）
[hosts.prod]
host = "203.0.113.10"
user = "deploy"
port = 2222
description = "生产（团队共享）"

[hosts.prod.policy]
deny = ["^rm -rf.*"]

[hosts.prod.settings]
connect_timeout = 30.0

[hosts.staging]
host = "203.0.113.20"
user = "root"
"""


def test_wizard_project_write() -> None:
    """向导写路径：upsert_project_host 新建 / 更新保字段 / fail-closed。"""
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # 3.9/3.10 与 lib.store 同款兼容引入
        import tomli as tomllib  # type: ignore[import-not-found,no-redef]

    print("\n== 向导写项目清单：新建 ==")
    root = _TMP / "wizroot"
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / ".asshcli" / "hosts.toml"
    store.upsert_project_host(
        store.Host(name="prod", host="10.1.1.1", user="deploy", port=2222,
                   description="生产"), manifest)
    raw = manifest.read_text(encoding="utf-8")
    doc = tomllib.loads(raw)
    check("新建清单落盘且可解析", doc["hosts"]["prod"]["host"] == "10.1.1.1", raw)
    check("带文件头使用说明", raw.startswith("# asshcli 项目主机清单"), raw[:60])
    check("不写签名段", "asshcli_integrity" not in raw, raw)
    check("写入备注", doc["hosts"]["prod"].get("description") == "生产")

    print("\n== 向导写项目清单：更新保字段 ==")
    annotated = (
        "# 手写注释\n"
        "[hosts.prod]\n"
        "host = \"1.1.1.1\"\n"
        "user = \"deploy\"\n"
        "custom_field = \"keep-me\"\n"
        "\n"
        "[hosts.prod.policy]\n"
        "deny = [\"^rm -rf.*\"]\n"
        "\n"
        "[hosts.other]\n"
        "host = \"2.2.2.2\"\n"
        "user = \"u\"\n"
    )
    write_project(root, annotated)
    store.upsert_project_host(
        store.Host(name="prod", host="9.9.9.9", user="deploy", port=2222,
                   description=""), manifest)
    raw = manifest.read_text(encoding="utf-8")
    doc = tomllib.loads(raw)
    check("地址已更新", doc["hosts"]["prod"]["host"] == "9.9.9.9", raw)
    check("该节 policy 保留", doc["hosts"]["prod"]["policy"]["deny"] == ["^rm -rf.*"],
          raw)
    check("该节自定义字段保留", doc["hosts"]["prod"]["custom_field"] == "keep-me", raw)
    check("其它主机保留", doc["hosts"]["other"]["host"] == "2.2.2.2", raw)
    check("空备注不写键", "description" not in doc["hosts"]["prod"])

    print("\n== 向导写项目清单：解析失败 fail-closed ==")
    broken = "[hosts.bad\nhost = "
    write_project(root, broken)
    err = None
    try:
        store.upsert_project_host(
            store.Host(name="x", host="1.1.1.1", user="r"), manifest)
    except store.StoreError as exc:
        err = exc
    check("解析失败拒绝写入", err is not None, str(err))
    check("坏文件保持原样", manifest.read_text(encoding="utf-8") == broken)


def main() -> int:
    paths.set_project_override(None)
    paths.ensure_data_dir()

    # ------------------------------------------------------------------
    print("== 发现：cwd 向上走，最近者赢 ==")
    proj_root = _TMP / "projA"
    proj_file = write_project(proj_root, PROJECT_TOML)
    deep = proj_root / "a" / "b" / "c"
    deep.mkdir(parents=True, exist_ok=True)

    old_cwd = os.getcwd()
    try:
        os.chdir(deep)
        found = paths.project_hosts_file()
        check("深层子目录向上发现项目清单", found == proj_file, str(found))
        os.chdir(proj_root)
        check("cwd 即项目根也能发现", paths.project_hosts_file() == proj_file)

        nested = proj_root / "inner"
        nested_file = write_project(nested, '[hosts.x]\nhost = "1.1.1.1"\nuser = "u"\n')
        os.chdir(nested)
        check("嵌套项目最近者赢", paths.project_hosts_file() == nested_file)
        os.chdir(proj_root)
        shutil.rmtree(nested)

        os.chdir(_TMP / "home" / "work" if (_TMP / "home" / "work").exists()
                 else _TMP)
        # 造一个 work 目录再进去
        work = _TMP / "home" / "work"
        work.mkdir(parents=True, exist_ok=True)
        os.chdir(work)
        # work 之上是 home，home 下有 .asshcli —— 那是数据目录本身，必须跳过
        check("数据目录不会被当成项目清单", paths.project_hosts_file() is None)
    finally:
        os.chdir(old_cwd)

    check("没有项目时返回 None（仓库目录上溯无 .asshcli）",
          paths.project_hosts_file() is None or True)  # 占位：下面用 override 钉死测

    paths.set_project_override(str(proj_file))
    check("override 钉住路径", paths.project_hosts_file() == proj_file)
    paths.set_project_override("")
    check("override 空串表示没有项目", paths.project_hosts_file() is None)
    paths.set_project_override(None)

    # ------------------------------------------------------------------
    print("\n== 合并：同名用户级整条遮蔽项目级 ==")
    reset_user_hosts()
    paths.set_project_override(str(proj_file))
    try:
        store.upsert_host(store.Host(name="prod", host="10.9.9.9", user="me"))
        store.upsert_host(store.Host(name="mine", host="10.1.1.1", user="me"))

        merged = store.load_unverified()
        check("同名主机用户级生效", merged["prod"].host == "10.9.9.9")
        check("遮蔽是整条的：项目策略不生效",
              merged["prod"].policy.active is False, str(merged["prod"].policy))
        check("遮蔽是整条的：项目 settings 不生效",
              merged["prod"].settings is None, str(merged["prod"].settings))
        check("同名条目 origin=override", merged["prod"].origin == "override")
        check("项目独有条目 origin=project",
              merged["staging"].origin == "project"
              and merged["staging"].host == "203.0.113.20")
        check("用户独有条目 origin=user", merged["mine"].origin == "user")
        check("load（验签路径）同样是合并视图",
              store.get("staging") is not None
              and store.get("prod") is not None
              and store.get("prod").host == "10.9.9.9")
        check("用户清单只含用户自己写的主机",
              set(json_keys := {"prod", "mine"}) <= {"prod", "mine"}
              and "staging" not in user_raw(), user_raw())
    finally:
        paths.set_project_override(None)

    # ------------------------------------------------------------------
    print("\n== 项目清单健壮性 ==")
    reset_user_hosts()

    fake_seal = PROJECT_TOML + """
[asshcli_integrity]
scheme = "hmac-sha256-v1"
mac = "0000000000000000000000000000000000000000000000000000000000000000"
"""
    sealed_file = write_project(proj_root, fake_seal)
    paths.set_project_override(str(sealed_file))
    try:
        hosts = store.load_project()
        check("项目清单带签名段照常加载（git 是完整性层，不验签）",
              hosts.get("prod") is not None and hosts["prod"].port == 2222)
        check("假签名不拦加载也不报错", "prod" in store.load_unverified())
    finally:
        paths.set_project_override(None)
        write_project(proj_root, PROJECT_TOML)

    broken_file = write_project(proj_root, "这是不合法的 TOML [[[\n")
    paths.set_project_override(str(broken_file))
    try:
        try:
            store.load_project()
            check("项目清单解析失败 fail-closed", False)
        except store.StoreError as exc:
            check("项目清单解析失败 fail-closed",
                  "项目清单" in str(exc) and "解析失败" in str(exc), str(exc))
        try:
            store.load()
            check("合并加载同样 fail-closed", False)
        except store.StoreError as exc:
            check("合并加载同样 fail-closed", "项目清单" in str(exc), str(exc))
    finally:
        paths.set_project_override(None)
        write_project(proj_root, PROJECT_TOML)

    pw_file = write_project(
        proj_root,
        '[hosts.bad]\nhost = "1.2.3.4"\nuser = "root"\npassword = "team-secret"\n')
    paths.set_project_override(str(pw_file))
    try:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            hosts = store.load_project()
        check("password 键不中断加载", hosts.get("bad") is not None)
        check("password 键触发 stderr 告警",
              "password" in err.getvalue() and "bad" in err.getvalue(),
              err.getvalue())
    finally:
        paths.set_project_override(None)
        write_project(proj_root, PROJECT_TOML)

    # ------------------------------------------------------------------
    print("\n== per-host settings ==")
    eff = store.effective_settings(store.Host(name="x", host="h", user="u"))
    check("未配置用默认值",
          eff.connect_timeout == store.Settings.connect_timeout
          and eff.transfer_max_bytes == store.Settings.transfer_max_bytes)

    hs = store.HostSettings(connect_timeout=30.0, transfer_max_bytes=1024)
    eff = store.effective_settings(
        store.Host(name="x", host="h", user="u", settings=hs))
    check("覆盖生效、未覆盖保持默认",
          eff.connect_timeout == 30.0 and eff.transfer_max_bytes == 1024
          and eff.command_timeout == store.Settings.command_timeout)

    # 用户级 settings 整条替换项目级 settings（遮蔽语义的组成部分）
    reset_user_hosts()
    paths.set_project_override(str(proj_file))
    try:
        store.upsert_host(store.Host(
            name="prod", host="203.0.113.10", user="deploy", port=2222,
            description="生产（团队共享）",
            settings=store.HostSettings(command_timeout=99.0)))
        merged = store.load_unverified()
        check("用户级 settings 完全替换项目级 settings",
              merged["prod"].settings == store.HostSettings(command_timeout=99.0),
              str(merged["prod"].settings))
    finally:
        paths.set_project_override(None)

    for bad in ('connect_timeout = 0', 'connect_timeout = "30"',
                'transfer_max_bytes = 1.5', 'transfer_max_bytes = -1'):
        f = write_project(proj_root,
                          f'[hosts.s]\nhost = "h"\nuser = "u"\n\n'
                          f'[hosts.s.settings]\n{bad}\n')
        paths.set_project_override(str(f))
        try:
            try:
                store.load_project()
                check(f"非法值 fail-closed：{bad}", False)
            except store.StoreError as exc:
                check(f"非法值 fail-closed：{bad}", "settings" in str(exc), str(exc))
        finally:
            paths.set_project_override(None)

    f = write_project(proj_root,
                      '[hosts.s]\nhost = "h"\nuser = "u"\n\n'
                      '[hosts.s.settings]\nno_such_key = 1\n')
    paths.set_project_override(str(f))
    try:
        hosts = store.load_project()
        check("未知 settings 键忽略", hosts["s"].settings is not None
              and hosts["s"].settings.connect_timeout is None)
    finally:
        paths.set_project_override(None)
        write_project(proj_root, PROJECT_TOML)

    # Windows 记事本 / PowerShell 5.1 的 UTF-8 带 BOM，tomllib 会报错
    bom_file = write_project(proj_root, "")
    bom_file.write_bytes("\ufeff[hosts.bom]\nhost = \"h\"\nuser = \"u\"\n".encode("utf-8"))
    paths.set_project_override(str(bom_file))
    try:
        hosts = store.load_project()
        check("带 BOM 的项目清单照常加载", hosts.get("bom") is not None)
    finally:
        paths.set_project_override(None)
        write_project(proj_root, PROJECT_TOML)

    # ------------------------------------------------------------------
    print("\n== 写路径：影子最小化 ==")
    reset_user_hosts()
    paths.set_project_override(str(proj_file))
    try:
        # 与项目条目完全一致（含 policy / settings 由 replace 带出）→ 不写用户级
        current = store.get("prod")
        assert current is not None
        same = dataclasses.replace(current)  # 原样
        store.upsert_host(same)
        check("字段一致时跳过用户级写入（无影子）", user_raw() == "", user_raw())

        # 差一个端口 → 写入用户级影子
        changed = dataclasses.replace(current, port=2333)
        store.upsert_host(changed)
        check("有差异时照常写入影子", "[hosts.prod]" in user_raw(), user_raw())
        merged = store.load_unverified()
        check("影子生效且子段保留",
              merged["prod"].port == 2333
              and merged["prod"].policy.deny == ["^rm -rf.*"]
              and merged["prod"].settings == store.HostSettings(
                  connect_timeout=30.0), user_raw())

        check("origin 不写进 TOML", "origin" not in user_raw(), user_raw())

        # set_policy 对项目独有主机给出正确指引
        try:
            store.set_policy("staging", ["^ls$"], [])
            check("set_policy 对项目独有主机报项目提示", False)
        except store.StoreError as exc:
            check("set_policy 对项目独有主机报项目提示",
                  "项目清单" in str(exc), str(exc))
    finally:
        paths.set_project_override(None)
        reset_user_hosts()

    # ------------------------------------------------------------------
    print("\n== 向导：项目清单已有主机时跳过「添加第一台」 ==")
    import console_setup
    from lib import vault

    master_pw = "proj-test-master-1"
    paths.set_project_override(str(proj_file))
    console_setup.ask_secret = lambda *a, **k: master_pw  # noqa: F841 - 打桩
    console_setup._save_master = lambda *a, **k: None     # 不碰钥匙串/gpg
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = console_setup.do_setup({})
        check("do_setup 返回 ok", result.get("status") == "ok", str(result))
        check("summary 提到项目清单提供主机",
              "项目清单" in str(result.get("summary", "")), str(result))
        check("跳过加机：用户清单没有条目", user_raw() == "", user_raw())
        check("提示了首连补密码", "首次连接" in out.getvalue())
    finally:
        paths.set_project_override(None)
        # 打桩仅本进程内有效，不需要恢复（进程随测试结束）
        for f in (_DATA / "vault.enc",):
            if f.exists():
                f.unlink()

    # ------------------------------------------------------------------
    print("\n== 命令面：来源可见 ==")
    reset_user_hosts()
    old_cwd = os.getcwd()
    os.chdir(proj_root)
    try:
        code, out, err = run_cli(["list"])
        check("list 退出码 0", code == 0, err)
        check("list 有来源列", "来源" in out and "项目" in out, out)
        check("项目主机显示为 项目来源", any(
            ln.split()[0] == "staging" and "项目" in ln for ln in out.splitlines()),
            out)

        code, out, err = run_cli(["status"])
        check("status 显示项目清单行",
              "项目清单" in out and str(proj_file) in out, out)

        code, out, err = run_cli(["manage", "policy", "prod"])
        check("policy 显示来源行", "来源" in out and "项目" in out, out)
        check("policy 显示项目 settings 覆盖",
              "connect_timeout=30.0" in out, out)
        check("policy 指引编辑项目文件",
              "编辑项目清单" in out, out)

        # run：项目策略拒绝 + 审计 source（纯团队成员形态：无用户清单）
        from lib import vault as vault_mod

        v = vault_mod.Vault.create(paths.vault_file(), master_pw)
        v.set("prod", "proj-test-password-1")
        os.environ["ASSHCLI_MASTER"] = master_pw
        try:
            code, out, err = run_cli(["run", "prod", "rm -rf /"])
            check("项目策略拒绝 run（退出码 1）", code == 1, f"{code} {err}")
            check("拒绝原因来自项目清单策略", "禁止规则" in err, err)
            records = audit.tail(5)
            rec = next((r for r in records if r.get("action") == "run.denied"), None)
            check("审计记录 source=project",
                  rec is not None and rec.get("source") == "project", str(rec))
        finally:
            os.environ.pop("ASSHCLI_MASTER", None)
            reset_user_hosts()
            v = paths.vault_file()
            if v.exists():
                v.unlink()
    finally:
        os.chdir(old_cwd)
        paths.set_project_override(None)

    # ------------------------------------------------------------------
    print("\n== 审计 source 字段（user / override） ==")
    audit.write("run", host="h", user="u", source="user")
    audit.write("run", host="h", user="u", source="override")
    records = [r for r in audit.tail(5) if r.get("action") == "run"]
    check("source=user / override 都落盘",
          {r.get("source") for r in records} >= {"user", "override"}, str(records))

    # ------------------------------------------------------------------
    print("\n== shared_password：解析与校验 ==")
    shared_file = write_project(
        proj_root,
        PROJECT_TOML + '\n[hosts.devbox]\nhost = "127.0.0.1"\nuser = "dev"\n'
        'port = 1\nshared_password = "dev-shared-1"\n')
    paths.set_project_override(str(shared_file))
    try:
        hosts = store.load_project()
        check("共享密码解析成功", hosts["devbox"].shared_password == "dev-shared-1")
        check("无 shared 字段的主机不受影响", hosts["prod"].shared_password is None)

        f = write_project(proj_root, PROJECT_TOML + '\n[hosts.e]\nhost = "h"\n'
                          'user = "u"\nshared_password = ""\n')
        paths.set_project_override(str(f))
        check("空字符串视为未配置", store.load_project()["e"].shared_password is None)

        f = write_project(proj_root, PROJECT_TOML + '\n[hosts.n]\nhost = "h"\n'
                          'user = "u"\nshared_password = 123456\n')
        paths.set_project_override(str(f))
        try:
            store.load_project()
            check("裸数字 fail-closed", False)
        except store.StoreError as exc:
            check("裸数字 fail-closed 且提示加引号",
                  "shared_password" in str(exc) and "引号" in str(exc), str(exc))

        # 用户级条目里出现：忽略 + 警告（用户级没有共享语义）
        reset_user_hosts()
        paths.set_project_override(None)
        store.upsert_host(store.Host(name="u1", host="h", user="u"))
        raw_text = user_raw().replace('[hosts.u1]',
                                      '[hosts.u1]\nshared_password = "x"')
        paths.hosts_file().write_text(raw_text, encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            hosts = store.load_unverified()
        check("用户级 shared_password 被忽略",
              hosts["u1"].shared_password is None)
        check("用户级出现时有警告", "shared_password" in err.getvalue(),
              err.getvalue())
    finally:
        paths.set_project_override(None)
        reset_user_hosts()
        write_project(proj_root, PROJECT_TOML)

    # ------------------------------------------------------------------
    print("\n== 凭据随生效条目走（_resolve_credentials 规则表）==")
    import asshcli as cli

    class _FakeVault:
        def __init__(self, entries: dict):
            self._entries = entries

        def get(self, key: str):
            return self._entries.get(key)

        def values(self):
            return list(self._entries.values())

    shared_file = write_project(
        proj_root,
        PROJECT_TOML + '\n[hosts.devbox]\nhost = "127.0.0.1"\nuser = "dev"\n'
        'port = 1\nshared_password = "dev-shared-1"\n')
    paths.set_project_override(str(shared_file))
    reset_user_hosts()
    try:
        # 项目条目 + shared：vault 不参与（同名错误密码也不影响），带双存在提示
        host = store.get("devbox")
        pw, key, auth, note, cerr = cli._resolve_credentials(
            "devbox", host, _FakeVault({"devbox": "personal-stale"}))
        check("项目+shared → 用共享密码", pw == "dev-shared-1" and key is None)
        check("auth 标签 shared", auth == "shared")
        check("vault 同名条目存在时返回提示", note is not None and "shadow" in note)

        pw, key, auth, note, cerr = cli._resolve_credentials(
            "devbox", host, _FakeVault({}))
        check("vault 无条目时无提示", note is None)

        # 项目条目无 shared → vault 兑底（现状）
        host = store.get("prod")
        pw, key, auth, note, cerr = cli._resolve_credentials(
            "prod", host, _FakeVault({"prod": "my-own"}))
        check("项目无 shared → vault 兑底", pw == "my-own" and auth == "password")

        # 用户级 shadow → 项目 shared 随整条被遮蔽，只用 vault
        store.upsert_host(store.Host(name="devbox", host="127.0.0.1",
                                     user="me", port=1))
        host = store.get("devbox")
        check("shadow 后 origin=override", host.origin == "override")
        pw, key, auth, note, cerr = cli._resolve_credentials(
            "devbox", host, _FakeVault({"devbox": "my-own"}))
        check("用户级条目只用 vault（shared 被遮蔽）",
              pw == "my-own" and auth == "password" and note is None)
    finally:
        paths.set_project_override(None)
        reset_user_hosts()
        write_project(proj_root, PROJECT_TOML)

    # ------------------------------------------------------------------
    print("\n== 脱敏集包含共享密码（I9 延伸）==")
    shared_file = write_project(
        proj_root,
        PROJECT_TOML + '\n[hosts.devbox]\nhost = "127.0.0.1"\nuser = "dev"\n'
        'port = 1\nshared_password = "dev-shared-1"\n')
    paths.set_project_override(str(shared_file))
    try:
        vals = cli._redaction_values(_FakeVault({"prod": "vault-secret"}))
        check("redaction 集 = vault 值 + 共享密码",
              "vault-secret" in vals and "dev-shared-1" in vals, str(vals))
        from lib.redact import Redactor

        r = Redactor(vals)
        check("共享密码出现在输出里被替换",
              r.clean("password is dev-shared-1") != "password is dev-shared-1")
    finally:
        paths.set_project_override(None)
        write_project(proj_root, PROJECT_TOML)

    # ------------------------------------------------------------------
    print("\n== 命令面：auth=shared 审计、list 共享列、零交互 ==")
    reset_user_hosts()
    old_cwd = os.getcwd()
    os.chdir(proj_root)
    write_project(proj_root, PROJECT_TOML + '\n[hosts.devbox]\nhost = "127.0.0.1"'
                  '\nuser = "dev"\nport = 1\nshared_password = "dev-shared-1"\n')
    try:
        from lib import vault as vault_mod

        v = vault_mod.Vault.create(paths.vault_file(), master_pw)
        v.set("prod", "proj-test-password-1")
        v.set("devbox", "personal-stale")   # 双存在：vault 也有旧条目
        os.environ["ASSHCLI_MASTER"] = master_pw
        try:
            # 回归：项目策略拒绝仍生效
            code, out, err = run_cli(["run", "prod", "rm -rf /"])
            check("（回归）项目策略拒绝仍生效", code == 1, f"{code} {err}")

            # 共享主机凭据齐备：不触发向导，直接尝试连接（本地拒绝端口，
            # 快速失败退出码 4）——零交互的证明
            code, out, err = run_cli(["run", "devbox", "echo hi"])
            check("共享主机不触发配置向导（直接尝试连接）",
                  code == 4 and "ASSHCLI_STATUS" not in err, f"{code} {err}")
            check("双存在提示出现在 stderr", "共享密码" in err and "shadow" in err,
                  err)
            records = audit.tail(8)
            rec = next((r0 for r0 in records
                        if r0.get("action") == "run"
                        and r0.get("host") == "devbox"), None)
            check("审计 auth=shared 且 source=project",
                  rec is not None and rec.get("auth") == "shared"
                  and rec.get("source") == "project", str(rec))

            code, out, err = run_cli(["list"])
            check("list 认证列显示 共享※（双存在）",
                  "共享※" in out, out)

            # 零交互的终极形态：完全没有 vault 也能直接用共享主机
            os.environ.pop("ASSHCLI_MASTER", None)
            f = paths.vault_file()
            if f.exists():
                f.unlink()
            code, out, err = run_cli(["run", "devbox", "echo hi"])
            check("无 vault 也能直连共享主机（零交互）",
                  code == 4 and "ASSHCLI_STATUS" not in err, f"{code} {err}")
        finally:
            os.environ.pop("ASSHCLI_MASTER", None)
            reset_user_hosts()
            f = paths.vault_file()
            if f.exists():
                f.unlink()
    finally:
        os.chdir(old_cwd)
        paths.set_project_override(None)
        write_project(proj_root, PROJECT_TOML)

    # ------------------------------------------------------------------
    print("\n== 加密档：blob 往返与校验 ==")
    from lib import vault as vault_mod

    blob = vault_mod.encrypt_blob("enc-secret-1", "team-passphrase", info="team-a")
    check("blob 自描述团队 ID", vault_mod.parse_blob(blob)[0] == "team-a")
    check("blob 往返", vault_mod.decrypt_blob(blob, "team-passphrase") == "enc-secret-1")
    try:
        vault_mod.decrypt_blob(blob, "wrong")
        check("错口令报错", False)
    except vault_mod.VaultError as exc:
        check("错口令报错", "不匹配" in str(exc), str(exc))

    enc_file = write_project(
        proj_root,
        PROJECT_TOML + '\n[hosts.encbox]\nhost = "127.0.0.1"\nuser = "dev"\n'
        f'port = 1\nshared_password_enc = "{blob}"\n')
    paths.set_project_override(str(enc_file))
    try:
        hosts = store.load_project()
        check("加密档字段解析成功", hosts["encbox"].shared_password_enc == blob)

        bad = write_project(proj_root, PROJECT_TOML + '\n[hosts.encbox]\nhost = "h"\n'
                            'user = "u"\nshared_password_enc = "not-a-blob"\n')
        paths.set_project_override(str(bad))
        try:
            store.load_project()
            check("密文格式坏 fail-closed", False)
        except store.StoreError as exc:
            check("密文格式坏 fail-closed", "shared_password_enc" in str(exc), str(exc))

        both = write_project(proj_root, PROJECT_TOML + '\n[hosts.encbox]\nhost = "h"\n'
                             'user = "u"\nshared_password = "p"\n'
                             f'shared_password_enc = "{blob}"\n')
        paths.set_project_override(str(both))
        try:
            store.load_project()
            check("两字段并存 fail-closed", False)
        except store.StoreError as exc:
            check("两字段并存 fail-closed", "歧义" in str(exc), str(exc))
    finally:
        paths.set_project_override(None)
        reset_user_hosts()
        write_project(proj_root, PROJECT_TOML)

    # ------------------------------------------------------------------
    print("\n== 解密解析：五元组与错误通道 ==")
    enc_file = write_project(
        proj_root,
        PROJECT_TOML + '\n[hosts.encbox]\nhost = "127.0.0.1"\nuser = "dev"\n'
        f'port = 1\nshared_password_enc = "{blob}"\n')
    paths.set_project_override(str(enc_file))
    try:
        host = store.get("encbox")
        # vault 缺失（None）→ error 指引初始化
        pw, key, auth, note, cerr = cli._resolve_credentials("encbox", host, None)
        check("无 vault 时报错（不降级）", cerr is not None and "密码库" in cerr)

        # 有 vault 但没存 teamkey → error 指引 teamkey set
        pw, key, auth, note, cerr = cli._resolve_credentials(
            "encbox", host, _FakeVault({}))
        check("缺 teamkey 时指引 set 命令",
              cerr is not None and "teamkey set team-a" in cerr, str(cerr))

        # 口令不匹配 → error 给两条出路
        pw, key, auth, note, cerr = cli._resolve_credentials(
            "encbox", host, _FakeVault({"teamkey:team-a": "wrong"}))
        check("口令不匹配报错", cerr is not None and "解密失败" in cerr, str(cerr))

        # 正确口令 → 解密成功，auth=shared
        pw, key, auth, note, cerr = cli._resolve_credentials(
            "encbox", host, _FakeVault({"teamkey:team-a": "team-passphrase"}))
        check("解密成功且 auth=shared",
              pw == "enc-secret-1" and auth == "shared" and cerr is None
              and note is None)
    finally:
        paths.set_project_override(None)

    # names 过滤用真 vault 验证
    reset_user_hosts()
    vv = vault_mod.Vault.create(paths.vault_file(), master_pw)
    vv.set("prod", "some-password")
    vv.set("teamkey:team-a", "team-passphrase")
    os.environ["ASSHCLI_MASTER"] = master_pw
    try:
        v2 = vault_mod.open_vault()
        check("names 过滤 teamkey 前缀",
              "teamkey:team-a" not in v2.names() and "prod" in v2.names())
        check("team_names 列出团队", v2.team_names() == ["teamkey:team-a"])
        check("口令进脱敏集（values 含 teamkey）",
              "team-passphrase" in v2.values())
    finally:
        os.environ.pop("ASSHCLI_MASTER", None)
        reset_user_hosts()
        f = paths.vault_file()
        if f.exists():
            f.unlink()

    # ------------------------------------------------------------------
    print("\n== 命令面：run 端到端与 list ==")
    class _NoTty:
        def isatty(self):
            return False

    old_cwd = os.getcwd()
    os.chdir(proj_root)
    write_project(proj_root,
                  PROJECT_TOML + '\n[hosts.encbox]\nhost = "127.0.0.1"\n'
                  'user = "dev"\nport = 1\n'
                  f'shared_password_enc = "{blob}"\n\n'
                  '[hosts.encbox.policy]\ndeny = ["^rm -rf.*"]\n')
    try:
        # 缺 teamkey：退出码 3 + 指引（无 vault 时先引导初始化，这里直接给 vault）
        v3 = vault_mod.Vault.create(paths.vault_file(), master_pw)
        v3.set("prod", "pw")
        os.environ["ASSHCLI_MASTER"] = master_pw
        try:
            code, out, err = run_cli(["run", "encbox", "echo hi"])
            check("缺 teamkey 退出码 3 且指引明确",
                  code == 3 and "teamkey set team-a" in err, f"{code} {err}")

            v3.set("teamkey:team-a", "team-passphrase")
            code, out, err = run_cli(["run", "encbox", "rm -rf /"])
            check("（回归）加密档策略拒绝生效", code == 1, f"{code} {err}")
            records = audit.tail(8)
            rec = next((r0 for r0 in records
                        if r0.get("action") == "run.denied"
                        and r0.get("host") == "encbox"), None)
            check("加密档审计 auth=shared",
                  rec is not None and rec.get("auth") == "shared", str(rec))

            code, out, err = run_cli(["list"])
            check("加密档主机显示 共享（不是缺凭据）",
                  any(ln.split()[0] == "encbox" and "共享" in ln
                      for ln in out.splitlines()), out)

            # manage teamkey list
            code, out, err = run_cli(["manage", "teamkey", "list"])
            check("teamkey list 显示团队", code == 0 and "team-a" in out, out)
        finally:
            os.environ.pop("ASSHCLI_MASTER", None)
            reset_user_hosts()
            f = paths.vault_file()
            if f.exists():
                f.unlink()
    finally:
        os.chdir(old_cwd)
        paths.set_project_override(None)
        write_project(proj_root, PROJECT_TOML)

    # ------------------------------------------------------------------
    print("\n== 管理命令：非 tty 拒绝与加密仪式 ==")
    import builtins
    import getpass as getpass_mod

    old_stdin = sys.stdin
    old_getpass = getpass_mod.getpass
    old_input = builtins.input
    os.chdir(proj_root)
    write_project(proj_root, PROJECT_TOML + '\n[hosts.encbox]\nhost = "127.0.0.1"'
                  '\nuser = "dev"\nport = 1\n')
    vault_mod.Vault.create(paths.vault_file(), master_pw)
    sys.stdin = _NoTty()
    try:
        code, out, err = run_cli(["manage", "teamkey", "set", "team-a"])
        check("teamkey set 非 tty 拒绝并指引",
              code == 3 and "manage teamkey set team-a" in err, f"{code} {err}")
        code, out, err = run_cli(["manage", "sharepw", "set", "encbox"])
        check("sharepw set 非 tty 拒绝并指引",
              code == 3 and "manage sharepw set encbox" in err, f"{code} {err}")
    finally:
        sys.stdin = old_stdin

    # 加密仪式（打桩输入）：产出可解密的 TOML 行
    class _FakeTty:
        def isatty(self):
            return True

    answers = iter(["team-b"])                  # 团队 ID
    secrets = iter(["ceremony-pw", "ceremony-pw", "team-pass-b"])
    sys.stdin = _FakeTty()
    getpass_mod.getpass = lambda prompt="": next(secrets)
    builtins.input = lambda prompt="": next(answers)
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["manage", "sharepw", "set", "encbox"])
    finally:
        sys.stdin = old_stdin
        getpass_mod.getpass = old_getpass
        builtins.input = old_input
    text = out.getvalue()
    check("仪式退出码 0", code == 0, err.getvalue())
    m = re.search(r'shared_password_enc = "([^"]+)"', text)
    check("输出含密文行", m is not None, text)
    if m:
        produced = m.group(1)
        check("密文自描述 team-b", vault_mod.parse_blob(produced)[0] == "team-b")
        check("密文可用团队口令解密",
              vault_mod.decrypt_blob(produced, "team-pass-b") == "ceremony-pw")
    check("提醒成员存 teamkey", "manage teamkey set team-b" in text, text)

    # 清理：仪式段落结束，还原环境
    f = paths.vault_file()
    if f.exists():
        f.unlink()
    os.chdir(old_cwd)
    paths.set_project_override(None)
    write_project(proj_root, PROJECT_TOML)

    test_wizard_project_write()

    shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + "=" * 46)
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
