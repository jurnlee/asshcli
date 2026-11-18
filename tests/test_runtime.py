"""lib.runtime：解释器管理卫生——不再用 eval、基础解释器按版本探测。

对应 docs/DESIGN.md P1（代码卫生）：
- 版本探测不再用 eval；本解释器通过、不存在的解释器返回 False
- 基础解释器按版本从新到旧动态探测（3.15..3.9），不再硬编码 py -3.14
- cmd_run 的死防御分支（`if vault_obj else None`）已删，
  vault 取值收口到 _resolve_credentials

运行：
    python tests/test_runtime.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common  # noqa: E402  先注入路径、设环境，再 import scripts 下的模块

TMP = _common.init_data_dir("asshcli-runtime-")

from _common import check, finish  # noqa: E402


def test_code_hygiene() -> None:
    """P1：runtime 的 eval 清除与探测规则；cmd_run 死防御删除。"""
    import inspect

    from lib import runtime

    print("\n== 版本探测不再用 eval ==")
    src = inspect.getsource(runtime)
    check("runtime 里没有 eval(", "eval(" not in src,
          "仍存在 eval 调用")
    check("本解释器版本探测通过",
          runtime._python_version_ok(sys.executable) is True)
    check("不存在的解释器返回 False",
          runtime._python_version_ok(str(TMP / "no-such-python.exe")) is False)

    print("\n== 基础解释器按版本从新到旧探测 ==")
    find_src = inspect.getsource(runtime.find_base_python)
    check("动态探测 3.15..3.9", "range(15, 8, -1)" in find_src, find_src[:200])
    check("不再硬编码 py -3.14", '"py", "-3.14"' not in find_src)

    print("\n== cmd_run 死防御分支已删 ==")
    import asshcli as cli

    run_src = inspect.getsource(cli.cmd_run)
    check("不再有 `if vault_obj else None`", "if vault_obj else None" not in run_src)
    check("vault 直取已收口到 _resolve_credentials",
          "_resolve_credentials(" in run_src
          and "vault_obj.get(" not in run_src)


def main() -> int:
    test_code_hygiene()
    return finish()


if __name__ == "__main__":
    sys.exit(main())
