"""bili upload: publish videos and track their review status.

Uploading goes through bilibili-api's VideoUploader with a hand-built meta
dict, because VideoMeta cannot express newer submit fields such as the
creation statement ("含AI生成内容"), private visibility or the new zone ID.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import typer
from bilibili_api import Credential, Picture
from bilibili_api import creative_center as cc
from bilibili_api import video_uploader as vu
from bilibili_api.utils.network import Api

from .. import config, guard, output
from ..errors import BiliError
from ..runtime import CST, iso_time, parse_timestamp, read_text, run, web_client

app = typer.Typer(help="视频投稿与稿件状态。", no_args_is_help=True)

PREDICT_URL = "https://member.bilibili.com/x/vupre/web/archive/types/predict"
PREUPLOAD_URL = "https://member.bilibili.com/preupload"
ZONES_URL = "https://member.bilibili.com/x/vupre/web/archive/human/type2/list"
# bilibili-api calls this with `t=`, which the server ignores; the parameter is `tag`.
TAG_CHECK_URL = "https://member.bilibili.com/x/vupre/web/topic/tag/check"
TOPICS_URL = "https://member.bilibili.com/x/vupre/web/topic/type"
TOPIC_SEARCH_URL = "https://member.bilibili.com/x/vupre/web/topic/search"
ARCHIVE_VIEW_URL = "https://member.bilibili.com/x/vupre/web/archive/view"
EDIT_URL = "https://member.bilibili.com/x/vu/web/edit"
# Chapter endpoints, as used by B 站's player/editor bundle (scene=chapter).
CHAPTER_RULE_URL = "https://member.bilibili.com/x/web/viewpoint/rule"
CHAPTER_SUBMIT_URL = "https://member.bilibili.com/x/web/card/submit"
ALLCARDS_URL = "https://member.bilibili.com/x/web/allcards"
CARD_TYPE_CHAPTER = 2

TITLE_MAX = 80
DESC_MAX = 2000
DESC_AT_OPT = typer.Option([], "--desc-at", help="简介里要 @ 的人：UID、完整昵称或 owner，可重复传入，最多 10 个；简介里没写 @昵称 时会加在开头。")
DESC_MENTIONS_MAX = 10
DYNAMIC_MAX = 233
TAGS_MAX = 10
SCHEDULE_MIN_SECONDS = 2 * 3600  # Bilibili accepts scheduled publishing 2 h to 15 days ahead
SCHEDULE_MAX_SECONDS = 15 * 24 * 3600

# creation_statement ids used by the web uploader
STATEMENT_NONE = -1
STATEMENT_AI = 1

STATUS_FILTERS = {
    "all": cc.UploadManagerStatus.ALL,
    "published": cc.UploadManagerStatus.PUBED,
    "reviewing": cc.UploadManagerStatus.IS_PUBING,
    "rejected": cc.UploadManagerStatus.NOT_PUBED,
}


async def _predict_zone(cred: Credential, title: str) -> dict[str, Any] | None:
    """Ask Bilibili which zone fits the title, like the web uploader does."""
    async with web_client(cred, referer="https://member.bilibili.com/platform/upload/video/frame") as client:
        resp = await client.post(
            PREDICT_URL,
            params={"csrf": cred.bili_jct, "ts": int(time.time() * 1000)},
            files={"filename": (None, ""), "title": (None, title)},
        )
        resp.raise_for_status()
        body = resp.json()
    data = body.get("data") or []
    if body.get("code") != 0 or not data:
        return None
    # Two independent guesses: the legacy zone (tid, still required by submit)
    # and the new zone (human_type2), which is what viewers see nowadays.
    first = data[0]
    human = first.get("human_type") or {}
    return {
        "tid": first.get("id"),
        "tid_name": f"{first.get('parent_name')} / {first.get('name')}",
        "zone": human.get("id"),
        "zone_name": human.get("name"),
    }


async def _zones(cred: Credential) -> list[dict[str, Any]]:
    async with web_client(cred, referer="https://member.bilibili.com/") as client:
        resp = await client.get(ZONES_URL)
        resp.raise_for_status()
        body = resp.json()
    data = body.get("data") or {}
    return [{"id": z.get("id"), "name": z.get("name")} for z in (data.get("type_list") or body.get("type_list") or [])]


async def _check_tags(cred: Credential, tags: list[str]) -> list[str]:
    """Return the tags Bilibili rejects outright (it may still drop others after submit)."""
    bad = []
    for t in tags:
        res = await Api(url=TAG_CHECK_URL, method="GET", verify=True, credential=cred, ignore_code=True).update_params(tag=t).result
        if res.get("code") != 0:
            bad.append(t)
    return bad


async def _activity_topics(cred: Credential, keyword: str | None, tid: int | None, page: int, size: int, offset: int) -> dict[str, Any]:
    """Use the uploader's topics, whose mission IDs can be passed to submit."""
    if keyword is not None:
        url = TOPIC_SEARCH_URL
        params = {"keywords": keyword, "page_size": size, "offset": offset}
    else:
        url = TOPICS_URL
        params = {"pn": page - 1, "ps": size}
        if tid is not None:
            params["type_id"] = tid
    async with web_client(cred, referer="https://member.bilibili.com/platform/upload/video/frame") as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        body = resp.json()
    if body.get("code") != 0:
        from bilibili_api.exceptions import ResponseCodeException

        raise ResponseCodeException(body.get("code"), body.get("message"), body)
    return body.get("data") or {}


def _activity_card(topic: dict[str, Any]) -> dict[str, Any]:
    topic_id = topic.get("topic_id") or topic.get("id")
    mission_id = topic.get("mission_id") or 0
    card = {
        "topic_id": topic_id,
        "mission_id": mission_id,
        "name": topic.get("topic_name") or topic.get("name"),
        "description": topic.get("description") or None,
        "activity_text": topic.get("activity_text") or None,
        "activity_description": topic.get("activity_description") or None,
        "activity_sign": topic.get("activity_sign") or None,
        "act_protocol": topic.get("act_protocol") or None,
    }
    flags = []
    if topic_id:
        flags.append(f"--topic-id {topic_id}")
    if mission_id:
        flags.append(f"--mission-id {mission_id}")
    card["use_with"] = " ".join(flags)
    return card


def _topic_fields(topic_id: int | None, mission_id: int | None) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if topic_id is not None:
        fields["topic_id"] = topic_id
        fields["topic_detail"] = {"from_topic_id": topic_id, "from_source": "arc.web.recommend"}
    if mission_id is not None:
        fields["mission_id"] = mission_id
    return fields


@app.command()
def activities(
    keyword: Optional[str] = typer.Option(None, "--keyword", "-k", help="按关键词搜索投稿活动；不传则查看推荐活动。"),
    tid: Optional[int] = typer.Option(None, "--tid", min=1, help="推荐活动的旧版分区 ID；不与 --keyword 同用。"),
    page: int = typer.Option(1, "--page", min=1, help="推荐活动页码，从 1 开始；关键词搜索用 --offset。"),
    size: int = typer.Option(20, "--size", min=1, max=50, help="每页获取的话题数，过滤后活动数可能更少。"),
    offset: int = typer.Option(0, "--offset", min=0, help="关键词搜索偏移量；下一页用返回的 next_offset。"),
    all_topics: bool = typer.Option(False, "--all-topics", help="同时返回没有活动关联的普通话题。"),
) -> None:
    """查询投稿活动，返回可供 upload video / edit 使用的 topic_id、mission_id。"""

    async def body():
        query = keyword.strip() if keyword is not None else None
        if query == "":
            raise BiliError("invalid_argument", "活动搜索关键词不能为空。")
        if query is not None and (tid is not None or page != 1):
            raise BiliError("invalid_argument", "关键词搜索不能同时指定 --tid 或 --page；翻页请用 --offset。")
        if query is None and offset != 0:
            raise BiliError("invalid_argument", "--offset 只用于关键词搜索；推荐活动翻页请用 --page。")
        data = await _activity_topics(config.require_credential(), query, tid, page, size, offset)
        if output.settings.raw:
            return data
        result = data.get("result") or data
        topics = result.get("topics") or []
        page_info = result.get("page_info") or {}
        return {
            "keyword": query,
            "tid": tid,
            "page": page if query is None else None,
            "offset": offset if query is not None else None,
            "has_more": page_info.get("has_more"),
            "next_offset": page_info.get("offset") if query is not None and page_info.get("has_more") else None,
            "items": [_activity_card(t) for t in topics if all_topics or t.get("mission_id")],
            "tips": result.get("tips") or None,
        }

    run(body)


async def _preflight(cred: Credential, video: Path) -> None:
    """Ask for an upload slot first, so a refusal comes back with Bilibili's reason.

    bilibili-api turns a refused preupload into a bare "HTTP 406"; the body
    actually says why, e.g. code 601 with a v_voucher when the account must
    pass a verification challenge (common for new accounts).
    """
    async with web_client(cred, referer="https://member.bilibili.com/") as client:
        resp = await client.get(
            PREUPLOAD_URL,
            params={"profile": "ugcfx/bup", "name": video.name, "size": video.stat().st_size, "r": "upos", "ssl": "0", "version": "2.14.0", "build": "2100400"},
        )
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code < 400 and body.get("OK") == 1:
        return
    message = body.get("message") or body.get("info") or f"HTTP {resp.status_code}"
    detail = {"bili_code": body.get("code"), "http_status": resp.status_code}
    if (body.get("detail") or {}).get("v_voucher"):
        raise BiliError(
            "captcha_required",
            f"B 站拒绝了上传：{message}",
            hint="账号触发了风控验证。请人类在网页端 https://member.bilibili.com/platform/upload/video/frame 手动上传一次并完成验证，或过一段时间再试；新号较常见。",
            detail=detail,
        )
    raise BiliError("risk_control" if resp.status_code in (406, 412) else "api_error", f"B 站拒绝了上传：{message}", detail=detail)


def _extract_cover(video: Path) -> Path:
    if not shutil.which("ffmpeg"):
        raise BiliError("invalid_argument", "未指定 --cover，且系统中没有 ffmpeg，无法自动截取封面。", hint="用 --cover 指定封面图片，或安装 ffmpeg。")
    out = Path(tempfile.mkdtemp(prefix="bili-cover-")) / "cover.jpg"
    for seek in ("3", "0"):  # very short videos have no frame at 3 s
        proc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", seek, "-i", str(video), "-frames:v", "1", "-q:v", "2", str(out)],
            capture_output=True,
        )
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return out
    raise BiliError("invalid_argument", f"无法从视频中截取封面：{proc.stderr.decode(errors='ignore')[-300:]}", hint="用 --cover 指定封面图片。")


def _parse_schedule(value: str | None) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise BiliError("invalid_argument", f"--schedule 需要 ISO 时间，如 2026-10-21T20:00，收到 {value!r}。") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)  # Bilibili users think in Beijing time
    ts = int(dt.timestamp())
    delta = ts - time.time()
    if not SCHEDULE_MIN_SECONDS <= delta <= SCHEDULE_MAX_SECONDS:
        raise BiliError("invalid_argument", "定时发布时间必须在 2 小时到 15 天之后。", detail={"schedule": iso_time(ts)})
    return ts


def _progress(uploader: vu.VideoUploader) -> None:
    """Upload progress for humans, on stderr only."""

    def on_chunk(data: dict) -> None:
        done, total = data.get("chunk_number", 0) + 1, data.get("total_chunk_count", 0)
        if total:
            sys.stderr.write(f"\r上传中 {done}/{total} 块 ({done * 100 // total}%)")
            sys.stderr.flush()

    uploader.add_event_listener(vu.VideoUploaderEvents.AFTER_CHUNK.value, on_chunk)
    uploader.add_event_listener(vu.VideoUploaderEvents.AFTER_PAGE.value, lambda d: output.info("\n分 P 上传完成"))
    uploader.add_event_listener(vu.VideoUploaderEvents.PRE_SUBMIT.value, lambda d: output.info("正在提交稿件…"))


@app.command("video")
def video_(
    files: list[Path] = typer.Argument(..., help="视频文件；传多个即为多 P，按顺序排列。"),
    title: str = typer.Option(..., "--title", help=f"标题，最多 {TITLE_MAX} 字。"),
    tag: list[str] = typer.Option(..., "--tag", help=f"标签，可重复传入，1 到 {TAGS_MAX} 个。"),
    desc: Optional[str] = typer.Option(None, "--desc", help=f"简介，最多 {DESC_MAX} 字。"),
    desc_file: Optional[Path] = typer.Option(None, "--desc-file", help="从文件读取简介，`-` 表示 stdin。"),
    desc_at: list[str] = DESC_AT_OPT,
    cover: Optional[Path] = typer.Option(None, "--cover", help="封面图片；不传则用 ffmpeg 从视频截取一帧。"),
    cover43: Optional[Path] = typer.Option(
        None, "--cover43", help="4:3 封面（手机端推荐流用这一张）；不传则由 B 站从横版封面处理。"
    ),
    zone_id: Optional[int] = typer.Option(
        None, "--zone", help="新版分区 ID（见 `bili upload zones`）；不传则按标题自动预测，预测不一定准。"
    ),
    tid: Optional[int] = typer.Option(None, "--tid", help="旧版分区 ID；不传则按标题自动预测。"),
    topic_id: Optional[int] = typer.Option(None, "--topic-id", min=1, help="投稿话题 ID，见 `bili upload activities`。"),
    mission_id: Optional[int] = typer.Option(None, "--mission-id", min=1, help="投稿活动 ID，与查询结果里的话题 ID 配套使用。"),
    repost: Optional[str] = typer.Option(None, "--repost", help="转载稿件的来源链接；不传即为自制。"),
    ai_declare: bool = typer.Option(
        True, "--ai-declare/--no-ai-declare", help="勾选创作声明“含AI生成内容”。默认开启，关闭需显式传 --no-ai-declare。"
    ),
    private: bool = typer.Option(False, "--private", help="仅自己可见（适合测试）。"),
    schedule: Optional[str] = typer.Option(None, "--schedule", help="定时发布，ISO 时间（默认北京时间），需在 2 小时到 15 天之后。"),
    dynamic: Optional[str] = typer.Option(None, "--dynamic", help=f"同步发布的粉丝动态文字，最多 {DYNAMIC_MAX} 字。"),
    no_reprint: bool = typer.Option(False, "--no-reprint", help="声明未经授权禁止转载。"),
    close_comments: bool = typer.Option(False, "--close-comments", help="关闭评论。"),
    close_danmaku: bool = typer.Option(False, "--close-danmaku", help="关闭弹幕。"),
    line: Optional[str] = typer.Option(None, "--line", help="上传线路：bda2 / qn / ws / bldsa；不传则自动测速。"),
) -> None:
    """上传视频并投稿。默认勾选“含AI生成内容”声明。上传可能耗时较久，进度输出在 stderr。"""

    async def body():
        cred = config.require_credential()
        for f in files:
            if not f.is_file():
                raise BiliError("invalid_argument", f"视频文件不存在：{f}")
        if cover is not None and not cover.is_file():
            raise BiliError("invalid_argument", f"封面文件不存在：{cover}")
        if cover43 is not None and not cover43.is_file():
            raise BiliError("invalid_argument", f"4:3 封面文件不存在：{cover43}")
        if not title.strip() or len(title) > TITLE_MAX:
            raise BiliError("invalid_argument", f"标题需为 1 到 {TITLE_MAX} 字。")
        tags = [t.strip() for t in tag if t.strip()]
        if not 1 <= len(tags) <= TAGS_MAX:
            raise BiliError("invalid_argument", f"标签需为 1 到 {TAGS_MAX} 个。")
        description = read_text(desc, desc_file) if (desc is not None or desc_file is not None) else ""
        description, desc_v2 = build_desc_v2(description, await _desc_mentions(desc_at))
        if len(description) > DESC_MAX:
            raise BiliError("invalid_argument", f"简介最多 {DESC_MAX} 字。")
        if dynamic and len(dynamic) > DYNAMIC_MAX:
            raise BiliError("invalid_argument", f"粉丝动态最多 {DYNAMIC_MAX} 字。")
        line_enum = None
        if line:
            try:
                line_enum = vu.Lines(line)
            except ValueError as exc:
                raise BiliError("invalid_argument", "--line 只能是 bda2 / qn / ws / bldsa。") from exc
        dtime = _parse_schedule(schedule)

        zone: dict[str, Any] = {"tid": tid, "tid_name": None, "zone": zone_id, "zone_name": None}
        if tid is None or zone_id is None:
            predicted = await _predict_zone(cred, title)
            if predicted is None:
                raise BiliError("invalid_argument", "无法自动预测分区。", hint="用 --tid 和 --zone 手动指定分区。")
            if tid is None:
                zone["tid"], zone["tid_name"] = predicted["tid"], predicted["tid_name"]
            if zone_id is None:
                zone["zone"], zone["zone_name"] = predicted["zone"], predicted["zone_name"]
        if zone_id is not None:
            known = {z["id"]: z["name"] for z in await _zones(cred)}
            if zone_id not in known:
                raise BiliError("invalid_argument", f"未知的分区 ID {zone_id}。", hint="运行 `bili upload zones` 查看可用分区。")
            zone["zone_name"] = known[zone_id]
        zone["predicted"] = zone_id is None
        bad_tags = await _check_tags(cred, tags)
        if bad_tags:
            raise BiliError("invalid_argument", f"B 站不接受这些标签：{', '.join(bad_tags)}", detail={"rejected_tags": bad_tags})

        meta: dict[str, Any] = {
            "title": title,
            "copyright": 2 if repost else 1,
            "tid": zone["tid"],
            "tag": ",".join(tags),
            "desc_format_id": 9999,
            "desc": description,
            "recreate": -1,
            "dynamic": dynamic or "",
            "interactive": 0,
            "act_reserve_create": 0,
            "no_disturbance": 0,
            "no_reprint": 1 if no_reprint else 0,
            "subtitle": {"open": 0, "lan": ""},
            "dolby": 0,
            "lossless_music": 0,
            "up_selection_reply": False,
            "up_close_reply": close_comments,
            "up_close_danmu": close_danmaku,
            "web_os": 1,
            "is_only_self": 1 if private else 0,
            "creation_statement": {"id": STATEMENT_AI if ai_declare else STATEMENT_NONE},
        }
        if zone.get("zone"):
            meta["human_type2"] = zone["zone"]
        if desc_v2:
            meta["desc_v2"] = desc_v2
        if repost:
            meta["source"] = repost
        if dtime:
            meta["dtime"] = dtime
        meta.update(_topic_fields(topic_id, mission_id))

        summary = {
            "files": [str(f) for f in files],
            "title": title,
            "tags": tags,
            "zone": zone,
            "ai_declare": ai_declare,
            "private": private,
            "schedule": iso_time(dtime),
            "copyright": "repost" if repost else "original",
            "cover": str(cover) if cover else "auto (ffmpeg)",
            "cover43": str(cover43) if cover43 else None,
        }
        if topic_id is not None or mission_id is not None:
            summary.update(topic_id=topic_id, mission_id=mission_id)
        if output.settings.dry_run:
            return {"dry_run": True, "action": "upload.video", "params": summary, "meta": meta}

        guard.check("upload")
        await _preflight(cred, files[0])
        cover_path = cover or _extract_cover(files[0])
        if cover43:
            # same image endpoint as the main cover; the archive keeps it in a separate `cover43` field
            meta["cover43"] = await vu.upload_cover(Picture.from_file(str(cover43)), cred)
        pages = [vu.VideoUploaderPage(path=str(f), title=f.stem if len(files) > 1 else title) for f in files]
        uploader = vu.VideoUploader(pages, meta, cred, cover=Picture.from_file(str(cover_path)), line=line_enum)
        if sys.stderr.isatty():
            _progress(uploader)
        res = await uploader.start()
        result = {"bvid": res.get("bvid"), "aid": res.get("aid"), "url": f"https://www.bilibili.com/video/{res.get('bvid')}"}
        guard.record("upload", summary, result)
        return {
            **result,
            **summary,
            "next": f"稿件需要审核，用 `bili upload status {res.get('bvid')}` 查看进度。",
        }

    run(body)


def _archive(a: dict[str, Any]) -> dict[str, Any]:
    arc = a.get("Archive") or {}
    stat = a.get("stat") or {}
    return {
        "bvid": arc.get("bvid"),
        "aid": arc.get("aid"),
        "title": arc.get("title"),
        "state": arc.get("state"),
        "state_desc": arc.get("state_desc"),
        "published": arc.get("state") == 0,
        "reject_reason": arc.get("reject_reason") or None,
        "publish_time": iso_time(arc.get("ptime")),
        "scheduled_time": iso_time(arc.get("dtime")),
        "stat": {k: stat.get(k) for k in ("view", "danmaku", "reply", "favorite", "coin", "share", "like")},
    }


@app.command("list")
def list_(
    status: str = typer.Option("all", "--status", help="all / published / reviewing / rejected"),
    page: int = typer.Option(1, "--page", min=1, help="页码，从 1 开始。"),
    size: int = typer.Option(10, "--size", min=1, max=50, help="每页条数。"),
) -> None:
    """我的稿件列表（按投稿时间倒序），含审核状态与数据。"""

    async def body():
        if status not in STATUS_FILTERS:
            raise BiliError("invalid_argument", f"--status 只能是 {' / '.join(STATUS_FILTERS)} 之一。")
        cred = config.require_credential()
        data = await cc.get_video_upload_manager_info(
            cred, pn=page, ps=size, order=cc.UploadManagerOrder.SENDDATE, status=STATUS_FILTERS[status]
        )
        if output.settings.raw:
            return data
        counts = data.get("class") or {}
        return {
            "counts": {"published": counts.get("pubed"), "reviewing": counts.get("is_pubing"), "rejected": counts.get("not_pubed")},
            "page": page,
            "items": [_archive(a) for a in data.get("arc_audits") or []],
        }

    run(body)


@app.command()
def status(bvid: str = typer.Argument(..., help="稿件 BV 号。")) -> None:
    """查看一个稿件的审核状态，以及 B 站实际记录的标签、分区、声明与可见性（可能和提交时不同）。"""

    async def body():
        data = await _archive_view(config.require_credential(), bvid)
        if output.settings.raw:
            return data
        arc = data.get("archive") or {}
        statement = arc.get("creation_statement") or {}
        human = arc.get("human_type2") or {}
        return {
            "bvid": arc.get("bvid"),
            "aid": arc.get("aid"),
            "title": arc.get("title"),
            "state": arc.get("state"),
            "state_desc": arc.get("state_desc"),
            "reject_reason": arc.get("reject_reason") or None,
            "private": arc.get("is_only_self") == 1,
            "ai_declare": statement.get("id") == STATEMENT_AI,
            "creation_statement": statement.get("content"),
            "tags": [t for t in (arc.get("tag") or "").split(",") if t],
            "zone": {"tid": arc.get("tid"), "zone": human.get("id"), "zone_name": human.get("name")},
            "topic_id": arc.get("topic_id"),
            "topic_name": arc.get("topic_name"),
            "mission_id": arc.get("mission_id"),
            "mission_name": arc.get("mission_name"),
            "publish_time": iso_time(arc.get("ptime")),
            "scheduled_time": iso_time(arc.get("dtime")),
            "pages": [
                {"cid": v.get("cid"), "title": v.get("title"), "status_desc": v.get("status_desc") or None, "fail_desc": v.get("fail_desc") or None}
                for v in data.get("videos") or []
            ],
            "url": f"https://www.bilibili.com/video/{arc.get('bvid')}",
        }

    run(body)


async def _archive_view(cred: Credential, bvid: str) -> dict[str, Any]:
    async with web_client(cred, referer="https://member.bilibili.com/") as client:
        resp = await client.get(ARCHIVE_VIEW_URL, params={"bvid": bvid})
        resp.raise_for_status()
        res = resp.json()
    if res.get("code") != 0:
        from bilibili_api.exceptions import ResponseCodeException

        raise ResponseCodeException(res.get("code"), res.get("message"), res)
    return res.get("data") or {}




def build_desc_v2(desc: str, mentions: dict[str, int]) -> tuple[str, list[dict[str, Any]] | None]:
    """Plain description plus the segmented desc_v2 the web uploader sends, so @name becomes a real mention.

    B 站 rejects the pair (21010) unless desc equals desc_v2 rendered with each mention as "@name "
    (trailing space included), so one space after a mention in the input is folded into it.
    """
    if not mentions:
        return desc, None
    desc = "".join(f"@{name} " for name in mentions if f"@{name}" not in desc) + desc
    marks = sorted((i, name) for name in mentions for i in _find_all(desc, f"@{name}"))
    segments: list[dict[str, Any]] = []
    pos = 0
    for i, name in marks:
        if i < pos:  # overlapping names: keep the earlier match
            continue
        if i > pos:
            segments.append({"raw_text": desc[pos:i], "type": 1, "biz_id": ""})
        segments.append({"raw_text": name, "type": 2, "biz_id": str(mentions[name])})  # the name without "@"
        pos = i + len(name) + 1
        if desc[pos:pos + 1] == " ":
            pos += 1
    if pos < len(desc):
        segments.append({"raw_text": desc[pos:], "type": 1, "biz_id": ""})
    rendered = "".join(s["raw_text"] if s["type"] == 1 else f"@{s['raw_text']} " for s in segments)
    return rendered, segments


def _find_all(text: str, needle: str) -> list[int]:
    out, i = [], text.find(needle)
    while i != -1:
        out.append(i)
        i = text.find(needle, i + len(needle))
    return out


async def _desc_mentions(values: list[str]) -> dict[str, int]:
    from .comment import _mentions

    return await _mentions(values, limit=DESC_MENTIONS_MAX)


def edit_blocker(view: dict[str, Any]) -> str | None:
    """Why this archive must be edited on the web instead, or None. build_edit can't carry these settings."""
    if ((view.get("reply") or {}).get("state") or 0) != 0:
        return "这个稿件的评论区不是普通开放状态，命令行改稿可能把评论设置改回默认。"
    # copyright is echoed back unchanged (the web uploader stores 3 even for solo uploads); staff lists are not
    if view.get("staffs"):
        return "这是联合投稿（有合作成员），命令行改稿不带成员名单，可能出错或把合作者移除。"
    return None


def build_edit(view: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """The edit endpoint replaces the whole archive, so start from what B 站 has now and apply the changes."""
    arc = view.get("archive") or {}
    subtitle = view.get("subtitle") or {}
    statement = (arc.get("creation_statement") or {}).get("id")
    body: dict[str, Any] = {
        "aid": arc.get("aid"),
        "title": arc.get("title"),
        "copyright": arc.get("copyright"),
        "source": arc.get("source") or "",
        "tid": arc.get("tid"),
        "tag": arc.get("tag"),
        "desc_format_id": arc.get("desc_format_id") or 9999,
        "desc": arc.get("desc") or "",
        "desc_v2": arc.get("desc_v2"),  # @mentions in the description; dropped only when the text is replaced
        "recreate": (arc.get("recreate") or {}).get("switch", -1),
        "dynamic": arc.get("dynamic") or "",
        "interactive": arc.get("interactive") or 0,
        "act_reserve_create": 0,
        "no_disturbance": view.get("no_disturbance") or 0,
        "no_reprint": arc.get("no_reprint") or 0,
        "subtitle": {"open": 1 if subtitle.get("allow") else 0, "lan": subtitle.get("lan") or ""},
        "dolby": arc.get("is_dolby") or 0,
        "lossless_music": arc.get("lossless_music") or 0,
        "up_selection_reply": bool((view.get("reply") or {}).get("up_selection")),
        "web_os": 1,
        "is_only_self": arc.get("is_only_self") or 0,
        "creation_statement": {"id": statement if statement is not None else STATEMENT_NONE},
        "cover": arc.get("cover"),
        "videos": [{"filename": v.get("filename"), "title": v.get("title"), "desc": v.get("desc") or "", "cid": v.get("cid")} for v in view.get("videos") or []],
    }
    if arc.get("cover43"):
        body["cover43"] = arc["cover43"]
    if (arc.get("human_type2") or {}).get("id"):
        body["human_type2"] = arc["human_type2"]["id"]
    if arc.get("dtime") and arc.get("state") != 0:  # keep a pending schedule
        body["dtime"] = arc["dtime"]
    # Editing replaces the archive; omitting these can detach an existing activity.
    for key in ("topic_id", "mission_id", "topic_detail"):
        if key in arc:
            body[key] = arc[key]
    body.update(_topic_fields(changes.get("topic_id"), changes.get("mission_id")))
    if "tags" in changes:
        body["tag"] = ",".join(changes["tags"])
    if "ai_declare" in changes:
        body["creation_statement"] = {"id": STATEMENT_AI if changes["ai_declare"] else STATEMENT_NONE}
    for key in ("title", "desc", "cover", "cover43"):
        if key in changes:
            body[key] = changes[key]
    if "desc" in changes:
        body["desc_v2"] = changes.get("desc_v2")
    return body


@app.command()
def edit(
    bvid: str = typer.Argument(..., help="自己的稿件 BV 号。"),
    title: Optional[str] = typer.Option(None, "--title", help=f"新标题，最多 {TITLE_MAX} 字。"),
    desc: Optional[str] = typer.Option(None, "--desc", help=f"新简介，最多 {DESC_MAX} 字。"),
    desc_file: Optional[Path] = typer.Option(None, "--desc-file", help="从文件读取新简介，`-` 表示 stdin。"),
    desc_at: list[str] = DESC_AT_OPT,
    tag: Optional[list[str]] = typer.Option(None, "--tag", help=f"新标签（整体替换），可重复传入，1 到 {TAGS_MAX} 个。"),
    cover: Optional[Path] = typer.Option(None, "--cover", help="新封面图片。"),
    cover43: Optional[Path] = typer.Option(None, "--cover43", help="新的 4:3 封面（手机端推荐流）。"),
    ai_declare: Optional[bool] = typer.Option(None, "--ai-declare/--no-ai-declare", help="修改“含AI生成内容”声明；不传则保持不变。"),
    topic_id: Optional[int] = typer.Option(None, "--topic-id", min=1, help="新话题 ID；不传则保留原话题。"),
    mission_id: Optional[int] = typer.Option(None, "--mission-id", min=1, help="新投稿活动 ID；请与 --topic-id 配套使用，不传则保留。"),
) -> None:
    """修改已投稿件的标题、简介、标签、封面、AI 声明或投稿活动；没指定的设置保持不变。改完稿件会重新审核。"""

    async def body():
        cred = config.require_credential()
        changes: dict[str, Any] = {}
        if title is not None:
            if not title.strip() or len(title) > TITLE_MAX:
                raise BiliError("invalid_argument", f"标题需为 1 到 {TITLE_MAX} 字。")
            changes["title"] = title
        if desc is not None or desc_file is not None:
            changes["desc"] = read_text(desc, desc_file)
            if len(changes["desc"]) > DESC_MAX:
                raise BiliError("invalid_argument", f"简介最多 {DESC_MAX} 字。")
        if tag:
            tags = [t.strip() for t in tag if t.strip()]
            if not 1 <= len(tags) <= TAGS_MAX:
                raise BiliError("invalid_argument", f"标签需为 1 到 {TAGS_MAX} 个。")
            changes["tags"] = tags
        for name, path in (("cover", cover), ("cover43", cover43)):
            if path is not None and not path.is_file():
                raise BiliError("invalid_argument", f"封面文件不存在：{path}")
        if ai_declare is not None:
            changes["ai_declare"] = ai_declare
        if topic_id is not None:
            changes["topic_id"] = topic_id
        if mission_id is not None:
            changes["mission_id"] = mission_id
        if not changes and cover is None and cover43 is None and not desc_at:
            raise BiliError("invalid_argument", "没有要修改的内容。", hint="传 --title / --desc / --desc-at / --tag / --cover / --cover43 / --ai-declare / --topic-id / --mission-id 中的至少一个。")
        desc_mentions = await _desc_mentions(desc_at)
        view = await _archive_view(cred, bvid)
        if reason := edit_blocker(view):
            raise BiliError("invalid_argument", reason, hint="请在网页端创作中心修改这个稿件。")
        if "tags" in changes and (bad := await _check_tags(cred, changes["tags"])):
            raise BiliError("invalid_argument", f"B 站不接受这些标签：{', '.join(bad)}", detail={"rejected_tags": bad})
        arc = view.get("archive") or {}
        if desc_mentions:  # applies to the new description, or to the current one when only --desc-at is given
            changes["desc"], changes["desc_v2"] = build_desc_v2(changes.get("desc", arc.get("desc") or ""), desc_mentions)
            if len(changes["desc"]) > DESC_MAX:
                raise BiliError("invalid_argument", f"加上 @ 后简介超过 {DESC_MAX} 字。")
        before = {"title": arc.get("title"), "desc": arc.get("desc"), "tags": [t for t in (arc.get("tag") or "").split(",") if t],
                  "ai_declare": (arc.get("creation_statement") or {}).get("id") == STATEMENT_AI,
                  "topic_id": arc.get("topic_id"), "mission_id": arc.get("mission_id")}
        diff = {k: {"from": before[k], "to": v} for k, v in changes.items() if k != "desc_v2" and before.get(k) != v}
        if desc_mentions:
            diff["desc_at"] = {"to": desc_mentions}
        if cover:
            diff["cover"] = {"to": str(cover)}
        if cover43:
            diff["cover43"] = {"to": str(cover43)}
        if not diff:
            raise BiliError("invalid_argument", "指定的内容和稿件现在的一样，无需修改。")
        if output.settings.dry_run:
            return {"dry_run": True, "action": "upload.edit", "bvid": bvid, "changes": diff, "payload": build_edit(view, changes)}
        guard.check("upload_edit")
        if cover:
            changes["cover"] = await vu.upload_cover(Picture.from_file(str(cover)), cred)
        if cover43:
            changes["cover43"] = await vu.upload_cover(Picture.from_file(str(cover43)), cred)
        payload = build_edit(view, changes)
        await (
            Api(url=EDIT_URL, method="POST", verify=True, credential=cred, no_csrf=True, json_body=True)
            .update_params(csrf=cred.bili_jct, t=int(time.time()))
            .update_data(**payload, csrf=cred.bili_jct)
            .update_headers(referer="https://member.bilibili.com")
            .result
        )
        guard.record("upload_edit", {"bvid": bvid, "changes": diff}, "ok")
        return {"bvid": bvid, "changes": diff, "next": f"改动需要重新审核；B 站可能丢弃个别标签，用 `bili upload status {bvid}` 确认实际生效的内容。"}

    run(body)


def video_rates(stat: dict[str, Any]) -> dict[str, float | None]:
    """Interactions per 100 plays — the numbers that decide whether B 站 pushes a video further."""
    view = stat.get("view") or 0
    keys = {"like": "like", "favorite": "favorite", "coin": "coin", "reply": "reply", "share": "share", "danmaku": "danmaku"}
    return {k: round((stat.get(src) or 0) * 100 / view, 2) if view else None for k, src in keys.items()}


@app.command()
def stats(bvid: str = typer.Argument(..., help="自己的稿件 BV 号。")) -> None:
    """单个稿件的数据：播放与互动、每百次播放的互动率、完播率和平均观看时长（创作中心数据，次日更新）。"""

    async def body():
        from bilibili_api import video as bvideo

        cred = config.require_credential()
        info = await bvideo.Video(bvid=bvid, credential=cred).get_info()
        stat = info.get("stat") or {}
        plays = await cc.get_video_playanalysis(cred)
        if output.settings.raw:
            return {"info_stat": stat, "playanalysis": plays}
        row = next((a for a in plays.get("arc_play_list") or [] if a.get("bvid") == info.get("bvid")), None)
        out: dict[str, Any] = {
            "bvid": info.get("bvid"),
            "title": info.get("title"),
            "published": iso_time(info.get("pubdate")),
            "duration": info.get("duration"),
            "stat": {k: stat.get(k) for k in ("view", "like", "favorite", "coin", "reply", "share", "danmaku")},
            "per_100_plays": video_rates(stat),
        }
        if row:
            out["completion_percent"] = round((row.get("rate") or 0) / 100, 2)
            out["avg_watch_seconds"] = row.get("avg_duration")
        else:
            out["completion_note"] = "创作中心还没有这个稿件的完播数据：数据次日更新，且只覆盖最近的约 30 个稿件。"
        return out

    run(body)


chapters = typer.Typer(help="分段章节（进度条上的章节标题）：查看、设置、清空自己稿件的章节。", no_args_is_help=True)
app.add_typer(chapters, name="chapters")


def parse_chapter_lines(lines: list[str]) -> list[tuple[int, str]]:
    """Each line is '<time> <title>', e.g. '01:30 正片开始' (the format of the web editor's text mode)."""
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        stamp, _, title = line.partition(" ")
        if not title.strip():
            raise BiliError("invalid_argument", f"章节缺少标题：{line!r}，格式应为 “01:30 标题”。")
        out.append((parse_timestamp(stamp), title.strip()))
    return out


def build_chapters(items: list[tuple[int, str]], duration: int, limit: int, min_seconds: int) -> list[dict[str, Any]]:
    if not items:
        raise BiliError("invalid_argument", "至少需要一个章节。")
    if len(items) > limit:
        raise BiliError("invalid_argument", f"最多 {limit} 个章节，收到 {len(items)} 个。")
    items = sorted(items)
    if items[0][0] != 0:
        raise BiliError("invalid_argument", "第一个章节必须从 0:00 开始。")
    cards = []
    for i, (start, title) in enumerate(items):
        end = items[i + 1][0] if i + 1 < len(items) else duration
        if end > duration or start >= duration:
            raise BiliError("invalid_argument", f"章节 {title!r} 的时间超出视频时长 {duration} 秒。")
        if end - start < min_seconds:
            raise BiliError("invalid_argument", f"章节 {title!r} 太短，每个章节至少 {min_seconds} 秒。")
        cards.append({"from": start, "to": end, "content": title})
    return cards


async def _chapter_rules(client, bvid: str, aid: int, cid: int) -> dict[str, Any]:
    resp = await client.get(CHAPTER_RULE_URL, params={"bvid": bvid, "aid": aid, "cid": cid})
    resp.raise_for_status()
    return ((resp.json().get("data") or {}).get("chapters")) or {}


async def _submit_chapters(client, cred: Credential, aid: int, cid: int, cards: list[dict]) -> None:
    resp = await client.post(
        CHAPTER_SUBMIT_URL,
        data={"aid": aid, "cid": cid, "type": CARD_TYPE_CHAPTER, "cards": json.dumps(cards, ensure_ascii=False), "permanent": 0, "csrf": cred.bili_jct},
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        from bilibili_api.exceptions import ResponseCodeException

        raise ResponseCodeException(body.get("code"), body.get("message"), body)


@chapters.command("show")
def chapters_show(
    video_id: str = typer.Argument(..., help="自己的稿件：BV 号、av 号或链接。"),
    page: int = typer.Option(1, "--page", "-p", min=1, help="分 P 序号，从 1 开始。"),
) -> None:
    """查看自己稿件里设置的章节（含审核中的）及章节规则。"""

    async def body():
        from .danmaku import own_video_page

        cred = config.require_credential()
        info, pg = await own_video_page(video_id, page, cred)
        async with web_client(cred, referer="https://www.bilibili.com/") as client:
            rules = await _chapter_rules(client, info["bvid"], info["aid"], pg["cid"])
            resp = await client.get(ALLCARDS_URL, params={"aid": info["aid"], "cid": pg["cid"]})
            resp.raise_for_status()
            data = resp.json().get("data") or {}
        catalog = next((c for c in data.get("catalog") or [] if c.get("type") == CARD_TYPE_CHAPTER), None)
        return {
            "bvid": info["bvid"],
            "page": page,
            "rules": {"max_chapters": rules.get("limit"), "min_seconds": rules.get("stay"), "min_video_minutes": rules.get("first_min_duration")},
            "chapters": [
                {"from": c.get("from"), "to": c.get("to"), "title": c.get("content")} for c in (catalog or {}).get("cards") or []
            ],
            "updated_at": (catalog or {}).get("mtime"),
        }

    run(body)


@chapters.command("set")
def chapters_set(
    video_id: str = typer.Argument(..., help="自己的稿件：BV 号、av 号或链接。"),
    chapter: list[str] = typer.Option([], "--chapter", "-c", help="章节，格式“时间 标题”，如 “0:00 开场”，可重复传入。"),
    file: Optional[Path] = typer.Option(None, "--file", "-f", help="从文件读取章节，每行“时间 标题”，`-` 表示 stdin。"),
    page: int = typer.Option(1, "--page", "-p", min=1, help="分 P 序号，从 1 开始。"),
) -> None:
    """设置分段章节（会整体替换已有章节）。每个章节到下一个章节开始为止，最后一个到视频结尾。"""

    async def body():
        from .danmaku import own_video_page

        cred = config.require_credential()
        lines = list(chapter)
        if file is not None:
            lines += read_text(None, file).splitlines()
        if not lines:
            raise BiliError("invalid_argument", "用 --chapter 或 --file 提供章节。")
        items = parse_chapter_lines(lines)
        info, pg = await own_video_page(video_id, page, cred)
        duration = int(pg.get("duration") or 0)
        async with web_client(cred, referer="https://www.bilibili.com/") as client:
            rules = await _chapter_rules(client, info["bvid"], info["aid"], pg["cid"])
            min_minutes = rules.get("first_min_duration") or 0
            if duration < min_minutes * 60:
                raise BiliError("invalid_argument", f"视频需长于 {min_minutes} 分钟才能添加章节。")
            cards = build_chapters(items, duration, rules.get("limit") or 10, rules.get("stay") or 0)
            params = {"bvid": info["bvid"], "page": page, "chapters": [{"from": c["from"], "to": c["to"], "title": c["content"]} for c in cards]}
            if output.settings.dry_run:
                return {"dry_run": True, "action": "upload.chapters.set", "params": params}
            guard.check("profile")
            await _submit_chapters(client, cred, info["aid"], pg["cid"], cards)
        guard.record("profile", params, "ok")
        return {**params, "updated": True, "note": "章节需要审核，通过后才会显示在进度条上。"}

    run(body)


@chapters.command("clear")
def chapters_clear(
    video_id: str = typer.Argument(..., help="自己的稿件：BV 号、av 号或链接。"),
    page: int = typer.Option(1, "--page", "-p", min=1, help="分 P 序号，从 1 开始。"),
) -> None:
    """清空稿件的分段章节。"""

    async def body():
        from .danmaku import own_video_page

        cred = config.require_credential()
        info, pg = await own_video_page(video_id, page, cred)
        if output.settings.dry_run:
            return {"dry_run": True, "action": "upload.chapters.clear", "params": {"bvid": info["bvid"], "page": page}}
        guard.check("profile")
        async with web_client(cred, referer="https://www.bilibili.com/") as client:
            # The web editor deletes chapters by submitting the remaining ones; none left = cleared.
            await _submit_chapters(client, cred, info["aid"], pg["cid"], [])
        guard.record("profile", {"bvid": info["bvid"], "chapters": []}, "ok")
        return {"bvid": info["bvid"], "page": page, "cleared": True}

    run(body)


@app.command()
def zones() -> None:
    """列出新版分区（投稿用 `--zone` 指定）。"""

    async def body():
        return {"zones": await _zones(config.require_credential())}

    run(body)
