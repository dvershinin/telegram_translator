"""Tests for destination resolution and validation in ConfigManager."""

from pathlib import Path

import pytest

from telegram_translator.config_manager import ConfigManager


# Minimal config skeleton — the tests synthesise a ConfigManager directly
# so they don't touch the real config.yml or app data directories.
_BASE_CONFIG = {
    "api_id": "1",
    "api_hash": "x",
    "translation": {
        "provider": "openai",
        "openai": {"api_key": "x"},
    },
    "sources": {"telegram": {}, "web": {}},
    "voicebox": {"url": "http://localhost:17493"},
}


def _make_mgr(extra: dict, tmp_path: Path) -> ConfigManager:
    """Build a ConfigManager without loading from disk.

    Skips ``__init__`` so no YAML is parsed, no app dirs are created, and
    no secrets are required. All filesystem-ish attributes point at the
    pytest-provided ``tmp_path``.

    Args:
        extra: Extra top-level keys merged on top of the base skeleton
            (typically ``destinations`` and ``podcasts``).
        tmp_path: Pytest tmp_path fixture.

    Returns:
        A configured ConfigManager instance ready for resolve_* calls.
    """
    mgr = ConfigManager.__new__(ConfigManager)
    mgr.config_file = "<test>"
    mgr.config = {**_BASE_CONFIG, **extra}
    mgr.app_name = "telegram_translator"
    mgr.app_author = "telegram_translator"
    for attr in (
        "data_dir",
        "config_dir",
        "sessions_dir",
        "logs_dir",
        "databases_dir",
        "podcasts_dir",
    ):
        setattr(mgr, attr, tmp_path)
    return mgr


class TestResolveDestinations:
    """resolve_destinations() normalization and defaults."""

    def test_empty_when_no_destinations_key(self, tmp_path):
        mgr = _make_mgr({}, tmp_path)
        assert mgr.resolve_destinations() == {}

    def test_strips_trailing_slash_from_base_url(self, tmp_path):
        mgr = _make_mgr(
            {
                "destinations": {
                    "gp": {
                        "type": "static",
                        "base_url": "https://podcasts.getpagespeed.com/",
                        "publish_dir": "./publish/gp",
                    }
                }
            },
            tmp_path,
        )
        dests = mgr.resolve_destinations()
        assert dests["gp"]["base_url"] == "https://podcasts.getpagespeed.com"

    def test_strips_multiple_trailing_slashes(self, tmp_path):
        mgr = _make_mgr(
            {
                "destinations": {
                    "gp": {
                        "type": "static",
                        "base_url": "https://x.com///",
                        "publish_dir": "./a",
                    }
                }
            },
            tmp_path,
        )
        dests = mgr.resolve_destinations()
        assert dests["gp"]["base_url"] == "https://x.com"

    def test_type_defaults_to_static(self, tmp_path):
        mgr = _make_mgr(
            {
                "destinations": {
                    "gp": {
                        "base_url": "https://x.com",
                        "publish_dir": "./a",
                    }
                }
            },
            tmp_path,
        )
        assert mgr.resolve_destinations()["gp"]["type"] == "static"

    def test_expands_tilde_in_publish_dir(self, tmp_path):
        mgr = _make_mgr(
            {
                "destinations": {
                    "gp": {
                        "type": "static",
                        "base_url": "https://x.com",
                        "publish_dir": "~/podcasts",
                    }
                }
            },
            tmp_path,
        )
        pub_dir = mgr.resolve_destinations()["gp"]["publish_dir"]
        assert "~" not in pub_dir
        assert pub_dir.endswith("/podcasts")

    def test_expands_tilde_in_astro_dirs(self, tmp_path):
        mgr = _make_mgr(
            {
                "destinations": {
                    "v": {
                        "type": "astro_collection",
                        "base_url": "https://vaske.ru/podcast",
                        "content_dir": "~/Projects/Vaske/src/content/podcast",
                        "public_dir": "~/Projects/Vaske/public/podcast",
                    }
                }
            },
            tmp_path,
        )
        dest = mgr.resolve_destinations()["v"]
        assert "~" not in dest["content_dir"]
        assert "~" not in dest["public_dir"]
        assert dest["content_dir"].endswith("src/content/podcast")

    def test_static_site_level_fields_preserved(self, tmp_path):
        mgr = _make_mgr(
            {
                "destinations": {
                    "gp": {
                        "type": "static",
                        "base_url": "https://x.com",
                        "publish_dir": "./a",
                        "site_title": "Site",
                        "site_description": "Desc",
                        "copyright": "© 2026",
                    }
                }
            },
            tmp_path,
        )
        dest = mgr.resolve_destinations()["gp"]
        assert dest["site_title"] == "Site"
        assert dest["site_description"] == "Desc"
        assert dest["copyright"] == "© 2026"

    def test_rejects_invalid_type(self, tmp_path):
        mgr = _make_mgr(
            {
                "destinations": {
                    "gp": {
                        "type": "bogus",
                        "base_url": "https://x.com",
                        "publish_dir": "./a",
                    }
                }
            },
            tmp_path,
        )
        with pytest.raises(ValueError, match="invalid type"):
            mgr.resolve_destinations()

    def test_static_missing_publish_dir_raises(self, tmp_path):
        mgr = _make_mgr(
            {
                "destinations": {
                    "gp": {"type": "static", "base_url": "https://x.com"}
                }
            },
            tmp_path,
        )
        with pytest.raises(ValueError, match="publish_dir"):
            mgr.resolve_destinations()

    def test_astro_missing_content_dir_raises(self, tmp_path):
        mgr = _make_mgr(
            {
                "destinations": {
                    "v": {
                        "type": "astro_collection",
                        "base_url": "https://x.com",
                        "public_dir": "./p",
                    }
                }
            },
            tmp_path,
        )
        with pytest.raises(ValueError, match="content_dir"):
            mgr.resolve_destinations()

    def test_astro_missing_public_dir_raises(self, tmp_path):
        mgr = _make_mgr(
            {
                "destinations": {
                    "v": {
                        "type": "astro_collection",
                        "base_url": "https://x.com",
                        "content_dir": "./c",
                    }
                }
            },
            tmp_path,
        )
        with pytest.raises(ValueError, match="public_dir"):
            mgr.resolve_destinations()

    def test_non_mapping_destination_raises(self, tmp_path):
        mgr = _make_mgr(
            {"destinations": {"gp": "not-a-dict"}}, tmp_path
        )
        with pytest.raises(ValueError, match="must be a mapping"):
            mgr.resolve_destinations()


class TestPodcastResolutionWithDestination:
    """Podcasts that reference a destination inherit derived publish keys."""

    def test_static_subpath_default_slug(self, tmp_path):
        mgr = _make_mgr(
            {
                "destinations": {
                    "gp": {
                        "type": "static",
                        "base_url": "https://podcasts.getpagespeed.com",
                        "publish_dir": "./publish/gp",
                        "sync_command": "echo sync",
                    }
                },
                "podcasts": {
                    "crosswire": {
                        "destination": "gp",
                        "title": "Crosswire",
                    }
                },
            },
            tmp_path,
        )
        resolved = mgr.resolve_podcast_configs()["crosswire"]
        assert resolved["destination_name"] == "gp"
        assert resolved["destination_type"] == "static"
        assert resolved["slug"] == "crosswire"
        assert (
            resolved["publish"]["base_url"]
            == "https://podcasts.getpagespeed.com/crosswire"
        )
        assert resolved["publish"]["publish_dir"].endswith(
            "publish/gp/crosswire"
        )
        assert resolved["publish"]["sync_command"] == "echo sync"

    def test_static_with_explicit_slug_override(self, tmp_path):
        """Slug override preserves legacy URL paths (e.g., the-stack vs the_stack)."""
        mgr = _make_mgr(
            {
                "destinations": {
                    "gp": {
                        "type": "static",
                        "base_url": "https://podcasts.getpagespeed.com",
                        "publish_dir": "./publish/gp",
                    }
                },
                "podcasts": {
                    "the_stack": {
                        "destination": "gp",
                        "slug": "the-stack",
                        "title": "The Stack",
                    }
                },
            },
            tmp_path,
        )
        resolved = mgr.resolve_podcast_configs()["the_stack"]
        assert resolved["slug"] == "the-stack"
        assert (
            resolved["publish"]["base_url"]
            == "https://podcasts.getpagespeed.com/the-stack"
        )
        assert resolved["publish"]["publish_dir"].endswith(
            "publish/gp/the-stack"
        )

    def test_static_root_mounted_empty_slug(self, tmp_path):
        mgr = _make_mgr(
            {
                "destinations": {
                    "host": {
                        "type": "static",
                        "base_url": "https://www.example.com/podcast",
                        "publish_dir": "./publish/host",
                    }
                },
                "podcasts": {
                    "only": {
                        "destination": "host",
                        "slug": "",
                        "title": "Only",
                    }
                },
            },
            tmp_path,
        )
        resolved = mgr.resolve_podcast_configs()["only"]
        assert resolved["slug"] == ""
        # Root-mounted: no /{slug} suffix
        assert (
            resolved["publish"]["base_url"]
            == "https://www.example.com/podcast"
        )
        assert resolved["publish"]["publish_dir"].endswith("publish/host")
        assert not resolved["publish"]["publish_dir"].endswith(
            "publish/host/only"
        )

    def test_per_podcast_show_metadata_merged(self, tmp_path):
        """Per-podcast show keys (artwork, description) coexist with destination keys."""
        mgr = _make_mgr(
            {
                "destinations": {
                    "gp": {
                        "type": "static",
                        "base_url": "https://x.com",
                        "publish_dir": "./a",
                    }
                },
                "podcasts": {
                    "p": {
                        "destination": "gp",
                        "title": "P",
                        "publish": {
                            "show_artwork": "./art.jpg",
                            "show_description": "A show.",
                            "show_category": "News",
                            "explicit": True,
                            "copyright": "© 2026",
                            "spotify_url": "https://spotify.com/abc",
                        },
                    }
                },
            },
            tmp_path,
        )
        pub = mgr.resolve_podcast_configs()["p"]["publish"]
        assert pub["show_artwork"] == "./art.jpg"
        assert pub["show_description"] == "A show."
        assert pub["show_category"] == "News"
        assert pub["explicit"] is True
        assert pub["copyright"] == "© 2026"
        assert pub["spotify_url"] == "https://spotify.com/abc"
        # Destination-derived keys also present
        assert pub["base_url"] == "https://x.com/p"
        assert pub["publish_dir"].endswith("a/p")

    def test_astro_collection_single_podcast(self, tmp_path):
        mgr = _make_mgr(
            {
                "destinations": {
                    "v": {
                        "type": "astro_collection",
                        "base_url": "https://www.vaske.ru/podcast/",
                        "content_dir": "./vaske/content",
                        "public_dir": "./vaske/public",
                        "sync_command": "echo vaske",
                    }
                },
                "podcasts": {
                    "vaske_daily": {
                        "destination": "v",
                        "title": "Vaske Daily",
                    }
                },
            },
            tmp_path,
        )
        resolved = mgr.resolve_podcast_configs()["vaske_daily"]
        assert resolved["destination_type"] == "astro_collection"
        assert resolved["slug"] is None
        # base_url is normalized (no trailing slash)
        assert (
            resolved["publish"]["base_url"]
            == "https://www.vaske.ru/podcast"
        )
        assert resolved["publish"]["content_dir"].endswith("vaske/content")
        assert resolved["publish"]["public_dir"].endswith("vaske/public")
        assert resolved["publish"]["sync_command"] == "echo vaske"
        # No derived publish_dir for astro_collection
        assert "publish_dir" not in resolved["publish"]


class TestValidationErrors:
    """Destination-related validation errors raised at resolve time."""

    def test_unknown_destination_reference(self, tmp_path):
        mgr = _make_mgr(
            {
                "destinations": {
                    "gp": {
                        "type": "static",
                        "base_url": "https://x.com",
                        "publish_dir": "./a",
                    }
                },
                "podcasts": {
                    "foo": {"destination": "bar", "title": "Foo"}
                },
            },
            tmp_path,
        )
        with pytest.raises(ValueError, match="unknown destination"):
            mgr.resolve_podcast_configs()

    @pytest.mark.parametrize(
        "forbidden_key,value",
        [
            ("base_url", "https://override.example.com"),
            ("publish_dir", "./override"),
            ("sync_command", "rsync -x ..."),
        ],
    )
    def test_forbidden_key_in_podcast_publish(
        self, tmp_path, forbidden_key, value
    ):
        mgr = _make_mgr(
            {
                "destinations": {
                    "gp": {
                        "type": "static",
                        "base_url": "https://x.com",
                        "publish_dir": "./a",
                    }
                },
                "podcasts": {
                    "foo": {
                        "destination": "gp",
                        "title": "Foo",
                        "publish": {forbidden_key: value},
                    }
                },
            },
            tmp_path,
        )
        with pytest.raises(ValueError, match="must not set"):
            mgr.resolve_podcast_configs()

    def test_astro_collection_multi_podcast_raises(self, tmp_path):
        mgr = _make_mgr(
            {
                "destinations": {
                    "v": {
                        "type": "astro_collection",
                        "base_url": "https://x.com",
                        "content_dir": "./c",
                        "public_dir": "./p",
                    }
                },
                "podcasts": {
                    "a": {"destination": "v", "title": "A"},
                    "b": {"destination": "v", "title": "B"},
                },
            },
            tmp_path,
        )
        with pytest.raises(ValueError, match="can host only one"):
            mgr.resolve_podcast_configs()

    def test_static_mixed_slug_scheme_raises(self, tmp_path):
        mgr = _make_mgr(
            {
                "destinations": {
                    "gp": {
                        "type": "static",
                        "base_url": "https://x.com",
                        "publish_dir": "./a",
                    }
                },
                "podcasts": {
                    "root": {
                        "destination": "gp",
                        "slug": "",
                        "title": "Root",
                    },
                    "sub": {
                        "destination": "gp",
                        "slug": "sub",
                        "title": "Sub",
                    },
                },
            },
            tmp_path,
        )
        with pytest.raises(ValueError, match="mix root-mounted"):
            mgr.resolve_podcast_configs()

    def test_static_multiple_root_mounted_raises(self, tmp_path):
        mgr = _make_mgr(
            {
                "destinations": {
                    "gp": {
                        "type": "static",
                        "base_url": "https://x.com",
                        "publish_dir": "./a",
                    }
                },
                "podcasts": {
                    "a": {
                        "destination": "gp",
                        "slug": "",
                        "title": "A",
                    },
                    "b": {
                        "destination": "gp",
                        "slug": "",
                        "title": "B",
                    },
                },
            },
            tmp_path,
        )
        with pytest.raises(ValueError, match="multiple root-mounted"):
            mgr.resolve_podcast_configs()


class TestBackwardCompat:
    """Legacy podcasts (no destination:) continue to work unchanged."""

    def test_legacy_podcast_keeps_its_own_publish_block(self, tmp_path):
        mgr = _make_mgr(
            {
                "podcasts": {
                    "legacy": {
                        "title": "Legacy",
                        "publish": {
                            "base_url": "https://example.com/legacy",
                            "publish_dir": "./publish/legacy",
                            "sync_command": "echo legacy",
                            "show_description": "Legacy show.",
                        },
                    }
                }
            },
            tmp_path,
        )
        resolved = mgr.resolve_podcast_configs()["legacy"]
        assert resolved["destination_name"] is None
        assert resolved["destination_type"] is None
        assert resolved["slug"] is None
        assert (
            resolved["publish"]["base_url"] == "https://example.com/legacy"
        )
        assert resolved["publish"]["sync_command"] == "echo legacy"
        assert resolved["publish"]["show_description"] == "Legacy show."

    def test_legacy_podcast_can_forbidden_keys(self, tmp_path):
        """Without a destination, the forbidden-keys validation doesn't fire."""
        mgr = _make_mgr(
            {
                "podcasts": {
                    "legacy": {
                        "title": "Legacy",
                        "publish": {
                            "base_url": "https://example.com/legacy",
                            "publish_dir": "./publish/legacy",
                            "sync_command": "rsync ...",
                        },
                    }
                }
            },
            tmp_path,
        )
        # Should not raise
        resolved = mgr.resolve_podcast_configs()
        assert "legacy" in resolved

    def test_no_podcasts_section_builds_default(self, tmp_path):
        """The _default legacy synthesized podcast still works."""
        mgr = _make_mgr(
            {
                "digest": {"executive_prompt": "x"},
                "podcast": {"voice_profile": "default"},
            },
            tmp_path,
        )
        resolved = mgr.resolve_podcast_configs()
        assert "_default" in resolved
        assert resolved["_default"]["destination_name"] is None
        assert resolved["_default"]["destination_type"] is None


class TestGroupPodcastsByDestination:
    """group_podcasts_by_destination() buckets podcasts by destination name."""

    def test_groups_only_destination_scoped(self, tmp_path):
        mgr = _make_mgr(
            {
                "destinations": {
                    "gp": {
                        "type": "static",
                        "base_url": "https://x.com",
                        "publish_dir": "./a",
                    },
                    "v": {
                        "type": "astro_collection",
                        "base_url": "https://y.com",
                        "content_dir": "./c",
                        "public_dir": "./p",
                    },
                },
                "podcasts": {
                    "a": {"destination": "gp", "title": "A"},
                    "b": {"destination": "gp", "title": "B"},
                    "c": {"destination": "v", "title": "C"},
                    "legacy": {
                        "title": "L",
                        "publish": {
                            "base_url": "https://z.com",
                            "publish_dir": "./z",
                        },
                    },
                },
            },
            tmp_path,
        )
        groups = mgr.group_podcasts_by_destination()
        assert set(groups.keys()) == {"gp", "v"}
        assert len(groups["gp"]) == 2
        assert {p["name"] for p in groups["gp"]} == {"a", "b"}
        assert len(groups["v"]) == 1
        assert groups["v"][0]["name"] == "c"

    def test_empty_when_no_destination_scoped(self, tmp_path):
        mgr = _make_mgr(
            {
                "podcasts": {
                    "legacy": {
                        "title": "L",
                        "publish": {
                            "base_url": "https://z.com",
                            "publish_dir": "./z",
                        },
                    }
                }
            },
            tmp_path,
        )
        assert mgr.group_podcasts_by_destination() == {}
