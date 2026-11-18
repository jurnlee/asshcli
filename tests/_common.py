"""测试公共设施：路径注入、数据目录隔离、断言计数与 CLI 调用助手。

本文件不是测试，不单独运行；其它测试文件 import 它（见 tests/README.md 的
「文件结构模板」）。约定：

- 每个测试文件按功能模块命名（test_<模块>.py），一条功能线一个文件；
- 测试文件必须在 import scripts/lib 之前调用 init_data_dir()——
  数据目录的环境变量要在任何数据访问发生前就位；
- 断言一律用 check(name, cond, detail)，计数收口在本模块，
  main() 末尾调 finish() 清理临时目录并给出退出码。
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

TESTS_DIR = Path(__file__).resolve().parent

# 数据目录：init_data_dir() 重建并注入环境变量；reset() 清空重用。
DATA_DIR: Path = Path(tempfile.mkdtemp(prefix="asshcli-test-"))

PASSED = 0
FAILED = 0


def init_data_dir(prefix: str) -> Path:
    """每个测试文件 import 后立即调用：隔离的数据目录 + 测试模式。

    必须在 import scripts 下的 lib / CLI 模块之前调用。返回数据目录，
    测试文件通常写成 `TMP = _common.init_data_dir("asshcli-<模块>-")`。
    """
    global DATA_DIR
    DATA_DIR = Path(tempfile.mkdtemp(prefix=prefix))
    os.environ["ASSHCLI_DATA_DIR"] = str(DATA_DIR)
    os.environ["ASSHCLI_TEST_MODE"] = "1"  # ASSHCLI_DATA_DIR 仅测试模式生效
    os.environ.pop("ASSHCLI_MASTER", None)
    return DATA_DIR


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [ok] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f"  -> {detail}" if detail else ""))


def reset() -> None:
    """清空数据目录。测试之间用它隔离，不假设别的用例留下的状态。"""
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def run_cli(args: list[str]) -> tuple[int, str, str]:
    """进程内调用 CLI 入口，返回 (退出码, stdout, stderr)。"""
    import asshcli as cli

    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(args)
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


def finish() -> int:
    """main() 的统一收尾：清理临时目录、汇总、给出退出码。"""
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    print("\n" + "=" * 46)
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    return 0 if FAILED == 0 else 1
