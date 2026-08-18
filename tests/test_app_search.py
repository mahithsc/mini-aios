import json
from pathlib import Path

import pytest

from aios_core import workspace
from aios_core.agent_prompt import build_agent_prompt
from aios_core.tools import search as generic_search
from aios_core.tools.app_search import (
    find_app_references,
    find_relevant_apps,
    inspect_app,
    list_app_files,
    read_app_file,
    search_app,
    search_app_content,
)


@pytest.fixture
def apps_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "_DEV_WORKSPACE_DIR", tmp_path / "workspace")
    monkeypatch.setenv("AIOS_ENV", "dev")
    apps = workspace.ensure_workspace_dir() / "apps"

    billing = apps / "app_billing"
    billing.mkdir(parents=True)
    (billing / ".aios-app.json").write_text(
        json.dumps({"app_id": "app_billing", "name": "Invoice Pilot"})
    )
    (billing / "README.md").write_text(
        "# Invoice Pilot\nStripe billing and webhook reconciliation dashboard.\n"
    )
    (billing / "src").mkdir()
    (billing / "src" / "webhooks.py").write_text(
        "def reconcile_stripe_invoice(event):\n    return event['invoice_id']\n"
    )
    (billing / "site" / "assets" / "images").mkdir(parents=True)
    (billing / "site" / "assets" / "images" / "hero.webp").write_bytes(b"RIFF-image")
    (billing / "site" / "assets" / "mark.svg").write_text("<svg></svg>\n")
    (billing / "site" / "app.js").write_text(
        "const hero = '/assets/images/hero.webp';\n"
        "renderImage(hero);\n"
    )
    (billing / "aios.deploy.yaml").write_text(
        "version: 1\nserver:\n  source: src\nfrontend:\n  source: site\n"
    )
    (billing / "node_modules").mkdir()
    (billing / "node_modules" / "noise.js").write_text("orchid fertilizer secret")
    (billing / ".env").write_text("STRIPE_SECRET=supersecretvalue")

    garden = apps / "app_garden"
    garden.mkdir()
    (garden / ".aios-app.json").write_text(
        json.dumps({"app_id": "app_garden", "name": "Garden Tracker"})
    )
    (garden / "README.md").write_text(
        "# Garden Tracker\nTrack orchid watering, soil moisture, and fertilizer schedules.\n"
    )
    (garden / "src").mkdir()
    (garden / "src" / "plants.py").write_text(
        "def record_soil_moisture(orchid_id, reading):\n    return reading\n"
    )

    return apps, billing, garden


def test_find_relevant_apps_ranks_matching_app(apps_workspace):
    _, billing, _ = apps_workspace

    result = json.loads(
        find_relevant_apps(
            "The customer reports that Stripe invoice webhook reconciliation is broken."
        )
    )

    assert result["apps"][0]["app_id"] == "app_billing"
    assert result["apps"][0]["path"] == str(billing)
    assert "stripe" in result["apps"][0]["matched_keywords"]


def test_find_relevant_apps_accepts_code_blob_and_app_id(apps_workspace):
    result = json.loads(
        find_relevant_apps("Please update app_garden record_soil_moisture for orchid readings")
    )

    assert result["apps"][0]["app_id"] == "app_garden"
    assert "record_soil_moisture" in result["query_keywords"]


def test_search_app_returns_ranked_line_matches(apps_workspace):
    _, billing, garden = apps_workspace

    result = json.loads(search_app("app_garden", "orchid soil moisture", limit=3))

    assert result["app"]["path"] == str(garden)
    assert result["matches"][0]["path"] in {"README.md", "src/plants.py"}
    assert set(result["matches"][0]["matched_keywords"]) >= {"orchid", "soil", "moisture"}
    assert all(str(billing) not in match["path"] for match in result["matches"])


def test_search_keeps_domain_significant_before_and_after(apps_workspace):
    _, billing, _ = apps_workspace
    (billing / "gallery.html").write_text("<h1>Before and after gallery</h1>\n")

    result = json.loads(search_app("app_billing", "before after gallery"))

    assert set(result["query_keywords"]) >= {"before", "after", "gallery"}
    assert result["matches"][0]["path"] == "gallery.html"


def test_search_app_accepts_workspace_relative_and_absolute_paths(apps_workspace):
    _, billing, _ = apps_workspace

    relative_result = json.loads(search_app("apps/app_billing", "stripe webhook"))
    absolute_result = json.loads(search_app(str(billing), "stripe webhook"))

    assert relative_result["app"]["app_id"] == "app_billing"
    assert absolute_result["app"]["app_id"] == "app_billing"


def test_search_app_rejects_paths_outside_apps(apps_workspace, tmp_path):
    apps, _, _ = apps_workspace

    assert "must be inside" in search_app(str(tmp_path), "stripe")
    assert "must identify one app" in search_app(str(apps), "stripe")
    assert "does not exist" in search_app("missing-app", "stripe")


def test_search_skips_dependencies_and_secret_files(apps_workspace):
    result = search_app("app_billing", "fertilizer supersecretvalue")

    assert result == "none: no matches found in app"


def test_search_does_not_follow_file_symlinks_outside_app(apps_workspace, tmp_path):
    _, billing, _ = apps_workspace
    outside = tmp_path / "outside.txt"
    outside.write_text("external-only-marker")
    (billing / "linked.txt").symlink_to(outside)

    assert search_app("app_billing", "external-only-marker") == "none: no matches found in app"


def test_search_validates_blob_input(apps_workspace):
    assert find_relevant_apps("") == "error: content is required"
    assert "does not contain searchable keywords" in search_app("app_billing", "the and this")


def test_inspect_app_reports_structure_components_and_file_types(apps_workspace):
    _, billing, _ = apps_workspace

    result = json.loads(inspect_app("app_billing", max_depth=4))

    assert result["app"]["path"] == str(billing)
    assert result["components"] == ["server", "frontend"]
    assert "aios.deploy.yaml" in result["important_files"]
    assert result["summary"]["extensions"]["webp"] == 1
    assert any(entry["path"] == "site/assets/images" for entry in result["tree"])


def test_list_app_files_inventories_assets_with_structured_extensions(apps_workspace):
    result = json.loads(
        list_app_files(
            "app_billing",
            under="site",
            extensions=["webp", ".svg"],
        )
    )

    assert [item["path"] for item in result["files"]] == [
        "site/assets/images/hero.webp",
        "site/assets/mark.svg",
    ]


def test_list_app_files_supports_name_and_path_filters(apps_workspace):
    result = json.loads(
        list_app_files(
            "app_billing",
            name_contains=["hero"],
            path_contains=["assets/images"],
        )
    )

    assert [item["path"] for item in result["files"]] == [
        "site/assets/images/hero.webp"
    ]


def test_search_app_content_chains_from_selected_paths(apps_workspace):
    inventory = json.loads(
        list_app_files("app_billing", under="site", extensions=["js"])
    )
    paths = [item["path"] for item in inventory["files"]]

    result = json.loads(
        search_app_content(
            "app_billing",
            query="hero.webp",
            paths=paths,
            match_mode="literal",
            context=1,
        )
    )

    assert result["searched_files"] == 1
    assert result["matches"][0]["path"] == "site/app.js"
    assert result["matches"][0]["line"] == 1
    assert result["matches"][0]["after"] == ["renderImage(hero);"]


def test_search_app_content_supports_keyword_and_regex_modes(apps_workspace):
    keywords = json.loads(
        search_app_content("app_garden", "orchid soil moisture", context=0)
    )
    regex = json.loads(
        search_app_content(
            "app_garden",
            r"record_soil_\w+",
            paths=["src/plants.py"],
            match_mode="regex",
        )
    )

    assert set(keywords["matches"][0]["matched_terms"]) >= {"orchid", "soil", "moisture"}
    assert regex["matches"][0]["line"] == 1


def test_find_app_references_finds_asset_usage(apps_workspace):
    result = json.loads(
        find_app_references(
            "app_billing",
            targets=["hero.webp", "/assets/images/"],
            extensions=["js", "html", "css"],
        )
    )

    assert result["matches"][0]["path"] == "site/app.js"
    assert result["matches"][0]["matched_targets"] == ["hero.webp", "/assets/images/"]


def test_read_app_file_reads_paginated_lines(apps_workspace):
    first_page = json.loads(
        read_app_file("app_billing", "site/app.js", offset=0, limit=1)
    )
    second_page = json.loads(
        read_app_file("app_billing", "site/app.js", offset=first_page["next_offset"], limit=1)
    )

    assert first_page["lines"][0]["number"] == 1
    assert first_page["next_offset"] == 1
    assert second_page["lines"][0]["text"] == "renderImage(hero);"


def test_app_scoped_tools_reject_path_escape_and_sensitive_reads(apps_workspace, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")

    assert "must remain inside" in read_app_file("app_billing", str(outside))
    assert "sensitive files" in read_app_file("app_billing", ".env")
    assert "must remain inside" in search_app_content(
        "app_billing", "outside", paths=[str(outside)]
    )


def test_generic_glob_expands_brace_patterns(apps_workspace):
    _, billing, _ = apps_workspace

    result = generic_search.glob("**/*.{webp,svg}", str(billing))

    assert "hero.webp" in result
    assert "mark.svg" in result


def test_main_prompt_exposes_app_navigation_tools():
    prompt = build_agent_prompt(
        include_subagent_tool=True,
        default_cron_timezone="UTC",
        workspace_dir="/tmp/workspace",
        include_memory_tools=True,
    )

    assert '"find_relevant_apps"' in prompt
    assert '"inspect_app"' in prompt
    assert '"list_app_files"' in prompt
    assert '"search_app_content"' in prompt
    assert '"find_app_references"' in prompt
    assert '"read_app_file"' in prompt
    assert "preserve its returned app path" in prompt


def test_worker_prompt_does_not_advertise_unregistered_app_tools():
    prompt = build_agent_prompt(
        include_subagent_tool=False,
        default_cron_timezone="UTC",
        workspace_dir="/tmp/workspace",
        include_memory_tools=False,
    )

    assert '"find_relevant_apps"' not in prompt
    assert '"inspect_app"' not in prompt
    assert '"list_app_files"' not in prompt
    assert '"search_app_content"' not in prompt
    assert '"find_app_references"' not in prompt
    assert '"read_app_file"' not in prompt
