import json
import mimetypes
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Union
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, Path, Query, Request, UploadFile
from fastapi.params import File
from fastapi.responses import FileResponse, StreamingResponse
from loguru import logger
from starlette.background import BackgroundTask

from app.config import config
from app.controllers import base
from app.controllers.manager.base_manager import TaskQueueFullError
from app.controllers.manager.memory_manager import InMemoryTaskManager
from app.controllers.manager.redis_manager import RedisTaskManager
from app.controllers.v1.base import new_router
from app.models import const
from app.models.exception import HttpException
from app.models.schema import (
    AudioRequest,
    BgmRetrieveResponse,
    BgmUploadResponse,
    SubtitleRequest,
    TaskDeletionResponse,
    TaskListResponse,
    TaskQueryRequest,
    TaskQueryResponse,
    TaskResponse,
    TaskVideoRequest,
    VideoMaterialRetrieveResponse,
    VideoMaterialUploadResponse,
)
from app.services import bgm as bgm_service
from app.services import material_upload as material_upload_service
from app.services import state as sm
from app.services import task as tm
from app.utils import file_security, utils

# 统一在 V1 视频路由入口执行鉴权。verify_token 会在 api_key 为空时
# 保留现有免认证行为，只有管理员显式配置后才会影响客户端。
router = new_router(dependencies=[Depends(base.verify_token)])

_enable_redis = config.app.get("enable_redis", False)
_redis_host = config.app.get("redis_host", "localhost")
_redis_port = config.app.get("redis_port", 6379)
_redis_db = config.app.get("redis_db", 0)
_redis_password = config.app.get("redis_password", None)
_max_concurrent_tasks = config.app.get("max_concurrent_tasks", 5)
_max_queued_tasks = config.app.get("max_queued_tasks", 100)


def _build_redis_url(host: str, port: int, db: int, password: str | None) -> str:
    # Passwords are URL userinfo. Escape reserved characters so Redis receives
    # the exact configured secret rather than parsing part of it as a host,
    # port, path, query, or fragment.
    auth = f":{quote(password, safe='')}@" if password else ""
    # URL authorities require brackets around IPv6 literals. RedisState also
    # accepts a plain IPv6 host, so keep the queue client compatible with it.
    url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"redis://{auth}{url_host}:{port}/{db}"


redis_url = _build_redis_url(_redis_host, _redis_port, _redis_db, _redis_password)
# 根据配置选择合适的任务管理器
if _enable_redis:
    task_manager = RedisTaskManager(
        max_concurrent_tasks=_max_concurrent_tasks,
        redis_url=redis_url,
        max_queued_tasks=_max_queued_tasks,
    )
else:
    task_manager = InMemoryTaskManager(
        max_concurrent_tasks=_max_concurrent_tasks,
        max_queued_tasks=_max_queued_tasks,
    )


def _sanitize_upload_filename(filename: str, request_id: str) -> str:
    # 浏览器或客户端有时会附带目录信息，甚至可能夹带 ../ 这类穿越片段。
    # 这里只保留纯文件名，避免上传接口把文件写到目标目录之外。
    normalized_name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if not normalized_name or normalized_name in {".", ".."}:
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: invalid filename",
        )
    return normalized_name


def _resolve_path_within_directory(base_dir: str, unsafe_path: str, request_id: str) -> str:
    try:
        return file_security.resolve_path_within_directory(base_dir, unsafe_path)
    except ValueError as exc:
        logger.warning(
            f"reject unsafe file path, request_id: {request_id}, path: {unsafe_path}, "
            f"error: {str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=404 if str(exc) == "file does not exist" else 403,
            message=f"{request_id}: invalid file path",
        )


def _public_task_data(task: dict) -> dict:
    """复制任务状态并移除仅用于服务端进程协调的内部字段。"""
    public_task = dict(task)
    public_task.pop("cross_post_owner", None)
    return public_task


def _task_file_to_uri(file: str, endpoint: str, task_dir: str, request_id: str) -> str:
    if not isinstance(file, str):
        return file

    if file.startswith(("http://", "https://")):
        return file

    try:
        resolved_path = file_security.resolve_path_within_directory(task_dir, file)
    except ValueError as exc:
        # 任务状态理论上只应保存任务目录内的产物路径。这里不再继续拼接 URL，
        # 避免把异常路径包装成可访问链接；同时保留原值，便于排查历史脏数据。
        logger.warning(
            f"skip unsafe task output path, request_id: {request_id}, path: {file}, "
            f"error: {str(exc)}"
        )
        return file

    relative_path = os.path.relpath(
        resolved_path, os.path.realpath(task_dir)
    ).replace("\\", "/")
    uri_path = f"tasks/{quote(relative_path, safe='/')}"
    if endpoint:
        return f"{endpoint.rstrip('/')}/{uri_path}"
    return f"/{uri_path}"


def _task_response_data(task: dict, endpoint: str, task_dir: str, request_id: str) -> dict:
    response_task = _public_task_data(task)
    for key in ("videos", "combined_videos"):
        if key in task:
            response_task[key] = [
                _task_file_to_uri(file, endpoint, task_dir, request_id)
                for file in task[key]
            ]
    for key in ("audio_file", "subtitle_path"):
        if task.get(key):
            response_task[key] = _task_file_to_uri(
                task[key], endpoint, task_dir, request_id
            )
    return response_task


def _parse_byte_range(
    range_header: str | None, file_size: int, request_id: str
) -> tuple[int, int]:
    """解析单段 HTTP Range，并把无效或越界请求稳定转换成 416。"""
    if file_size <= 0:
        raise HttpException(
            task_id=request_id,
            status_code=416,
            message=f"{request_id}: requested range is not satisfiable",
        )

    if not range_header:
        return 0, file_size - 1

    try:
        # 视频播放器这里只需要单段 bytes range。拒绝多段请求可以避免返回体
        # 与 Content-Range 不一致，也避免异常字符串落入 int() 产生 500。
        if not range_header.startswith("bytes=") or "," in range_header:
            raise ValueError("unsupported range format")
        start_text, end_text = range_header[6:].split("-", 1)
        if not start_text and not end_text:
            raise ValueError("empty range")

        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise ValueError("invalid suffix length")
            start = max(file_size - suffix_length, 0)
            end = file_size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1
            if start < 0 or start >= file_size or end < start:
                raise ValueError("range outside file")
            end = min(end, file_size - 1)
    except (TypeError, ValueError) as exc:
        logger.warning(
            f"reject invalid video range, request_id: {request_id}, "
            f"range: {range_header}, file_size: {file_size}, error: {str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=416,
            message=f"{request_id}: requested range is not satisfiable",
        ) from exc

    return start, end


@router.post("/videos", response_model=TaskResponse, summary="Generate a short video")
def create_video(
    background_tasks: BackgroundTasks, request: Request, body: TaskVideoRequest
):
    return create_task(request, body, stop_at="video")


@router.post("/subtitle", response_model=TaskResponse, summary="Generate subtitle only")
def create_subtitle(
    background_tasks: BackgroundTasks, request: Request, body: SubtitleRequest
):
    return create_task(request, body, stop_at="subtitle")


@router.post("/audio", response_model=TaskResponse, summary="Generate audio only")
def create_audio(
    background_tasks: BackgroundTasks, request: Request, body: AudioRequest
):
    return create_task(request, body, stop_at="audio")


def create_task(
    request: Request,
    body: Union[TaskVideoRequest, SubtitleRequest, AudioRequest],
    stop_at: str,
):
    task_id = utils.get_uuid()
    request_id = base.get_task_id(request)
    try:
        if (
            stop_at == "video"
            and isinstance(body, TaskVideoRequest)
            and body.subtitle_enabled
        ):
            # 字体名可由 API 客户端直接提交，不能等到后台渲染时才发现路径越界。
            # 这里与渲染层共用目录边界校验，让非法请求在创建付费任务前返回 400。
            file_security.resolve_path_within_directory(
                utils.font_dir(), body.font_name or "STHeitiMedium.ttc"
            )
        task = {
            "task_id": task_id,
            "request_id": request_id,
            "params": body.model_dump(),
        }
        sm.state.update_task(task_id)
        try:
            task_manager.add_task(
                tm.start, task_id=task_id, params=body, stop_at=stop_at
            )
        except Exception:
            # 状态记录在调度前创建，默认标记为 processing。如果调度器没能
            # 接管任务（例如线程启动失败或 Redis 队列不可用），必须回滚该
            # 记录，否则 API 和 WebUI 会永久展示一个实际从未运行的任务。
            sm.state.delete_task(task_id)
            raise
        logger.success(f"Task created: {utils.to_json(task)}")
        return utils.get_response(200, task)
    except TaskQueueFullError as e:
        logger.warning(
            f"reject task because queue is full, request_id: {request_id}, task_id: {task_id}"
        )
        raise HttpException(
            task_id=task_id, status_code=429, message=f"{request_id}: {str(e)}"
        )
    except ValueError as e:
        raise HttpException(
            task_id=task_id, status_code=400, message=f"{request_id}: {str(e)}"
        )


def _run_type_beat_project_render(task_id: str, project_id: str) -> None:
    root = pathlib.Path(config.root_dir)
    script = root / "scripts" / "render_typebeat_project.py"
    output_root = pathlib.Path(
        os.environ.get("TYPEBEAT_OUTPUT_ROOT", "/srv/data/n8n-media/store/strictlybeats")
    )
    output = output_root / project_id / "final" / "moneyprinterturbo-master.mp4"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)
    sm.state.patch_task(task_id, progress=5, project_id=project_id, output_path=str(output))
    try:
        result = subprocess.run(
            [sys.executable, str(script), project_id, "--output", str(output)],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=7200,
            check=False,
        )
        if result.returncode != 0 or not output.is_file():
            detail = (result.stderr or result.stdout or "type-beat render failed").strip()[-4000:]
            sm.state.patch_task(
                task_id,
                state=const.TASK_STATE_FAILED,
                progress=100,
                failed_stage="type_beat_render",
                error=detail,
            )
            return
        sm.state.patch_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            videos=[str(output)],
            output_path=str(output),
            manifest_path=str(output.with_suffix(".manifest.json")),
            render_engine="moneyprinterturbo-type-beat",
        )
    except subprocess.TimeoutExpired:
        sm.state.patch_task(
            task_id,
            state=const.TASK_STATE_FAILED,
            progress=100,
            failed_stage="type_beat_render",
            error="type-beat render exceeded 7200 seconds",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.exception(
            f"type-beat project render failed: task_id={task_id}, project_id={project_id}"
        )
        sm.state.patch_task(
            task_id,
            state=const.TASK_STATE_FAILED,
            progress=100,
            failed_stage="type_beat_render",
            error=str(exc),
        )


@router.post(
    "/type-beat/projects/assemble",
    summary="Assemble a canonical type-beat project from 10 free motion clips",
)
def assemble_type_beat_project(request: Request, body: dict):
    request_id = base.get_task_id(request)
    root = pathlib.Path(config.root_dir)
    script = root / "scripts" / "assemble_typebeat_project.py"
    if not isinstance(body, dict):
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: JSON object body required",
        )
    payload_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            prefix="mpt-typebeat-assemble-",
            dir=str(root / "storage" / "temp"),
            delete=False,
        ) as handle:
            json.dump(body, handle)
            payload_file = pathlib.Path(handle.name)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(root)
        result = subprocess.run(
            [sys.executable, str(script), "--payload", str(payload_file)],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=1200,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "type-beat assembly failed").strip()[-4000:]
            raise HttpException(
                task_id=request_id,
                status_code=422,
                message=f"{request_id}: {detail}",
            )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        payload = json.loads(lines[-1]) if lines else {}
        return utils.get_response(200, payload)
    except json.JSONDecodeError as exc:
        raise HttpException(
            task_id=request_id,
            status_code=500,
            message=f"{request_id}: invalid assembly response: {exc}",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise HttpException(
            task_id=request_id,
            status_code=504,
            message=f"{request_id}: type-beat assembly exceeded 1200 seconds",
        ) from exc
    finally:
        if payload_file is not None:
            payload_file.unlink(missing_ok=True)


@router.post(
    "/internal/type-beat/canonical-image-motion",
    summary="Trusted-LAN cinematic camera motion from a canonical still",
)
def render_canonical_still_motion_internal(request: Request, body: dict):
    """This is exact-image camera motion, never unverified generative I2V."""
    request_id = base.get_task_id(request)
    project_id = str(body.get("project_id") or "") if isinstance(body, dict) else ""
    host_path = str(body.get("host_path") or "") if isinstance(body, dict) else ""
    slot = body.get("slot") if isinstance(body, dict) else None
    duration = body.get("duration", 5) if isinstance(body, dict) else None
    try:
        slot = int(slot)
        duration = float(duration)
    except (TypeError, ValueError) as exc:
        raise HttpException(task_id=request_id, status_code=400, message="invalid slot/duration") from exc
    if (not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", project_id)
            or not host_path.startswith("/srv/data/n8n-media/store/")
            or ".." in pathlib.Path(host_path).parts
            or pathlib.Path(host_path).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}
            or not 1 <= slot <= 10 or not 2 <= duration <= 10):
        raise HttpException(task_id=request_id, status_code=400, message="untrusted canonical image input")
    root = pathlib.Path(config.root_dir)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)
    result = subprocess.run(
        [sys.executable, "-m", "scripts.canonical_still_motion_bridge",
         "--host-path", host_path, "--project-id", project_id,
         "--slot", str(slot), "--duration", str(duration)],
        cwd=str(root), env=env, text=True, capture_output=True, timeout=240,
        check=False,
    )
    if result.returncode != 0:
        raise HttpException(task_id=request_id, status_code=422,
                            message="canonical image-motion render failed; verify source and slot state")
    return utils.get_response(200, json.loads(result.stdout))


@router.post(
    "/internal/type-beat/projects/assemble",
    summary="Trusted-LAN type-beat project assembly",
)
def assemble_type_beat_project_internal(request: Request, body: dict):
    return assemble_type_beat_project(request, body)


@router.post(
    "/internal/type-beat/projects/{project_id}/render-sync",
    summary="Trusted-LAN synchronous type-beat render",
)
def render_type_beat_project_internal_sync(
    request: Request,
    project_id: str = Path(..., description="Canonical media-video project ID"),
):
    request_id = base.get_task_id(request)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", project_id):
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: invalid project id",
        )

    root = pathlib.Path(config.root_dir)
    script = root / "scripts" / "render_typebeat_project.py"
    output_root = pathlib.Path(
        os.environ.get("TYPEBEAT_OUTPUT_ROOT", "/srv/data/n8n-media/store/strictlybeats")
    )
    output = output_root / project_id / "final" / "moneyprinterturbo-master.mp4"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)

    try:
        result = subprocess.run(
            [sys.executable, str(script), project_id, "--output", str(output)],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=7200,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HttpException(
            task_id=request_id,
            status_code=504,
            message=f"{request_id}: type-beat render exceeded 7200 seconds",
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise HttpException(
            task_id=request_id,
            status_code=500,
            message=f"{request_id}: type-beat render failed: {exc}",
        ) from exc

    if result.returncode != 0 or not output.is_file():
        detail = (result.stderr or result.stdout or "type-beat render failed").strip()[-4000:]
        raise HttpException(
            task_id=request_id,
            status_code=422,
            message=f"{request_id}: {detail}",
        )

    manifest = output.with_suffix(".manifest.json")
    return utils.get_response(
        200,
        {
            "project_id": project_id,
            "output_path": str(output),
            "manifest_path": str(manifest),
            "render_engine": "moneyprinterturbo-type-beat",
            "render_mode": os.environ.get("TYPEBEAT_RENDER_MODE", "fast"),
        },
    )


@router.post(
    "/type-beat/projects/{project_id}/render",
    response_model=TaskResponse,
    summary="Render a canonical type-beat project",
)
def render_type_beat_project(
    background_tasks: BackgroundTasks,
    request: Request,
    project_id: str = Path(..., description="Canonical media-video project ID"),
):
    request_id = base.get_task_id(request)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", project_id):
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: invalid project id",
        )
    task_id = utils.get_uuid()
    sm.state.update_task(
        task_id,
        state=const.TASK_STATE_PROCESSING,
        progress=0,
        task_type="type_beat_project_render",
        project_id=project_id,
    )
    background_tasks.add_task(_run_type_beat_project_render, task_id, project_id)
    return utils.get_response(200, {"task_id": task_id})


@router.get("/tasks", response_model=TaskListResponse, summary="Get all tasks")
def get_all_tasks(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=1000),
):
    tasks, total = sm.state.get_all_tasks(page, page_size)
    request_id = base.get_task_id(request)
    endpoint = config.app.get("endpoint", "").rstrip("/")
    task_dir = utils.task_dir()

    response = {
        "tasks": [
            _task_response_data(task, endpoint, task_dir, request_id) for task in tasks
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    return utils.get_response(200, response)



@router.get(
    "/tasks/{task_id}", response_model=TaskQueryResponse, summary="Query task status"
)
def get_task(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    query: TaskQueryRequest = Depends(),
):
    request_id = base.get_task_id(request)
    endpoint = config.app.get("endpoint", "").rstrip("/")
    task = sm.state.get_task(task_id)
    if task:
        task_dir = utils.task_dir()
        response_task = _task_response_data(task, endpoint, task_dir, request_id)
        return utils.get_response(200, response_task)

    raise HttpException(
        task_id=task_id, status_code=404, message=f"{request_id}: task not found"
    )


@router.delete(
    "/tasks/{task_id}",
    response_model=TaskDeletionResponse,
    summary="Delete a generated short video task",
)
def delete_video(request: Request, task_id: str = Path(..., description="Task ID")):
    request_id = base.get_task_id(request)
    task = sm.state.get_task(task_id)
    if task:
        if tm.is_task_busy(task):
            logger.warning(
                f"refuse to delete busy task, request_id: {request_id}, "
                f"task_id: {task_id}, state: {task.get('state')}, "
                f"cross_post_state: {task.get('cross_post_state')}"
            )
            raise HttpException(
                task_id=task_id,
                status_code=409,
                message=f"{request_id}: task is still running",
            )

        tasks_dir = utils.task_dir()
        current_task_dir = os.path.join(tasks_dir, task_id)
        if os.path.exists(current_task_dir):
            try:
                shutil.rmtree(current_task_dir)
            except FileNotFoundError:
                # Another completed-task deletion may remove this directory
                # after our existence check. Only accept an absent root;
                # partial deletion and other filesystem errors must keep state.
                if os.path.lexists(current_task_dir):
                    raise

        sm.state.delete_task(task_id)
        logger.success(f"video deleted: {utils.to_json(task)}")
        return utils.get_response(200)

    raise HttpException(
        task_id=task_id, status_code=404, message=f"{request_id}: task not found"
    )


@router.get(
    "/musics", response_model=BgmRetrieveResponse, summary="Retrieve local BGM files"
)
def get_bgm_list(request: Request):
    bgm_list = []
    for file in bgm_service.list_bgm_files():
        filename = os.path.basename(file)
        try:
            size = os.path.getsize(file)
        except OSError as exc:
            logger.warning(f"skip unavailable background music: name={filename}, error={exc}")
            continue
        bgm_list.append(
            {
                "name": filename,
                "size": size,
                # 只返回文件名，避免把服务器绝对路径暴露给调用方。服务端会
                # 在 storage/bgm 和 resource/songs 两个白名单目录中重新解析。
                "file": filename,
            }
        )
    response = {"files": bgm_list}
    return utils.get_response(200, response)


@router.post(
    "/musics",
    response_model=BgmUploadResponse,
    summary="Upload a background music file",
    description=(
        "Validate an MP3, M4A, AAC, WAV, FLAC, OGG, OPUS, or WMA file up to "
        "30 MB and store it under an immutable UUID filename in storage/bgm."
    ),
    responses={
        400: {"description": "The filename, format, size, or audio stream is invalid"},
        500: {"description": "FFmpeg validation or persistent storage is unavailable"},
    },
)
def upload_bgm_file(request: Request, file: UploadFile = File(...)):
    request_id = base.get_task_id(request)
    try:
        safe_filename = bgm_service.save_bgm_upload(file.filename, file.file)
    except bgm_service.BgmUploadError as exc:
        # 上传失败通常可以由用户更换文件后恢复，因此记录 request_id 和明确原因，
        # 但不输出文件内容或绝对路径，避免日志泄露用户数据。
        logger.warning(
            f"background music upload rejected: request_id={request_id}, error={str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: {str(exc)}",
        )
    except bgm_service.BgmServiceError as exc:
        # 工具链或存储故障属于服务端问题，不能伪装成用户文件错误。日志保留
        # request_id 和内部原因，HTTP 响应只返回稳定文案，避免暴露服务器路径。
        logger.error(
            f"background music upload failed: request_id={request_id}, error={str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=500,
            message=f"{request_id}: background music validation is unavailable",
        )

    response = {"file": safe_filename}
    return utils.get_response(200, response)

@router.get(
    "/video_materials", response_model=VideoMaterialRetrieveResponse, summary="Retrieve local video materials"
)
def get_video_materials_list(request: Request):
    allowed_suffixes = material_upload_service.SUPPORTED_MATERIAL_EXTENSIONS
    local_videos_dir = utils.storage_dir("local_videos", create=True)
    video_materials_list = []
    with os.scandir(local_videos_dir) as entries:
        for entry in entries:
            if (
                entry.name.startswith(".")
                or pathlib.Path(entry.name).suffix.lower() not in allowed_suffixes
            ):
                continue
            try:
                # Do not follow links outside local_videos or list an upload
                # that disappeared while the directory was being scanned.
                if not entry.is_file(follow_symlinks=False):
                    continue
                size = entry.stat(follow_symlinks=False).st_size
            except OSError as exc:
                logger.warning(
                    f"skip unavailable local material: name={entry.name}, error={exc}"
                )
                continue
            video_materials_list.append(
                {"name": entry.name, "size": size, "file": entry.name}
            )
    # Keep ordered material selection stable across file systems and runs.
    video_materials_list.sort(key=lambda item: (item["name"].casefold(), item["name"]))
    response = {"files": video_materials_list}
    return utils.get_response(200, response)


@router.post(
    "/video_materials",
    response_model=VideoMaterialUploadResponse,
    summary="Upload the video material file to the local videos directory",
)
def upload_video_material_file(request: Request, file: UploadFile = File(...)):
    request_id = base.get_task_id(request)
    try:
        # Keep accepting browser-supplied client paths, but persist an immutable
        # UUID storage key so repeated names cannot overwrite queued task inputs.
        safe_filename = _sanitize_upload_filename(file.filename, request_id)
        stored_filename = material_upload_service.save_material_upload(
            safe_filename, file.file
        )
    except material_upload_service.MaterialUploadError as exc:
        logger.warning(
            f"local material upload rejected: request_id={request_id}, "
            f"error={str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: {str(exc)}",
        )
    except material_upload_service.MaterialServiceError as exc:
        logger.error(
            f"local material upload failed: request_id={request_id}, "
            f"error={str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=500,
            message=f"{request_id}: local material validation is unavailable",
        )

    response = {"file": stored_filename}
    return utils.get_response(200, response)

@router.get("/stream/{file_path:path}")
async def stream_video(request: Request, file_path: str):
    request_id = base.get_task_id(request)
    tasks_dir = utils.task_dir()
    video_path = _resolve_path_within_directory(tasks_dir, file_path, request_id)
    range_header = request.headers.get("Range")
    # The body is produced after this handler returns. Open now so a task
    # deletion or replacement between headers and iteration cannot make the
    # stream fail or mismatch the size used for Content-Range.
    try:
        video_file = open(video_path, "rb")
    except FileNotFoundError as exc:
        raise HttpException(
            task_id=request_id,
            status_code=404,
            message=f"{request_id}: file no longer exists",
        ) from exc
    try:
        video_size = os.fstat(video_file.fileno()).st_size
        start, end = _parse_byte_range(range_header, video_size, request_id)
    except Exception:
        video_file.close()
        raise
    length = end - start + 1

    def file_iterator():
        try:
            video_file.seek(start, os.SEEK_SET)
            remaining = length
            while remaining > 0:
                data = video_file.read(min(4096, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data
        finally:
            video_file.close()

    response = StreamingResponse(
        file_iterator(),
        media_type="video/mp4",
        background=BackgroundTask(video_file.close),
    )
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Content-Length"] = str(length)
    if range_header:
        response.headers["Content-Range"] = f"bytes {start}-{end}/{video_size}"
        response.status_code = 206  # Partial Content

    return response


@router.get("/download/{file_path:path}")
async def download_video(request: Request, file_path: str):
    """
    download video
    :param request: Request request
    :param file_path: video file path, eg: /cd1727ed-3473-42a2-a7da-4faafafec72b/final-1.mp4
    :return: video file
    """
    request_id = base.get_task_id(request)
    tasks_dir = utils.task_dir()
    video_path = _resolve_path_within_directory(tasks_dir, file_path, request_id)
    file_path = pathlib.Path(video_path)
    filename = file_path.name
    media_type, _ = mimetypes.guess_type(filename)
    return FileResponse(
        path=video_path,
        filename=filename,
        media_type=media_type or "application/octet-stream",
    )
