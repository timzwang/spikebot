"""Tests for loading Twitch credentials from .env. Run: python -m pytest -q"""
from spikebot.main import apply_env, load_dotenv


def test_load_dotenv_parses_quotes_comments_and_export(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# comment\n\nexport TWITCH_CLIENT_ID="abc"\nTWITCH_CLIENT_SECRET=\'s3c\'\nBAD LINE\n')
    assert load_dotenv(f) == {"TWITCH_CLIENT_ID": "abc", "TWITCH_CLIENT_SECRET": "s3c"}


def test_missing_env_file_is_ignored(tmp_path):
    assert load_dotenv(tmp_path / "nope") == {}


def test_env_file_overrides_config(tmp_path):
    f = tmp_path / ".env"
    f.write_text("TWITCH_CLIENT_ID=fromfile\nTWITCH_CLIENT_SECRET=filesecret\n")
    cfg = apply_env({"twitch_client_id": "old", "twitch_client_secret": ""}, [f], {})
    assert cfg["twitch_client_id"] == "fromfile"
    assert cfg["twitch_client_secret"] == "filesecret"


def test_environment_beats_env_file_and_empty_values_keep_config(tmp_path):
    f = tmp_path / ".env"
    f.write_text("TWITCH_CLIENT_ID=fromfile\nTWITCH_CLIENT_SECRET=\n")
    cfg = apply_env({"twitch_client_secret": "keep"}, [f], {"TWITCH_CLIENT_ID": "fromenv"})
    assert cfg["twitch_client_id"] == "fromenv"
    assert cfg["twitch_client_secret"] == "keep"


def test_first_env_file_wins(tmp_path):
    a, b = tmp_path / "a.env", tmp_path / "b.env"
    a.write_text("TWITCH_CLIENT_ID=first\n")
    b.write_text("TWITCH_CLIENT_ID=second\nTWITCH_CLIENT_SECRET=onlyb\n")
    cfg = apply_env({}, [a, b], {})
    assert cfg == {"twitch_client_id": "first", "twitch_client_secret": "onlyb"}
