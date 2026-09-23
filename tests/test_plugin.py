"""The uploadable ChatGPT / Codex plugin package."""
import io
import json
import struct
import zipfile
from pathlib import Path

import pytest

from webcoder.hub.plugin import PLUGIN_NAME, SKILL_NAME, build_plugin_zip, build_skill_zip, plugin_files

jsonschema = pytest.importorskip("jsonschema")
FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://hub.example.com"


def names(data: bytes) -> list[str]:
    return zipfile.ZipFile(io.BytesIO(data)).namelist()


def test_one_plugin_root_with_both_manifest_layouts():
    entries = names(build_plugin_zip(URL))
    assert {name.split("/")[0] for name in entries} == {PLUGIN_NAME}
    assert {name.split("/", 1)[1] for name in entries} == {
        "plugin.json", "mcp.json", ".codex-plugin/plugin.json", ".mcp.json", f"skills/{SKILL_NAME}/SKILL.md",
        f"skills/{SKILL_NAME}/agents/openai.yaml", "assets/logo.png", "assets/icon.svg"}


@pytest.mark.parametrize("app_id", [None, "plugin_asdk_app_0123abcd"])
def test_portable_manifests_match_agent_plugins_schemas(app_id):
    files = plugin_files(URL, app_id)
    for name, schema_file in (("plugin.json", "agent-plugins-1.0.0-plugin.schema.json"),
                              ("mcp.json", "agent-plugins-1.0.0-mcp.schema.json")):
        schema = json.loads((FIXTURES / schema_file).read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(json.loads(files[name]))
    assert json.loads(files["mcp.json"])["mcpServers"] == {
        "webcoder": {"type": "streamable-http", "url": URL + "/mcp"}}


def test_codex_manifest_references_files_that_exist():
    files = plugin_files(URL, "asdk_app_abc123")
    manifest = json.loads(files[".codex-plugin/plugin.json"])
    for key in ("skills", "mcpServers", "apps"):
        path = manifest[key]
        assert path.startswith("./") and ".." not in path
        assert any(name == path[2:] or name.startswith(path[2:]) for name in files), key
    interface = manifest["interface"]
    assert interface["logo"][2:] in files and interface["composerIcon"][2:] in files
    assert len(interface["defaultPrompt"]) <= 3 and all(len(prompt) <= 128 for prompt in interface["defaultPrompt"])
    assert json.loads(files[".mcp.json"]) == {"mcpServers": {"webcoder": {"type": "http", "url": URL + "/mcp"}}}
    assert json.loads(files[".app.json"]) == {"apps": {PLUGIN_NAME: {"id": "asdk_app_abc123"}}}


def test_app_reference_only_when_an_app_id_is_given():
    files = plugin_files(URL)
    assert ".app.json" not in files and "apps" not in json.loads(files[".codex-plugin/plugin.json"])
    with pytest.raises(ValueError):
        plugin_files(URL, "../../etc")


def test_skill_frontmatter_and_skill_only_zip():
    skill = plugin_files(URL)[f"skills/{SKILL_NAME}/SKILL.md"].decode("utf-8")
    _, front, body = skill.split("---", 2)
    assert f"name: {SKILL_NAME}" in front and "description: " in front
    assert "expected_sha256" in body and "list_projects" in body
    assert sorted(names(build_skill_zip())) == [f"{SKILL_NAME}/SKILL.md", f"{SKILL_NAME}/agents/openai.yaml"]


def test_logo_is_a_valid_png_and_builds_are_reproducible():
    logo = plugin_files(URL)["assets/logo.png"]
    assert logo[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", logo[16:24]) == (256, 256)
    assert build_plugin_zip(URL) == build_plugin_zip(URL)


def test_panel_download_uses_the_hub_public_url(hub):
    assert hub.http.get("/api/chatgpt-plugin.zip").status_code == 401
    response = hub.panel.get("/api/chatgpt-plugin.zip")
    assert response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert json.loads(archive.read(f"{PLUGIN_NAME}/mcp.json"))["mcpServers"]["webcoder"]["url"] == hub.url + "/mcp"
    assert hub.panel.get("/api/chatgpt-plugin.zip", params={"app_id": "../x"}).status_code == 400
    with_app = zipfile.ZipFile(io.BytesIO(hub.panel.get("/api/chatgpt-plugin.zip",
                                                        params={"app_id": "plugin_asdk_app_42abc"}).content))
    assert f"{PLUGIN_NAME}/.app.json" in with_app.namelist()
    assert hub.panel.get("/api/chatgpt-skill.zip").status_code == 200
