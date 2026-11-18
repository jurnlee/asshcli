"""lib.output：远端输出整形——大小上限、--tail 与审计字段。

对应 docs/DESIGN.md P0（每段一个）：
- 默认上限 + 头尾保留；--max-bytes 0 = 不限制
- --tail 只回传最后 N 行；行数不够时不插提示
- 多字节字符不被切坏
- 截断信息写进审计（truncated / bytes）

运行：
    python tests/test_output.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common  # noqa: E402  先注入路径、设环境，再 import scripts 下的模块

TMP = _common.init_data_dir("asshcli-output-")

from _common import check, finish, reset  # noqa: E402


def test_output_limit() -> None:
    """P0：远端输出直接进调用方上下文，必须有默认上限。"""

    from lib import output

    print("\n== 默认上限与头尾保留 ==")
    text = "line\n" * 100  # 500 字节
    out, info = output.shape(text, max_bytes=50)
    check("超过上限会截断", info["truncated"] is True, str(info))
    check("保留开头", out.startswith("line\n"), out[:20])
    check("保留结尾", out.endswith("line\n"), out[-20:])
    check("提示里有原始字节数", f"原始 {len(text.encode('utf-8'))} 字节" in out, out)
    check("原始字节数统计正确",
          info["original_bytes"] == len(text.encode("utf-8")), str(info))

    out, info = output.shape("abc", max_bytes=50)
    check("没超就原样返回", out == "abc" and info["truncated"] is False, str(info))
    check("空输出不插提示", output.shape("", max_bytes=50)[0] == "")

    out, info = output.shape(text, max_bytes=0)
    check("--max-bytes 0 = 不限制", out == text and info["truncated"] is False)

    print("\n== --tail ==")
    out, info = output.shape(text, max_bytes=50, tail_lines=3)
    check("只回传最后 3 行", out.count("line") == 3, out)
    check("顺序保留", out.endswith("line\nline\nline\n"), out[-20:])
    check("提示说明丢了前面的",
          "只回传最后 3 行" in out and "原本 100 行" in out, out.strip()[:60])
    check("tail 统计标记截断", info["truncated"] is True)

    out, info = output.shape("only\ntwo\n", tail_lines=5)
    check("行数不够时不插提示", out == "only\ntwo\n" and info["truncated"] is False)

    print("\n== 多字节字符不被切坏 ==")
    cn = "中文内容测试" * 200
    out, _ = output.shape(cn, max_bytes=61)
    check("不留乱码替换字符", "\ufffd" not in out, out[-30:])
    check("仍是合法 UTF-8", out.encode("utf-8").decode("utf-8") is not None)

    print("\n== 审计字段 ==")
    from lib import audit

    reset()
    audit.write("run", host="h", command="cat big", truncated=True,
                original_bytes=123456)
    rec = audit.tail(1)[0]
    check("审计带 truncated", rec.get("truncated") is True, str(rec))
    check("审计带原始字节数", rec.get("bytes") == 123456, str(rec))

    import inspect

    run_src = inspect.getsource(__import__("asshcli").cmd_run)
    check("run 用同一套整形处理 stdout 与 stderr",
          run_src.count("output.shape(") == 2, run_src[:200])
    check("截断信息写进审计", "truncated=" in run_src)


def main() -> int:
    test_output_limit()
    return finish()


if __name__ == "__main__":
    sys.exit(main())
