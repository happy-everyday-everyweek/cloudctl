"""源码运行入口（未打包时用）。

    python run_agent.py --run

打包后的 exe 直接用 pyinstaller 生成的入口，不需要本文件。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cloudctl_agent.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
