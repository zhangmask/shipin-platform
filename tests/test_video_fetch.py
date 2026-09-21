"""video_fetch 测试：URL 白名单 / 分享文本解析 / 下载参数数组无 shell / 围栏。"""
from __future__ import annotations

from pathlib import Path

import pytest

from shipin_platform.tools import video_fetch as vf
from shipin_platform.tools.video_fetch import (
    VideoFetchError, safe_share_url, search_videos, download_video,
)


def _fake_ytdlp(monkeypatch):
    """把 video_fetch.shutil.which 打桩为固定路径（yt-dlp 已装时也保证可控）。"""
    monkeypatch.setattr(vf.shutil, "which",
                        lambda name, **kw: "C:/Tools/yt-dlp.exe"
                        if name == "yt-dlp" else None)


class TestSafeShareUrl:
    def test_accept_platform_urls(self):
        assert safe_share_url("https://www.bilibili.com/video/BV1xx") \
            == "https://www.bilibili.com/video/BV1xx"
        assert safe_share_url("https://b23.tv/abc123") == "https://b23.tv/abc123"
        assert safe_share_url("https://v.douyin.com/iAbCdEf/") \
            == "https://v.douyin.com/iAbCdEf/"
        assert safe_share_url("https://youtu.be/xyz9") == "https://youtu.be/xyz9"

    def test_share_text_extract(self):
        # B 站/抖音常见分享文案
        assert safe_share_url(
            "【1分钟咖啡广告】复制打开抖音 https://v.douyin.com/iTesZz/ 观看") \
            == "https://v.douyin.com/iTesZz/"
        assert safe_share_url(
            "0.35 复制  https://b23.tv/xxxxx 打开b站") == "https://b23.tv/xxxxx"

    def test_query_stripped(self):
        assert safe_share_url(
            "https://www.bilibili.com/video/BV1xx?p=2&spm_id_from=333.999") \
            == "https://www.bilibili.com/video/BV1xx"

    @pytest.mark.parametrize("bad", [
        "http://www.bilibili.com/video/BV1",        # 非 https
        "https://127.0.0.1/x",                      # 回环 IP
        "https://localhost/x",
        "https://192.168.1.1/x",                   # 私网
        "https://evilsite.com/v",                  # 非白名单域名
        "https://bilibili.com.evil.com/v",         # 域名伪造（后缀劫持）
        "https://api.bilibili.com/x/search",       # 子域不在白名单
        "https://user:pass@b23.tv/x",              # 带凭据
        "ftp://b23.tv/x",                          # 非 http(s)
        "no-link-here",
        "",
    ])
    def test_reject(self, bad):
        with pytest.raises(VideoFetchError):
            safe_share_url(bad)


class TestSearch:
    def test_bad_source(self):
        with pytest.raises(VideoFetchError):
            search_videos("咖啡", source="pornhub")

    def test_ytdlp_missing(self, monkeypatch):
        monkeypatch.setattr(vf.shutil, "which", lambda name: None)
        with pytest.raises(VideoFetchError, match="未找到 yt-dlp"):
            search_videos("咖啡广告", source="bilibili")

    def test_calls_flat_playlist(self, monkeypatch):
        """搜索必须走 yt-dlp 提取器（参数数组、限额）。"""
        argv_seen = []

        def fake_run(argv, timeout):
            argv_seen.append(argv)
            return type("R", (), {"returncode": 0,
                                  "stdout": '{"entries": []}',
                                  "stderr": ""})()
        monkeypatch.setattr(vf, "_run", fake_run)
        monkeypatch.setattr(vf.shutil, "which", lambda name: "C:/Tools/yt-dlp.exe")
        items = search_videos("咖啡广告 TVC", source="bilibili", limit=5)
        assert items == []
        assert argv_seen and argv_seen[0][0].endswith("yt-dlp.exe")
        joined = " ".join(argv_seen[0])
        assert "--flat-playlist" in joined        # 不允许逐条解析页面
        assert "bilisearch5:咖啡广告 TVC" in joined
        assert "--no-warnings" in joined


class TestDownload:
    def test_shell_false_argv(self, monkeypatch, tmp_path):
        """关键安全断言：调用 yt-dlp 只用参数数组，无 shell 拼接。"""
        calls = []

        def fake_run(argv, timeout):
            calls.append(argv)
            (tmp_path / "out").mkdir(exist_ok=True)
            (tmp_path / "out" / "video.mp4").write_bytes(b"x")
            return type("R", (), {"returncode": 0, "stderr": ""})()
        monkeypatch.setattr(vf, "_run", fake_run)
        monkeypatch.setattr(vf.shutil, "which", lambda name: "C:/Tools/yt-dlp.exe")

        r = download_video("https://www.bilibili.com/video/BV1xx",
                           tmp_path / "out", ref_root=tmp_path)
        assert r["ok"] and str(r["file"]).endswith("video.mp4")

        assert isinstance(calls[0], list)
        joined = " ".join(calls[0])
        assert ";" not in joined and "|" not in joined and "&&" not in joined
        assert joined.count("yt-dlp") == 1
        assert "--match-filter" in joined          # 时长上限放 filter 而非 shell

    def test_confine_rejects_outside(self, tmp_path):
        with pytest.raises(VideoFetchError):
            download_video("https://www.bilibili.com/video/BV1xx",
                           tmp_path / "escape",        # 越界目录
                           ref_root=tmp_path / "other")

    def test_duration_cap_applied(self, monkeypatch, tmp_path):
        caps = []

        def fake_run(argv, timeout):
            caps.append(next(a for a in argv if "duration" in a))
            return type("R", (), {"returncode": 1, "stderr": "x"})()
        monkeypatch.setattr(vf, "_run", fake_run)
        monkeypatch.setattr(vf.shutil, "which", lambda name: "C:/Tools/yt-dlp.exe")
        with pytest.raises(VideoFetchError):
            download_video("https://www.bilibili.com/video/BV1xx",
                           tmp_path / "out", ref_root=tmp_path,
                           max_duration=120)
        assert any("duration <= 120" in c for c in caps)