from local_ci.security import Redactor


def test_redacts_environment_and_patterns() -> None:
    redactor = Redactor({"MY_TOKEN": "abc123456", "OTHER": "visible"})
    text = (
        "abc123456 ghp_abcdefgh1234 github_pat_abcdefgh sk-ant-abcdefgh12345678 "
        "AKIAABCDEFGHIJKLMNOP xoxb-123-abc Authorization: Bearer abcdef "
        "https://user:password@example.com visible"
    )
    masked = redactor.redact(text)
    for secret in (
        "abc123456",
        "ghp_",
        "github_pat_",
        "sk-ant-",
        "AKIA",
        "xoxb-",
        "password",
        "Bearer abcdef",
    ):
        assert secret not in masked
    assert "visible" in masked


def test_private_key_stream() -> None:
    redactor = Redactor({})
    output = "".join(
        redactor.feed_line(line)
        for line in (
            "-----BEGIN RSA PRIVATE KEY-----\n",
            "very-secret-material\n",
            "-----END RSA PRIVATE KEY-----\n",
            "safe\n",
        )
    )
    assert "very-secret" not in output
    assert "safe" in output
