# tests/test_roles.py — SPEC-AGENT-ID-AND-ROLES Phase 1 (utils/roles.py).
#
# Pure utility: role files under <config_dir>/roles/. No GTK (runs bare).

import builtins

import pytest

import utils.roles as roles


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Point get_config_dir at an isolated tmp dir."""
    monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
    return tmp_path


def test_save_and_roundtrip(cfg):
    slug, err = roles.save_role("Alpha Coder", "Write careful code.")
    assert err is None
    assert slug == "alpha-coder"

    assert (cfg / "roles" / "alpha-coder.yaml").is_file()

    loaded = roles.load_role("alpha-coder")
    assert loaded == {
        "name": "Alpha Coder",
        "slug": "alpha-coder",
        "prompt": "Write careful code.",
    }
    assert roles.load_roles() == [loaded]


def test_load_role_missing_returns_none(cfg):
    assert roles.load_role("nope") is None
    assert roles.load_roles() == []


def test_builtin_refusal(cfg):
    slug, err = roles.save_role("Coder", "some prompt")
    assert slug is None
    assert err is not None
    assert not (cfg / "roles" / "coder.yaml").exists()

    for builtin in roles.BUILTIN_ROLES:
        assert roles.load_role(builtin) is None


def test_duplicate_refusal(cfg):
    slug, err = roles.save_role("My Role", "first prompt")
    assert slug == "my-role"
    assert err is None

    # Same name again — no overwrite.
    slug2, err2 = roles.save_role("My Role", "second prompt")
    assert slug2 is None
    assert err2 is not None

    # Different spelling, same slug — still refused.
    slug3, err3 = roles.save_role("My-Role", "third prompt")
    assert slug3 is None
    assert err3 is not None

    # Original file untouched.
    loaded = roles.load_role("my-role")
    assert loaded["prompt"] == "first prompt"


def test_empty_prompt_refusal(cfg):
    slug, err = roles.save_role("Some Role", "   ")
    assert slug is None
    assert err is not None
    assert not (cfg / "roles").exists()

    slug2, err2 = roles.save_role("Some Role", "")
    assert slug2 is None
    assert err2 is not None


def test_empty_name_refusal(cfg):
    slug, err = roles.save_role("", "prompt")
    assert slug is None
    assert err is not None
    slug2, err2 = roles.save_role("   ", "prompt")
    assert slug2 is None
    assert err2 is not None
    assert not (cfg / "roles").exists()


def test_slug_rules():
    assert roles.role_slug("Alpha Coder") == "alpha-coder"
    assert roles.role_slug("!!!") == ""
    assert roles.role_slug("-alpha-") == "alpha"
    assert roles.role_slug("_alpha_") == "alpha"
    assert roles.role_slug("-My Role-") == "my-role"
    assert roles.role_slug("My_Role") == "my_role"
    assert roles.role_slug("My\tRole") == "myrole"  # tab dropped


def test_slug_drops_non_ascii_alphanumerics():
    assert roles.role_slug("Héllo") == "hllo"
    assert roles.role_slug("日本語") == ""
    assert roles.role_slug("Héllo Wörld") == "hllo-wrld"
    assert roles.role_slug("Café Study") == "caf-study"


def test_slug_no_usable_characters_refusal(cfg):
    slug, err = roles.save_role("!!!", "prompt")
    assert slug is None
    assert err is not None
    assert not (cfg / "roles").exists()


def test_load_roles_excludes_builtins(cfg):
    roles_dir = cfg / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)
    # A hand-edited file claiming a built-in slug/name.
    (roles_dir / "coder.yaml").write_text(
        "name: Coder\nslug: coder\nprompt: builtin prompt\n",
        encoding="utf-8",
    )
    roles.save_role("Reviewer", "Looks at diffs.")
    loaded = roles.load_roles()
    assert [r["slug"] for r in loaded] == ["reviewer"]
    assert loaded[0]["name"] == "Reviewer"


def test_load_roles_sorted_by_name(cfg):
    roles.save_role("Zebra Role", "z prompt")
    roles.save_role("Apple Role", "a prompt")
    loaded = roles.load_roles()
    assert [r["name"] for r in loaded] == ["Apple Role", "Zebra Role"]


def test_load_roles_skips_unparsable_files(cfg):
    roles_dir = cfg / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)
    (roles_dir / "broken.yaml").write_text(
        "name: [unclosed\n", encoding="utf-8"
    )
    (roles_dir / "scalar.yaml").write_text("just a string\n", encoding="utf-8")
    roles.save_role("Good Role", "prompt")
    loaded = roles.load_roles()
    assert [r["slug"] for r in loaded] == ["good-role"]


def test_load_roles_skips_empty_prompt(cfg):
    roles_dir = cfg / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)
    (roles_dir / "empty-prompt.yaml").write_text(
        "name: Empty Prompt\nslug: empty-prompt\nprompt: \"\"\n",
        encoding="utf-8",
    )
    (roles_dir / "no-name.yaml").write_text(
        "name: ''\nslug: no-name\nprompt: x\n",
        encoding="utf-8",
    )
    roles.save_role("Fine Role", "prompt")
    loaded = roles.load_roles()
    assert [r["slug"] for r in loaded] == ["fine-role"]


def test_json_fallback_when_pyyaml_missing(cfg, monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("No module named 'yaml'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    slug, err = roles.save_role("Fallback Role", "works offline")
    assert err is None
    assert slug == "fallback-role"
    assert (cfg / "roles" / "fallback-role.json").is_file()
    assert not (cfg / "roles" / "fallback-role.yaml").exists()

    assert roles.load_role("fallback-role") == {
        "name": "Fallback Role",
        "slug": "fallback-role",
        "prompt": "works offline",
    }
    assert [r["slug"] for r in roles.load_roles()] == ["fallback-role"]


def test_slug_collapses_runs_of_spaces_hyphens_and_underscores():
    assert roles.role_slug("Hello World") == "hello-world"
    assert roles.role_slug("Hello  World") == "hello-world"
    assert roles.role_slug("Hello - World") == "hello-world"
    assert roles.role_slug("Hello--World") == "hello-world"
    assert roles.role_slug("a - b") == "a-b"
    assert roles.role_slug("a  -  b") == "a-b"
    assert roles.role_slug("  Hello  ") == "hello"
    # Runs of hyphens/underscores collapse to a single hyphen; a lone
    # underscore is preserved.
    assert roles.role_slug("My--Role") == "my-role"
    assert roles.role_slug("My__Role") == "my-role"
    assert roles.role_slug("My-_Role") == "my-role"
    assert roles.role_slug("My_-_Role") == "my-role"
    assert roles.role_slug("Hello__World") == "hello-world"
    assert roles.role_slug("My_Role") == "my_role"
    # Tabs are dropped by the Unicode/alnum policy (existing behavior).
    assert roles.role_slug("Hello\tWorld") == "helloworld"
    assert roles.role_slug("Hello \t World") == "hello-world"
    assert roles.role_slug("\tHello\t") == "hello"


def test_save_multi_space_name_uses_collapsed_slug(cfg):
    slug, err = roles.save_role("Hello  World", "hello prompt")
    assert err is None
    assert slug == "hello-world"
    assert (cfg / "roles" / "hello-world.yaml").is_file()
    assert roles.load_role("hello-world")["name"] == "Hello  World"


def test_save_role_preserves_prompt_whitespace_verbatim(cfg):
    prompt = "  Hello world  \n"
    slug, err = roles.save_role("Padding Role", prompt)
    assert err is None
    assert slug == "padding-role"
    assert roles.load_role("padding-role")["prompt"] == prompt


def test_load_role_roundtrip_stored_slug_differs_from_filename(cfg):
    roles_dir = cfg / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)
    # Hand-edited file whose stored slug differs from its filename.
    (roles_dir / "odd-file.yaml").write_text(
        "name: Odd\nslug: odd-role\nprompt: x\n",
        encoding="utf-8",
    )

    # load_roles exposes the stored slug as the role identity.
    loaded_all = roles.load_roles()
    assert loaded_all == [{"name": "Odd", "slug": "odd-role", "prompt": "x"}]

    # load_role agrees: by stored slug (scan fallback)...
    assert roles.load_role("odd-role") == loaded_all[0]
    # ...and by filename (returns the STORED slug, not the requested one).
    assert roles.load_role("odd-file") == loaded_all[0]

    # A fresh save of that name writes a separate file and never overwrites
    # the hand-edited file.
    slug, err = roles.save_role("Odd Role", "fresh prompt")
    assert err is None
    assert slug == "odd-role"
    assert (roles_dir / "odd-role.yaml").is_file()
    assert (roles_dir / "odd-file.yaml").read_text(encoding="utf-8") == (
        "name: Odd\nslug: odd-role\nprompt: x\n"
    )
    assert roles.load_role("odd-file")["prompt"] == "x"
    assert roles.load_role("odd-role")["prompt"] == "fresh prompt"


def test_load_role_isolated_yml_and_json_files(cfg):
    roles_dir = cfg / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)
    (roles_dir / "checker.yml").write_text(
        "name: Checker\nslug: checker\nprompt: yml prompt\n",
        encoding="utf-8",
    )
    (roles_dir / "reviewer.json").write_text(
        '{"name": "Reviewer", "slug": "reviewer", "prompt": "json prompt"}',
        encoding="utf-8",
    )
    loaded = roles.load_roles()
    assert [r["slug"] for r in loaded] == ["checker", "reviewer"]
    assert roles.load_role("checker")["prompt"] == "yml prompt"
    assert roles.load_role("reviewer")["prompt"] == "json prompt"


def test_load_roles_only_yml_files(cfg):
    roles_dir = cfg / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)
    (roles_dir / "checker.yml").write_text(
        "name: Checker\nslug: checker\nprompt: yml prompt\n",
        encoding="utf-8",
    )
    (roles_dir / "reviewer.yml").write_text(
        "name: Reviewer\nslug: reviewer\nprompt: yml second\n",
        encoding="utf-8",
    )
    loaded = roles.load_roles()
    assert [r["slug"] for r in loaded] == ["checker", "reviewer"]
    assert roles.load_role("checker")["prompt"] == "yml prompt"
    assert roles.load_role("reviewer")["prompt"] == "yml second"


def test_load_roles_only_json_files(cfg):
    roles_dir = cfg / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)
    (roles_dir / "checker.json").write_text(
        '{"name": "Checker", "slug": "checker", "prompt": "json prompt"}',
        encoding="utf-8",
    )
    (roles_dir / "reviewer.json").write_text(
        '{"name": "Reviewer", "slug": "reviewer", "prompt": "json second"}',
        encoding="utf-8",
    )
    loaded = roles.load_roles()
    assert [r["slug"] for r in loaded] == ["checker", "reviewer"]
    assert roles.load_role("checker")["prompt"] == "json prompt"
    assert roles.load_role("reviewer")["prompt"] == "json second"


def test_save_load_roundtrip_after_file_rename(cfg):
    slug, err = roles.save_role("My Role", "rename prompt")
    assert err is None
    assert slug == "my-role"
    assert (cfg / "roles" / "my-role.yaml").is_file()

    # Rename the file behind the role's back; the stored slug stays.
    (cfg / "roles" / "my-role.yaml").rename(cfg / "roles" / "renamed.yaml")

    loaded_all = roles.load_roles()
    assert loaded_all == [
        {"name": "My Role", "slug": "my-role", "prompt": "rename prompt"}
    ]
    # Resolved by stored slug (consistent with load_roles)...
    assert roles.load_role("my-role") == loaded_all[0]
    # ...and by the file's (new) name.
    assert roles.load_role("renamed") == loaded_all[0]


def test_load_role_lone_yml_file(cfg):
    roles_dir = cfg / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)
    (roles_dir / "lone.yml").write_text(
        "name: Lone\nslug: lone\nprompt: yml only\n",
        encoding="utf-8",
    )
    assert [r["slug"] for r in roles.load_roles()] == ["lone"]
    assert roles.load_role("lone")["prompt"] == "yml only"


def test_load_role_lone_json_file(cfg):
    roles_dir = cfg / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)
    (roles_dir / "lone.json").write_text(
        '{"name": "Lone", "slug": "lone", "prompt": "json only"}',
        encoding="utf-8",
    )
    assert [r["slug"] for r in roles.load_roles()] == ["lone"]
    assert roles.load_role("lone")["prompt"] == "json only"
