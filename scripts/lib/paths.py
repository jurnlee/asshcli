"""固定路径定义。

所有 agent 共用同一份数据目录 —— 这是「装多少份 skill 都只配一次密码」的关键。
路径写死在用户主目录下，与 skill 的安装位置无关，因此升级 / 重装 skill
不会覆盖配置。

凭据与个人数据（vault / known_hosts / audit）固定在用户级；主机**定义**与
策略可以另由项目级 `.asshcli/hosts.toml` 随 git 共享——发现逻辑见本文件
`project_hosts_file`，合并语义见 lib.store。

环境变量 ASSHCLI_DATA_DIR 仅在显式测试模式（ASSHCLI_TEST_MODE=1）下生效：
它指到别处就能绕过基于路径的本地保护（put/get 的数据目录约束、指纹库与
签名文件的落点等）。正常使用不要设置。
"""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_DATA_DIR = Path.home() / ".asshcli"

DATA_DIR_ENV = "ASSHCLI_DATA_DIR"
TEST_MODE_ENV = "ASSHCLI_TEST_MODE"


def data_dir() -> Path:
    """数据目录。

    `ASSHCLI_DATA_DIR` 只在 `ASSHCLI_TEST_MODE=1` 同时存在时生效；否则忽略该
    变量并回落默认目录。入口（asshcli.main / console_setup.main）会用
    `override_without_test_mode()` 检测这种组合并明确拒绝 —— 这里无声
    回落是为了让深层调用不至于拿到一个谁都没料到的目录。
    """
    override = os.environ.get(DATA_DIR_ENV)
    if override and os.environ.get(TEST_MODE_ENV) == "1":
        return Path(override)
    return _DEFAULT_DATA_DIR


def override_without_test_mode() -> str | None:
    """ASSHCLI_DATA_DIR 被设置但不在测试模式 → 返回拒绝理由；否则 None。

    拒绝而不是无声忽略：无声忽略会让「以为换了目录」的调用方把
    数据写到默认目录里，制造两份半重叠的配置，比明确报错难查得多。
    """
    if os.environ.get(DATA_DIR_ENV) and os.environ.get(TEST_MODE_ENV) != "1":
        return (
            f"{DATA_DIR_ENV} 只在测试模式（{TEST_MODE_ENV}=1）下生效："
            "它能改变凭据与保护机制的落点，正常使用不应设置。\n"
            "如果这确实是测试环境，请同时设置 ASSHCLI_TEST_MODE=1 后重试。"
        )
    return None


def vault_file() -> Path:
    return data_dir() / "vault.enc"


def hosts_file() -> Path:
    return data_dir() / "hosts.toml"


def audit_file() -> Path:
    return data_dir() / "audit.log"


def lock_file() -> Path:
    return data_dir() / ".lock"


def bootstrap_lock_file() -> Path:
    """运行环境自举专用的锁。

    与 `.lock` 分开：首次启动要建 venv + 装依赖，可能占用几分钟，不能让期间
    其它 agent 的读写操作陪着一起等。
    """
    return data_dir() / ".bootstrap"


def no_window_marker() -> Path:
    """标记文件：这个环境里弹窗撑不住（命令一结束就被宿主回收）。

    出现这个文件说明之前交接出去的窗口已经死了、配置却没完成，
    之后不再尝试弹窗，直接引导用户在自己的终端里配置。
    """
    return data_dir() / ".no-window"


def venv_dir() -> Path:
    return data_dir() / "venv"


def venv_python() -> Path:
    v = venv_dir()
    if os.name == "nt":
        return v / "Scripts" / "python.exe"
    return v / "bin" / "python"


def known_hosts_file() -> Path:
    return data_dir() / "known_hosts"


# --------------------------------------------------------------------------
# 项目级清单（团队共享）
# --------------------------------------------------------------------------

PROJECT_DIR_NAME = ".asshcli"
PROJECT_HOSTS_NAME = "hosts.toml"

# 进程内覆盖：正常使用为 None（发现纯靠 cwd 向上走）。两条设置路径：
#   1. 配置向导子进程 —— 父进程把解析好的项目清单路径放进 payload 传过来
#      （explorer 拉起的窗口 cwd 落在 ~/.asshcli/.setup-*，自己发现必错，见 §5.5）
#   2. 测试 —— 直接钉住路径，免得依赖测试进程的 cwd
# 不是环境变量：多一个 env 就多一个绕过面（的门禁哲学）。
_project_override: Path | None = None


def set_project_override(value: str | Path | None) -> None:
    """钉住项目清单路径；空值表示「此进程明确没有项目清单」。"""
    global _project_override
    if value is None or str(value) == "":
        _project_override = None
        return
    _project_override = Path(str(value))


def _norm_path(p: Path) -> str:
    """归一成可比较的字符串：解析符号链接、Windows 统一大小写。"""
    return os.path.normcase(os.path.realpath(str(p)))


def project_hosts_file() -> Path | None:
    """当前目录所在项目的清单文件；不在任何项目里时返回 None。

    从 cwd（含自身）逐级向上找 `<目录>/.asshcli/hosts.toml`，**最近的赢**：
    monorepo 嵌套时内层项目优先。有一个必须跳过的坑：走到用户主目录时，
    `~/.asshcli/hosts.toml` 恰好长成项目清单的样子——那是**用户级**清单的
    固定位置，当作项目清单再合并一遍纯属意外（ASSHCLI_DATA_DIR 被测试
    改写到别处时，主目录自己的清单也在祖先链上——同样必须排除）。
    """
    if _project_override is not None:
        return _project_override if _project_override.exists() else None
    try:
        cwd = Path.cwd()
    except OSError:
        return None
    user_hosts = _norm_path(hosts_file())
    home_store = _norm_path(Path.home() / PROJECT_DIR_NAME / PROJECT_HOSTS_NAME)
    for directory in (cwd, *cwd.parents):
        candidate = directory / PROJECT_DIR_NAME / PROJECT_HOSTS_NAME
        if _norm_path(candidate) in (user_hosts, home_store):
            continue
        if candidate.exists():
            return candidate
    return None


def ensure_data_dir() -> Path:
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


KEYRING_SERVICE = "asshcli"

# 配置窗口等待用户输入的默认上限（秒）
DEFAULT_SETUP_TIMEOUT = 180
MIN_SETUP_TIMEOUT = 30
MAX_SETUP_TIMEOUT = 600
