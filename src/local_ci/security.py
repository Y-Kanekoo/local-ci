"""子プロセスの出力を保存前にマスクする。"""

import os
import re

SECRET_NAME = re.compile(
    r"TOKEN|SECRET|KEY|PASSWORD|PASSWD|CREDENTIAL|AUTH|PRIVATE|COOKIE|SESSION", re.I
)
PATTERNS = (
    re.compile(r"(?:ghp_|gho_|ghs_|ghu_|github_pat_)[A-Za-z0-9_]+"),
    re.compile(r"(?:sk-ant-|sk-)[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]+"),
    re.compile(r"(Authorization:\s*Bearer\s+)\S+", re.I),
    re.compile(r"(https?://)[^\s/@:]+:[^\s/@]+@", re.I),
    re.compile(r"-----BEGIN [^-\n]*PRIVATE KEY-----[\s\S]*?-----END [^-\n]*PRIVATE KEY-----"),
)


class Redactor:
    def __init__(self, environ: dict[str, str] | None = None) -> None:
        values = (environ or dict(os.environ)).items()
        self.secrets = sorted(
            {value for name, value in values if SECRET_NAME.search(name) and len(value) >= 6},
            key=len,
            reverse=True,
        )
        self.in_private_key = False

    def redact(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "***")
        for pattern in PATTERNS:
            if pattern.pattern.startswith("(Authorization:"):
                value = pattern.sub(r"\1***", value)
            elif pattern.pattern.startswith("(https?://)"):
                value = pattern.sub(r"\1***@", value)
            else:
                value = pattern.sub("***", value)
        return value

    def feed_line(self, line: str) -> str:
        if self.in_private_key:
            if "-----END " in line and "PRIVATE KEY-----" in line:
                self.in_private_key = False
            return "***\n" if line.endswith("\n") else "***"
        if "-----BEGIN " in line and "PRIVATE KEY-----" in line:
            self.in_private_key = "-----END " not in line
            return "***\n" if line.endswith("\n") else "***"
        return self.redact(line)
