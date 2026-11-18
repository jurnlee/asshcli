# tests/ 目录规范

测试按**功能模块**组织：一个模块（或一条功能线）一个文件，不再按优先级
（P0/P1/P2）堆成大文件。issue 归属写进每个测试函数的 docstring，与
`docs/DESIGN.md` 的条目一一对应。

## 文件清单与被测模块

| 文件 | 被测模块 | 覆盖的 issue |
| --- | --- | --- |
| `test_policy.py` | `lib.store`（Policy） | P0 策略语义与绕过、P1 I6 test 策略门禁 |
| `test_redact.py` | `lib.redact` | P1 `pwd=` 不误伤、I9 阈值与 vault 全量值 |
| `test_audit.py` | `lib.audit` + CLI 参数 | P0 argv 不落盘 / caller 删除 / --stdin 门禁、P1 分流 / tail / 轮转 / auth 字段 |
| `test_vault.py` | `lib.vault` | P1 S7 AAD、S8 临时文件、headless gpg 后端、`key:<host>` 命名空间 |
| `test_transport.py` | `lib.transport` | P1 --sudo+--stdin 顺序、P0 known_hosts、P1 S6 TOFU 状态行、P0 传输上限 |
| `test_protocol.py` | `lib.protocol` | P1 nonce 防伪造 |
| `test_output.py` | `lib.output` | P0 输出上限 / --tail / 多字节 / 审计字段 |
| `test_console.py` | `console_setup` + `lib.console` | P0 setpass 保字段、P1 无窗口指引 / status headless 指引、项目级向导（作用域 / 项目根 / 可留空密码） |
| `test_integrity.py` | `lib.integrity` + `lib.store` 写路径 | P2 签名 / 篡改 / reseal / CLI 强制路径、P1 方案 B 规范重排 |
| `test_sshkeys.py` | `lib.sshkeys` | P2 keygen / keyimport / keyshow、密钥优先与回落留痕 |
| `test_manage.py` | CLI `manage` 分发 | P1 子命令分发、别名不破坏；rename 改名与凭据随迁 |
| `test_harden.py` | CLI `harden --check` | P1 加固只读探测 |
| `test_runtime.py` | `lib.runtime` | P1 eval 清除、版本探测、死防御删除 |
| `test_paths.py` | `lib.paths` | P2 ASSHCLI_DATA_DIR 门禁 |

其余文件：`test_flow.py`（端到端流程）、`test_lock.py`（并发锁）、
`test_reexec.py`（自举重 exec）、`test_store.py`（hosts.toml 读写）、
`test_project.py`（项目级清单：发现 / 合并 / 共享密码 / 向导写路径）、
`test_docs.py`（文档一致性）。

## 文件结构模板

```python
"""<模块>：<一句话职责>。

对应 docs/DESIGN.md（每段一个）：
- <P0/P1/P2 · issue 描述>

运行：
    python tests/test_<module>.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common  # noqa: E402  先注入路径、设环境，再 import scripts 下的模块

TMP = _common.init_data_dir("asshcli-<module>-")

from _common import check, finish, reset, run_cli  # noqa: E402
from lib import ...  # noqa: E402


def test_xxx() -> None:
    """P?:<issue 描述>。"""

    print("\n== 分节标题 ==")
    check("断言名", 条件, "失败时的细节")


def main() -> int:
    test_xxx()
    return finish()


if __name__ == "__main__":
    sys.exit(main())
```

## 规则

1. **一条功能线一个文件**：新增 issue 回归时，加到对应模块的文件里，
   不再新开 `test_pN.py` 这类按优先级命名的文件。
2. **环境隔离**：`ASSHCLI_DATA_DIR` + `ASSHCLI_TEST_MODE` 由
   `_common.init_data_dir()` 统一设置；它必须在 import scripts/lib 之前调用。
   每个文件独立进程运行，互不共享状态；需要主密码的文件（integrity /
   sshkeys）在 init 之后自行 `os.environ["ASSHCLI_MASTER"] = MASTER`。
3. **断言收口**：只用 `_common.check(name, cond, detail)` 计数；
   结果汇总与退出码由 `_common.finish()` 给出（失败退出码 1，CI 直接标红）。
4. **分节输出**：函数内用 `print("\n== 小节 ==")` 分节，输出保持
   `  [ok]` / `  [FAIL]` 两列格式。
5. **测试间隔离**：跨用例用 `reset()` 清数据目录，不假设别的用例留下的
   文件或环境变量；用完的环境变量（如 `ASSHCLI_MASTER`）要恢复。
6. **main() 显式列序**：文件内测试的执行顺序在 `main()` 里一目了然。
7. **可独立运行**：`python tests/test_<module>.py` 即可单跑一个模块；
   CI（`.github/workflows/ci.yml`）逐文件执行。
8. **替身与桩**：假的 SSH/SFTP/Client 替身（`_Fake*` / `_ProbeClient` /
   `_ForgingClient` / `_AuthServer`）定义在使用它们的文件里，不跨文件复用。

## 历史对照

`test_p0.py` / `test_p1.py` / `test_p2.py` 已按上表拆分删除：
P0 的 6 组用例分散到 policy / audit / transport / output / console；
P1 的 15 组分散到 redact / audit / vault / transport / protocol /
console / integrity / manage / harden / runtime / policy；
P2 的 6 组分散到 integrity / paths / sshkeys。断言内容与名称未变。
