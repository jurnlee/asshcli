"""配置向导 —— 在**新开的控制台窗口**里运行。

由 asshcli.py 通过 console.launch() 拉起。用户在这个窗口里输入主密码和 SSH 密码，
密码全程只存在于本进程内存，不经过命令行参数，也不会被父进程读到。

退出时把结果写进一个临时 JSON 文件，父进程读它判断成功 / 取消 / 出错。

Windows 专属部分（标注）：CONIN$/CONOUT$ 绑定（_bind_console）、
msvcrt 键盘读取（_read_line / _kbhit / _getch）、SetConsoleTitleW。
这些在自动化测试里只有「msvcrt 打桩」级别的覆盖（test_flow 的读行测试），
真实窗口的键盘焦点、回收时机是测试盲区，需人工验证。
"""

from __future__ import annotations

import dataclasses
import getpass
import json
import os
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import launcher  # noqa: E402

LINE = "=" * 58
THIN = "-" * 58

# 允许通过临时文件回传给父进程的字段白名单。
# 密码、主密码、种子等任何敏感值都不得出现在这里。
RESULT_KEYS = frozenset({"status", "host", "error"})


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------
# 控制台绑定与日志
# --------------------------------------------------------------------------


def _bind_console() -> None:
    """把标准流绑到**本窗口自己的**控制台。

    窗口是隔一层启动器拉起来的，正常情况下父进程没有传句柄，标准流本来就
    指向新控制台。但如果某个宿主把句柄也继承了（stdin 是管道），getpass 会
    读不到键盘、print 会写进管道 —— 表现就是「窗口是空的，然后自己关掉」。
    这里显式打开 CONIN$/CONOUT$ 兜底，保证一定读写得到这个窗口。
    """
    if os.name != "nt":
        return
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            if stream is not None and stream.isatty():
                continue
        except Exception:
            pass
        device = "CONIN$" if name == "stdin" else "CONOUT$"
        try:
            setattr(sys, name, open(device, "r" if name == "stdin" else "w",
                                    encoding="utf-8", errors="replace", buffering=1))
        except OSError:
            pass


def _log(msg: str) -> None:
    """写诊断日志到 ~/.asshcli/console.log。

    只记录流程与异常，不记录用户输入 —— 密码永远不会经过这里。
    """
    try:
        from lib import paths

        paths.ensure_data_dir()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(paths.data_dir() / "console.log", "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} [pid {os.getpid()}] {msg}\n")
    except Exception:
        pass


def _log_path() -> str:
    try:
        from lib import paths

        return str(paths.data_dir() / "console.log")
    except Exception:
        return "console.log"


def _log_handles() -> None:
    """记录标准流是不是控制台，出问题时一眼能看出来。"""
    parts = []
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            ok = bool(stream is not None and stream.isatty())
        except Exception:
            ok = False
        parts.append(f"{name}={'tty' if ok else 'not-tty'}")
    _log("句柄 " + " ".join(parts))



# --------------------------------------------------------------------------
# 界面
# --------------------------------------------------------------------------


def _title(text: str) -> None:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW(text)
        except Exception:
            pass


def _banner(subtitle: str) -> None:
    print()
    print(LINE)
    print("  asshcli 配置向导")
    print(f"  {subtitle}")
    print("  密码只在这个窗口里输入，不会进入 AI 的对话上下文")
    print("  按 Ctrl+C 可随时取消")
    print(LINE)
    print()


def _section(step: str, title: str) -> None:
    print(f"[{step}] {title}")
    print(THIN)


def _ok(msg: str) -> None:
    print(f"  [ok] {msg}")


def _err(msg: str) -> None:
    print(f"  [!!] {msg}")


def _hold(seconds: int = 5) -> None:
    """停留几秒让用户看清结果，按任意键可提前关闭。"""
    print()
    end = time.time() + seconds
    while time.time() < end:
        remain = int(end - time.time()) + 1
        print(f"\r  窗口将在 {remain} 秒后关闭，按任意键立即关闭 ", end="", flush=True)
        if _kbhit():
            _getch()
            break
        time.sleep(0.2)
    print("\r" + " " * 52 + "\r", end="")


def _kbhit() -> bool:
    if os.name == "nt":
        try:
            import msvcrt

            return msvcrt.kbhit()
        except Exception:
            return False
    import select

    try:
        return bool(select.select([sys.stdin], [], [], 0)[0])
    except Exception:
        return False


def _getch() -> None:
    if os.name == "nt":
        try:
            import msvcrt

            msvcrt.getch()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 输入
# --------------------------------------------------------------------------


def _read_line(prompt: str) -> str:
    """从控制台读一行，不依赖 sys.stdin。

    为什么不用 input()：窗口是 explorer → cmd → python 拉起来的，某些环境下
    python 的 stdin 不是控制台句柄，input() 会把提示符写进 stderr（用户看不到），
    然后立刻 EOF —— 表现就是「向导走到一半突然没了」。
    直接用 msvcrt 读键盘，就没有这个问题。
    """
    print(prompt, end="", flush=True)
    if os.name == "nt":
        try:
            import msvcrt
        except Exception:
            return input()
        chars: list[str] = []
        try:
            while True:
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    print()
                    return "".join(chars)
                if ch == "\x08":  # 退格
                    if chars:
                        chars.pop()
                        print("\b \b", end="", flush=True)
                    continue
                if ch in ("\x00", "\xe0"):  # 方向键等功能键，吃掉第二个字节
                    msvcrt.getwch()
                    continue
                if ch == "\x03":  # Ctrl+C
                    raise KeyboardInterrupt
                if ch == "\x1b":  # Esc 当取消
                    raise KeyboardInterrupt
                chars.append(ch)
                print(ch, end="", flush=True)
                if len(chars) > 512:
                    print()
                    return "".join(chars)
        except OSError:
            # 没有控制台（例如被重定向），退回标准输入
            try:
                return input()
            except EOFError as exc:
                raise RuntimeError(
                    "这个窗口读不到键盘输入，请在终端里执行 config --here"
                ) from exc
    try:
        return input()
    except EOFError as exc:
        raise RuntimeError(
            "这个窗口读不到键盘输入，请在终端里执行 config --here"
        ) from exc


def ask(prompt: str, default: str = "", required: bool = True) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        raw = _read_line(f"  {prompt}{suffix}: ").strip()
        if not raw and default:
            return default
        if raw or not required:
            return raw
        _err("不能为空，请重新输入（Ctrl+C 可取消）")


def ask_secret(prompt: str, confirm: bool = False,
               allow_empty: bool = False) -> str:
    while True:
        first = getpass.getpass(f"  {prompt}: ")
        if not first:
            if allow_empty:
                return ""
            _err("密码不能为空，请重新输入（Ctrl+C 可取消）")
            continue
        if confirm:
            again = getpass.getpass("  再输入一次: ")
            if again != first:
                _err("两次输入不一致，请重新输入")
                continue
        return first


def ask_port(prompt: str, default: str = "22") -> int:
    while True:
        raw = ask(prompt, default)
        try:
            port = int(raw)
            if 1 <= port <= 65535:
                return port
        except ValueError:
            pass
        _err("端口必须是 1-65535 之间的数字")


# --------------------------------------------------------------------------
# 任务
# --------------------------------------------------------------------------


def _import_lib():
    from lib import paths, store, vault
    from lib.lock import data_lock

    return paths, store, vault, data_lock


def _save_master(vault_mod, vault_path, master: str) -> None:
    """把主密码落到本机后端：优先钥匙串，其次 gpg 加密文件。

    两种都不行时如实告知：只剩 ASSHCLI_MASTER 环境变量兜底，但 agent 能读它。
    """
    backend = vault_mod.save_master_auto(vault_path, master)
    if backend == "keyring":
        _ok("主密码已保存到系统凭据管理器")
    elif backend == "gpg":
        _ok("系统凭据管理器不可用；主密码已用本机 gpg 加密保存到 "
            "~/.asshcli/master.gpg（headless 环境同样能自动解锁）")
    else:
        _err("系统凭据管理器与 gpg 都不可用，之后需设置环境变量 ASSHCLI_MASTER "
             "才能自动连接（headless 指引见 README「主密码后端」一节）")


def _host_hints(existing=None) -> None:
    """添加主机前把每个字段讲清楚。"""
    print("  名称     给这台机器起个短名，之后 `run <名称>` 用它定位")
    print("           例如 hk、prod、db1，只在本机使用，随便起")
    print("  地址     服务器的 IP 或域名，例如 192.168.1.10 或 example.com")
    print("  用户名   登录账号，常见是 root")
    print("  端口     SSH 端口，服务器没改过就是 22")
    print("  密码     登录密码，输入时不会显示；万一填错，之后可以用")
    print("           `setpass <名称>` 单独改，不用重配")
    if existing is not None:
        print()
        print("  这台机器已经配置过：中括号里是现在的值，直接回车表示保持不变。")
        print("  sudo / 密钥 / 命令策略等这个向导不问的字段一律原样保留。")
    print()


def _ask_host_info(existing=None):
    """依次问地址 / 用户名 / 端口 / 备注 / 密码。

    已有主机时把现值作为默认值：回车即「不改」。返回值里的前四项因此
    可以直接覆盖，**没问到的字段（sudo / key_file / policy）由调用方
    用 dataclasses.replace 保住**，不会被悄悄清空。
    """
    h = ask("地址", default=existing.host if existing else "")
    u = ask("用户名", default=existing.user if existing else "root")
    p = ask_port("端口", default=str(existing.port) if existing else "22")
    d = ask("备注（可留空）", default=existing.description if existing else "",
            required=False)
    pw = ask_secret("SSH 密码")
    return h, u, p, d, pw


def do_setup(payload: dict) -> dict:
    paths, store, vault_mod, data_lock = _import_lib()

    _banner("第一次使用，需要先设置主密码和一台服务器")

    _section("1/2", "设置主密码（用来加密本地密码库）")
    print("  这个密码用来加密保存在本机的密码库，本身不会写进任何文件。")
    print("  同时它会存进系统凭据管理器，之后 AI 调用时自动取用，不用再输。")
    print("  忘了就只能删库重来，请务必记牢。")
    print()
    master = ask_secret("主密码（输入时不显示）", confirm=True)

    paths.ensure_data_dir()
    with data_lock(timeout=30):
        if paths.vault_file().exists():
            vault_mod.Vault.open(paths.vault_file(), master=master, allow_keyring=False)
        else:
            vault_mod.Vault.create(paths.vault_file(), master)

    _save_master(vault_mod, paths.vault_file(), master)

    # 项目清单已有主机时跳过「添加第一台服务器」：团队成员 clone 后
    # 只需设主密码，主机定义随仓库就位，各台机器的密码首连时再补。
    try:
        project_hosts = store.load_project()
    except store.StoreError as exc:
        _err(f"项目清单读取失败：{exc}")
        raise
    if project_hosts:
        print()
        names = "、".join(sorted(project_hosts))
        _ok(f"项目清单已有 {len(project_hosts)} 台主机：{names}")
        _ok("跳过“添加第一台服务器”——首次连接某台主机时会单独要它的密码")
        return {"status": "ok",
                "summary": f"项目清单提供 {len(project_hosts)} 台主机（{names}）"}

    print()
    print("  下面填一台服务器，窗口不会自己关，可以慢慢填。")
    print()
    _section("2/2", "添加一台 SSH 服务器")
    _host_hints()

    name = ask("名称")
    h, u, p, d, pw = _ask_host_info()

    with data_lock(timeout=30):
        store.upsert_host(
            store.Host(name=name, host=h, user=u, port=p, description=d)
        )
        v = vault_mod.open_vault()
        v.set(name, pw)

    return {"status": "ok", "host": name,
            "summary": f"{name} → {u}@{h}:{p}"}


def _ensure_vault_unlocked(paths, vault_mod):
    """确保密码库可用并返回已打开的 Vault：能解锁就不打扰，否则问一次主密码。

    只有「密码要落进个人 vault」的路径才调用；项目级留空密码时完全不碰。"""
    try:
        v = vault_mod.open_vault()
        _ok("密码库已解锁")
        return v
    except vault_mod.VaultError:
        print("  需要主密码才能把 SSH 密码存进密码库")
        print()
        master = ask_secret("主密码")
        v = vault_mod.Vault.open(paths.vault_file(), master=master,
                                 allow_keyring=False)
        _save_master(vault_mod, paths.vault_file(), master)
        return v


def _ask_scope() -> str:
    """问这次配置写到哪一级：1 个人级（回车默认）/ 2 项目级。"""
    print("  1  个人级（~/.asshcli/hosts.toml，仅自己；密码进本机密码库）")
    print("  2  项目级（<项目根>/.asshcli/hosts.toml，随 git 团队共享）")
    while True:
        raw = ask("配置级别", default="1")
        if raw in ("1", "2"):
            return raw
        _err("请输入 1 或 2（回车默认 1；Ctrl+C 取消）")


def _resolve_project_root(payload, paths):
    """必问项目根目录，返回将要写入的清单路径 <根>/.asshcli/hosts.toml。

    默认值 = CLI 发现的项目清单所在根（payload project_file，弹窗向导
    cwd 不可靠所以由父进程传），否则 = 真实 cwd（payload cwd）。
    支持相对路径（按真实 cwd 解析）与 ~；根必须存在，且不能是用户级
    清单所在的数据目录。
    """
    project_file = payload.get("project_file") or ""
    if project_file:
        default_root = str(Path(project_file).parent.parent)
    else:
        default_root = payload.get("cwd") or os.getcwd()
    base = Path(payload.get("cwd") or os.getcwd())
    print("  将写入 <根>/.asshcli/hosts.toml；支持相对路径（相对当前目录）")
    while True:
        raw = ask("项目根目录", default=default_root)
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        if not candidate.is_dir():
            _err(f"目录不存在：{candidate}（Ctrl+C 取消）")
            continue
        manifest = candidate / ".asshcli" / "hosts.toml"
        if (os.path.normcase(str(manifest.resolve()))
                == os.path.normcase(str(paths.hosts_file().resolve()))):
            _err("这个目录是用户级清单所在的数据目录，不能当项目根")
            continue
        return manifest


def _ask_project_info(existing=None):
    """问项目级主机的四项基础信息（不含密码——密码不进项目清单）。"""
    h = ask("地址", default=existing.host if existing else "")
    u = ask("用户名", default=existing.user if existing else "root")
    p = ask_port("端口", default=str(existing.port) if existing else "22")
    d = ask("备注（可留空）", default=existing.description if existing else "",
            required=False)
    return h, u, p, d


def _maybe_shared_enc(vault_mod, existing, pw: str):
    """问密码存到哪：1 个人密码库（返回 None，走原路径）/ 2 项目共享。

    选共享时用团队口令加密（asshcli1 密文随项目清单进 git，口令本身
    不进 repo）：vault 里已存 teamkey:<团队> 则复用（保证与成员解密侧
    一致），没存则问一次、不代存（成员各自 manage teamkey set）。返回
    shared_password_enc 密文，或 None 表示走个人 vault。"""
    if not pw:
        return None
    print("  密码存到哪：")
    print("  1  个人密码库（仅本机自己；团队成员首次连接时各自补录）")
    print("  2  项目共享（团队口令加密后写进项目清单，口令不进 repo）")
    while True:
        where = ask("密码存到哪", default="1")
        if where in ("1", "2"):
            break
        _err("请输入 1 或 2（回车默认 1；Ctrl+C 取消）")
    if where == "1":
        return None
    import re

    default_team = ""
    if existing is not None and existing.shared_password_enc:
        try:
            default_team = vault_mod.parse_blob(existing.shared_password_enc)[0]
        except vault_mod.VaultError:
            default_team = ""
    while True:
        team = ask("团队 ID", default=default_team)
        if re.fullmatch(r"[A-Za-z0-9._-]+", team):
            break
        _err("团队 ID 只能含字母数字 . _ -")
    passphrase = None
    try:
        passphrase = vault_mod.open_vault().get(f"teamkey:{team}")
    except vault_mod.VaultError:
        passphrase = None
    if passphrase:
        _ok(f"团队口令复用已存的 teamkey:{team}")
    else:
        passphrase = ask_secret(
            f"团队口令（{team}；成员用它解密，各自 manage teamkey set {team} 存一份）")
    blob = vault_mod.encrypt_blob(pw, passphrase, info=team)
    _ok("已按团队口令加密；密文随项目清单进 git，口令本身不进 repo")
    return blob


def do_add_host(payload: dict) -> dict:
    paths, store, vault_mod, data_lock = _import_lib()

    name = payload.get("host_name") or ""
    reset_password = bool(payload.get("reset_password"))
    existing = store.get(name) if name else None

    _banner(f"添加服务器 {name}" if name else "添加一台 SSH 服务器")

    if not name:
        name = ask("名称")
        existing = store.get(name)

    scope = None
    shared_enc = None
    if reset_password and existing is not None:
        # setpass 只改密码：地址 / 端口 / 用户名一个字都不该重问，
        # 更不该借这次写入把 sudo / key_file / 备注抹掉。
        _ensure_vault_unlocked(paths, vault_mod)
        print()
        _section("修改密码", f"{name} 的连接信息保持不变")
        pw = ask_secret("新的 SSH 密码")
        h = u = p = d = None
    else:
        scope = _ask_scope()
        if scope == "1":
            # 个人级：密码必填，进本机密码库（原流程不变）
            _ensure_vault_unlocked(paths, vault_mod)
            print()
            _section("服务器信息", "")
            _host_hints(existing)
            h, u, p, d, pw = _ask_host_info(existing)
        else:
            # 项目级：主机定义进项目清单（随 git 共享）；密码可留空，
            # 填了可选存个人 vault 或团队共享（shared_password_enc）
            manifest = _resolve_project_root(payload, paths)
            print()
            _section(f"服务器信息（写入 {manifest}）", "")
            _host_hints(existing)
            h, u, p, d = _ask_project_info(existing)
            pw = ask_secret("SSH 密码（可留空；留空则首次连接时补录）",
                            allow_empty=True)
            shared_enc = _maybe_shared_enc(vault_mod, existing, pw)
            if shared_enc:
                pw = ""  # 共享档：不写个人 vault

    if scope == "2":
        manifest_existed = manifest.exists()
        store.upsert_project_host(
            store.Host(name=name, host=h, user=u, port=p, description=d or ""),
            manifest, shared_enc=shared_enc)
        if pw:
            v = _ensure_vault_unlocked(paths, vault_mod)
            v.set(name, pw)
        target = f"{u}@{h}:{p}"
        _ok(f"已{'更新' if manifest_existed else '创建'}项目清单 {manifest}")
        if manifest_existed:
            _ok("重排不保留手写注释：提交前请 git diff 检查，随代码一起过评审")
        else:
            _ok("提交进 git 后，团队成员 clone 即可拿到这台主机")
        if shared_enc:
            _ok("共享密码（团队口令加密档）已写进项目清单；"
                "成员各自 manage teamkey set <团队ID> 存口令后即可使用")
        elif pw:
            _ok("密码已存进你个人的密码库（不进项目文件）；"
                "其他成员首次连接时各自补录")
        else:
            _ok(f"未存密码：首次 run {name} 时会提示补录；"
                f"团队共享可执行 manage sharepw set {name}")
        try:
            user_hosts = store.load_unverified()
        except store.StoreError:
            user_hosts = {}
        entry = user_hosts.get(name)
        if entry is not None and entry.origin != "project":
            _ok("注意：个人清单里已有同名主机，个人条目优先（覆盖项目条目）")
        return {"status": "ok", "host": name,
                "summary": f"{name} → {target}（项目级）"}

    with data_lock(timeout=30):
        # 锁内重读：向导展示默认值到落盘之间，别的 agent 可能刚改过这台主机。
        # 以磁盘上的最新值为基底，只覆盖这次问到（且非空）的字段。
        current = store.get(name) or existing
        if current is not None:
            host = dataclasses.replace(
                current,
                host=h or current.host,
                user=u or current.user,
                port=p or current.port,
                description=current.description if d is None else d,
            )
        else:
            host = store.Host(name=name, host=h, user=u, port=p,
                              description=d or "")
        store.upsert_host(host)
        v = vault_mod.open_vault()
        v.set(name, pw)

    # 共享主机提示：生效条目是项目级时用的是共享密码，刚存的
    # 个人密码要 shadow 条目后才生效 —— 把生效条件说在明处，免得
    #「改了密码怎么没生效」的疑惑
    try:
        effective = store.get(name)
    except store.StoreError:
        effective = None
    if (effective is not None and effective.origin == "project"
            and effective.shared_password):
        _ok("注意：该主机按项目条目使用共享密码；刚存的个人密码会在你")
        _ok("shadow 该主机条目（在 ~/.asshcli/hosts.toml 定义同名主机）后生效")

    target = f"{host.user}@{host.host}:{host.port}"
    return {"status": "ok", "host": name, "summary": f"{name} → {target}"}


def do_unlock(payload: dict) -> dict:
    paths, store, vault_mod, data_lock = _import_lib()

    _banner("解锁密码库")
    print("  密码库无法自动解锁，请输入主密码。")
    print()

    for attempt in range(3):
        master = ask_secret("主密码")
        try:
            vault_mod.Vault.open(paths.vault_file(), master=master, allow_keyring=False)
            break
        except vault_mod.VaultError:
            _err("主密码错误")
    else:
        raise RuntimeError("主密码连续 3 次错误")

    _save_master(vault_mod, paths.vault_file(), master)

    return {"status": "ok"}


def do_ping(payload: dict) -> dict:
    """自检用：立即返回，验证弹窗链路是否打通。"""
    return {"status": "ok"}


TASKS = {
    "setup": do_setup,
    "add_host": do_add_host,
    "unlock": do_unlock,
    "ping": do_ping,
}


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def _write_result(path: Path, data: dict) -> None:
    """写回传文件。

    安全约束：**只允许** status / host / error 三个字段落盘。
    其它 key 一律丢弃，防止后续维护时不小心把密码或主密码塞进来。
    """
    safe = {k: v for k, v in data.items() if k in RESULT_KEYS}
    if safe.get("status") not in ("ok", "cancel", "error"):
        safe = {"status": "error", "error": "内部结果格式错误"}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(safe, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def main() -> int:
    # 与 asshcli.main 同一道门：ASSHCLI_DATA_DIR 只在测试模式下生效。
    # 父进程经 argv[5] 传入的目录是内部可信通道，在门禁之后显式应用。
    from lib import paths

    refusal = paths.override_without_test_mode()
    if refusal:
        print(f"[asshcli] {refusal}", file=sys.stderr)
        return 3

    if len(sys.argv) < 5:
        print("参数错误", file=sys.stderr)
        return 2

    task = sys.argv[1]
    payload_path = Path(sys.argv[2])
    result_file = Path(sys.argv[3])
    pid_file = Path(sys.argv[4])

    # 数据目录由父进程显式传入：窗口是 explorer 拉起的，
    # 环境变量不一定还是调用方那一份，必须钉死，否则会读写到另一份密码库。
    # 该通道等同测试模式的受信设置（门禁之后仍需生效）
    if len(sys.argv) >= 6:
        os.environ["ASSHCLI_DATA_DIR"] = sys.argv[5]
        os.environ["ASSHCLI_TEST_MODE"] = "1"

    # 一次只允许一个向导实例。窗口被重复拉起时（explorer 转发、
    # 命令行被解析两次等），后到的这个立刻退出，不跟用户抢同一个控制台。
    claim = pid_file.with_name(".claimed")
    try:
        claim.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return 0
    except OSError:
        pass

    _bind_console()

    try:
        launcher.write_pid(pid_file)
    except Exception:
        pass

    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log(f"读取参数失败：{exc}")
        _write_result(result_file, {"status": "error", "error": "参数读取失败"})
        return 2
    if not isinstance(payload, dict):
        payload = {}

    # 项目清单路径由父进程钉死：explorer 拉起的窗口 cwd 落在
    # ~/.asshcli/.setup-*，自己向上发现必错；inline 路径虽然继承 cwd，但统一
    # 走 payload 保证两平台一致。空字符串表示「没有项目清单」，也要钉住
    # （不能静默回落到子进程自己的发现，否则两进程看到不同的清单）。
    # 旧版父进程的 payload 没有这个键 —— 回落到子进程自己的发现（尽力而为）。
    if "project_file" in payload:
        paths.set_project_override(str(payload.get("project_file") or ""))

    _log(f"启动任务 {task}（pid {os.getpid()}，父进程 {os.getppid()}）")
    _log_handles()
    _title("asshcli 配置向导")

    try:
        handler = TASKS.get(task)
        if handler is None:
            raise RuntimeError(f"未知任务：{task}")
        result = handler(payload)
    except KeyboardInterrupt:
        print()
        print(THIN)
        print("  已取消，没有做任何修改。")
        _log("用户按 Ctrl+C 取消")
        _write_result(result_file, {"status": "cancel"})
        _hold(3)
        return 0
    except Exception as exc:
        import traceback

        _log("出错：" + "".join(traceback.format_exception(exc))[-1500:])
        print
        print(THIN)
        _err(f"出错了：{exc}")
        print(f"  详细日志：{_log_path()}")
        _write_result(result_file, {"status": "error", "error": str(exc)[:300]})
        _hold(30)
        return 1

    _write_result(result_file, result)
    _log(f"任务 {task} 完成：{result.get('status')}")

    if task == "ping":
        return 0

    print()
    print(THIN)
    summary = str(result.get("summary") or "")
    if summary:
        _ok(f"已保存：{summary}")
    elif result.get("host"):
        _ok(f"已保存，AI 现在可以访问 {result['host']} 了")
    else:
        _ok("已保存")
    print("  回到对话里让 AI 重试那条命令即可，不需要再做别的。")
    _hold(10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
