"""视频模型协议适配测试：agnes-video-2.5-flash（reference 协议）与
v2.0（keyframes/ti2vid 旧协议）的双协议分发。

验证要点（2026-09-19 真实探测固化）：
- 2.5-flash 不接受 duration/resolution 字段（服务端 400 "duration is not
  an allowed request field"）
- 2.5 必须 mode=reference + images[]（双图=首尾帧锚定；服务端报错
  "reference mode cannot include first_frame" 与 "requires images"）
- 2.5 完成响应 url 在 metadata.url（顶层 url 为空合法）；seconds 是字符串 "5"
- v2.0 协议保持原有形态（mode=keyframes + image[] + duration/resolution）
全部走 mock，不出网。
"""
import json
from pathlib import Path
from unittest import mock

import pytest

from shipin_platform.generation.generate_assets import generate_video_agnes


class _FakeResp:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self.json_data = json_data if json_data is not None else {}
        self.text = json.dumps(self.json_data)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")

    def json(self):
        return self.json_data


def _install_stubs(monkeypatch, poll_payload, submit_payload=None):
    """接管网络层；返回提交体捕获 dict 与下载计数。"""
    sent = {}
    fetched = []

    def _post(url, json=None, headers=None, timeout=None):
        sent.update(json or {})
        return _FakeResp(200, submit_payload or {"task_id": "t", "id": "t"})

    def _get(url, headers=None, timeout=None):
        return _FakeResp(200, poll_payload)

    def _fetch(url, timeout=30, **kw):
        fetched.append(url)
        return b"\x00\x00\x00GENERATED_MP4"

    def _trim(*a, **k):
        return mock.Mock(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("requests.post", _post)
    monkeypatch.setattr("requests.get", _get)
    monkeypatch.setattr(
        "shipin_platform.generation.generate_assets._agnes_creds",
        lambda cap: ("sk-test", "https://apihub.agnes-ai.com/v1"))
    monkeypatch.setattr(
        "shipin_platform.generation.generate_assets._image_to_data_url",
        lambda p: f"data:image/jpeg;base64,{Path(p).name}")
    monkeypatch.setattr(
        "shipin_platform.generation.generate_assets._safe_fetch", _fetch)
    monkeypatch.setattr(
        "shipin_platform.generation.generate_assets.subprocess.run", _trim)
    return sent, fetched


def test_v25_flash_uses_reference_protocol(monkeypatch, tmp_path):
    """2.5-flash：mode=reference + images[]；不带 duration/resolution；
    双图（首尾帧）锚定 anchored=True；url 从 metadata.url 取。"""
    sent, fetched = _install_stubs(
        monkeypatch, poll_payload={
            "id": "t25", "task_id": "t25", "status": "completed", "seconds": "5",
            "metadata": {"url": "https://platform-outputs.agnes-ai.space/v.mp4"}})
    out = tmp_path / "v.mp4"
    r = generate_video_agnes(
        prompt="咖啡机蒸汽", model="agnes-video-2.5-flash", duration=4,
        first_frame=str(tmp_path / "a.jpg"), last_frame=str(tmp_path / "b.jpg"),
        output_path=str(out), work_dir=str(tmp_path))
    assert r["ok"] is True
    assert r["model"] == "agnes-video-2.5-flash"
    assert r["anchored"] is True
    assert sent["mode"] == "reference"
    assert len(sent["images"]) == 2
    assert "image" not in sent
    assert "duration" not in sent
    assert "resolution" not in sent
    assert "negative_prompt" not in sent  # 协议拒收，不发送
    assert len(fetched) == 1  # 只下载一次（master 复用同一字节）
    # 裁剪逻辑真实 ffmpeg 做（mock 环境默认不产裁剪文件）→ 返回服务端时长
    assert r["duration_sec"] == 5.0
    assert 4.0 <= 5.0


def test_v25_single_image_reference_not_anchored(monkeypatch, tmp_path):
    """只有首帧：reference + 单图，anchored=False 且带缺尾帧警告。"""
    sent, _ = _install_stubs(
        monkeypatch, poll_payload={
            "id": "t", "task_id": "t", "status": "completed", "seconds": 5,
            "metadata": {"url": "https://platform-outputs.agnes-ai.cn/x.mp4"}})
    r = generate_video_agnes(
        prompt="p", model="agnes-video-2.5-flash", duration=4,
        first_frame=str(tmp_path / "a.jpg"), output_path=str(tmp_path / "v.mp4"),
        work_dir=str(tmp_path))
    assert r["ok"] is True
    assert r["anchored"] is False
    assert len(sent["images"]) == 1
    assert any("缺尾帧" in w for w in r.get("warnings", []))


def test_v25_no_image_plain_text_warning(monkeypatch, tmp_path):
    """无首尾帧：reference 但无 images（纯文生），警告未锚定。"""
    sent, _ = _install_stubs(
        monkeypatch, poll_payload={
            "id": "t", "task_id": "t", "status": "completed", "seconds": "5",
            "metadata": {"url": "https://platform-outputs.agnes-ai.space/z.mp4"}})
    r = generate_video_agnes(
        prompt="x", model="agnes-video-2.5-flash", duration=4,
        output_path=str(tmp_path / "v.mp4"), work_dir=str(tmp_path))
    assert r["ok"] is True
    assert r["anchored"] is False
    assert "images" not in sent
    assert any("未锚定" in w for w in r.get("warnings", []))


def test_v20_keyframes_protocol_unchanged(monkeypatch, tmp_path):
    """v2.0 协议不回归：keyframes + image[] + duration/resolution。"""
    sent, _ = _install_stubs(
        monkeypatch, poll_payload={
            "id": "t1", "task_id": "t1", "status": "completed", "seconds": 5,
            "url": "https://platform-outputs.agnes-ai.cn/x.mp4"})
    r = generate_video_agnes(
        prompt="p", model="agnes-video-v2.0", duration=4, resolution="720p",
        first_frame=str(tmp_path / "a.jpg"), last_frame=str(tmp_path / "b.jpg"),
        output_path=str(tmp_path / "v.mp4"), work_dir=str(tmp_path))
    assert r["ok"] is True
    assert r["anchored"] is True
    assert sent["mode"] == "keyframes"
    assert len(sent["image"]) == 2
    assert sent["duration"] == 4
    assert sent["resolution"] == "720p"
    # mock 环境无 ffmpeg 裁剪 → 保留服务端 5s（真实链路会裁到 4s）
    assert r["duration_sec"] == 5.0


def test_v20_ti2vid_when_no_frames(monkeypatch, tmp_path):
    """v2.0 无缝入帧 → ti2vid 纯文生，锚定 False。"""
    sent, _ = _install_stubs(
        monkeypatch, poll_payload={
            "id": "t2", "task_id": "t2", "status": "completed", "seconds": 5,
            "url": "https://platform-outputs.agnes-ai.cn/t.mp4"})
    r = generate_video_agnes(
        prompt="p", model="agnes-video-v2.0", duration=5,
        output_path=str(tmp_path / "v.mp4"), work_dir=str(tmp_path))
    assert r["ok"] is True
    assert r["anchored"] is False
    assert sent["mode"] == "ti2vid"