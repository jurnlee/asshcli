"""lib.paths：ASSHCLI_DATA_DIR 门禁——仅测试模式生效。

对应 docs/DESIGN.md P2（每段一个）：
- ASSHCLI_DATA_DIR 无测试模式时 CLI 明确拒绝（退出码 3），拒绝发生在
  任何数据访问之前；测试模式下覆盖生效
- console_setup 入口同样拒绝，拒绝理由进 stderr

运行：
    python tests/test_paths.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common  # noqa: E402  先注入路径、设环境，再 import scripts 下的模块

TMP = _common.init_data_dir("asshcli-paths-")

from _common import SCRIPTS, check, finish, run_cli  # noqa: E402


def test_data_dir_gating() -> None:
    """P2：ASSHCLI_DATA_DIR 仅测试模式生效（CLI 与向导入口双门禁）。"""
    print("\n== ASSHCLI_DATA_DIR 无测试模式时拒绝 ==")
    saved_dir = os.environ.get("ASSHCLI_DATA_DIR")
    saved_mode = os.environ.get("ASSHCLI_TEST_MODE")
    try:
        os.environ["ASSHCLI_DATA_DIR"] = str(TMP / "elsewhere")
        os.environ.pop("ASSHCLI_TEST_MODE", None)
        code, out, err = run_cli(["status"])
        check("CLI 明确拒绝（退出码 3）", code == 3, f"{code} {out}{err}")
        check("报错说明测试模式", "ASSHCLI_TEST_MODE" in err, err)
        check("拒绝发生在任何数据访问之前", "数据目录" not in out, out)

        print("\n== 测试模式下覆盖生效 ==")
        os.environ["ASSHCLI_TEST_MODE"] = "1"
        code, out, err = run_cli(["status"])
        check("status 正常运行", code == 0, f"{out}{err}")
        check("数据目录就是覆盖值", str(TMP) in out, out)
    finally:
        if saved_dir is not None:
            os.environ["ASSHCLI_DATA_DIR"] = saved_dir
        else:
            os.environ.pop("ASSHCLI_DATA_DIR", None)
        if saved_mode is not None:
            os.environ["ASSHCLI_TEST_MODE"] = saved_mode
        else:
            os.environ.pop("ASSHCLI_TEST_MODE", None)

    print("\n== console_setup 入口同样拒绝 ==")
    worker = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import console_setup; sys.exit(console_setup.main())"
    )
    env = dict(os.environ)
    env["ASSHCLI_DATA_DIR"] = str(TMP / "elsewhere")
    env.pop("ASSHCLI_TEST_MODE", None)
    proc = subprocess.run(
        [sys.executable, "-I", "-c", worker, str(SCRIPTS)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, env=env,
    )
    check("向导入口退出码 3", proc.returncode == 3,
          f"{proc.stdout[-200:]}{proc.stderr[-200:]}")
    check("拒绝理由进 stderr", "ASSHCLI_TEST_MODE" in proc.stderr,
          proc.stderr[-300:])


def main() -> int:
    test_data_dir_gating()
    return finish()


if __name__ == "__main__":
    sys.exit(main())
