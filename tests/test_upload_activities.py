import json

import httpx
import pytest
from bilibili_api import Credential
from typer.testing import CliRunner

from bili_cli import output
from bili_cli.commands import upload as upload_cmd
from bili_cli.main import _hoist_global_flags, app


RECOMMENDED = {"topic_id": 123, "topic_name": "创作活动", "mission_id": 456, "description": "活动介绍", "activity_text": "有奖活动"}
SEARCHED = {"id": 123, "name": "创作活动", "mission_id": 456, "description": "活动介绍", "activity_sign": "活动说明", "act_protocol": "协议说明"}
VIEW = {
    "archive": {
        "aid": 1, "bvid": "BV1GJ411x7h7", "title": "原标题", "copyright": 1, "tid": 231,
        "tag": "创作活动,AI", "topic_id": 123, "topic_name": "创作活动", "mission_id": 456, "mission_name": "活动",
        "topic_detail": {"from_topic_id": 123, "from_source": "arc.web.search"},
        "creation_statement": {"id": 1}, "cover": "https://i0.hdslb.com/cover.jpg",
    },
    "videos": [{"filename": "part1", "title": "原标题", "cid": 7}],
}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BILI_CLI_HOME", str(tmp_path))
    for flag in ("dry_run", "raw", "wait"):
        monkeypatch.setattr(output.settings, flag, False)

    def unexpected_request(request):
        pytest.fail(f"Unexpected HTTP request: {request.method} {request.url}")

    mock_http(monkeypatch, unexpected_request)


@pytest.fixture
def credential(monkeypatch):
    cred = Credential(sessdata="test-session", bili_jct="test-csrf")
    monkeypatch.setattr(upload_cmd.config, "require_credential", lambda: cred)
    return cred


def mock_http(monkeypatch, handler):
    monkeypatch.setattr(upload_cmd, "web_client", lambda *args, **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def invoke(*args):
    result = CliRunner().invoke(app, _hoist_global_flags(list(args)))
    assert result.output, repr(result.exception)
    return result, json.loads(result.output)


def test_recommended_activities_use_legacy_zone_and_zero_based_page(monkeypatch, credential):
    def handler(request):
        assert str(request.url).startswith(upload_cmd.TOPICS_URL)
        assert dict(request.url.params) == {"type_id": "231", "pn": "1", "ps": "5"}
        return httpx.Response(200, json={"code": 0, "data": {"topics": [RECOMMENDED, {"topic_id": 9, "topic_name": "普通话题", "mission_id": 0}]}})

    mock_http(monkeypatch, handler)
    result, out = invoke("upload", "activities", "--tid", "231", "--page", "2", "--size", "5")
    assert result.exit_code == 0
    data = out["data"]
    assert len(data["items"]) == 1
    assert data["items"][0]["name"] == "创作活动"
    assert data["items"][0]["use_with"] == "--topic-id 123 --mission-id 456"
    assert data["page"] == 2 and data["has_more"] is None


def test_search_activities_preserves_upstream_offset_and_protocol(monkeypatch, credential):
    def handler(request):
        assert str(request.url).startswith(upload_cmd.TOPIC_SEARCH_URL)
        assert dict(request.url.params) == {"keywords": "创作", "page_size": "20", "offset": "37"}
        return httpx.Response(200, json={"code": 0, "data": {"result": {
            "topics": [SEARCHED], "page_info": {"has_more": True, "offset": 83}, "tips": "活动提示",
        }}})

    mock_http(monkeypatch, handler)
    result, out = invoke("upload", "activities", "-k", " 创作 ", "--offset", "37")
    assert result.exit_code == 0
    data = out["data"]
    assert data["next_offset"] == 83 and data["has_more"] is True
    assert data["tips"] == "活动提示"
    assert data["items"][0]["topic_id"] == 123
    assert data["items"][0]["mission_id"] == 456
    assert data["items"][0]["act_protocol"] == "协议说明"


def test_empty_filtered_search_page_still_returns_next_offset(monkeypatch, credential):
    mock_http(monkeypatch, lambda r: httpx.Response(200, json={"code": 0, "data": {"result": {
        "topics": [{"id": 9, "name": "普通话题", "mission_id": 0}], "page_info": {"has_more": True, "offset": 17},
    }}}))
    result, out = invoke("upload", "activities", "-k", "话题")
    assert result.exit_code == 0 and out["data"]["items"] == []
    assert out["data"]["next_offset"] == 17
    result, out = invoke("upload", "activities", "-k", "话题", "--all-topics")
    assert result.exit_code == 0
    assert out["data"]["items"][0]["use_with"] == "--topic-id 9"


def test_raw_activities_returns_unmodified_data(monkeypatch, credential):
    raw = {"result": {"topics": [SEARCHED], "extra": [1, 2]}}
    mock_http(monkeypatch, lambda r: httpx.Response(200, json={"code": 0, "data": raw}))
    result, out = invoke("upload", "activities", "-k", "创作", "--raw")
    assert result.exit_code == 0 and out["data"] == raw


@pytest.mark.parametrize("args", [
    ["-k", " "], ["-k", "创作", "--tid", "231"], ["-k", "创作", "--page", "2"], ["--offset", "1"],
])
def test_activity_query_rejects_ambiguous_arguments(args):
    result, out = invoke("upload", "activities", *args)
    assert result.exit_code == 2 and out["error"]["code"] == "invalid_argument"


def test_activities_requires_login():
    result, out = invoke("upload", "activities")
    assert result.exit_code == 3 and out["error"]["code"] == "not_logged_in"


@pytest.mark.parametrize("code,expected", [(-101, "not_logged_in"), (-352, "risk_control"), (12015, "captcha_required"), (-509, "rate_limited"), (99999, "api_error")])
def test_activity_api_errors_keep_the_standard_error_contract(monkeypatch, credential, code, expected):
    mock_http(monkeypatch, lambda r: httpx.Response(200, json={"code": code, "message": "测试错误"}))
    result, out = invoke("upload", "activities")
    assert result.exit_code != 0 and out["error"]["code"] == expected
    assert out["error"]["detail"]["bili_code"] == code


def test_activity_http_risk_control_is_not_retried(monkeypatch, credential):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(412)

    mock_http(monkeypatch, handler)
    result, out = invoke("upload", "activities")
    assert result.exit_code == 5 and out["error"]["code"] == "risk_control"
    assert len(calls) == 1


@pytest.fixture
def upload_args(tmp_path, monkeypatch, credential):
    video, cover = tmp_path / "video.mp4", tmp_path / "cover.png"
    video.write_bytes(b"test-video")
    cover.write_bytes(b"test-cover")

    async def zones(cred):
        return [{"id": 1010, "name": "知识"}]

    async def check_tags(cred, tags):
        return []

    monkeypatch.setattr(upload_cmd, "_zones", zones)
    monkeypatch.setattr(upload_cmd, "_check_tags", check_tags)
    return ["upload", "video", str(video), "--title", "测试", "--tag", "创作活动", "--cover", str(cover), "--tid", "231", "--zone", "1010"]


@pytest.mark.parametrize("flags,fields", [
    ([], {}),
    (["--mission-id", "456"], {"mission_id": 456}),
    (["--topic-id", "123"], {"topic_id": 123, "topic_detail": {"from_topic_id": 123, "from_source": "arc.web.recommend"}}),
    (["--topic-id", "123", "--mission-id", "456"], {"topic_id": 123, "mission_id": 456, "topic_detail": {"from_topic_id": 123, "from_source": "arc.web.recommend"}}),
])
def test_upload_dry_run_previews_activity_without_writes(monkeypatch, upload_args, flags, fields):
    monkeypatch.setattr(upload_cmd.guard, "check", lambda *a: pytest.fail("dry run must not check write rate limits"))
    monkeypatch.setattr(upload_cmd.guard, "record", lambda *a: pytest.fail("dry run must not record a write"))
    monkeypatch.setattr(upload_cmd, "_preflight", lambda *a: pytest.fail("dry run must not reserve an upload slot"))
    monkeypatch.setattr(upload_cmd.vu, "VideoUploader", lambda *a, **kw: pytest.fail("dry run must not upload"))
    result, out = invoke(*upload_args, *flags, "--dry-run")
    assert result.exit_code == 0 and out["data"]["dry_run"]
    meta = out["data"]["meta"]
    assert {k: meta[k] for k in ("topic_id", "mission_id", "topic_detail") if k in meta} == fields
    assert meta["creation_statement"] == {"id": 1}
    for key in ("topic_id", "mission_id"):
        if key in fields:
            assert out["data"]["params"][key] == fields[key]


def test_upload_passes_activity_to_uploader_and_audit(monkeypatch, upload_args):
    captured, events = {}, []

    async def preflight(*args):
        events.append("preflight")

    class Uploader:
        def __init__(self, pages, meta, cred, **kwargs):
            captured.update(meta)

        async def start(self):
            events.append("submit")
            return {"bvid": "BV1GJ411x7h7", "aid": 1}

    monkeypatch.setattr(upload_cmd, "_preflight", preflight)
    monkeypatch.setattr(upload_cmd.Picture, "from_file", lambda path: object())
    monkeypatch.setattr(upload_cmd.vu, "VideoUploader", Uploader)
    monkeypatch.setattr(upload_cmd.guard, "check", lambda action: events.append(action))
    monkeypatch.setattr(upload_cmd.guard, "record", lambda action, params, result: events.append((action, params, result)))
    result, out = invoke(*upload_args, "--topic-id", "123", "--mission-id", "456")
    assert result.exit_code == 0 and out["ok"]
    assert captured["topic_id"] == 123 and captured["mission_id"] == 456
    assert captured["topic_detail"]["from_topic_id"] == 123
    assert events[:3] == ["upload", "preflight", "submit"]
    assert events[3][0] == "upload" and events[3][1]["mission_id"] == 456


def test_build_edit_preserves_activity_even_when_changing_tags():
    body = upload_cmd.build_edit(VIEW, {"title": "新标题", "tags": ["创作活动", "教程"]})
    assert body["topic_id"] == 123 and body["mission_id"] == 456
    assert body["topic_detail"] == VIEW["archive"]["topic_detail"]
    assert body["creation_statement"] == {"id": 1}
    assert VIEW["archive"]["title"] == "原标题"


def test_build_edit_updates_topic_source_without_mutating_original():
    body = upload_cmd.build_edit(VIEW, {"topic_id": 321, "mission_id": 654})
    assert body["topic_id"] == 321 and body["mission_id"] == 654
    assert body["topic_detail"] == {"from_topic_id": 321, "from_source": "arc.web.recommend"}
    assert VIEW["archive"]["topic_detail"]["from_topic_id"] == 123


@pytest.mark.parametrize("arc", [{}, {"topic_id": 0, "mission_id": 0}])
def test_edit_without_activity_keeps_absent_or_zero_fields(arc):
    body = upload_cmd.build_edit({"archive": arc}, {"title": "new"})
    for key in ("topic_id", "mission_id"):
        assert body.get(key) == arc.get(key)
        assert (key in body) == (key in arc)
    assert "topic_detail" not in body


@pytest.fixture
def archive_view(monkeypatch, credential):
    async def view(*args):
        return VIEW

    monkeypatch.setattr(upload_cmd, "_archive_view", view)


def test_edit_dry_run_shows_activity_changes(monkeypatch, archive_view):
    monkeypatch.setattr(upload_cmd.guard, "check", lambda *a: pytest.fail("dry run must not write"))
    result, out = invoke("upload", "edit", "BV1GJ411x7h7", "--topic-id", "321", "--mission-id", "654", "--dry-run")
    assert result.exit_code == 0
    assert out["data"]["changes"] == {"topic_id": {"from": 123, "to": 321}, "mission_id": {"from": 456, "to": 654}}
    assert out["data"]["payload"]["mission_id"] == 654


def test_edit_submits_activity_fields_and_audits_change(monkeypatch, archive_view):
    payload, events = {}, []

    class EditApi:
        def __init__(self, **kwargs):
            assert kwargs["url"] == upload_cmd.EDIT_URL

        def update_params(self, **kwargs):
            return self

        def update_data(self, **kwargs):
            payload.update(kwargs)
            return self

        def update_headers(self, **kwargs):
            return self

        @property
        async def result(self):
            events.append("submit")
            return {}

    monkeypatch.setattr(upload_cmd, "Api", EditApi)
    monkeypatch.setattr(upload_cmd.guard, "check", lambda action: events.append(action))
    monkeypatch.setattr(upload_cmd.guard, "record", lambda action, params, result: events.append((action, params)))
    result, out = invoke("upload", "edit", "BV1GJ411x7h7", "--topic-id", "321", "--mission-id", "654")
    assert result.exit_code == 0 and out["ok"]
    assert payload["topic_id"] == 321 and payload["mission_id"] == 654
    assert events[:2] == ["upload_edit", "submit"]
    assert events[2][1]["changes"]["mission_id"]["to"] == 654


def test_status_exposes_saved_activity(archive_view):
    result, out = invoke("upload", "status", "BV1GJ411x7h7")
    assert result.exit_code == 0
    assert {k: out["data"][k] for k in ("topic_id", "topic_name", "mission_id", "mission_name")} == {
        "topic_id": 123, "topic_name": "创作活动", "mission_id": 456, "mission_name": "活动",
    }


@pytest.mark.parametrize("command", ["video", "edit"])
@pytest.mark.parametrize("flag,value", [("--topic-id", "0"), ("--mission-id", "-1")])
def test_activity_ids_must_be_positive(command, flag, value):
    args = ["upload", command, "missing.mp4", "--title", "t", "--tag", "a"] if command == "video" else ["upload", command, "BV1GJ411x7h7"]
    result = CliRunner().invoke(app, [*args, flag, value])
    assert result.exit_code == 2


def test_schema_discovers_activity_command_and_parameters():
    result, out = invoke("schema")
    assert result.exit_code == 0
    commands = {c["command"]: c for c in out["data"]["commands"]}
    assert "bili upload activities" in commands
    for name in ("bili upload video", "bili upload edit"):
        flags = {flag for p in commands[name]["params"] for flag in p["flags"] or []}
        assert {"--topic-id", "--mission-id"} <= flags
