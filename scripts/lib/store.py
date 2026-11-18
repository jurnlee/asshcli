"""主机清单（hosts.toml）。

只存连接信息，**永不存密码** —— 密码一律在 vault.enc 里按主机名索引。

两级清单：主机定义可同时来自用户级（~/.asshcli/hosts.toml，带 HMAC
签名）与项目级（<项目>/.asshcli/hosts.toml，随 git 共享、不签名）。加载时
合并，**同名主机用户级整条遮蔽项目级**（含 policy / settings）。凭据永远
只在用户级：项目清单里没有任何密码字段可以生效。

写路径（方案 B，2026-11-15 作者拍板）：不再做「保留注释与排版」的
文本手术 —— 读进 dict、改、用 tomli-w 整份规范重排。代价是程序写入后
不保留手写注释；换来的是「手动编辑格式稍有出入就写坏清单」这一类问题
从根上消失，加字段也不用再动手术代码。
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.9/3.10 由运行时装 tomli（见 runtime.PACKAGES）
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

from . import integrity, paths
from .fsutil import atomic_write_text, read_text
from .lock import data_lock

HEADER = """\
# asshcli 主机清单 —— 由程序自动维护，也可以手写
#
# 这里没有密码字段，密码在 vault.enc 里，用 asshcli setpass <名称> 录入。
# 手动加主机后不必再做什么，第一次执行时会自动要求补充密码。
# 注意：程序写入时会整份规范重排（不保留注释与排版），字段值始终保留。
# 注意：清单带完整性签名（[asshcli_integrity] 段）—— 手编之后连接会被拒绝，
# 这是预期行为：检查内容无误后执行 asshcli manage reseal 重新签名。

"""

PROJECT_HEADER = """\
# asshcli 项目主机清单（随 git 共享，工具与向导也可写入）
#
# 这里没有密码字段：个人凭据在各自的 vault.enc；团队共享密码用
# shared_password / shared_password_enc（见 README「项目级清单」）。
# 注意：向导写入时会整份规范重排（不保留注释与排版），字段值始终保留；
# 提交前请用 git diff 检查变更，随代码一起过评审。

"""

# TOML 裸键只允许 A-Za-z0-9_-；中文、空格、点号等一律要用带引号的键
_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_HEADER_RE = re.compile(r"^(\s*)\[([^\[\]]+)\](\s*)$")


def _key(name: str) -> str:
    """把主机名写成 TOML 键：合法的用裸键，其余套双引号。

    json 的双引号转义规则（\\" \\\\ \\n \\uXXXX）恰好是 TOML 基本字符串
    的子集，所以直接复用它，省得自己处理控制字符。
    """
    return name if _BARE_KEY_RE.match(name) else json.dumps(name, ensure_ascii=False)


def _section_path(line: str) -> list[str] | None:
    """解析 `[hosts."名字".policy]` 这类段头的键路径。

    交给 tomllib 解析，引号、转义、点号都由它兜住；不是合法段头则返回 None。
    """
    stripped = line.strip()
    if not stripped.startswith("[") or stripped.startswith("[["):
        return None
    try:
        node = tomllib.loads(stripped)
    except tomllib.TOMLDecodeError:
        return None
    path: list[str] = []
    while isinstance(node, dict) and len(node) == 1:
        key = next(iter(node))
        path.append(key)
        node = node[key]
    if not isinstance(node, dict) or node:
        return None
    return path or None


def _heal_legacy(text: str) -> str:
    """把历史上写坏的裸键段头补上引号：`[hosts.李家豪LJH]` → `[hosts."李家豪LJH"]`。

    只改动整行解析不了的段头，正常文件原样返回，可安全地反复调用。
    """
    lines = text.splitlines()
    changed = False
    for idx, line in enumerate(lines):
        if _section_path(line) is not None:
            continue
        m = _HEADER_RE.match(line)
        if not m:
            continue
        parts = [_key(p.strip()) for p in m.group(2).split(".")]
        fixed = f"{m.group(1)}[{'.'.join(parts)}]"
        if _section_path(fixed) is None:
            continue
        lines[idx] = fixed
        changed = True
    if not changed:
        return text
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _warn(msg: str) -> None:
    print(f"[asshcli] {msg}", file=sys.stderr)


def _read_text_nobom(path: Path) -> str:
    """读文本并剥掉可能存在的 UTF-8 BOM。

    Windows 记事本 / PowerShell 5.1 的 UTF-8 默认带 BOM，tomllib 对
    BOM 直接报 Invalid statement。项目清单以手编为常态，这是必经路径；
    用户清单同样受益。仅剥文件开头的 U+FEFF，不动其它内容。"""
    text = read_text(path)
    return text.lstrip("\ufeff") if text.startswith("\ufeff") else text


class StoreError(Exception):
    pass


# 完整性签名失效时的指引。这段话写给最终用户：签名失效最常见的
# 原因是本人手编了 hosts.toml（预期行为，reseal 一次即可）；但也可能是
# 别的进程改了清单 —— 所以必须先检查内容，再重新签名。
_RESEAL_HINT = (
    "如果你刚手动编辑过 hosts.toml，这是预期行为：先检查文件内容是否符合"
    "你的本意，然后在你的终端执行 asshcli manage reseal 重新签名。\n"
    "如果你没有改过它，说明有别的程序动过这份清单——先弄清楚改了什么。"
)


def _verify_integrity(doc: dict) -> None:
    """读路径的强制验签点。签名密钥未绑定（密码库没解锁）时跳过。

    run / test / put / get / harden 全部先解锁密码库才走到这里，所以
    强制路径必然验签；list / status / policy 在锁定状态下仍可用来
    检查被改过的文件。
    """
    key = integrity.mac_key()
    if key is None:
        return
    problem = integrity.verify_doc(doc, key)
    if problem is not None:
        raise StoreError(f"{problem}\n{_RESEAL_HINT}")


# 命令里出现这些字符就默认拒绝。它们让「一条命令」变成「一串命令」，
# 任何按整条字符串写的 allow / deny 规则都会被绕开。
_SHELL_META = frozenset(";&|$`\n\r<>")

_META_LABEL = {
    "\n": "\\n", "\r": "\\r",
}


def _matches(pattern: str, text: str) -> bool:
    """整条字符串匹配一条规则。

    用 fullmatch 而不是 search：`allow = ['df -h']` 在 search 下会放过
    `x; rm -rf /; df -h`。规则写坏（非法正则）时退化成字面全串比较，
    不让一条笔误把整台机器变成「不设防」或「完全不可用」。
    """
    try:
        return re.fullmatch(pattern, text) is not None
    except re.error:
        return pattern == text


@dataclass
class Policy:
    """主机策略。

    `allow` / `deny` 管 `run` 的命令；`allow_put` / `deny_put` 等管文件
    通道，匹配的是**远端路径**（put 看目标、get 看源）。命令策略和文件策略
    分开配置，是因为它们拦的是完全不同的东西：一条白名单管不住
    `get /etc/shadow`。

    定性（重要）：这是**防手滑，非安全边界**。hosts.toml 和本文件对同用户
    的进程都可写，agent 想绕开随时能绕开（改文件、直接调 lib）。真正的强制
    只能在服务端做。这里的价值是让「顺手犯的错」付出一点摩擦成本，并留下
    拒绝记录，而不是拦住有意绕过的调用方。
    """

    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    allow_put: list[str] = field(default_factory=list)
    allow_get: list[str] = field(default_factory=list)
    deny_put: list[str] = field(default_factory=list)
    deny_get: list[str] = field(default_factory=list)

    def check(self, command: str, *, allow_shell: bool = False) -> str | None:
        """校验一条 `run` 命令。返回 None 表示放行，否则返回拒绝原因。

        `allow_shell=True` 的主机跳过元字符检查（例如需要 `2>/dev/null`
        或 `a && b` 的场景），代价是 allow / deny 只对整条命令原样生效。
        """
        if not allow_shell:
            meta = sorted({_META_LABEL.get(ch, ch) for ch in command
                           if ch in _SHELL_META})
            if meta:
                return ("命令包含 shell 元字符（" + "、".join(meta)
                        + "），默认拒绝；确需使用请在主机配置里加 "
                          "allow_shell = true")
        for pat in self.deny:
            if _matches(pat, command):
                return f"命中禁止规则 /{pat}/"
        if self.allow:
            for pat in self.allow:
                if _matches(pat, command):
                    return None
            return "未命中任何允许规则（该主机处于白名单模式）"
        return None

    def check_transfer(self, direction: str, remote_path: str) -> str | None:
        """校验 put / get 的远端路径。返回 None 表示放行。

        默认关闭：主机一旦配了命令白名单（`allow`），文件通道就必须
        显式用 `allow_put` / `allow_get` 打开。否则「只允许 df -h 的机器」
        仍然可以 `get /etc/shadow` —— 白名单等于没配。
        """
        is_put = direction == "put"
        allow = self.allow_put if is_put else self.allow_get
        deny = self.deny_put if is_put else self.deny_get
        label = "上传" if is_put else "下载"

        for pat in deny:
            if re.search(pat, remote_path):
                return f"{label}命中禁止规则 /{pat}/"
        if allow:
            for pat in allow:
                if re.search(pat, remote_path):
                    return None
            return f"远端路径未命中任何允许规则（{label}处于白名单模式）"
        if self.allow:
            key = "allow_put" if is_put else "allow_get"
            return f"该主机启用了命令白名单，{label}默认关闭（需配置 {key}）"
        return None

    @property
    def active(self) -> bool:
        return bool(
            self.allow or self.deny
            or self.allow_put or self.allow_get
            or self.deny_put or self.deny_get
        )


@dataclass
class HostSettings:
    """主机级 settings 覆盖。未配置的字段一律用 Settings 默认值。

    只允许覆盖连接 / 执行 / 传输的四个数值参数 —— 「这台机器慢」是主机自身
    的属性，随主机条目走（用户级与项目级都行）；没有全局 [settings] 段，
    作者拍板不做共享（2026-11-17）。
    """
    connect_timeout: float | None = None
    command_timeout: float | None = None
    transfer_max_bytes: int | None = None
    transfer_timeout: float | None = None


@dataclass
class Host:
    name: str
    host: str
    user: str
    port: int = 22
    description: str = ""
    sudo: bool = False
    key_file: str | None = None
    # 允许命令里出现 shell 元字符（`;` `&&` `|` 重定向等）。默认关闭，
    # 见 Policy.check： 之后含元字符的命令默认拒绝。
    allow_shell: bool = False
    policy: Policy = field(default_factory=Policy)
    settings: HostSettings | None = None
    # 项目级共享密码：仅项目条目生效（凭据随生效条目走）；用户级
    # 条目里出现会被忽略并警告。永不为私钥让路——项目级不支持任何私钥。
    shared_password: str | None = None
    # 加密档：asshcli1:<团队ID>:<盐>:<密文>，密钥（团队口令）在各人
    # vault 的 teamkey:<团队ID> 条目里，不随 repo 走。与 shared_password
    # 互斥（并存 = 安全意图歧义，fail-closed）。
    shared_password_enc: str | None = None
    # 运行时来源标注："user" / "project" / "override"（同名时用户级
    # 生效）。由合并层填写，不序列化进 TOML。
    origin: str = "user"


@dataclass
class Settings:
    connect_timeout: float = 15.0
    command_timeout: float = 300.0
    # 单次 put / get 的字节上限与整体超时。默认 64 MiB / 300 秒：
    # 够传配置、日志、发布包，又不至于一次 get 把磁盘填满或挂到天荒地老。
    transfer_max_bytes: int = 64 * 1024 * 1024
    transfer_timeout: float = 300.0

    @property
    def known_hosts(self) -> Path:
        return paths.known_hosts_file()


# per-host settings 允许的四个键：前三项秒（正数），后一项字节（正整数）
_FLOAT_SETTING_KEYS = ("connect_timeout", "command_timeout", "transfer_timeout")


def _positive_setting(name: str, key: str, value: object,
                      path_label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StoreError(
            f"{path_label}：主机 {name} 的 settings.{key} 需要正数（秒），"
            f"收到 {value!r}")
    number = float(value)
    if number <= 0:
        raise StoreError(
            f"{path_label}：主机 {name} 的 settings.{key} 需要正数，收到 {value!r}")
    return number


def _parse_host_settings(name: str, raw_settings: object,
                         path_label: str) -> HostSettings | None:
    """解析 [hosts.<名称>.settings] 子表。未知键忽略（与主机条目宽松解析一致），
    已知键的类型 / 取值非法则 fail-closed：静默回落默认值会把笔误藏起来。"""
    if raw_settings is None:
        return None
    if not isinstance(raw_settings, dict):
        raise StoreError(
            f"{path_label}：主机 {name} 的 settings 需要是一个子表（[hosts.名字.settings]）")
    values: dict[str, float | int] = {}
    for key in _FLOAT_SETTING_KEYS:
        if key in raw_settings:
            values[key] = _positive_setting(name, key, raw_settings[key], path_label)
    if "transfer_max_bytes" in raw_settings:
        raw = raw_settings["transfer_max_bytes"]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise StoreError(
                f"{path_label}：主机 {name} 的 settings.transfer_max_bytes "
                f"需要正整数（字节），收到 {raw!r}")
        values["transfer_max_bytes"] = raw
    return HostSettings(**values)  # type: ignore[arg-type]


def _parse_shared_password(name: str, item: dict, *, is_project: bool,
                           path_label: str) -> str | None:
    """解析 shared_password。仅项目级生效；用户级出现则忽略并警告。

    空字符串视为未配置；非字符串 fail-closed —— `123456` 忘加引号是真实
    笔误，值得一条明确的错而不是静默变成另一种凭据。"""
    if "shared_password" not in item:
        return None
    value = item["shared_password"]
    if not is_project:
        _warn(f"{path_label} 的主机 {name} 含 shared_password 字段："
              "共享密码只对项目级清单有意义，该字段已忽略（用户级凭据"
              "请用 asshcli setpass 存入 vault）")
        return None
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise StoreError(
            f"{path_label}：主机 {name} 的 shared_password 需要字符串"
            f"（数字密码请加引号，如 \"123456\"），收到 {value!r}")
    return value


def _parse_shared_password_enc(name: str, item: dict, *, is_project: bool,
                               path_label: str) -> str | None:
    """解析 shared_password_enc。仅项目级生效；格式坏 fail-closed。

    这里只做结构校验（防笔误）；解密在凭据解析时进行（需要团队口令）。"""
    if "shared_password_enc" not in item:
        return None
    value = item["shared_password_enc"]
    if not is_project:
        _warn(f"{path_label} 的主机 {name} 含 shared_password_enc 字段："
              "加密共享只对项目级清单有意义，该字段已忽略")
        return None
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise StoreError(
            f"{path_label}：主机 {name} 的 shared_password_enc 需要字符串"
            f"密文（用 manage sharepw set 生成），收到 {value!r}")
    from . import vault as vault_mod
    try:
        vault_mod.parse_blob(value)
    except vault_mod.VaultError as exc:
        raise StoreError(
            f"{path_label}：主机 {name} 的 shared_password_enc 无效：{exc}") from None
    return value


def effective_settings(host: Host) -> Settings:
    """主机的生效 settings：默认值逐字段被主机覆盖。"""
    result = Settings()
    override = host.settings
    if override is None:
        return result
    if override.connect_timeout is not None:
        result.connect_timeout = override.connect_timeout
    if override.command_timeout is not None:
        result.command_timeout = override.command_timeout
    if override.transfer_max_bytes is not None:
        result.transfer_max_bytes = override.transfer_max_bytes
    if override.transfer_timeout is not None:
        result.transfer_timeout = override.transfer_timeout
    return result


def load() -> dict[str, Host]:
    """读合并后的主机清单（用户级 + 项目级）。

    密码库已解锁时会先验用户级清单的完整性签名，失败抛 StoreError
    （fail-closed）：宁可拒绝服务，也不按一份被改过的清单放行命令。
    签名只管用户级文件 —— 项目级清单不签名（git 是它的完整性层）。
    用户级文件不存在时照样合并项目级：纯团队成员的首跑路径正是这个形态。"""
    if paths.hosts_file().exists():
        _verify_integrity(_read_doc())
    return _merge(_user_hosts_unverified())


def load_unverified() -> dict[str, Host]:
    """不做完整性验签的合并读取，仅供 list / status / policy 这类只读展示。

    这些视图的存在意义之一就是让用户在被改动的清单上「看看改了什么」，
    所以不能被验签挡住。任何会触发连接或策略判定的路径禁止用本函数。
    """
    return _merge(_user_hosts_unverified())


def _user_hosts_unverified() -> dict[str, Host]:
    """只读用户级清单（不验签、不合并）。"""
    path = paths.hosts_file()
    if not path.exists():
        return {}
    try:
        text = _read_text_nobom(path)
    except OSError as exc:
        raise StoreError(f"hosts.toml 读取失败：{exc}") from None
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        # 旧版本把中文别名写成了裸键，整份清单会解析不了。
        # 先按老格式修一遍再试，这样升级后不必手工改文件。
        try:
            raw = tomllib.loads(_heal_legacy(text))
        except tomllib.TOMLDecodeError:
            raise StoreError(f"hosts.toml 解析失败：{exc}") from None
    return _hosts_from_doc(raw, origin="user", path_label=str(path))


def load_project() -> dict[str, Host]:
    """只读项目级清单。不验签；解析失败 fail-closed。

    项目级清单随 git 走、由人手编：git 历史 / PR 评审是它的完整性层，
    每人不同的 HMAC 密钥签名不了共享文件（设计定案）。[asshcli_integrity]
    段只可能出现在用户级文件里，这里天然不会被读取（只取 [hosts] 表）。"""
    path = paths.project_hosts_file()
    if path is None:
        return {}
    try:
        text = _read_text_nobom(path)
    except OSError as exc:
        raise StoreError(f"项目清单 {path} 读取失败：{exc}") from None
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        try:
            raw = tomllib.loads(_heal_legacy(text))
        except tomllib.TOMLDecodeError as exc:
            raise StoreError(f"项目清单 {path} 解析失败：{exc}") from None
    return _hosts_from_doc(raw, origin="project", path_label=str(path),
                           warn_password=True)


def _merge(user_hosts: dict[str, Host]) -> dict[str, Host]:
    """两级清单合并：同名主机用户级整条遮蔽项目级（含 policy / settings）。

    作者拍板（2026-11-17）：个人的可随意修改、修改成本最低，所以个人优先。
    遮蔽是整条的 —— 不做字段级合并、不做 deny 并集：可预测性优先，
    「谁生效」永远能用 list 的来源列一眼看清。"""
    project = load_project()
    for name, host in user_hosts.items():
        host.origin = "override" if name in project else "user"
    merged = dict(project)
    merged.update(user_hosts)
    return merged


def _hosts_from_doc(raw: dict, *, origin: str, path_label: str,
                    warn_password: bool = False) -> dict[str, Host]:
    """从 TOML 文档抽主机表（两级清单共用）。字段级客容与原实现一致。"""
    hosts: dict[str, Host] = {}
    for name, item in (raw.get("hosts") or {}).items():
        if not isinstance(item, dict):
            continue
        if not item.get("host") or not item.get("user"):
            continue
        if warn_password and "password" in item:
            # 项目清单要提交进 git：有人误放密码时必须当场说话。
            # 不中断加载 —— 该字段本来就不参与解析，不构成威胁。
            _warn(f"{path_label} 的主机 {name} 含 password 字段：密码不入清单"
                  "（永远在各自的 vault 里），该字段不会生效；"
                  "要共享请改用 shared_password")
        pol = item.get("policy") or {}
        shared_pw = _parse_shared_password(
            name, item, is_project=(origin == "project"), path_label=path_label)
        shared_enc = _parse_shared_password_enc(
            name, item, is_project=(origin == "project"), path_label=path_label)
        if shared_pw and shared_enc:
            raise StoreError(
                f"{path_label}：主机 {name} 同时配置了 shared_password 与"
                " shared_password_enc —— 安全意图歧义，只留一个")
        hosts[name] = Host(
            name=name,
            host=str(item["host"]),
            user=str(item["user"]),
            port=int(item.get("port", 22)),
            description=str(item.get("description", "")),
            sudo=bool(item.get("sudo", False)),
            key_file=item.get("key_file") or None,
            allow_shell=bool(item.get("allow_shell", False)),
            policy=Policy(
                allow=[str(x) for x in (pol.get("allow") or [])],
                deny=[str(x) for x in (pol.get("deny") or [])],
                allow_put=[str(x) for x in (pol.get("allow_put") or [])],
                allow_get=[str(x) for x in (pol.get("allow_get") or [])],
                deny_put=[str(x) for x in (pol.get("deny_put") or [])],
                deny_get=[str(x) for x in (pol.get("deny_get") or [])],
            ),
            settings=_parse_host_settings(name, item.get("settings"), path_label),
            shared_password=shared_pw,
            shared_password_enc=shared_enc,
            origin=origin,
        )
    return hosts


def get(name: str) -> Host | None:
    return load().get(name)


def _read_doc() -> dict:
    """读整份原始 TOML 文档（不清洗、保留未知字段），供写路径使用。

    解析不了先按旧格式修一遍（的遗留文件），仍然失败就抛 StoreError
    （fail-closed）：绝不基于半份清单写入，那比不写更糟。
    """
    path = paths.hosts_file()
    if not path.exists():
        return {}
    text = _read_text_nobom(path)
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        try:
            return tomllib.loads(_heal_legacy(text))
        except tomllib.TOMLDecodeError as exc:
            raise StoreError(f"hosts.toml 解析失败：{exc}") from None


def _write_doc(doc: dict, *, force_seal: bool = False) -> None:
    """整份规范重排落盘：tomli-w 统一序列化 + 完整性签名 + 原子替换。

    签名续签规则（防洗白）：
    - 文件尚不存在 → 直接签名（首台主机的正常路径）
    - 原有文件带**有效**签名 → 重签（向导加主机、迁移都会自动续签）
    - 原有文件没有签名或签名已失效 → **不签名**照写：一次普通写入不能把
      被篡改或遗留未签的清单洗成「已验证」状态；强制路径依旧 fail-closed，
      直到用户检查内容后显式 reseal。
    `force_seal` 仅供 manage reseal（用户本人检查过内容后的显式确认）。
    """
    try:
        import tomli_w
    except ImportError as exc:  # pragma: no cover - 运行时会装好依赖
        raise StoreError(
            "缺少 tomli-w（写 hosts.toml 需要）。请运行 asshcli manage setup "
            "重建运行环境，或 pip install tomli-w"
        ) from exc
    path = paths.hosts_file()
    key = integrity.mac_key()
    if force_seal:
        if key is None:
            raise StoreError("密码库未解锁，无法生成 hosts.toml 的完整性签名")
        doc = integrity.seal_doc(doc, key)
    elif not path.exists():
        # 全新清单：没有历史可洗白，直接进入签名状态
        if key is not None:
            doc = integrity.seal_doc(doc, key)
    else:
        old = _read_doc()
        if integrity.SECTION in old:
            # 已带签名段的文件：必须持钥重签。所有合法写路径（向导 / 迁移 /
            # reseal）都先解锁了密码库；没持钥就写，唯一结局是把签名弄成
            # 陈旧值让下一次连接 fail-closed —— 不如现在就拒绝，把话说清楚。
            if key is None:
                raise StoreError(
                    "hosts.toml 带完整性签名，写入需要解锁密码库来重新签名；"
                    "请使用 asshcli 的管理命令操作，而不是直接调内部写函数"
                )
            if integrity.verify_doc(old, key) is None:
                doc = integrity.seal_doc(doc, key)
            # 原有签名已失效 → 不签名照写：普通写入不能把被篡改或遗留
            # 未签的清单洗成「已验证」状态，强制路径依旧 fail-closed，
            # 直到用户检查内容后显式 reseal。
        elif key is not None and not old.get("hosts"):
            # 空清单的首次写入（升级前的遗留空文件）：没有可保护的内容，
            # 直接进入签名状态
            doc = integrity.seal_doc(doc, key)
    atomic_write_text(path, HEADER + tomli_w.dumps(doc), mode=0o600)


def reseal() -> None:
    """给当前 hosts.toml 重新签名（manage reseal，用户本人执行）。

    前提：密码库已解锁 —— 签名密钥从主密码派生。文件解析失败抛 StoreError，
    绝不基于半份清单签名。签名覆盖当前文件的全部内容，所以必须在用户
    检查过内容之后调用。
    """
    with data_lock():
        doc = _read_doc()
        _write_doc(doc, force_seal=True)


def _same_effective(a: Host, b: Host) -> bool:
    """两台主机的可序列化字段是否完全一致（name / origin 除外）。"""
    return (a.host == b.host and a.user == b.user and a.port == b.port
            and a.description == b.description and a.sudo == b.sudo
            and a.allow_shell == b.allow_shell and a.key_file == b.key_file
            and a.policy == b.policy and a.settings == b.settings
            and a.shared_password == b.shared_password
            and a.shared_password_enc == b.shared_password_enc)


def upsert_host(host: Host) -> None:
    """新增或更新一台主机（保留清单里的其它内容；policy / settings 子段
    由传入的 Host 对象携带时一并写入，否则原样保留）。

    写入本身在 data_lock 内完成，调用方**不需要**自己加锁。

    影子最小化：用户级清单还没有这台主机、且传入的全部字段与项目级
    条目完全一致时，跳过用户级写入 —— 「给项目主机补个密码」不该悄悄冻结
    一份团队地址副本（否则项目清单日后更新，本机会一直连旧机器）。字段
    只要有一处不同照常写入：那是用户主动的偏离，遮蔽是故意的。
    """
    with data_lock():
        doc = _read_doc()
        hosts = doc.setdefault("hosts", {})
        if host.name not in hosts:
            project_host = load_project().get(host.name)
            if project_host is not None and _same_effective(host, project_host):
                return
        item = hosts.setdefault(host.name, {})
        item["host"] = host.host
        item["user"] = host.user
        item["port"] = host.port
        if host.description:
            item["description"] = host.description
        else:
            item.pop("description", None)
        if host.sudo:
            item["sudo"] = True
        else:
            item.pop("sudo", None)
        if host.allow_shell:
            item["allow_shell"] = True
        else:
            item.pop("allow_shell", None)
        if host.key_file:
            item["key_file"] = host.key_file
        else:
            item.pop("key_file", None)
        # policy / settings 子段：传入对象携带非默认值时一并写入（向导从
        # 合并视图 replace 出来的 Host 会带着项目的子段，影子不丢字段）；
        # 否则不动文件里的既有子段（set_policy 专门负责 policy 的改写）
        if host.policy.active:
            rules = {
                "allow": host.policy.allow, "deny": host.policy.deny,
                "allow_put": host.policy.allow_put,
                "allow_get": host.policy.allow_get,
                "deny_put": host.policy.deny_put,
                "deny_get": host.policy.deny_get,
            }
            item["policy"] = {k: v for k, v in rules.items() if v}
        if host.settings is not None:
            values = {
                "connect_timeout": host.settings.connect_timeout,
                "command_timeout": host.settings.command_timeout,
                "transfer_max_bytes": host.settings.transfer_max_bytes,
                "transfer_timeout": host.settings.transfer_timeout,
            }
            item["settings"] = {k: v for k, v in values.items() if v is not None}
        _write_doc(doc)


def remove_host(name: str) -> bool:
    """从用户级清单删除一台主机（项目级条目不归本地删）。"""
    with data_lock():
        if not paths.hosts_file().exists():
            return False
        doc = _read_doc()
        hosts = doc.get("hosts") or {}
        if name not in hosts:
            return False
        del hosts[name]
        _write_doc(doc)
        return True


def rename_host(old: str, new: str) -> None:
    """用户级清单里把一台主机改名（policy / settings 子段随条目整体移动）。

    只管用户级文件：项目级条目随 git 走，改名请走 git。写入自动续签
    （原有签名有效且密码库已解锁时）；签名已失效则拒绝 —— 改名是普通
    写入，不能顺手把一份被改过的清单洗成「已验证」状态，先检查内容、
    `manage reseal`，再来改名。vault 里的凭据条目由 CLI 层随迁。
    """
    with data_lock():
        if not paths.hosts_file().exists():
            raise StoreError("还没有 hosts.toml，没有可改名的主机")
        doc = _read_doc()
        hosts = doc.get("hosts") or {}
        if old not in hosts:
            raise _no_user_entry_error(old)
        if new in hosts:
            raise StoreError(f"用户级清单里已有名为 {new} 的主机")
        if integrity.SECTION in doc:
            key = integrity.mac_key()
            if key is None:
                raise StoreError(
                    "hosts.toml 带完整性签名，改名重签需要解锁密码库；"
                    "请使用 asshcli 的管理命令操作，而不是直接调内部写函数")
            problem = integrity.verify_doc(doc, key)
            if problem is not None:
                raise StoreError(
                    f"{problem}。\n改名不会把被改动的清单洗成「已验证」状态："
                    "先检查内容并执行 asshcli manage reseal，再来改名")
        hosts[new] = hosts.pop(old)
        _write_doc(doc)


def upsert_project_host(host: Host, manifest: Path,
                        *, shared_enc: str | None = None) -> None:
    """向项目清单写入一台主机（config 向导的「项目级」作用域）。

    manifest 由调用方显式传入（向导里用户确认过的项目根），不走
    paths.project_hosts_file() 的全局发现 —— 弹窗向导的 cwd 不可靠，
    而 --here 模式下也不应为了写文件去改动进程级 override。

    - data_lock 内完成，调用方不需要自己加锁
    - 文件不存在 → 新建（带 PROJECT_HEADER 使用说明）
    - 存在 → 整份读入（保留未知字段与该节 policy / settings / 自定义
      字段），只覆写 host / user / port / description 四个向导问过的键，
      再 tomli-w 规范重排（注释会丢 —— 与用户级方案 B 同款代价）
    - shared_enc 非空时写 shared_password_enc 并清掉旧明文 shared_password
      （两字段并存会被读侧以「安全意图歧义」拒绝）；None 则不动共享字段
    - 解析失败 fail-closed：抛 StoreError，绝不基于半份清单写入
    - 不写 [asshcli_integrity]（项目清单不签名，git / PR 是它的完整性层），
      不碰 vault
    """
    try:
        import tomli_w
    except ImportError as exc:  # pragma: no cover - 安装时带装齐
        raise StoreError(
            "缺少 tomli-w，写项目清单需要它。请运行 asshcli manage setup 重建"
            "运行环境，或 pip install tomli-w"
        ) from exc
    with data_lock():
        doc: dict = {}
        if manifest.exists():
            text = _read_text_nobom(manifest)
            try:
                doc = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                try:
                    doc = tomllib.loads(_heal_legacy(text))
                except tomllib.TOMLDecodeError as exc:
                    raise StoreError(
                        f"项目清单 {manifest} 解析失败：{exc}（已保留原样，未写入）"
                    ) from None
        hosts = doc.setdefault("hosts", {})
        item = hosts.setdefault(host.name, {})
        item["host"] = host.host
        item["user"] = host.user
        item["port"] = host.port
        if host.description:
            item["description"] = host.description
        else:
            item.pop("description", None)
        if shared_enc:
            item["shared_password_enc"] = shared_enc
            item.pop("shared_password", None)
        atomic_write_text(manifest, PROJECT_HEADER + tomli_w.dumps(doc))


def _no_user_entry_error(name: str) -> StoreError:
    """用户级清单里没有这台主机时的报错（区分项目级来源）。"""
    project_path = paths.project_hosts_file()
    if project_path is not None:
        return StoreError(
            f"主机 {name} 不在用户级清单里（可能来自项目清单 {project_path}）："
            "项目主机的策略请直接编辑项目文件，随 git 与团队共享；"
            "要在本机覆盖它，先在用户级清单里定义同名主机")
    return StoreError(f"主机 {name} 不存在")


def set_policy(name: str, allow: list[str], deny: list[str], *,
               allow_put: list[str] | None = None,
               allow_get: list[str] | None = None,
               deny_put: list[str] | None = None,
               deny_get: list[str] | None = None) -> None:
    """覆盖写入用户级清单的 [hosts.<name>.policy] 段；全部为空则删除该段。

    文件通道的四条规则是关键字参数，老调用方（只传 allow / deny）不受影响。
    """
    with data_lock():
        if not paths.hosts_file().exists():
            raise _no_user_entry_error(name)
        doc = _read_doc()
        hosts = doc.get("hosts") or {}
        if name not in hosts:
            raise _no_user_entry_error(name)
        rules = {
            "allow": allow, "deny": deny,
            "allow_put": allow_put or [], "allow_get": allow_get or [],
            "deny_put": deny_put or [], "deny_get": deny_get or [],
        }
        pol = {k: v for k, v in rules.items() if v}
        if pol:
            hosts[name]["policy"] = pol
        else:
            hosts[name].pop("policy", None)
        _write_doc(doc)
