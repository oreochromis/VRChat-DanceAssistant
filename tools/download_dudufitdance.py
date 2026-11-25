"""DuDu FitDance 分类曲库下载器。

示例：
    python tools/download_dudufitdance.py --out "D:\\曲库\\DuDuFitDance"
    python tools/download_dudufitdance.py --out "D:\\曲库\\DuDuFitDance" --ids 2314 2325
    python tools/download_dudufitdance.py --out "D:\\曲库\\DuDuFitDance" --flat
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dance_ui.downloader import run_cli


if __name__ == "__main__":
    raise SystemExit(run_cli("dudu"))
