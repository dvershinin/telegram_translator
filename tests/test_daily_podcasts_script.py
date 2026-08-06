"""Structural guards for the unattended daily podcast runner."""

from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "daily_podcasts.sh"
).read_text(encoding="utf-8")


def test_scalable_stories_runs_before_russian_podcast() -> None:
    """The English WordPress show should run before the Russian pipeline."""
    assert SCRIPT.index("run_podcast scalable_stories") < SCRIPT.index(
        "run_podcast vaske_daily"
    )


def test_wordpress_credentials_are_scoped_to_scalable_stories() -> None:
    """Cron retrieves the app password from Keychain and removes it afterward."""
    assert 'if [ "$name" = "scalable_stories" ]' in SCRIPT
    assert "/usr/bin/security find-generic-password" in SCRIPT
    assert "-s getpagespeed-scalable-stories-wordpress -w" in SCRIPT
    assert "export GPS_WP_USER=danila" in SCRIPT
    assert "export GPS_WP_APP_PASSWORD" in SCRIPT
    assert "unset GPS_WP_USER GPS_WP_APP_PASSWORD" in SCRIPT
