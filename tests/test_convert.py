import pytest

from hfapi.convert import build_params, job_to_task, parse_request, wrap_body
from hfapi.errors import ApiError


def _req(**over):
    body = {
        "model": "magi_2",
        "content": [{"type": "text", "text": "一只猫在雨里跑"}],
        "duration": 4,
        "ratio": "16:9",
    }
    body.update(over)
    return parse_request(body)


# ------------------------------------------------------------------ 入参拒绝

def test_empty_content_rejected():
    with pytest.raises(ApiError) as e:
        parse_request({"model": "magi_2", "content": []})
    assert e.value.code == "InvalidParameter"
    assert e.value.http_status == 400


def test_missing_model_rejected():
    with pytest.raises(ApiError) as e:
        parse_request({"content": [{"type": "text", "text": "x"}]})
    assert e.value.code == "MissingParameter"


def test_unknown_model_rejected():
    with pytest.raises(ApiError) as e:
        parse_request({"model": "seedance_2_5", "content": [{"type": "text", "text": "x"}]})
    assert e.value.code == "InvalidParameter"


def test_duration_out_of_range():
    with pytest.raises(ApiError) as e:
        _req(duration=3)
    assert "4..30" in e.value.message


def test_resolution_downgrade_rejected_loudly():
    """分辨率直接决定账单，不能静默升档。"""
    with pytest.raises(ApiError) as e:
        _req(resolution="720p")
    assert e.value.code == "InvalidParameter"
    assert "1080p" in e.value.message


def test_batch_over_local_cap():
    with pytest.raises(ApiError) as e:
        _req(batch_size=4)
    assert e.value.http_status == 400
    assert "MAX_BATCH" in e.value.message


def test_keyframes_mixed_with_reference_rejected():
    body = {
        "model": "magi_2",
        "content": [
            {"type": "image_url", "image_url": {"url": "asset://a"}, "role": "first_frame"},
            {"type": "image_url", "image_url": {"url": "asset://b"}, "role": "reference_image"},
        ],
    }
    with pytest.raises(ApiError) as e:
        parse_request(body)
    assert e.value.code == "InvalidParameter.TaskTypeConstraint"


def test_last_frame_without_first_rejected():
    body = {
        "model": "magi_2",
        "content": [{"type": "image_url", "image_url": {"url": "asset://b"}, "role": "last_frame"}],
    }
    with pytest.raises(ApiError):
        parse_request(body)


def test_public_url_rejected_with_guidance():
    body = {"model": "magi_2", "content": [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}]}
    with pytest.raises(ApiError) as e:
        parse_request(body)
    assert "asset://" in e.value.message


# ------------------------------------------------------------------ 上游报文

def test_t2v_payload_shape():
    req = _req()
    params = build_params(req)
    body = wrap_body(params, use_free_gens=True)

    assert body == {
        "params": {
            "prompt": "一只猫在雨里跑",
            "resolution": "1080p",
            "duration": 4,
            "aspect_ratio": "16:9",
            "batch_size": 1,
            "medias": [],
        },
        "use_free_gens": True,
    }


def test_keyframes_payload_roles():
    body = {
        "model": "MAGI-2",
        "content": [
            {"type": "text", "text": "从左到右"},
            {"type": "image_url", "image_url": {"url": "asset://11111111-1111-1111-1111-111111111111"},
             "role": "first_frame"},
            {"type": "image_url", "image_url": {"url": "asset://22222222-2222-2222-2222-222222222222"},
             "role": "last_frame"},
        ],
    }
    req = parse_request(body)
    params = build_params(req)
    assert [m["role"] for m in params["medias"]] == ["start_image", "end_image"]
    assert params["medias"][0]["data"]["type"] == "image"


# ------------------------------------------------------------------ 出参映射

def test_running_has_no_content_key():
    task = job_to_task({"id": "j1", "status": "in_progress"}, model_echo="magi_2", submitted_at=1000, now=1005)
    assert task["status"] == "running"
    assert "content" not in task
    assert "error" not in task


def test_completed_with_url_is_succeeded():
    job = {"id": "j1", "status": "completed", "results": {"raw": {"url": "https://cdn/x.mp4"}}}
    task = job_to_task(job, model_echo="magi_2", submitted_at=1000, now=1005)
    assert task["status"] == "succeeded"
    assert task["content"]["video_url"] == "https://cdn/x.mp4"


def test_completed_without_artifact_is_not_succeeded():
    task = job_to_task({"id": "j1", "status": "completed"}, model_echo="magi_2", submitted_at=1000, now=1005)
    assert task["status"] == "running"


def test_artifact_before_status_word():
    job = {"id": "j1", "status": "queued", "results": {"raw": {"url": "https://cdn/x.mp4"}}}
    task = job_to_task(job, model_echo="magi_2", submitted_at=1000, now=1005)
    assert task["status"] == "succeeded"


def test_nsfw_maps_to_sensitive_code():
    task = job_to_task({"id": "j1", "status": "nsfw"}, model_echo="magi_2", submitted_at=1000, now=1005)
    assert task["status"] == "failed"
    assert task["error"]["code"] == "OutputVideoSensitiveContentDetected"


def test_unknown_status_collapses_to_running():
    task = job_to_task({"id": "j1", "status": "COMPLETED"}, model_echo="magi_2", submitted_at=1000, now=1005)
    assert task["status"] == "running"


def test_expired_is_local_judgement():
    task = job_to_task({"id": "j1", "status": "queued", "created_at": 1000},
                       model_echo="magi_2", submitted_at=1000, now=1000 + 10, expires_after=5)
    assert task["status"] == "expired"
