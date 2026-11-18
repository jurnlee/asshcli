"""asshcli —— 让 AI agent 使用 SSH，而密码永远不进入它的上下文。

这是 agent 调用的唯一入口。典型调用：

    python asshcli.py run prod "df -h"

首次运行会自动拉起一个控制台窗口让用户设置主密码和服务器，
之后所有调用都复用同一份加密密码库（~/.asshcli/vault.enc）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import paths  # noqa: E402
from lib.lock import LockTimeout  # noqa: E402

_SELF = Path(__file__).resolve()

USAGE = """\
asshcli —— AI 可用的 SSH 通道，密码不进入 AI 上下文

执行层（agent 调用）：
  run <主机> <命令...>      执行命令（退出码透传）
  list                     列出已配置的主机
  test <主机>               连通性测试
  put <主机> <本地> <远端>  上传文件
  get <主机> <远端> <本地>  下载文件
  status                   查看整体状态（自诊断）

管理层（用户本人执行）：
  manage setup            只安装运行环境
  manage hosts            列出主机清单（同 list）
  manage setpass <主机>    修改某台主机的密码
  manage unlock           重新输入主密码
  manage policy <主机>     查看该主机的生效策略
  manage reseal            手编 hosts.toml 后重新签名
  manage rename <旧名> <新名>  主机改名（清单与凭据随迁，自动重签名）
  manage keygen <主机> [--force]  生成密钥（私钥入密码库，打印公钥）
  manage keyimport <主机> [文件]  导入已有私钥到密码库
  manage keyshow <主机>    显示该主机的公钥行与指纹
  manage teamkey set <团队ID> / list  团队口令（共享密码加密档的解密钥匙）
  manage sharepw set <主机>  生成 shared_password_enc 密文行（或用向导「项目共享」）
  manage audit [-n N] [--all] 查看审计记录（--all 含解密留痕）
  manage harden <主机> --check 只读探测远端可加固性
  config [--here]         打开配置窗口；加 --here 则在当前终端里配置
  （setpass / unlock / audit / setup / harden 也可直接作顶层命令）

数据目录（所有 agent 共用）：{data}
项目清单（当前项目带 .asshcli/hosts.toml 时随 git 共享，同名用户级优先；
config 向导可选「项目级」写入，密码可存个人库或团队共享加密档）：{project}
"""


# --------------------------------------------------------------------------
# 基础
# --------------------------------------------------------------------------


def _setup_io() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def _usage_text() -> str:
    """USAGE 填充：项目清单那一行随 cwd 所在项目变化，每次展示时现算。"""
    project = paths.project_hosts_file()
    return USAGE.format(data=paths.data_dir(),
                        project=str(project) if project else "（当前目录不在任何项目里）")


def _say(msg: str = "") -> None:
    print(msg)


def _warn(msg: str) -> None:
    print(msg, file=sys.stderr)


class UsageError(Exception):
    pass


# 可以写在位置参数之后的长选项（布尔型）。
# 短选项不在此列：远端命令里的 `-n` / `-f` 很常见，不能吞。
_LONG_BOOL_FLAGS = frozenset({"sudo", "all"})


def _parse(argv: list[str], value_flags: set[str]) -> tuple[dict, list[str]]:
    """解析参数。

    长选项（`--timeout` / `--stdin-file` / `--sudo` / `--stdin`）写在主机名
    之前或之后都等价：

        run --sudo hk "systemctl restart nginx"
        run hk --sudo "systemctl restart nginx"

    短选项仍只识别第一个位置参数之前的，避免把远端命令里的 `-n` 当成本地选项。
    需要把 `--sudo` 这类字样原样交给远端时，用 `--` 显式分隔：

        run hk -- "--sudo something"
    """
    opts: dict = {}
    pos: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--":
            pos.extend(argv[i + 1:])
            break
        if token.startswith("--") and len(token) > 2:
            name, sep, inline = token[2:].partition("=")
            if name in value_flags:
                if sep:
                    opts[name] = inline
                else:
                    i += 1
                    if i >= len(argv):
                        raise UsageError(f"--{name} 需要一个值")
                    opts[name] = argv[i]
            elif name in _LONG_BOOL_FLAGS:
                if sep:
                    raise UsageError(f"--{name} 不接受值")
                opts[name] = True
            elif pos:
                # 位置参数之后的未知长选项属于远端命令，原样保留
                pos.append(token)
            elif sep:
                opts[name] = inline
            else:
                opts[name] = True
        elif token.startswith("-") and len(token) > 1 and not pos:
            flag = token[1:]
            if flag in value_flags:
                i += 1
                if i >= len(argv):
                    raise UsageError(f"-{flag} 需要一个值")
                opts[flag] = argv[i]
            else:
                for ch in flag:
                    opts[ch] = True
        else:
            pos.append(token)
        i += 1
    return opts, pos


def _manual_cmd(sub: str) -> str:
    # 这个环境里弹窗撑不住时，manual 的 config 自动变成 --here
    if sub == "config" and paths.no_window_marker().exists():
        sub = "config --here"
    return f'python "{_SELF}" {sub}'


def _python_too_old_hint() -> str:
    """当前 Python 低于 3.9 时给用户的指引。

    只覆盖「有 Python 但太旧」。如果机器上完全没有 Python，脚本根本启动不了，
    那种情况脚本没法自己报，必须由 agent 看到 command not found 后判断
    （见 SKILL.md 的「如果 python 命令本身不存在」一节）。
    """
    return (
        "这台机器上的 Python 版本低于 3.9，跑不了本脚本。请告诉用户：\n"
        "1. 打开 https://www.python.org/downloads/ 安装 Python 3.9 或更高版本\n"
        '   安装时勾选 "Add python.exe to PATH"\n'
        "2. 装完重新执行刚才的命令，不需要设置任何环境变量\n"
        "注意：机器上其实已经装了 Python，只是版本旧，说的是升级不是首次安装"
    )


def _deps_hint() -> str:
    """有 Python，但虚拟环境或依赖装不上。"""
    return (
        "运行环境没装好（依赖下载失败）。请告诉用户在自己的终端里执行：\n"
        f"{_manual_cmd('setup')}\n"
        "如果卡在下载，可以先换国内镜像源再执行：\n"
        "  set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple"
    )


# --------------------------------------------------------------------------
# 配置窗口编排
# --------------------------------------------------------------------------


def _setup_timeout() -> float:
    """配置窗口等待时长。

    默认 180 秒，可用 ASSHCLI_SETUP_TIMEOUT 覆盖；合法范围 30-600，
    超出范围或非法值一律回落默认，避免把等待时间设成 0 或无穷大。
    """
    default = float(paths.DEFAULT_SETUP_TIMEOUT)
    raw = os.environ.get("ASSHCLI_SETUP_TIMEOUT")
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not (paths.MIN_SETUP_TIMEOUT <= value <= paths.MAX_SETUP_TIMEOUT):
        return default
    return value


def _unknown_host(name: str) -> int:
    """主机不存在时的统一出口。

    注意：这里返回 3（配置错误），不是 2（参数错误）——参数格式没问题，
    只是清单里没有这台机器。也不是 13（LOCKED），密码库此时是好的。
    """
    from lib import protocol

    _warn(f"[asshcli] 未知主机：{name}")
    _warn("[asshcli] 可用 list 查看已有主机，或用 run / config 触发添加")
    return protocol.EXIT_CONFIG


def _handoff_seconds() -> float | None:
    """等多久就把窗口交给用户。

    默认 120 秒 —— 设主密码（输两遍）+ 填主机信息，一两分钟很正常。

    窗口由 explorer 拉起，不在本进程的作业对象里，所以本进程退出（或被
    宿主杀掉）都不影响它。交接只是为了让 agent 早点拿到一个明确状态，
    而不是为了保住窗口。

    `ASSHCLI_SETUP_HANDOFF=0` 表示不交接，一直等到窗口结束或超时。
    """
    default = 120.0
    raw = os.environ.get("ASSHCLI_SETUP_HANDOFF")
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return None
    return min(value, 600.0)


def _launch(task: str, payload: dict) -> tuple[str, dict]:
    from lib import console

    # 项目清单路径钉进 payload（§5.5）：explorer 拉起的向导窗口 cwd 落在
    # ~/.asshcli/.setup-*，自己向上发现必错（甚至发现到别的项目）；非 Windows
    # 的 inline 路径虽然继承 cwd，但统一走 payload 保证两个平台行为一致。
    payload = dict(payload)
    project = paths.project_hosts_file()
    payload["project_file"] = str(project) if project else ""
    # 向导窗口的 cwd 落在 ~/.asshcli/.setup-*，自己探测必错；真实 cwd 由
    # 父进程传进去（项目级作用域的「项目根目录」默认值从这里来）
    payload["cwd"] = os.getcwd()

    if not console.available():
        # 弹窗不可用有多种原因，文案必须对症：非 Windows 的 agent shell
        # 没有 tty，根本没有窗口可等；.no-window 标记说明这个环境弹窗撑不住。
        # 退出码一律 12（TIMEOUT）不变契约，但 hint 不再提「窗口可能还开着」。
        if paths.no_window_marker().exists():
            return "gone", {}  # 弹窗链路已知撑不住，复用同款指引
        return "unavailable", {}
    return console.launch(task, payload, _setup_timeout(), _handoff_seconds())


def _setup_failed(status: str, detail: dict, manual_sub: str,
                  nonce: str = "") -> int:
    """把配置窗口的非成功结果翻译成协议状态码。

    `nonce`：run 路径传入本次调用的 nonce，让 agent 只认带同一标识的状态行。
    """
    from lib import protocol

    if status == "unavailable":
        return protocol.emit(
            protocol.EXIT_TIMEOUT,
            "这里没有可用的交互窗口环境，配置没有完成",
            hint=(
                "这台机器（或当前调用环境）无法弹出配置窗口（非图形环境，"
                "或 agent 的非交互终端）。\n"
                "不要提「窗口」——这里根本没有窗口。请告诉用户在自己的终端里"
                "执行下面的命令，跟着提示设置主密码和服务器信息：\n"
                f"{_manual_cmd('config --here')}\n"
                "配置完成后重试原命令即可。"
            ),
            nonce=nonce,
        )
    if status == "cancel":
        return protocol.emit(
            protocol.EXIT_CANCELLED, "用户取消了配置，未做任何修改", nonce=nonce
        )
    if status == "timeout":
        return protocol.emit(
            protocol.EXIT_TIMEOUT,
            "配置窗口等待超时",
            hint=(
                "配置窗口等待超时。请告诉用户在自己的终端里执行：\n"
                f"{_manual_cmd(manual_sub)}\n"
                "窗口可能还开着，里面输入完会照样生效；如果已经关了，重跑上面这条"
            ),
            nonce=nonce,
        )
    if status == "gone":
        return protocol.emit(
            protocol.EXIT_TIMEOUT,
            "配置窗口被外部终止了，配置没有完成",
            hint=(
                "这个环境会在命令结束时把残留进程一起回收，弹窗撑不住。\n"
                "请告诉用户：在自己的终端里执行下面的命令，全程在当前终端完成，"
                "不会再弹新窗口：\n"
                f"{_manual_cmd('config --here')}"
            ),
            nonce=nonce,
        )
    if status == "pending":
        if detail.get("active"):
            return protocol.emit(
                protocol.EXIT_PENDING,
                "已经有一个配置窗口在等用户输入",
                hint=(
                    "用户正在窗口里填主密码和服务器信息，这需要一两分钟，属于正常。\n"
                    "不要重复调用本命令，也不要再开新窗口；等用户说填完了，"
                    "再重试原命令一次。\n"
                    "窗口标题是「asshcli setup」，如果用户说找不到，让他在任务栏里找一下。"
                ),
                nonce=nonce,
            )
        return protocol.emit(
            protocol.EXIT_PENDING,
            "配置窗口已打开，正在等待用户输入",
            hint=(
                "请告诉用户：桌面上已经打开了一个配置窗口，请在里面设置主密码、"
                "再填一台服务器的信息（地址、用户名、端口、密码），不用赶时间。\n"
                "窗口是独立进程，就算这里的命令被中断也不会关。用户填完后，"
                "再重试原命令即可。\n"
                "不要反复调用本命令，也不要再开新窗口。"
            ),
            nonce=nonce,
        )
    err = detail.get("error") or "配置窗口返回错误"
    return protocol.emit(
        protocol.EXIT_CONFIG, str(err),
        hint=("如果配置窗口反复异常退出，可以直接在用户自己的终端里执行下面的命令，"
              "全程不弹新窗口：\n"
              f"{_manual_cmd('config --here')}"),
        nonce=nonce,
    )


def _run_here(task: str, payload: dict) -> int:
    """在当前终端里直接跑配置向导（不弹窗）。"""
    from lib import protocol

    if not sys.stdin.isatty():
        return protocol.emit(
            protocol.EXIT_CONFIG,
            "当前终端不可交互，无法在这里配置",
            hint=("请在真实的终端窗口里执行：\n"
                  f"{_manual_cmd('config --here')}"),
        )

    import console_setup

    handler = console_setup.TASKS.get(task)
    if handler is None:
        return protocol.emit(protocol.EXIT_CONFIG, f"未知任务：{task}")

    try:
        result = handler(payload)
    except KeyboardInterrupt:
        print()
        return protocol.emit(protocol.EXIT_CANCELLED, "已取消，未做任何修改")
    except Exception as exc:  # noqa: BLE001
        return protocol.emit(
            protocol.EXIT_CONFIG, f"配置失败：{exc}",
            hint=f"详细日志见 {paths.data_dir() / 'console.log'}",
        )

    host = str(result.get("host") or "")
    _say(f"[ok] 配置完成{f'，主机 {host}' if host else ''}")
    return 0


# --------------------------------------------------------------------------
# 命令实现
# --------------------------------------------------------------------------


def cmd_setup(argv: list[str]) -> int:
    from lib import runtime

    _parse(argv, set())
    try:
        runtime.ensure()
    except runtime.PythonTooOldError as exc:
        _warn(f"[asshcli] {exc}")
        _warn("[asshcli] 请升级 Python 到 3.9+：https://www.python.org/downloads/")
        _warn('[asshcli] 安装时勾选 "Add python.exe to PATH"，装好后重新执行本命令')
        return 20
    except runtime.RuntimeError_ as exc:
        _warn(f"[asshcli] {exc}")
        _warn("[asshcli] 如果卡在下载，可换国内镜像后重试：")
        _warn("        set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple")
        _warn(f"[asshcli] 然后重新执行：{_manual_cmd('setup')}")
        return 20
    _say("[ok] 运行环境已就绪")
    return 0


def _stdin_data(opts: dict) -> str | None:
    """取要喂给远端命令的标准输入。

    内容一律不经过 argv —— argv 会进进程列表，也会进审计：

        cat schema.sql | asshcli run prod --stdin - "mysql -u app"
        asshcli run prod --stdin-file ./schema.sql "mysql -u app"

    `--stdin` 只接受 `-`（标准输入）。旧写法 `--stdin <值>` 直接报错而不是
    默默忽略：那个值会变成命令的一部分，等于把秘密写进审计日志。
    """
    if "stdin" in opts:
        value = str(opts["stdin"])
        if value != "-":
            raise UsageError(
                "--stdin 只接受 -（表示从标准输入读取）。\n"
                "旧的 --stdin <值> 写法已删除：值会进 argv、进程列表和审计日志。\n"
                "请改用：cat 文件 | asshcli run <主机> --stdin - <命令>\n"
                "        或：asshcli run <主机> --stdin-file <文件> <命令>"
            )
        return sys.stdin.read() or None
    if "stdin-file" in opts:
        source = str(opts["stdin-file"])
        if source == "-":
            return sys.stdin.read() or None
        try:
            return Path(source).expanduser().read_text(encoding="utf-8") or None
        except OSError as exc:
            raise UsageError(f"读不了 --stdin-file {source}：{exc}") from None
    return None


def _int_opt(opts: dict, flag: str, default: int,
             allow_zero: bool) -> tuple[int, bool]:
    """读一个整数选项，返回 (值, 是否出现过)。"""
    if flag not in opts:
        return default, False
    raw = str(opts[flag])
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise UsageError(f"--{flag} 需要一个整数，收到 {raw!r}") from None
    if value < 0 or (value == 0 and not allow_zero):
        raise UsageError(
            f"--{flag} 需要{'非负整数（0 = 不限制）' if allow_zero else '正整数'}，"
            f"收到 {value}"
        )
    return value, True


def _float_opt(opts: dict, flag: str) -> float | None:
    if flag not in opts:
        return None
    raw = str(opts[flag])
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise UsageError(f"--{flag} 需要一个数字，收到 {raw!r}") from None
    if value <= 0:
        raise UsageError(f"--{flag} 需要正数，收到 {value}")
    return value


# --------------------------------------------------------------------------
# 凭据解析（随生效条目走）
# --------------------------------------------------------------------------


def _open_vault_for(host):
    """按需解锁密码库。返回 (vault_obj, error)。

    明文共享主机（生效条目为项目级且带 shared_password）的凭据不依赖
    vault：vault 不存在或锁定都不阻塞——返回 (None, None)，仅当 vault
    存在时尽力解锁补充脱敏值。加密档（shared_password_enc）的团队口令
    在 vault 里，不享受豁免。非共享主机解锁失败返回错误原文，由调用方
    走 LOCKED 出口（与原行为一致）。"""
    from lib import vault as vault_mod

    shared = (host is not None and host.origin == "project"
              and bool(host.shared_password))
    if shared and not paths.vault_file().exists():
        return None, None
    try:
        return vault_mod.open_vault(), None
    except vault_mod.VaultError as exc:
        if shared:
            return None, None
        return None, str(exc)


def _resolve_credentials(name: str, host, vault_obj):
    """凭据随生效条目走（§4 规则表； 增加密档）。

    - 用户级条目（个人 / 覆盖）：只用 vault；项目共享字段已随整条遮蔽
    - 项目级条目带 shared_password（明文档）：用共享密码，vault 同名条目
      （含 key: 私钥）不参与；vault 也有条目时返回提示
    - 项目级条目带 shared_password_enc（加密档）：用团队口令解密，
      解密只发生在本进程内——与个人 vault 密码同级的防护
    - 项目级条目都不带：vault 兑底（现状流程）

    返回 (password, key_pem, auth, note, error)。note 非空 → 调用方 _warn
    （切换可见）；error 非空 → 调用方按配置错误处理（退出码 3）。auth：
    "key" / "password" / "shared"（两档共用——档位在项目文件里可见，
    不进 agent 视野）。"""
    if host.origin == "project" and (host.shared_password
                                     or host.shared_password_enc):
        if host.shared_password_enc:
            return _resolve_shared_enc(name, host, vault_obj)
        note = None
        if vault_obj is not None and (vault_obj.get(name)
                                      or vault_obj.get(f"key:{name}")):
            note = ("本地 vault 也有该主机的凭据；当前按项目条目使用共享密码。"
                    "要用自己的，在 ~/.asshcli/hosts.toml 里 shadow 该主机条目")
        return host.shared_password, None, "shared", note, None
    password = vault_obj.get(name) if vault_obj is not None else None
    key_pem = (vault_obj.get(f"key:{name}") if vault_obj is not None else None)
    return password, key_pem, ("key" if key_pem else "password"), None, None


def _resolve_shared_enc(name: str, host, vault_obj):
    """加密档：团队口令在 vault 的 teamkey:<团队ID> 条目里。

    密文自带团队 ID（asshcli1:<团队ID>:...）——一人多团队各存各的口令，
    泄漏一个团队只波及该团队。任何一步不成立都返回 error（退出码 3，
    指引明确到具体命令），不降级、不猜测。"""
    from lib import vault as vault_mod

    if vault_obj is None:
        return None, None, "shared", None, (
            "共享密码是加密的，需要先有密码库并解锁；"
            f"请先完成一次初始化（{_manual_cmd('config')}）")
    try:
        team, _salt, _payload = vault_mod.parse_blob(host.shared_password_enc)
    except vault_mod.VaultError as exc:
        return None, None, "shared", None, str(exc)
    passphrase = vault_obj.get(f"teamkey:{team}")
    if not passphrase:
        return None, None, "shared", None, (
            f"还没有存过团队 {team} 的口令。请告诉用户在自己的终端里执行：\n"
            f"{_manual_cmd(f'manage teamkey set {team}')}")
    try:
        password = vault_mod.decrypt_blob(host.shared_password_enc, passphrase)
    except vault_mod.VaultError:
        return None, None, "shared", None, (
            f"团队 {team} 的共享密码解密失败：口令不匹配或密文损坏。"
            f"让成员重设口令（{_manual_cmd(f'manage teamkey set {team}')}），"
            "或让加密者重新生成密文（manage sharepw set）")
    return password, None, "shared", None, None


def _redaction_values(vault_obj) -> list[str]:
    """脱敏输入集：vault 全量值 + 全部项目共享密码（I9 /）。

    A 机的共享密码出现在 B 机输出里同样要替换 —— 共享面比个人凭据更宽。"""
    from lib import store

    values = list(vault_obj.values()) if vault_obj is not None else []
    try:
        for h in store.load_project().values():
            if h.shared_password:
                values.append(h.shared_password)
    except store.StoreError:
        pass  # 项目清单问题在更早的路径已 fail-closed，这里不会走到
    return values


def _audit_auth(base: str, client) -> str:
    """审计里的认证标签：shared 是来源标记，不被机制名（password）覆盖。"""
    if base == "shared":
        return "shared"
    return client.auth_used or base


def _output_opts(opts: dict) -> tuple[int, int | None]:
    """解析 `--max-bytes` / `--tail`。

    默认 50 KiB 上限：够看一份日志尾巴、一份 `df -h`，又不至于把会话上下文
    灌满。`--max-bytes 0` 表示不限制，`--tail N` 只回传最后 N 行。
    """
    from lib import output

    max_bytes, _ = _int_opt(opts, "max-bytes", output.DEFAULT_MAX_BYTES,
                            allow_zero=True)
    tail_lines, present = _int_opt(opts, "tail", 0, allow_zero=False)
    return max_bytes, (tail_lines if present else None)


def cmd_run(argv: list[str]) -> int:
    from lib import audit, output, protocol, store, vault
    from lib.redact import Redactor
    from lib.transport import Client, TransportError

    # run 是唯一混杂远端输出的命令：远端可以在 stderr 里伪造 ASSHCLI_STATUS
    # 劫持 agent。所以本次调用先打一行本地 nonce，协议状态行全部
    # 带上它 —— 远端在本地输出之后才开口，抢不到先手。
    nonce = protocol.new_nonce()
    print(f"ASSHCLI_INVOCATION: {nonce}", file=sys.stderr)

    opts, pos = _parse(argv, {"timeout", "stdin", "stdin-file",
                              "max-bytes", "tail"})
    if not pos:
        raise UsageError('用法：asshcli run <主机> "<命令>"')
    name = pos[0]
    command = " ".join(pos[1:]).strip()
    if not command:
        raise UsageError('缺少要执行的命令，例如：asshcli run prod "df -h"')

    # 选项本身先校验一遍：参数写错就报 2，不要先去弹配置窗口
    timeout = _float_opt(opts, "timeout")
    max_bytes, tail_lines = _output_opts(opts)

    # --- 1. 主机解析（合并清单；项目共享主机不需要密码库）----------
    try:
        host = store.get(name)
    except store.StoreError as exc:
        # 清单解析失败或完整性签名失效：建连前中止，
        # 绝不按一份被改过的清单放行命令
        return protocol.emit(protocol.EXIT_CONFIG, str(exc), nonce=nonce)
    shared_host = (host is not None and host.origin == "project"
                   and bool(host.shared_password))

    # --- 2. 密码库：共享主机的凭据不依赖它 -------------------------------
    # 共享主机：vault 不存在或锁定都不阻塞——凭据随生效条目走；
    # vault 存在则尽力解锁，只为补充脱敏值与用户清单验签（失败不阻塞：
    # 项目条目本就不受签名保护，用户清单的强制验签在非共享路径依旧成立）
    vault_obj = None
    if not shared_host:
        if not paths.vault_file().exists():
            status, detail = _launch("setup", {})
            if status != "ok":
                return _setup_failed(status, detail, "config", nonce=nonce)
            return protocol.emit(
                protocol.EXIT_CONFIGURED, "已经完成初始化配置，请重试这条命令",
                nonce=nonce,
            )
        try:
            vault_obj = vault.open_vault()
        except vault.VaultError:
            status, detail = _launch("unlock", {})
            if status != "ok":
                code = _setup_failed(status, detail, "unlock", nonce=nonce)
                if code == protocol.EXIT_CONFIG:
                    return protocol.emit(
                        protocol.EXIT_LOCKED, "密码库无法自动解锁",
                        hint=(f"密码库无法自动解锁。请告诉用户在自己的终端里执行：\n"
                              f"{_manual_cmd('unlock')}"),
                        nonce=nonce,
                    )
                return code
            try:
                vault_obj = vault.open_vault()
            except vault.VaultError as exc:
                return protocol.emit(protocol.EXIT_LOCKED, str(exc), nonce=nonce)
    elif paths.vault_file().exists():
        try:
            vault_obj = vault.open_vault()
        except vault.VaultError:
            vault_obj = None

    # --- 3. 主机必须已配置 -----------------------------------------------
    if host is None:
        status, detail = _launch("add_host", {"host_name": name})
        if status != "ok":
            return _setup_failed(status, detail, f"config", nonce=nonce)
        return protocol.emit(
            protocol.EXIT_HOST_ADDED, f"已添加主机 {name}，请重试这条命令",
            nonce=nonce,
        )

    # --- 4. 取凭据：vault 密钥优先，密码兜底（基线） -------------------
    # vault_obj 到这里必然非 None：第 2 步失败早已走 unlock 路径并 return
    password, key_pem, auth, cred_note, cred_error = _resolve_credentials(
        name, host, vault_obj)
    if cred_note:
        _warn(f"[asshcli] {cred_note}")
    if cred_error:
        return protocol.emit(protocol.EXIT_CONFIG, cred_error, nonce=nonce)
    if not password and not key_pem:
        if host.key_file:
            # key_file 托管旁路已封：磁盘路径上的私钥不再参与认证
            return protocol.emit(
                protocol.EXIT_CONFIG,
                f"主机 {name} 只配置了已废弃的 key_file 字段，需要把私钥"
                "导入密码库后才能连接",
                hint=("请告诉用户在自己的终端里执行：\n"
                      f"{_manual_cmd(f'manage keyimport {name}')}\n"
                      "（会自动使用主机里遗留的 key_file 路径）"),
                nonce=nonce,
            )
        status, detail = _launch("add_host", {"host_name": name})
        if status != "ok":
            return _setup_failed(status, detail, "config", nonce=nonce)
        return protocol.emit(
            protocol.EXIT_CONFIGURED, f"已补充 {name} 的凭据，请重试这条命令",
            nonce=nonce,
        )

    # --- 5. 执行 ---------------------------------------------------------
    # 脱敏用 vault 全量值 + 项目共享密码（I9 /）；enc 档解密出的
    # 当前主机密码追加进来（其它 enc 主机不追解：KDF 成本 × 主机数不可
    # 接受，如实记录在 README）。
    redact_values = _redaction_values(vault_obj)
    if password:
        redact_values.append(password)
    redactor = Redactor(redact_values)
    safe_cmd = redactor.clean(command)

    # 认证方式进审计：拒绝类记录写「预期的主认证方式」；真实连接的记录
    # 在连接后按实际使用的认证写（key / password，回落加 fallback；
    # shared 是来源标记）

    reason = host.policy.check(command, allow_shell=host.allow_shell)
    if reason:
        audit.write("run.denied", host=name, user=host.user, command=safe_cmd,
                    ok=False, error=reason, auth=auth, source=host.origin)
        _warn(f"[asshcli] 命令被策略拒绝：{reason}")
        return 1

    # stdin 放到策略检查之后再读：被拒的命令不该先把调用方的管道吃掉
    stdin_data = _stdin_data(opts)

    client = Client(host, password, store.effective_settings(host), redactor,
                    key_pem=key_pem)
    try:
        with client:
            result = client.run(
                command, stdin_data=stdin_data, timeout=timeout,
                use_sudo=bool(opts.get("sudo")) or host.sudo,
            )
    except TransportError as exc:
        audit.write("run", host=name, user=host.user, command=safe_cmd,
                    ok=False, error=str(exc), auth=auth, source=host.origin)
        _warn(f"[asshcli] {exc}")
        return protocol.EXIT_NETWORK

    if client.auth_fallback:
        _warn("[asshcli] 注意：密钥认证被服务器拒绝，本次连接回落到了密码"
              "（已写入审计）。若这不是预期，请检查服务器上的公钥是否还在")

    stdout_text, out_info = output.shape(
        result.stdout, max_bytes=max_bytes, tail_lines=tail_lines)
    stderr_text, err_info = output.shape(
        result.stderr, max_bytes=max_bytes, tail_lines=tail_lines)

    sys.stdout.write(stdout_text)
    if stdout_text and not stdout_text.endswith("\n") and sys.stdout.isatty():
        sys.stdout.write("\n")
    sys.stdout.flush()
    if stderr_text:
        sys.stderr.write(stderr_text)
        sys.stderr.flush()

    truncated = out_info["truncated"] or err_info["truncated"]
    audit.write("run", host=name, user=host.user, command=safe_cmd,
                exit_code=result.exit_code, duration_ms=result.duration_ms,
                ok=result.exit_code == 0, truncated=truncated,
                original_bytes=max(out_info["original_bytes"],
                                   err_info["original_bytes"]),
                auth=_audit_auth(auth, client),
                fallback="key" if client.auth_fallback else "",
                source=host.origin)
    return result.exit_code


# 来源列标签：同名时用户级整条遮蔽项目级，覆盖即此意
_ORIGIN_LABELS = {"user": "用户", "project": "项目", "override": "覆盖"}


def cmd_list(argv: list[str]) -> int:
    from lib import store, vault

    _parse(argv, set())
    # 只读展示用免验签读取（load_unverified）：清单签名失效时用户恰恰需要
    # 用 list 看看里面有什么；验签由 run / test 等强制路径负责
    try:
        hosts = store.load_unverified()
    except store.StoreError as exc:
        _warn(f"[asshcli] {exc}")
        return 3
    if not hosts:
        _say("还没有配置任何主机。")
        _say(f"对 AI 说「连一下 <名称>」即可触发配置，或手动执行：{_manual_cmd('config')}")
        return 0

    try:
        v = vault.open_vault()
        have_pw = set(v.names())
        # key_names() 带 key: 前缀，按裸主机名比对需要剥掉
        have_keys = {n.removeprefix("key:") for n in v.key_names()}
    except vault.VaultError:
        have_pw = set()
        have_keys = set()

    width = max(len("名称"), *(len(n) for n in hosts))
    _say(f"{'名称'.ljust(width)}  来源  地址                        认证    备注")
    _say("-" * (width + 50))
    legacy = False
    shared_both = False
    for n, h in sorted(hosts.items()):
        if h.origin == "project" and (h.shared_password or h.shared_password_enc):
            # 共享密码主机（两档同列）：生效条目是项目级 → 用共享
            # 密码；本地 vault 也有条目时加※，把「存了但当前不用」说在明处
            if n in have_pw or n in have_keys:
                auth = "共享※"
                shared_both = True
            else:
                auth = "共享"
        elif n in have_keys and n in have_pw:
            auth = "密钥+密码"
        elif n in have_keys:
            auth = "密钥"
        elif n in have_pw:
            auth = "密码"
        elif h.key_file:
            auth = "key_file*"  # 遗留字段已不参与认证，提示迁移
            legacy = True
        else:
            auth = "缺凭据"
        addr = f"{h.user}@{h.host}:{h.port}"
        origin = _ORIGIN_LABELS.get(h.origin, h.origin)
        _say(f"{n.ljust(width)}  {origin.ljust(4)}  {addr.ljust(26)}  "
             f"{auth.ljust(10)}  {h.description}")
    if paths.project_hosts_file() is not None:
        _say("")
        _say("来源：用户 = 仅 ~/.asshcli；项目 = 随 git 共享的项目清单；"
             "覆盖 = 两级清单同名，用户级生效")
    if shared_both:
        _say("※ 本地 vault 也有该主机的凭据，当前按项目条目使用共享密码"
             "（要用自己的，在 ~/.asshcli/hosts.toml 里 shadow 该主机条目）")
    if legacy:
        _say("")
        _say("* key_file 字段已不再参与认证："
             "请执行 manage keyimport <主机> 把私钥导入密码库")
    return 0


def cmd_test(argv: list[str]) -> int:
    from lib import audit, protocol, store, vault
    from lib.redact import Redactor
    from lib.transport import Client, TransportError

    _, pos = _parse(argv, set())
    if not pos:
        raise UsageError("用法：asshcli test <主机>")
    name = pos[0]

    try:
        host = store.get(name)
    except store.StoreError as exc:
        return protocol.emit(protocol.EXIT_CONFIG, str(exc))
    if host is None:
        return _unknown_host(name)

    # 共享主机：vault 缺失/锁定不阻塞，凭据随生效条目走
    v, vault_error = _open_vault_for(host)
    if vault_error:
        return protocol.emit(protocol.EXIT_LOCKED, vault_error)
    password, key_pem, auth, cred_note, cred_error = _resolve_credentials(name, host, v)
    if cred_note:
        _warn(f"[asshcli] {cred_note}")
    if cred_error:
        return protocol.emit(protocol.EXIT_CONFIG, cred_error)

    # 探测命令是脚本自己写死的，但同样要过策略（I6）：用户把主机配成
    # 白名单模式（「只许跑 df -h」）时，test 无权在这台机器上另跑别的命令。
    # 探测拆成三条独立命令，避免为了拼一行而依赖 shell 元字符。
    probes = ["echo __ASSHCLI_OK__", "uname -s", "hostname"]
    for probe in probes:
        reason = host.policy.check(probe, allow_shell=host.allow_shell)
        if reason:
            audit.write("test.denied", host=name, user=host.user,
                        command=probe, ok=False, error=reason, source=host.origin)
            _warn(f"[asshcli] 探测命令被策略拒绝：{reason}")
            if host.policy.allow:
                _warn("[asshcli] 该主机处于白名单模式，test 的探测命令未获放行；"
                      "请直接 run 一条白名单内的命令验证连通")
            return 1

    target = f"{host.user}@{host.host}:{host.port}"
    _say(f"连接 {name} ({target}) ...")
    redact_values = _redaction_values(v)
    if password:
        redact_values.append(password)   # enc 档解密出的当前主机密码
    redactor = Redactor(redact_values)
    client = Client(host, password, store.effective_settings(host), redactor,
                    key_pem=key_pem)
    try:
        with client:
            r = client.run(probes[0], timeout=20)
            if "__ASSHCLI_OK__" not in r.stdout:
                error = "连接建立但测试命令未返回预期输出"
                audit.write("test", host=name, user=host.user, command=target,
                            ok=False, error=error, duration_ms=r.duration_ms,
                            auth=_audit_auth(auth, client),
                            fallback="key" if client.auth_fallback else "",
                            source=host.origin)
                _warn(f"[asshcli] {error}")
                return 4
            info = ""
            for probe in probes[1:]:
                rr = client.run(probe, timeout=20)
                info += rr.stdout
    except TransportError as exc:
        audit.write("test", host=name, user=host.user, command=target,
                    ok=False, error=str(exc), auth=auth, source=host.origin)
        _warn(f"[asshcli] 连接失败：{exc}")
        return 4

    if client.auth_fallback:
        _warn("[asshcli] 注意：密钥认证被服务器拒绝，本次连接回落到了密码"
              "（已写入审计）")
    audit.write("test", host=name, user=host.user, command=target,
                duration_ms=r.duration_ms, auth=_audit_auth(auth, client),
                fallback="key" if client.auth_fallback else "",
                source=host.origin)
    _say(f"[ok] 连接成功（{r.duration_ms} ms）")
    for line in info.splitlines():
        if line:
            _say(f"     {line}")
    return 0


def _norm(path: str | Path) -> str:
    """归一成本地可比较的绝对路径：展开 ~、解析符号链接、Windows 上统一大小写。"""
    return os.path.normcase(os.path.realpath(os.path.expanduser(str(path))))


def _inside(child: str, parent: str) -> bool:
    if child == parent:
        return True
    return child.startswith(parent.rstrip(os.sep) + os.sep)


def _guard_local_path(direction: str, path: str) -> str | None:
    """文件通道的本地路径约束。返回拒绝原因，None 表示放行。

    `put` 的本地源由调用方指定，`get` 的本地目标也是 —— 两边都不设防的话，
    `get prod /etc/shadow ~/.asshcli/hosts.toml` 能把密码库/清单写坏（DoS），
    `put prod ~/.asshcli/vault.enc /tmp/x` 能把整库密文送到远端。
    realpath 归一之后再判断，防止用符号链接、`..` 或大小写变体绕过去。
    """
    target = _norm(path)
    data_dir = _norm(paths.data_dir())
    tmp_dir = _norm(paths.data_dir() / "tmp")

    if direction == "get":
        if _inside(target, tmp_dir):
            return None
        if _inside(target, data_dir):
            return (f"下载目标不能落在 asshcli 数据目录内（{data_dir}）："
                    "那里是密码库、主机清单和指纹库")
        try:
            cwd = _norm(os.getcwd())
        except OSError:
            cwd = ""
        if cwd and _inside(target, cwd):
            return None
        return (f"下载目标必须在当前目录（{cwd or '（未知）'}）或其子目录，"
                f"或 {tmp_dir} 内；收到的是 {target}")

    if _inside(target, data_dir):
        return (f"上传源不能是 asshcli 数据目录里的文件（{data_dir}）："
                "那里是密码库、主机清单和指纹库")
    if not os.path.exists(target):
        return f"上传源不存在：{target}"
    if not os.path.isfile(target):
        return f"上传源不是普通文件：{target}"
    return None


def _transfer(argv: list[str], direction: str) -> int:
    from lib import audit, protocol, store, vault
    from lib.transport import Client, TransportError

    opts, pos = _parse(argv, {"max-bytes", "timeout"})
    if len(pos) < 3:
        raise UsageError(f"用法：asshcli {direction} <主机> <本地> <远端>")
    name, a, b = pos[0], pos[1], pos[2]

    try:
        host = store.get(name)
    except store.StoreError as exc:
        return protocol.emit(protocol.EXIT_CONFIG, str(exc))
    if host is None:
        return _unknown_host(name)
    # 共享主机：vault 缺失/锁定不阻塞，凭据随生效条目走
    v, vault_error = _open_vault_for(host)
    if vault_error:
        return protocol.emit(protocol.EXIT_LOCKED, vault_error)

    local = os.path.expanduser(a if direction == "put" else b)
    remote = b if direction == "put" else a
    # 审计里按数据流向写：put 是「本地 → 远端」，get 反过来，一眼能看出往哪走
    summary = (f"put {local} -> {remote}" if direction == "put"
               else f"get {remote} -> {local}")

    # 文件通道必须和 run 一样过策略。只配了命令白名单的主机，默认连传文件都不许：
    # 否则「只允许 df -h」的机器照样能被 get /etc/shadow 掏空。
    reason = host.policy.check_transfer(direction, remote)
    if reason:
        audit.write("transfer.denied", host=name, user=host.user,
                    command=summary, ok=False, error=reason, source=host.origin)
        _warn(f"[asshcli] 文件传输被策略拒绝：{reason}")
        return 1

    reason = _guard_local_path(direction, local)
    if reason:
        audit.write("transfer.denied", host=name, user=host.user,
                    command=summary, ok=False, error=reason, source=host.origin)
        _warn(f"[asshcli] 本地路径被拒绝：{reason}")
        return 1

    cap, has_cap = _int_opt(opts, "max-bytes", 0, allow_zero=True)
    max_bytes = cap if has_cap else None
    timeout = _float_opt(opts, "timeout")

    password, key_pem, auth, cred_note, cred_error = _resolve_credentials(name, host, v)
    if cred_note:
        _warn(f"[asshcli] {cred_note}")
    if cred_error:
        return protocol.emit(protocol.EXIT_CONFIG, cred_error)
    client = Client(host, password, store.effective_settings(host),
                    key_pem=key_pem)
    try:
        with client:
            if direction == "put":
                size = client.put_file(local, remote, max_bytes=max_bytes,
                                       timeout=timeout)
            else:
                size = client.get_file(remote, local, max_bytes=max_bytes,
                                       timeout=timeout)
    except TransportError as exc:
        audit.write(f"transfer.{direction}", host=name, user=host.user,
                    command=summary, ok=False, error=str(exc), auth=auth,
                    source=host.origin)
        _warn(f"[asshcli] {exc}")
        return 4
    except OSError as exc:
        audit.write(f"transfer.{direction}", host=name, user=host.user,
                    command=summary, ok=False, error=str(exc), auth=auth,
                    source=host.origin)
        _warn(f"[asshcli] 文件传输失败：{exc}")
        return 4
    if client.auth_fallback:
        _warn("[asshcli] 注意：密钥认证被服务器拒绝，本次连接回落到了密码"
              "（已写入审计）")
    audit.write(f"transfer.{direction}", host=name, user=host.user,
                command=f"{summary}（{size} 字节）",
                auth=_audit_auth(auth, client),
                fallback="key" if client.auth_fallback else "",
                source=host.origin)
    _say(f"[ok] {direction} 完成：{a} -> {b}（{size} 字节）")
    return 0


def cmd_put(argv: list[str]) -> int:
    return _transfer(argv, "put")


def cmd_get(argv: list[str]) -> int:
    return _transfer(argv, "get")


def cmd_config(argv: list[str]) -> int:
    """手动配置入口：没有库就做完整初始化，有库就加一台主机。

    `--here` 表示在当前终端里直接跑向导，不弹新窗口。给「窗口总是被
    外部杀掉」的场景留的退路 —— 全程在用户自己的终端里完成，没有子进程
    可以被别人带走。
    """
    from lib import vault

    opts, _ = _parse(argv, set())
    here = bool(opts.get("here"))

    if not paths.vault_file().exists():
        task, done = "setup", "[ok] 初始化完成"
    else:
        try:
            vault.open_vault()
            task, done = "add_host", "[ok] 已添加主机"
        except vault.VaultError:
            task, done = "unlock", "[ok] 密码库已解锁"

    if here:
        return _run_here(task, {})

    status, detail = _launch(task, {})
    if status != "ok":
        return _setup_failed(status, detail, "config")
    if task == "add_host":
        _say(f"[ok] 已添加主机 {detail.get('host', '')}".rstrip())
    else:
        _say(done)
    return 0


def cmd_setpass(argv: list[str]) -> int:
    from lib import audit, store, vault

    _, pos = _parse(argv, set())
    if not pos:
        raise UsageError("用法：asshcli setpass <主机>")
    name = pos[0]

    try:
        host = store.get(name)
    except store.StoreError as exc:
        return protocol.emit(protocol.EXIT_CONFIG, str(exc))
    if host is None:
        return _unknown_host(name)

    try:
        vault.open_vault()
    except vault.VaultError:
        status, detail = _launch("unlock", {})
        if status != "ok":
            code = _setup_failed(status, detail, "unlock")
            audit.write("setpass", host=name, user=host.user, ok=False,
                        error=f"密码库未解锁（{status}），密码未修改")
            return code

    status, detail = _launch("add_host", {"host_name": name, "reset_password": True})
    if status != "ok":
        code = _setup_failed(status, detail, "config")
        audit.write("setpass", host=name, user=host.user, ok=False,
                    error=str(detail.get("error") or status))
        return code
    audit.write("setpass", host=name, user=host.user)
    _say(f"[ok] {name} 的密码已更新")
    return 0


def cmd_unlock(argv: list[str]) -> int:
    from lib import audit, vault

    _parse(argv, set())
    if not paths.vault_file().exists():
        audit.write("unlock", ok=False, error="尚未初始化")
        _warn("[asshcli] 还没有初始化，请先执行 config")
        return 3
    try:
        vault.open_vault()
    except vault.VaultError as exc:
        # 自动解锁失败本身就是安全相关事件，先记一笔再让用户输入
        audit.write("unlock", ok=False, command="钥匙串自动解锁失败", error=str(exc))
    else:
        audit.write("unlock", ok=True, command="钥匙串自动解锁")
        _say("[ok] 密码库当前可自动解锁，无需操作")
        return 0

    status, detail = _launch("unlock", {})
    if status != "ok":
        code = _setup_failed(status, detail, "unlock")
        audit.write("unlock", ok=False, command="等待用户输入",
                    error=str(detail.get("error") or status))
        return code
    audit.write("unlock", ok=True, command="用户输入")
    _say("[ok] 已解锁")
    return 0


def _backend_guidance() -> None:
    """无钥匙串环境（headless Linux / 容器）的针对性指引。"""
    from lib import vault

    if vault.keyring_available():
        return
    _say("     本环境没有可用的系统凭据库（无钥匙串 / Secret Service）")
    if vault.gpg_available():
        _say("     可用 gpg 加密文件后端：让用户在自己的终端里执行 "
             f"{_manual_cmd('unlock')}，主密码会加密存到 ~/.asshcli/master.gpg")
        _say("     （解密走本机 gpg 私钥 + gpg-agent；私钥带口令时需 agent 已缓存口令）")
    else:
        _say("     本机也没有 gpg。各平台后端可用性见 README「主密码后端」一节；")
        _say("     ASSHCLI_MASTER 环境变量可临时兜底，但 agent 能 printenv 读到，"
             "只做最后手段")


def cmd_status(argv: list[str]) -> int:
    from lib import store, vault

    _parse(argv, set())
    _say(f"数据目录：{paths.data_dir()}")
    _say(f"密码库：  {paths.vault_file()}" +
         ("（存在）" if paths.vault_file().exists() else "（不存在）"))
    _say(f"主机清单：{paths.hosts_file()}" +
         ("（存在）" if paths.hosts_file().exists() else "（不存在）"))
    project = paths.project_hosts_file()
    _say(f"项目清单：{project if project is not None else '（未发现）'}"
         + ("（随 git 共享，不签名）" if project is not None else ""))

    if paths.vault_file().exists():
        try:
            v = vault.open_vault()
            _say(f"[ok] 密码库可自动解锁，含 {len(v.names())} 条密码")
        except vault.VaultError as exc:
            _say(f"[!!] 密码库无法自动解锁：{exc}")
            _backend_guidance()

    try:
        hosts = store.load_unverified()
    except store.StoreError as exc:
        _say(f"[!!] 主机清单读取失败：{exc}")
        return 3
    _say(f"[--] 已配置 {len(hosts)} 台主机")
    for n, h in sorted(hosts.items()):
        _say(f"     {n}: {h.user}@{h.host}:{h.port} {h.description}")

    from lib import console

    active = console.find_active()
    if active is not None:
        _say(f"[!!] 有一个配置窗口正在运行（pid {active[1]}），用户可能正在里面输入")
    return 0


def cmd_audit(argv: list[str]) -> int:
    """查看审计记录（管理操作，本质是用户审查 agent 的行为）。

    展示层分流：默认隐藏 vault.open 解密留痕，正常使用只见业务动作；
    `--all` 看全量（文件本身全保真，一条不漏）。
    """
    from lib import audit

    opts, _ = _parse(argv, {"n"})
    limit, _ = _int_opt(opts, "n", 20, allow_zero=False)
    show_all = bool(opts.get("all"))
    records = audit.tail(limit, include_internal=show_all)
    if not records:
        _say("暂无审计记录")
        return 0
    if not show_all:
        _say("（vault.open 解密留痕默认隐藏；--all 查看全部）")
    for rec in records:
        flag = "ok " if rec.get("ok") else "ERR"
        ts = str(rec.get("ts", ""))[5:19].replace("T", " ")
        cmd = rec.get("command", "")
        if len(cmd) > 80:
            # vault.open 的命令行摘要很长，展示时收一收；完整内容在 audit.log 里
            cmd = cmd[:80] + "…"
        extra = f"  « {cmd}" if cmd else ""
        err = f"  ({rec['error']})" if rec.get("error") else ""
        _say(f"{flag} {ts}  {rec.get('action')} {rec.get('host')}{extra}{err}")
    return 0


def cmd_harden(argv: list[str]) -> int:
    """加固模式：只开接口，点到为止（作者决策 2026-11-09）。

    `harden <主机> --check` 是**只读探测**：检查 sshd 是否 OpenSSH、shell 是否
    可用、authorized_keys 是否可写，输出可加固性报告与建议的 restrict 行。
    不装公钥、不部署服务端包装、不做回滚 —— 那是 的完整实现，在观察名单，
    有真实需求或用户反馈再启动。
    """
    import re

    from lib import audit, store, vault
    from lib.redact import Redactor
    from lib.transport import Client, TransportError

    # --check 自己识别，不进全局解析器：harden 有位置参数，`harden prod --check`
    # 里的 --check 会被 _parse 当成「位置参数之后的未知长选项」留给远端
    check_flag = "--check" in argv
    opts, pos = _parse([a for a in argv if a != "--check"], set())
    if not pos or not check_flag:
        _warn("用法：asshcli manage harden <主机> --check（别名 asshcli harden）")
        _warn("目前只提供 --check 只读探测；完整加固（装公钥 / 服务端包装 / 回滚）"
              "尚未实现（观察名单）")
        return 2
    name = pos[0]

    try:
        host = store.get(name)
    except store.StoreError as exc:
        return protocol.emit(protocol.EXIT_CONFIG, str(exc))
    if host is None:
        return _unknown_host(name)
    # 共享主机：vault 缺失/锁定不阻塞，凭据随生效条目走
    v, vault_error = _open_vault_for(host)
    if vault_error:
        return protocol.emit(protocol.EXIT_LOCKED, vault_error)
    password, key_pem, auth, cred_note, cred_error = _resolve_credentials(name, host, v)
    if cred_note:
        _warn(f"[asshcli] {cred_note}")
    if cred_error:
        return protocol.emit(protocol.EXIT_CONFIG, cred_error)

    # 探测命令与 test 同一纪律：过策略。脚本经 stdin 喂给 `bash -s`，
    # 命令行本身没有元字符；白名单主机不放行 `bash -s` 时如实拒绝。
    probe_cmd = "bash -s"
    reason = host.policy.check(probe_cmd, allow_shell=host.allow_shell)
    if reason:
        audit.write("harden.check", host=name, user=host.user,
                    command=probe_cmd, ok=False, error=reason, auth=auth)
        _warn(f"[asshcli] 探测命令被策略拒绝：{reason}")
        if host.policy.allow:
            _warn("[asshcli] 该主机处于白名单模式；探测命令未获放行")
        return 1

    # 探测脚本：只读检查 ~/.ssh 与 authorized_keys 的存在性与可写性。
    # keyw 的语义：文件可写，或文件不存在但目录可写（可创建）。
    script = (
        'd="$HOME/.ssh"; k="$d/authorized_keys"\n'
        'printf \'ASSHCLI_PROBE dir=%s dirw=%s keyw=%s\\n\' '
        '"$([ -d "$d" ] && echo 1 || echo 0)" '
        '"$([ -d "$d" ] && [ -w "$d" ] && echo 1 || echo 0)" '
        '"$([ -f "$k" ] && [ -w "$k" ] && echo 1 || echo 0; '
        '[ ! -e "$k" ] && [ -w "$d" ] && echo 1)"\n'
    )

    target = f"{host.user}@{host.host}:{host.port}"
    _say(f"[asshcli] 加固探测（只读）：{name} ({target})")
    redact_values = _redaction_values(v)
    if password:
        redact_values.append(password)   # enc 档解密出的当前主机密码
    client = Client(host, password, store.effective_settings(host),
                    Redactor(redact_values), key_pem=key_pem)
    try:
        with client:
            banner = client.banner()
            r = client.run(probe_cmd, stdin_data=script, timeout=20)
    except TransportError as exc:
        audit.write("harden.check", host=name, user=host.user, command=probe_cmd,
                    ok=False, error=str(exc), auth=auth, source=host.origin)
        _warn(f"[asshcli] 探测失败：{exc}")
        return 4
    if client.auth_fallback:
        _warn("[asshcli] 注意：密钥认证被服务器拒绝，本次连接回落到了密码"
              "（已写入审计）")

    checks: list[tuple[str, bool, str]] = []

    m = re.search(r"OpenSSH[_ ]([0-9][0-9.p]*)", banner)
    if m:
        checks.append(("sshd", True,
                       f"OpenSSH {m.group(1)}（支持 restrict / command=）"))
    elif banner:
        checks.append(("sshd", False,
                       f"非 OpenSSH：{banner[:60]}（受限 authorized_keys 语法不保证可用）"))
    else:
        checks.append(("sshd", False, "无法识别（服务器未提供 banner）"))

    fields: dict[str, str] = {}
    for line in r.stdout.splitlines():
        if line.startswith("ASSHCLI_PROBE "):
            for kv in line[len("ASSHCLI_PROBE "):].split():
                key, _, val = kv.partition("=")
                fields[key] = val
            break

    if not fields:
        checks.append(("shell", False,
                       "bash 存在但探测脚本没有返回结果（shell 受限或行为异常）"))
    else:
        checks.append(("shell", True, "bash 可用"))
        dir_ok = fields.get("dir") == "1"
        dir_w = fields.get("dirw") == "1"
        key_w = fields.get("keyw") == "1"
        checks.append(("~/.ssh 目录", dir_ok,
                       "存在" if dir_ok else "不存在（首次部署时创建即可，不算阻塞）"))
        checks.append(("authorized_keys 可写", key_w,
                       "可写或可创建" if key_w else
                       "不可写（需要先修权限或换部署方式）"))
        if not dir_w and dir_ok:
            checks.append(("~/.ssh 目录可写", False,
                           "目录存在但不可写，装公钥会失败"))

    _say("")
    ok_all = True
    for label, good, detail in checks:
        ok_all = ok_all and good
        _say(f"  {'[ok]' if good else '[!!]'} {label}：{detail}")
    audit.write("harden.check", host=name, user=host.user, command=probe_cmd,
                ok=ok_all, auth=_audit_auth(auth, client),
                fallback="key" if client.auth_fallback else "",
                source=host.origin)
    _say("")
    if ok_all:
        _say("结论：具备可加固条件。")
    else:
        _say("结论：存在阻塞项（见上）。先把 [!!] 的项处理好再评估加固。")
    _say("建议的 restrict 行（完整实现后自动生成并部署，当前仅供评估）：")
    _say('  restrict,command="$HOME/bin/asshcli-agent-shell" <key-type> '
         "<base64 公钥> asshcli-agent")
    return 0 if ok_all else 1


def cmd_policy(argv: list[str]) -> int:
    """查看某台主机的生效策略（管理操作）。

    修改策略仍走 hosts.toml 手编（语法见 README 策略一节）；这里是只读视图，
    免得用户为「看一眼」去解析整份清单。用免验签读取：签名失效时用户恰恰
    需要先用它检查清单被改成了什么，再决定是否 reseal。
    """
    from lib import store

    _, pos = _parse(argv, set())
    if not pos:
        raise UsageError("用法：asshcli manage policy <主机>")
    name = pos[0]
    try:
        host = store.load_unverified().get(name)
    except store.StoreError as exc:
        _warn(f"[asshcli] {exc}")
        return 3
    if host is None:
        return _unknown_host(name)

    _say(f"[{name}] {host.user}@{host.host}:{host.port}"
         + (f"  {host.description}" if host.description else ""))
    _say(f"  来源：{_ORIGIN_LABELS.get(host.origin, host.origin)}"
         + ("（同名遮蔽了项目级条目）" if host.origin == "override" else ""))
    if host.allow_shell:
        _say("allow_shell = true（允许 shell 元字符；allow/deny 只对整条命令原样生效）")
    if host.settings is not None:
        eff = store.effective_settings(host)
        overrides = []
        if host.settings.connect_timeout is not None:
            overrides.append(f"connect_timeout={eff.connect_timeout}")
        if host.settings.command_timeout is not None:
            overrides.append(f"command_timeout={eff.command_timeout}")
        if host.settings.transfer_max_bytes is not None:
            overrides.append(f"transfer_max_bytes={eff.transfer_max_bytes}")
        if host.settings.transfer_timeout is not None:
            overrides.append(f"transfer_timeout={eff.transfer_timeout}")
        _say("  settings 覆盖：" + "，".join(overrides) + "（未列出的用默认值）")
    pol = host.policy
    if not pol.active:
        _say("  未配置策略：所有命令与文件通道默认放行（元字符仍默认拒绝）")
        return _policy_tail(host)
    for key, rules in (("allow", pol.allow), ("deny", pol.deny),
                       ("allow_put", pol.allow_put), ("allow_get", pol.allow_get),
                       ("deny_put", pol.deny_put), ("deny_get", pol.deny_get)):
        if rules:
            _say(f"  {key} = ['" + "', '".join(rules) + "']")
    return _policy_tail(host)
    return 0


def _policy_tail(host) -> int:
    """policy 视图的收尾：按来源给出正确的编辑指引。"""
    if host.origin == "project":
        _say(f"修改请编辑项目清单 {paths.project_hosts_file()}（随 git 共享）；"
             "策略语法见 README")
    else:
        _say("修改请编辑 ~/.asshcli/hosts.toml 的 [hosts.<名称>.policy] 段，语法见 README")
    return 0


def cmd_reseal(argv: list[str]) -> int:
    """重新签名 hosts.toml（完整性校验的合法化出口）。

    hosts.toml 现在带 HMAC 签名：程序写入自动续签，手编之后签名失效，
    所有连接在建连前被拒 —— 这是预期行为，不是故障。本命令在用户检查过
    文件内容之后重新签名。签名密钥从主密码派生，密码库未解锁时先走解锁
    流程；解锁经由窗口完成时，本进程会在拿到结果后自行再开一次密码库。
    """
    from lib import audit, protocol, store, vault

    _parse(argv, set())
    if not paths.hosts_file().exists():
        _warn("[asshcli] 还没有 hosts.toml，没有可签名的内容")
        return 3
    try:
        vault.open_vault()
    except vault.VaultError:
        status, detail = _launch("unlock", {})
        if status != "ok":
            audit.write("reseal", ok=False, error=f"密码库未解锁（{status}）")
            return _setup_failed(status, detail, "unlock")
        try:
            vault.open_vault()
        except vault.VaultError as exc:
            audit.write("reseal", ok=False, error=str(exc))
            return protocol.emit(protocol.EXIT_LOCKED, str(exc))

    try:
        store.reseal()
    except store.StoreError as exc:
        audit.write("reseal", ok=False, error=str(exc))
        _warn(f"[asshcli] {exc}")
        return 3
    audit.write("reseal")
    _say("[ok] hosts.toml 已重新签名，之后的连接会重新校验完整性")
    return 0


def cmd_rename(argv: list[str]) -> int:
    """用户级主机改名：清单条目（含策略 / settings）与 vault 凭据一并随迁。

    为什么需要它：凭据在 vault 里按主机名索引（`<名称>` 与 `key:<名称>`），
    手编清单改名后新名字查不到任何凭据 —— 密码要重录，私钥要原始文件
    重新导入（文件丢了甚至只能 keygen 重生成）。本命令一次做完三件事：

    1. hosts.toml 里 old → new（子段随条目整体移动，写入自动重签名）
    2. vault 条目迁移：密码 <old> → <new>、私钥 key:<old> → key:<new>，
       旧条目删除（不用重录任何凭据）
    3. 审计留一条 rename 记录

    拒绝条件（fail-closed）：old 只在项目级清单（项目文件随 git 走）；
    new 与任何现有主机重名（用户级或项目级，避免悄悄造出新的遮蔽）；
    vault 里已有 new 的遗留孤儿条目（除非 --force 覆盖）；清单签名已
    失效（先检查内容 reseal，再来改名）。
    """
    from lib import audit, protocol, store, vault

    force = "--force" in argv
    _, pos = _parse([a for a in argv if a != "--force"], set())
    if len(pos) != 2 or pos[0] == pos[1]:
        _warn("用法：asshcli manage rename <旧名> <新名> [--force]")
        _warn("改名范围：用户级清单条目（含策略 / settings）+ vault 里的密码与私钥条目")
        return 2
    old, new = pos[0].strip(), pos[1].strip()
    for label, name in (("旧名", old), ("新名", new)):
        if not name or any(ch in name for ch in "\r\n\t"):
            _warn(f"[asshcli] {label}不能为空，也不能含换行 / 制表符")
            return 2

    try:
        merged = store.load_unverified()
    except store.StoreError as exc:
        return protocol.emit(protocol.EXIT_CONFIG, str(exc))
    entry = merged.get(old)
    if entry is None:
        return _unknown_host(old)
    if entry.origin == "project":
        project_file = paths.project_hosts_file()
        _warn(f"[asshcli] {old} 只存在于项目级清单（{project_file}）")
        _warn("[asshcli] 项目文件随 git 共享，改名请走 git；"
              "要留个人副本可先用 config 向导补一条个人条目")
        return 3
    if new in merged:
        _warn(f"[asshcli] 新名称 {new} 已有同名主机（用户级或项目级）")
        _warn("[asshcli] 改名不能造出重名或新的遮蔽；换一个名字")
        return 3

    if not paths.vault_file().exists():
        _warn("[asshcli] 还没有密码库，先执行一次 config 完成初始化")
        return 3
    try:
        v = vault.open_vault()
    except vault.VaultError:
        status, detail = _launch("unlock", {})
        if status != "ok":
            return _setup_failed(status, detail, "unlock")
        try:
            v = vault.open_vault()
        except vault.VaultError as exc:
            return protocol.emit(protocol.EXIT_LOCKED, str(exc))

    orphans = [k for k in (new, f"key:{new}") if v.get(k)]
    if orphans and not force:
        _warn(f"[asshcli] vault 里已有 {' 与 '.join(orphans)} 的遗留条目（已无对应主机）")
        _warn("[asshcli] 改名会覆盖它们；确认覆盖请加 --force")
        return 3

    try:
        store.rename_host(old, new)
    except store.StoreError as exc:
        _warn(f"[asshcli] {exc}")
        return 3

    moved: list[str] = []
    try:
        password = v.get(old)
        if password:
            v.set(new, password)
            v.delete(old)
            moved.append("密码")
        key_pem = v.get(f"key:{old}")
        if key_pem:
            v.set(f"key:{new}", key_pem)
            v.delete(f"key:{old}")
            moved.append("私钥")
    except Exception as exc:  # noqa: BLE001
        audit.write("rename", host=new, user=entry.user,
                    command=f"{old} -> {new}", ok=False,
                    error=f"清单已改名，但凭据迁移失败：{exc}")
        _warn(f"[asshcli] 清单已改名 {old} -> {new}，但凭据迁移失败：{exc}")
        _warn(f"[asshcli] 补救：{_manual_cmd(f'manage setpass {new}')}；"
              f"私钥用 {_manual_cmd(f'manage keyimport {new} <私钥文件>')}")
        return 3

    audit.write("rename", host=new, user=entry.user,
                command=f"{old} -> {new}（凭据迁移：{'/'.join(moved) or '无'}）")
    _say(f"[ok] 已改名 {old} -> {new}（清单已自动重签名）")
    if moved:
        _say(f"     vault 凭据已随迁：{'、'.join(moved)}（旧条目已删除，不用重录）")
    else:
        _say("     vault 里没有这台主机的密码 / 私钥条目，无需迁移")
    if entry.origin == "override":
        _say(f"     注意：项目清单里的 {old} 不再被遮蔽，{old} 现在生效的是项目定义")
    return 0


# --------------------------------------------------------------------------
# 密钥管理（基线：生成 / 导入 / 展示；公钥部署属加固档，观察名单）
# --------------------------------------------------------------------------


def _key_entry(name: str) -> str:
    """vault 里的私钥条目名（预留的 key:<host> 命名空间）。"""
    return f"key:{name}"


def _key_intro(pem: str, name: str) -> None:
    """打印公钥行与指纹 —— 唯一允许出现在终端上的密钥形态。"""
    from lib import sshkeys

    comment = f"asshcli-{name}"
    _say(f"     指纹：{sshkeys.sha256_fingerprint(pem)}")
    _say(f"     公钥（放进服务器 authorized_keys 的一行）：")
    _say(f"     {sshkeys.public_key_line(pem, comment=comment)}")


def cmd_keygen(argv: list[str]) -> int:
    """生成 Ed25519 密钥对：私钥加密入 vault（key:<host>），公钥打印出来。

    私钥只在内存与密文之间流转，绝不落盘（设计宪法：明文私钥落盘 = 禁止）。
    公钥部署（写服务器 authorized_keys）属加固档写操作，当前由用户手动
    完成；`manage harden <主机> --check` 可先探测服务器端条件。
    `--force` 轮换：旧公钥不会自动从服务器移除，输出里会提醒。
    """
    from lib import audit, protocol, sshkeys, store, vault

    force = "--force" in argv
    _, pos = _parse([a for a in argv if a != "--force"], set())
    if not pos:
        raise UsageError("用法：asshcli manage keygen <主机> [--force]")
    name = pos[0]

    try:
        host = store.get(name)
    except store.StoreError as exc:
        return protocol.emit(protocol.EXIT_CONFIG, str(exc))
    if host is None:
        return _unknown_host(name)

    try:
        v = vault.open_vault()
    except vault.VaultError as exc:
        return protocol.emit(protocol.EXIT_LOCKED, str(exc))

    old = v.get(_key_entry(name))
    if old and not force:
        _warn(f"[asshcli] {name} 已有密钥（{sshkeys.sha256_fingerprint(old)}）")
        _warn("[asshcli] 确认要轮换请加 --force；旧公钥需在服务器上手工移除")
        return 3

    pem = sshkeys.generate_ed25519_pem(comment=f"asshcli-{name}")
    v.set(_key_entry(name), pem)
    audit.write("key.gen", host=name, user=host.user,
                command=f"ed25519 {sshkeys.sha256_fingerprint(pem)}")
    _say(f"[ok] 已生成 Ed25519 密钥并加密存入密码库（key:{name}）")
    _key_intro(pem, name)
    _say("     部署提示：追加到服务器的 ~/.ssh/authorized_keys；Windows")
    _say("     管理员组用户是 administrators_authorized_keys（注意 ACL 只留")
    _say("     Administrators/SYSTEM）。部署后本工具会自动优先用密钥认证。")
    if old:
        _say("     注意：这是轮换出的新密钥，旧公钥记得从服务器上移除。")
    return 0


def cmd_keyimport(argv: list[str]) -> int:
    """导入已有私钥：解析校验 + 归一化后加密入 vault，全程不落盘不回显。

    文件路径省略时使用该主机遗留的 key_file 字段（之后该字段不再参与
    认证，只作为迁移线索保留）。带口令的私钥拒绝导入，错误信息给出用户
    本机移除口令的办法。
    """
    from lib import audit, protocol, sshkeys, store, vault

    _, pos = _parse(argv, set())
    if not pos:
        raise UsageError("用法：asshcli manage keyimport <主机> [私钥文件]")
    name = pos[0]
    path_arg = pos[1] if len(pos) > 1 else None

    try:
        host = store.get(name)
    except store.StoreError as exc:
        return protocol.emit(protocol.EXIT_CONFIG, str(exc))
    if host is None:
        return _unknown_host(name)

    legacy = host.key_file
    if path_arg is None:
        if not legacy:
            _warn("[asshcli] 未指定私钥文件，该主机也没有遗留的 key_file 字段")
            _warn("[asshcli] 用法：asshcli manage keyimport <主机> <私钥文件>")
            return 2
        path_arg = legacy
        _say(f"[asshcli] 使用主机遗留的 key_file 路径：{path_arg}")

    source = Path(path_arg).expanduser()
    try:
        pem_raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        audit.write("key.import", host=name, ok=False,
                    error=f"私钥文件读取失败：{source}")
        _warn(f"[asshcli] 读不了私钥文件 {source}：{exc}")
        return 3
    except UnicodeDecodeError:
        audit.write("key.import", host=name, ok=False,
                    error="私钥文件不是文本 PEM")
        _warn(f"[asshcli] {source} 不是文本 PEM（PuTTY 的 .ppk 请先转换导出）")
        return 3

    try:
        pem = sshkeys.normalize_pem(pem_raw)
        sshkeys.load_pkey(pem)  # 双保险：入库前确认 paramiko 能加载归一化产物
    except sshkeys.SSHKeyError as exc:
        audit.write("key.import", host=name, ok=False, error=str(exc))
        _warn(f"[asshcli] {exc}")
        return 3

    try:
        v = vault.open_vault()
    except vault.VaultError as exc:
        return protocol.emit(protocol.EXIT_LOCKED, str(exc))
    v.set(_key_entry(name), pem)

    audit.write("key.import", host=name, user=host.user,
                command=f"from {source} {sshkeys.sha256_fingerprint(pem)}")
    _say(f"[ok] 私钥已导入并加密存入密码库（key:{name}），明文不落盘")
    _key_intro(pem, name)
    if legacy:
        _say("     提示：hosts.toml 的 key_file 字段已不再参与认证，可删除")
    return 0


def cmd_keyshow(argv: list[str]) -> int:
    """显示某主机密钥的公钥行与指纹（只有公开材料会出现在输出里）。"""
    from lib import protocol, sshkeys, store, vault

    _, pos = _parse(argv, set())
    if not pos:
        raise UsageError("用法：asshcli manage keyshow <主机>")
    name = pos[0]

    try:
        host = store.get(name)
    except store.StoreError as exc:
        return protocol.emit(protocol.EXIT_CONFIG, str(exc))
    if host is None:
        return _unknown_host(name)

    try:
        v = vault.open_vault()
    except vault.VaultError as exc:
        return protocol.emit(protocol.EXIT_LOCKED, str(exc))
    pem = v.get(_key_entry(name))
    if not pem:
        _warn(f"[asshcli] {name} 还没有密钥")
        _warn(f"[asshcli] 生成：{_manual_cmd(f'manage keygen {name}')}")
        _warn(f"[asshcli] 导入：{_manual_cmd(f'manage keyimport {name} <私钥文件>')}")
        return 3
    _key_intro(pem, name)
    return 0


# --------------------------------------------------------------------------
# 团队口令与加密仪式
# --------------------------------------------------------------------------


def _team_id_ok(team: str) -> bool:
    import re

    return bool(re.fullmatch(r"[A-Za-z0-9._-]+", team))


def cmd_teamkey(argv: list[str]) -> int:
    """管理团队口令：加密档共享密码的解密钥匙，按团队存个人 vault。

    多团队各自独立：一人参与 N 个团队 = N 条 teamkey:<ID>，泄漏一个
    只波及该团队。管理命令不进向导：非交互终端直接拒绝并给出手动指引。"""
    import getpass

    from lib import audit, protocol, vault

    _, pos = _parse(argv, set())
    if not pos:
        _warn("用法：asshcli manage teamkey set <团队ID>  |  manage teamkey list")
        return 2
    sub, rest = pos[0], pos[1:]

    if sub == "list":
        if not paths.vault_file().exists():
            _say("还没有密码库，也就没有团队口令")
            return 0
        try:
            v = vault.open_vault()
        except vault.VaultError as exc:
            _warn(f"[asshcli] {exc}")
            return 3
        teams = [n.removeprefix("teamkey:") for n in v.team_names()]
        if not teams:
            _say("还没有存过任何团队的口令")
            _say(f"添加：{_manual_cmd('manage teamkey set <团队ID>')}")
        else:
            _say(f"已存 {len(teams)} 个团队的口令：{', '.join(teams)}")
        return 0

    if sub != "set" or not rest:
        _warn("用法：asshcli manage teamkey set <团队ID>")
        return 2
    team = rest[0]
    if not _team_id_ok(team):
        _warn(f"[asshcli] 团队 ID 只能含字母数字 ._ ，收到 {team!r}")
        return 2
    if not paths.vault_file().exists():
        _warn("[asshcli] 还没有密码库，先执行一次 config 完成初始化")
        return 3
    if not sys.stdin.isatty():
        return protocol.emit(
            protocol.EXIT_CONFIG, "需要在交互终端里输入团队口令",
            hint=("请告诉用户在自己的终端里执行：\n"
                  f"{_manual_cmd(f'manage teamkey set {team}')}"))
    try:
        first = getpass.getpass(f"  团队 {team} 的口令（输入时不显示）: ")
        again = getpass.getpass("  再输入一次: ")
    except (EOFError, KeyboardInterrupt):
        _warn("\n[asshcli] 已取消，未做任何修改")
        return 130
    if not first or first != again:
        _warn("[asshcli] 口令为空或两次不一致，未做任何修改")
        return 3
    try:
        v = vault.open_vault()
    except vault.VaultError as exc:
        return protocol.emit(
            protocol.EXIT_LOCKED, str(exc),
            hint=f"请先解锁密码库：{_manual_cmd('unlock')}")
    v.set(f"teamkey:{team}", first)
    audit.write("teamkey.set", command=team)
    _say(f"[ok] 团队 {team} 的口令已存入密码库（之后随主密码自动解锁，不用再输）")
    return 0


def cmd_sharepw(argv: list[str]) -> int:
    """加密仪式：把明文密码加密成 shared_password_enc 行，打印供粘贴。

    asshcli 对项目文件维持只读（原则）：密文产出走 git / PR 评审，
    不直接写文件。团队口令优先复用 vault 里已存的 teamkey，没有当场问。"""
    import getpass

    from lib import audit, protocol, store, vault

    _, pos = _parse(argv, set)
    if not pos or pos[0] != "set" or len(pos) < 2:
        _warn("用法：asshcli manage sharepw set <主机>")
        return 2
    name = pos[1]
    project_file = paths.project_hosts_file()
    if project_file is None:
        _warn("[asshcli] 当前目录不在任何项目里（找不到 .asshcli/hosts.toml）")
        return 3
    try:
        hosts = store.load_project()
    except store.StoreError as exc:
        _warn(f"[asshcli] {exc}")
        return 3
    host = hosts.get(name)
    if host is None:
        _warn(f"[asshcli] 主机 {name} 不在项目清单 {project_file} 里"
              "（sharepw 只服务项目级共享密码）")
        return 3

    default_team = ""
    if host.shared_password_enc:
        try:
            default_team = vault.parse_blob(host.shared_password_enc)[0]
        except vault.VaultError:
            default_team = ""

    if not sys.stdin.isatty():
        return protocol.emit(
            protocol.EXIT_CONFIG, "需要在交互终端里输入明文密码",
            hint=("请告诉用户在自己的终端里执行：\n"
                  f"{_manual_cmd(f'manage sharepw set {name}')}"))
    try:
        first = getpass.getpass("  明文密码（输入时不显示）: ")
        again = getpass.getpass("  再输入一次: ")
        prompt = f"  团队 ID{f' [{default_team}]' if default_team else ''}: "
        team = input(prompt).strip() or default_team
    except (EOFError, KeyboardInterrupt):
        _warn("\n[asshcli] 已取消，未做任何修改")
        return 130
    if not first or first != again:
        _warn("[asshcli] 密码为空或两次不一致，未做任何修改")
        return 3
    if not _team_id_ok(team or ""):
        _warn(f"[asshcli] 团队 ID 只能含字母数字 ._ ，收到 {team!r}")
        return 3

    # 团队口令：vault 里已存则复用（保证与成员解密侧一致），否则当场问
    passphrase = None
    if paths.vault_file().exists():
        try:
            v = vault.open_vault()
            passphrase = v.get(f"teamkey:{team}")
        except vault.VaultError:
            passphrase = None
    if not passphrase:
        try:
            passphrase = getpass.getpass(
                f"  团队 {team} 的口令（成员用同一口令解密）: ")
        except (EOFError, KeyboardInterrupt):
            _warn("\n[asshcli] 已取消，未做任何修改")
            return 130
        if not passphrase:
            _warn("[asshcli] 团队口令不能为空")
            return 3

    blob = vault.encrypt_blob(first, passphrase, info=team)
    _say("")
    _say("[ok] 已加密。把下面几行放进项目清单并提交（如该主机原来有")
    _say("     shared_password 明文字段，一并删除——两字段并存会被拒绝）：")
    _say("")
    _say(f"[hosts.{store._key(name)}]")
    _say(f'shared_password_enc = "{blob}"')
    _say("")
    _say(f"     提醒成员各自执行一次：{_manual_cmd(f'manage teamkey set {team}')}")
    _say("     （成员把团队口令存进自己的密码库，之后自动解密，无需再输）")
    audit.write("sharepw.set", host=name, command=f"team {team}")
    return 0


MANAGE_USAGE = """\
asshcli manage —— 管理操作（给用户本人执行，AI 不需要代跑）

  manage setup             只安装运行环境
  manage hosts             列出主机清单（同 list）
  manage setpass <主机>     修改某台主机的密码
  manage unlock            重新输入主密码
  manage policy <主机>      查看该主机的生效策略
  manage reseal            手编 hosts.toml 后重新签名（完整性校验）
  manage rename <旧名> <新名> [--force]  改名（清单+凭据随迁，自动重签名）
  manage keygen <主机> [--force]  生成 Ed25519 密钥（私钥入密码库，打印公钥）
  manage keyimport <主机> [文件]  导入已有私钥到密码库（省略文件用遗留 key_file）
  manage keyshow <主机>     显示该主机的公钥行与指纹
  manage teamkey set <团队ID>  存团队口令（加密档共享密码的解密钥匙）
  manage teamkey list       已存过哪些团队的口令
  manage sharepw set <主机>  加密仪式：生成 shared_password_enc 行供粘贴进 git
  manage audit [-n N] [--all] 查看审计记录（--all 含解密留痕）
  manage harden <主机> --check 只读探测远端可加固性（只读，不改动远端）

加主机：对 AI 说「连一下 <名称>」自动触发；或手动执行 config
（加 --here 在当前终端完成，不弹窗）。向导里可选个人级 / 项目级：
项目级会确认项目根后写入项目清单，密码可选存个人库或团队共享（加密档）。
"""


def cmd_manage(argv: list[str]) -> int:
    """管理操作入口：按受众拆分，配置 / 密码 / 审计归用户本人。"""
    if not argv:
        _say(MANAGE_USAGE)
        return 2
    sub, rest = argv[0], argv[1:]
    handler = MANAGE_COMMANDS.get(sub)
    if handler is None:
        _warn(f"未知管理子命令：{sub}")
        _say(MANAGE_USAGE)
        return 2
    return handler(rest)


COMMANDS = {
    "run": cmd_run,
    "list": cmd_list,
    "ls": cmd_list,
    "test": cmd_test,
    "put": cmd_put,
    "get": cmd_get,
    "status": cmd_status,
    # 管理层入口与旧命令别名（旧手感不破坏）
    "manage": cmd_manage,
    "config": cmd_config,
    "setpass": cmd_setpass,
    "unlock": cmd_unlock,
    "audit": cmd_audit,
    "setup": cmd_setup,
    "harden": cmd_harden,
}

MANAGE_COMMANDS = {
    "setup": cmd_setup,
    "hosts": cmd_list,
    "setpass": cmd_setpass,
    "unlock": cmd_unlock,
    "policy": cmd_policy,
    "reseal": cmd_reseal,
    "rename": cmd_rename,
    "keygen": cmd_keygen,
    "keyimport": cmd_keyimport,
    "keyshow": cmd_keyshow,
    "teamkey": cmd_teamkey,
    "sharepw": cmd_sharepw,
    "audit": cmd_audit,
    "harden": cmd_harden,
}


def main(argv: list[str] | None = None) -> int:
    _setup_io()
    # ASSHCLI_DATA_DIR 是路径类保护的根：只在显式测试模式下生效。
    # 拒绝发生在一切数据访问之前。
    refusal = paths.override_without_test_mode()
    if refusal:
        _warn(f"[asshcli] {refusal}")
        return 3
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in ("-h", "--help", "help"):
        _say(_usage_text())
        return 0
    if args[0] in ("-V", "--version", "version"):
        _say("asshcli 1.0.0")
        return 0

    cmd, rest = args[0], args[1:]
    handler = COMMANDS.get(cmd)
    if handler is None:
        _warn(f"未知命令：{cmd}")
        _say(_usage_text())
        return 2

    try:
        # setup 只装环境，不需要 venv 里才有依赖；manage setup 同理
        if cmd != "setup" and not (cmd == "manage" and rest
                                   and rest[0] == "setup"):
            from lib import runtime

            try:
                runtime.ensure()
            except runtime.PythonTooOldError as exc:
                from lib import protocol

                _warn(f"[asshcli] {exc}")
                return protocol.emit(
                    protocol.EXIT_NO_RUNTIME, str(exc), hint=_python_too_old_hint()
                )
            except runtime.RuntimeError_ as exc:
                from lib import protocol

                _warn(f"[asshcli] {exc}")
                return protocol.emit(
                    protocol.EXIT_NO_RUNTIME, str(exc), hint=_deps_hint()
                )
        return handler(rest)
    except UsageError as exc:
        _warn(f"[asshcli] {exc}")
        return 2
    except LockTimeout as exc:
        # 另一个 asshcli 进程正在配置。可重试，不是内部错误。
        _warn(f"[asshcli] {exc}")
        return 3
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        _warn("\n[asshcli] 已取消")
        return 130
    except Exception as exc:  # noqa: BLE001
        from lib import protocol

        _warn(f"[asshcli] 内部错误：{exc}")
        return protocol.EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main())
