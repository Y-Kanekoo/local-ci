"""正当化・回避策コメントの検出（テスト標準の static 段階）。標準ライブラリのみ。

コメントに残された回避策・暫定対応の宣言（例: 「とりあえず」「暫定」
"workaround" 等）を探す。こうした正当化は陳腐化するうえ、次の実装が
「確立したパターン」としてコピーする経路になる。残すくらいなら根本原因を
直すか issue 化する、という運用を機械的に支えるための検査。
検出はファイル名・行番号・種類だけを出す。
誤検知は行末に `justification-scan: allow` を付けて抑止する
（抑止の追加はレビュー対象）。
対象は Python ファイルのコメント部分のみ。このスクリプト自身は、
語彙の定義そのものを含むため検査対象から外す。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERNS = {
    "暫定対応の宣言": re.compile(r"暫定"),
    "回避策の宣言": re.compile(r"とりあえず|応急|その場しのぎ|場当たり"),
    "英語の回避策の宣言": re.compile(r"workaround|temporary\s+fix|temp\s+fix", re.IGNORECASE),
}
ALLOW = "justification-scan: allow"
SELF_NAME = Path(__file__).name
GUIDANCE = (
    "→ 正当化・回避策のコメントは残さないでください。"
    "なぜその回避策が必要なのかという根本原因を直すのが正しい対応です。"
    "直せない場合は理由を issue に記録し、コメントには issue への参照だけを残してください。"
    "やむを得ない場合は行末に `justification-scan: allow` を付け、"
    "レビューで理由を説明してください。"
)


def comment_text(line: str) -> str:
    """行のコメント部分（最初の # 以降）を返す。コメントが無ければ空文字。"""
    index = line.find("#")
    return line[index:] if index >= 0 else ""


def scan_file(path: Path) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if ALLOW in line:
            continue
        comment = comment_text(line)
        if not comment:
            continue
        for category, pattern in PATTERNS.items():
            if pattern.search(comment):
                findings.append((lineno, category))
    return findings


def iter_targets(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        target = Path(raw)
        if target.is_file() and target.suffix == ".py":
            files.append(target)
        elif target.is_dir():
            files.extend(sorted(target.rglob("*.py")))
    return [f for f in files if "__pycache__" not in f.parts and f.name != SELF_NAME]


def main(argv: list[str]) -> int:
    paths = argv[1:] or ["src", "scripts"]
    total = 0
    for path in iter_targets(paths):
        for lineno, category in scan_file(path):
            print(f"{path}:{lineno}: {category}")
            total += 1
    if total:
        print(GUIDANCE)
        print(f"正当化・回避策コメントの検出: {total} 件")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
