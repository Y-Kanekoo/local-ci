import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "scan_justification.py"


def run_scan(target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(target)],
        capture_output=True,
        text=True,
    )


def test_detects_justification_comment(tmp_path: Path) -> None:
    target = tmp_path / "bad.py"
    target.write_text("x = 1  # とりあえずこの値で固定する\n", encoding="utf-8")
    result = run_scan(tmp_path)
    assert result.returncode == 1
    assert "bad.py:1" in result.stdout
    assert "根本原因" in result.stdout


def test_clean_file_passes(tmp_path: Path) -> None:
    target = tmp_path / "good.py"
    target.write_text("x = 1  # 単位はミリ秒。呼び出し側の契約に合わせる\n", encoding="utf-8")
    result = run_scan(tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""


def test_allow_marker_suppresses(tmp_path: Path) -> None:
    target = tmp_path / "allowed.py"
    target.write_text(
        "x = 1  # 暫定値。根拠は issue #123 justification-scan: allow\n",
        encoding="utf-8",
    )
    result = run_scan(tmp_path)
    assert result.returncode == 0


def test_vocabulary_in_string_literal_is_not_flagged(tmp_path: Path) -> None:
    target = tmp_path / "strings.py"
    target.write_text('LABEL = "暫定"\n', encoding="utf-8")
    result = run_scan(tmp_path)
    assert result.returncode == 0
