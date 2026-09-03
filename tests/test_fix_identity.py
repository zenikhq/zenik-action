"""The fix commit's author: the app's bot user, formatted the way GitHub links
commits to the bot's account."""
from fix import bot_identity_string, format_bot_identity, parse_command


def test_bot_identity_from_users_endpoint():
    user = {"login": "zenik-ai[bot]", "id": 987654, "type": "Bot"}
    assert format_bot_identity(user) == (
        "zenik-ai[bot]", "987654+zenik-ai[bot]@users.noreply.github.com")
    assert bot_identity_string(user) == (
        "zenik-ai[bot] <987654+zenik-ai[bot]@users.noreply.github.com>")


def test_bot_identity_without_id_still_carries_login():
    assert format_bot_identity({"login": "zenik-ai[bot]"}) == (
        "zenik-ai[bot]", "zenik-ai[bot]@users.noreply.github.com")


def test_parse_command_gates():
    assert parse_command("/zenik fix") == {"scope": []}
    assert parse_command("/zenik fix charge refund") == {"scope": ["charge", "refund"]}
    assert "error" in parse_command("/zenik dance")
    assert parse_command("looks good to me") is None
