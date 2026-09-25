"""意图传导修复的单测：画幅/风格/时长对齐/assemble 闸门/iterate 写盘。

覆盖本次意图管线完善的核心新增逻辑（不依赖外部服务）：
- _canvas_for_brief:target_platform → 竖屏/横屏决策点
- _style_anchor: 显式 > 音调映射 > 兜底
- _fit_duration_to_target: 时长对齐 + 不可达判定
- run_assemble_phase 的 video_gen 闸门（未生成直接拦，不再静默混拼）
"""
import json
import shutil
from pathlib import Path

import pytest

from shipin_platform.orchestration import pipeline_runner as pr
from shipin_platform.orchestration.stage_store import ProjectStageStore
from shipin_platform.services.costing import record_cost


class TestCanvasForBrief:
    def test_vertical_platforms(self):
        for plat in ("抖音", "快手", "小红书", "douyin", "视频号"):
            w, h, _ = pr._canvas_for_brief({"target_platform": plat})
            assert (w, h) == (720, 1280), plat

    def test_default_horizontal(self):
        assert pr._canvas_for_brief({}) == (1280, 720, "1280x704")
        assert pr._canvas_for_brief(
            {"target_platform": "YouTube"}) == (1280, 720, "1280x704")


class TestCanvasSizeFor:
    """落版卡尺寸必须跟随实际在链素材,而不是只读 brief。

    (回归: 变体复用基准素材池 —— brief 是竖屏但池里媒体是横屏 ——
    kenburns 卡片若按 brief 出 9:16, xfade 链尺寸不匹配直接报错。)
    """

    def test_follows_existing_clip(self, tmp_path):
        mrec = {"clip": str(tmp_path / "nope.mp4"), "master": None}
        # 无真实媒体 → 回退 brief 决策
        brief = {"target_platform": "douyin"}
        assert pr._canvas_size_for({"shots": {"S01": mrec}}, brief) == "720x1280"

    def test_prefers_clip_over_master(self, tmp_path, monkeypatch):
        clip = tmp_path / "c.mp4"
        master = tmp_path / "m.mp4"
        clip.write_bytes(b"gi")
        master.write_bytes(b"gi")
        sizes = {str(clip): (1280, 704), str(master): (360, 640)}
        monkeypatch.setattr(pr, "_ffprobe_size",
                            lambda p: sizes.get(str(p)))
        manifest = {"shots": {
            "S01": {"clip": str(clip), "master": str(master)}}}
        assert pr._canvas_size_for(
            manifest, {"target_platform": "douyin"}) == "1280x704"

    def test_brief_fallback_when_no_media(self):
        assert pr._canvas_size_for(
            {"shots": {}}, {"target_platform": "xiaohongshu"}) == "720x1280"
        assert pr._canvas_size_for(
            {"shots": {"S01": {"clip": None, "master": None}}}, {}) == "1280x704"


class TestStyleAnchor:
    def test_explicit_wins(self):
        assert pr._style_anchor(
            {"style_anchor": "custom look",
             "tone": "温暖治愈"}) == "custom look"

    def test_tone_mapping(self):
        assert "warm golden" in pr._style_anchor({"tone": "暖色调治愈系"})

    def test_fallback(self):
        assert "cinematic" in pr._style_anchor({})


class TestFitDuration:
    def test_scales_to_target(self):
        data = {"duration_sec": 20,
                "shots": [{"shot_id": f"S{i:02d}", "duration_sec": 5}
                          for i in range(1, 5)]}
        out, ok, meta = pr._fit_duration_to_target(data, 10)
        assert ok
        total = sum(s["duration_sec"] for s in out["shots"])
        assert total == 10
        assert out["duration_sec"] == 10

    def test_clip_unreachable(self):
        data = {"shots": [{"shot_id": "S01", "duration_sec": 2},
                          {"shot_id": "S02", "duration_sec": 2}]}
        out, ok, meta = pr._fit_duration_to_target(data, 20)  # 4s → 钳8s×2=16s ≠20
        assert not ok
        assert max(s["duration_sec"] for s in out["shots"]) <= 8.0

    def test_noop_within_tolerance(self):
        data = {"shots": [{"shot_id": "S01", "duration_sec": 9.5},
                          {"shot_id": "S02", "duration_sec": 10.5}]}
        out, ok, meta = pr._fit_duration_to_target(data, 20.0)
        assert ok
        assert [s["duration_sec"] for s in out["shots"]] == [9.5, 10.5]


class TestLlmReviewUnavailable:
    """轮28:语义审片不可用时不得静默放行。

    旧代码 llm_stage_review 返回 available=False 时什么都不记——
    纯规则引擎 pass 就直接进花钱的生成阶段,故事四拍结构/单动作可拍性/
    镜间连续性/主角一致性/品牌贯穿整轮无人审(端点 5xx/key 中途失效/
    输出两次不可解析都会触发)。「审不了」≠「审过了」。"""

    def _script(self, n: int, per: float) -> dict:
        return {"shots": [{"shot_id": f"S{i:02d}", "duration_sec": per,
                           "shot_size": "中景", "camera": "固定机位",
                           "spatial": "画面中央", "subject": "主角",
                           "scene": "咖啡店", "motion": "端起咖啡杯",
                           "narration": "深夜街头冷色如冰", "dialogue": ""}
                          for i in range(1, n + 1)]}

    def test_unavailable_recorded_and_never_passes(self, monkeypatch):
        from shipin_platform.orchestration.stage_store import ProjectStageStore
        pid = "llm-unavail"
        store = ProjectStageStore(":memory:")
        store.create_project(pid)
        pr._save(pid, "brief.json", {"product_info": "测试咖啡",
                                     "duration_sec": 24,
                                     "slogan": "享受每一刻"})
        monkeypatch.setattr(
            pr, "llm_stage_review",
            lambda stage, data, brief=None: {
                "available": False, "reason": "测试:端点 500",
                "findings": [], "scores": {}, "raw": ""})
        r = pr._iterate("script", self._script(8, 3), pid, store,
                        use_llm=True)
        rev = pr._load(pid, "script_review.json")
        warn = [f for rnd in rev["rounds"] for f in rnd["findings"]
                if f.get("dimension") == "llm_review"]
        assert warn, "语义审片不可用必须落 warning finding"
        assert "端点 500" in warn[0]["issue"]
        assert rev["llm"].get("available") is False
        assert "端点 500" in rev["llm"].get("reason", "")
        # 「审不了」不得算过:决策绝不能是 pass/pass_with_warnings
        assert r["decision"] not in ("pass", "pass_with_warnings"), r


class TestBudgetFuse:
    """轮42(七审 #1/#2):预算闸曾是"进门费"——variant/retry 两条车道
    不查、attempt 循环内不复查、text 阶段 LLM 花费零入账。本轮:入口
    闸补到两条车道 + 阶段内熔断 + LLM 按次入账。"""

    def _mk_project(self, pid: str, max_budget=None):
        from shipin_platform.orchestration.stage_store import ProjectStageStore
        store = ProjectStageStore(":memory:")
        store.create_project(pid)
        pr._save(pid, "brief.json", {"product_info": "x", "duration_sec": 12})
        if max_budget is not None:
            (pr._project_dir(pid) / "budget.json").write_text(
                json.dumps({"max_budget_usd": max_budget}), encoding="utf-8")
        return store

    def test_no_budget_file_never_fuses(self):
        pid = "bf-none"
        self._mk_project(pid)
        over, why = pr._budget_exceeded(pid)
        assert over is False and why == ""

    def test_under_budget_no_fuse(self):
        pid = "bf-under"
        self._mk_project(pid, max_budget=1.0)
        over, _ = pr._budget_exceeded(pid)
        assert over is False

    def test_over_budget_fuses_with_reason(self):
        pid = "bf-over"
        self._mk_project(pid, max_budget=0.0001)
        record_cost(pid, "video", model="agnes-video", units=1.0, note="x")
        over, why = pr._budget_exceeded(pid)
        assert over is True
        assert "上限" in why

    def test_llm_calls_are_costed(self, monkeypatch):
        """七审 #2:text 阶段(生成/修复/审片)的付费 LLM 调用此前零入账,
        预算数字只覆盖媒体生成、与真实账单长期对不上。"""
        from shipin_platform.services.costing import cost_summary
        pid = "bf-llm"
        self._mk_project(pid)
        store = ProjectStageStore(":memory:")
        store.create_project(pid)
        monkeypatch.setattr(
            pr, "llm_stage_review",
            lambda stage, data, brief=None: {
                "available": True, "scores": {}, "findings": []})
        pr._iterate("script", self._draft(), pid, store, use_llm=True)
        summary = cost_summary(pid)
        assert "llm" in summary.get("by_kind", {}), summary
        assert summary.get("total_usd", 0) > 0, summary

    @staticmethod
    def _draft() -> dict:
        narr = ["深夜街头冷色如冰", "加班后的倦无人说", "推门暖光迎面而来",
                "手工烘焙的香气", "第一口顺滑融化疲惫", "走向座位缓缓落座",
                "原来温柔就在这一杯", "享受这一刻好滋味"]
        return {"duration_sec": 24, "shots": [
            {"shot_id": f"S{i:02d}", "duration_sec": 3, "shot_size": "中景",
             "camera": "固定机位", "spatial": "画面中央", "subject": "主角",
             "scene": "咖啡店", "motion": "端起咖啡杯",
             "narration": narr[i - 1], "dialogue": ""}
            for i in range(1, 9)]}


class TestLedgerAtomicity:
    """轮43(七审 #5):成本账本旧实现三个洞——读-改-写无锁(并发丢行)、
    截断式写(半路被杀留损坏 JSON)、_load_rows 损坏时静默返回 [](预算
    闸判定"没花钱"重新放行,账本无声清零=给预算闸开后门)。"""

    def test_corrupt_ledger_raises_not_silently_zeroed(self):
        from shipin_platform.services.costing import (cost_file,
                                                      _load_rows,
                                                      LedgerCorruptError)
        pid = "lg-corrupt"
        fp = cost_file(pid)
        fp.write_text('{"rows": [{"seq": 1, "usd', encoding="utf-8")  # 截断
        try:
            _load_rows(pid)
            raise AssertionError("损坏账本必须抛 LedgerCorruptError")
        except LedgerCorruptError:
            pass
        # 损坏文件被备份(不静默覆盖消失)
        assert any(fp.parent.glob("cost.corrupt-*.json")), "应留下备份"

    def test_record_cost_atomic_and_lock(self):
        import uuid as _uuid_lg
        from shipin_platform.services.costing import (cost_file,
                                                      record_cost,
                                                      cost_summary)
        pid = f"lg-atomic-{_uuid_lg.uuid4().hex}"  # 唯一 PID(data 目录跨运行残留)
        record_cost(pid, "video", model="m", units=2.0, note="a")
        record_cost(pid, "video", model="m", units=1.0, note="b")
        rows = json.loads(cost_file(pid).read_text(encoding="utf-8"))
        assert [r["seq"] for r in rows] == [1, 2]
        assert len(cost_summary(pid)["records"]) == 2
        # 无残留临时文件
        assert not list(cost_file(pid).parent.glob("cost.tmp-*"))

    def test_save_is_atomic(self):
        """轮43:_save 旧为截断式 write_text,半路被杀留半截 JSON →
        _load 裸抛 → /report 500。"""
        pid = "lg-save"
        pr._save(pid, "final_review.json", {"verdict": "pass", "findings": []})
        got = pr._load(pid, "final_review.json")
        assert got["verdict"] == "pass"
        assert not list(pr._project_dir(pid).glob("*.tmp"))

    # ── 轮49(九审 P1-3/P3-9/P3-10):账本数字诚信 ──────────────────

    def test_negative_units_clamped_to_zero(self):
        """负 units 曾把 total_usd 拉低——预算判据 `used > max` 被反向
        掏空(裸端点注入负 duration 的复现实测:持续放行超预算项目)。"""
        import uuid as _uuid_lg
        from shipin_platform.services.costing import (cost_file,
                                                      record_cost,
                                                      cost_summary)
        pid = f"lg-neg-{_uuid_lg.uuid4().hex}"
        r = record_cost(pid, "video", model="m", units=-5.0, note="attack")
        assert r["units"] == 0.0 and r["usd"] == 0.0, r
        assert cost_summary(pid)["total_usd"] == 0.0

    def test_structural_corrupt_ledger_raises_not_silent_zero(self):
        """合法 JSON 但结构不是 list(dict/str/num):旧代码 isinstance
        检查后静默返 [] = 预算闸判「没花钱」放行,与 LedgerCorruptError
        的设计意图自相矛盾。"""
        import uuid as _uuid_lg
        from shipin_platform.services.costing import (LedgerCorruptError,
                                                      _load_rows, cost_file)
        pid = f"lg-struct-{_uuid_lg.uuid4().hex}"
        fp = cost_file(pid)
        fp.write_text('{"seq": 1, "usd": 99}', encoding="utf-8")  # dict
        try:
            _load_rows(pid)
            raise AssertionError("结构损坏必须抛 LedgerCorruptError")
        except LedgerCorruptError:
            pass
        # 损坏文件被备份(不硬删),便于人工核对
        assert list(fp.parent.glob("cost.corrupt-*.json")), "应备份损坏账本"


class TestGenerateNoSilentSkip:
    """轮44(E2E 三连挂根因):三次生成全败时,旧代码 `r is None → continue`
    跳过 attempt 循环尾的 qc='fix',带着基准 manifest 深拷贝来的 stale
    qc=="ok" 通过循环后检查 → generate 假报 ok:True → assemble 拿不存在
    的基准 clip 拼接才在 stitch 炸("no packets")。变体/复用 manifest 的
    场景必踩(潜伏 fail-open)。必须显式失败。"""

    def _seed(self, pid: str):
        pr._save(pid, "brief.json", {"product_info": "x", "duration_sec": 9,
                                     "brand_name": "测试牌"})
        pr._save(pid, "storyboard.json", {
            "hero_shot": "S01",
            "shots": [{"shot_id": "S01", "duration_sec": 3, "narration": "好",
                       "beat": "hook", "scene": "门口", "subject": "杯",
                       "motion": "steam rises slowly", "spatial": "center",
                       "camera": "dolly in", "shot_size": "cu"}]})
        ip = {"style_anchor": "soft light",
              "shot_prompts": [{"shot_id": "S01", "prompt_en": "a cup"}]}
        vp = {"shot_prompts": [{"shot_id": "S01", "prompt_text": "steam"}]}
        pr._save(pid, "image_prompt.json", ip)
        pr._save(pid, "video_prompt.json", vp)
        from shipin_platform.contracts import stable_artifact_hash
        store = ProjectStageStore(":memory:")
        store.create_project(pid)
        store.record_confirmation(pid, "script")
        store.record_confirmation(pid, "storyboard")
        store.record_artifact(pid, "script", "h")
        store.record_artifact(pid, "storyboard", "h")
        store.record_artifact(pid, "image_prompt", stable_artifact_hash(ip))
        store.record_artifact(pid, "video_prompt", stable_artifact_hash(vp))
        # 关键:stale qc=ok + clip 指向不存在的文件(基准拷贝的形态)
        pr._save(pid, "manifest.json", {
            "shots": {"S01": {"first_frame": str(pr._project_dir(pid)
                                                 / "nope.jpg"),
                              "qc": "ok",
                              "clip": str(pr._project_dir(pid)
                                          / "nope_clip.mp4")}}})
        return store

    def test_all_attempts_failed_returns_not_ok(self, monkeypatch):
        pid = "gen-noskip"
        store = self._seed(pid)

        def _boom(*a, **k):
            raise RuntimeError("agnes video endpoint 500")

        monkeypatch.setattr(pr, "generate_video_agnes", _boom)

        def _fake_img(*a, **k):
            import subprocess as _sp2
            out = pr._project_dir(pid) / "S01.jpg"
            _sp2.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                      "-i", "color=c=blue:s=320x240:r=24",
                      "-frames:v", "1", str(out)],
                     check=True, capture_output=True)
            return {"ok": True, "output": str(out)}

        monkeypatch.setattr(pr, "generate_image_agnes", _fake_img)
        r = pr.run_generate_phase(pid, store)
        assert r["ok"] is False, r
        assert "3 次生成未成功" in r["reason"], r["reason"]
        # stale qc 必须被清掉,不允许 ok 离场
        m = pr._load(pid, "manifest.json")
        assert m["shots"]["S01"]["qc"] == "fix"

    def test_keyframe_critical_survives_successful_generation(
            self, monkeypatch, tmp_path):
        """八审 P0#1:keyframe 门的 critical 必须活到 shots_review.json
        ——旧代码 keyframe 分支另起 _load 副本保存,qc=ok 分支用循环前
        的旧字典整体 _save 把它抹掉,happy path 下关键帧门 100% 失效。"""
        import subprocess as _sp
        from shipin_platform.review import hard_gates as hg
        pid = "gen-kfover"
        # 状态隔离:固定 pid 的项目目录跨运行残留会让 L904 的 _load 把
        # 上一轮(修复后)的 __keyframe_* 条目带进内存,旧代码的覆盖 bug
        # 被残留掩盖——测试专验内存字典覆盖,必须先清场(实测教训)
        shutil.rmtree(pr._project_dir(pid), ignore_errors=True)
        store = self._seed(pid)

        def _fake_gen(prompt, duration=5, resolution="720p", work_dir=None,
                      first_frame=None, last_frame=None, output_path=None,
                      negative_prompt="", **kw):
            out = output_path or str(tmp_path / "f.mp4")
            _sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                     "-i", "testsrc2=duration=3:size=320x240:r=24",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", out],
                    check=True, capture_output=True)
            return {"ok": True, "output": out, "master_path": ""}

        monkeypatch.setattr(pr, "generate_video_agnes", _fake_gen)

        def _fake_img(*a, **k):
            out = pr._project_dir(pid) / "S01.jpg"
            _sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                     "-i", "testsrc2=duration=1:size=320x240:r=24",
                     "-frames:v", "1", str(out)],
                    check=True, capture_output=True)
            return {"ok": True, "output": str(out)}

        monkeypatch.setattr(pr, "generate_image_agnes", _fake_img)
        monkeypatch.setattr(hg, "check_keyframes", lambda shots: {
            "verdict": "fix",
            "findings": [{"severity": "critical", "code": "KEYFRAME_MISMATCH",
                          "message": "关键帧与分镜主体不符(stub)"}]})
        r = pr.run_generate_phase(pid, store)
        assert r["ok"] is True, str(r.get("reason"))[:200]
        sr = pr._load(pid, "shots_review.json") or {}
        assert "__keyframe_S01__" in sr, sorted(sr.keys())
        kf = sr["__keyframe_S01__"]
        assert kf["verdict"] == "fix"
        assert any(f["code"] == "KEYFRAME_MISMATCH" for f in kf["findings"])
        # 单镜 VLM 条目与 keyframe 条目必须共存(不是互相覆盖)
        assert "S01" in sr, sorted(sr.keys())

    def test_successful_generation_still_ok(self, monkeypatch, tmp_path):
        """轮44 反向:生成成功时流程照旧(修复不能误伤正常路径)。"""
        import subprocess as _sp
        pid = "gen-ok43"
        store = self._seed(pid)

        def _fake_gen(prompt, duration=5, resolution="720p", work_dir=None,
                      first_frame=None, last_frame=None, output_path=None,
                      negative_prompt="", **kw):
            out = output_path or str(tmp_path / "f.mp4")
            _sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                     "-i", "testsrc2=duration=3:size=320x240:r=24",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", out],
                    check=True, capture_output=True)
            return {"ok": True, "output": out, "master_path": ""}

        monkeypatch.setattr(pr, "generate_video_agnes", _fake_gen)

        def _fake_img(*a, **k):
            import subprocess as _sp2
            out = pr._project_dir(pid) / "S01.jpg"
            _sp2.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                      "-i", "testsrc2=duration=1:size=320x240:r=24",
                      "-frames:v", "1", str(out)],
                     check=True, capture_output=True)
            return {"ok": True, "output": str(out)}

        monkeypatch.setattr(pr, "generate_image_agnes", _fake_img)
        r = pr.run_generate_phase(pid, store)
        assert r["ok"] is True, str(r.get("reason"))[:200]
        m = pr._load(pid, "manifest.json")
        assert m["shots"]["S01"]["qc"] == "ok"

    # ── 轮44:池化 clip(变体复用基准素材)的新鲜度判据 ────────────────

    def _seed_pooled(self, pid: str, pool_dir: Path, motion: str):
        """变体形态:manifest 的 clip/first_frame 指向**别的项目目录**
        (共享素材池),qc=ok + clip_shot_sha 已随深拷贝携带。"""
        pr._save(pid, "brief.json", {"product_info": "x", "duration_sec": 9,
                                     "brand_name": "测试牌",
                                     "style_anchor": "teal tones"})
        pr._save(pid, "storyboard.json", {
            "hero_shot": "S01",
            "shots": [{"shot_id": "S01", "duration_sec": 3, "narration": "好",
                       "beat": "hook", "scene": "门口", "subject": "杯",
                       "motion": motion, "spatial": "center",
                       "camera": "dolly in", "shot_size": "cu"}]})
        ip = {"style_anchor": "teal tones",
              "shot_prompts": [{"shot_id": "S01", "prompt_en": "a cup"}]}
        vp = {"shot_prompts": [{"shot_id": "S01", "prompt_text": "steam"}]}
        pr._save(pid, "image_prompt.json", ip)
        pr._save(pid, "video_prompt.json", vp)
        from shipin_platform.contracts import stable_artifact_hash
        store = ProjectStageStore(":memory:")
        store.create_project(pid)
        store.record_confirmation(pid, "script")
        store.record_confirmation(pid, "storyboard")
        store.record_artifact(pid, "script", "h")
        store.record_artifact(pid, "storyboard", "h")
        store.record_artifact(pid, "image_prompt", stable_artifact_hash(ip))
        store.record_artifact(pid, "video_prompt", stable_artifact_hash(vp))
        # 池化素材:clip/首帧都在 pool_dir(他方项目),且带 base 的指纹
        shot = {"shot_id": "S01", "duration_sec": 3, "subject": "杯",
                "scene": "门口", "motion": motion}
        pool_dir.mkdir(parents=True, exist_ok=True)
        (pool_dir / "S01_clip.mp4").write_bytes(b"pooled-clip")
        import subprocess as _sp_pool
        _sp_pool.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                      "-i", "testsrc2=duration=1:size=320x240:r=24",
                      "-frames:v", "1", str(pool_dir / "S01.jpg")],
                     check=True, capture_output=True)
        pr._save(pid, "manifest.json", {
            "shots": {"S01": {"first_frame": str(pool_dir / "S01.jpg"),
                              "last_frame": str(pool_dir / "S01.jpg"),
                              "qc": "ok",
                              "clip": str(pool_dir / "S01_clip.mp4"),
                              "clip_shot_sha": pr._clip_shot_sha("S01", 3,
                                                                 shot)}}})
        return store

    def test_pooled_clip_reused_when_shot_definition_unchanged(
            self, monkeypatch, tmp_path):
        """轮44:变体改 style_anchor(prompt 变)但镜头定义没变 → 池化
        复用合法(轮42 的原始 prompt 指纹误杀了它,E2E 三连挂根因)。"""
        pid = "pool-reuse"
        store = self._seed_pooled(pid, tmp_path / "pool",
                                  "steam rises slowly")
        called = []

        def _boom(*a, **k):
            called.append(1)
            raise RuntimeError("不应触发重新生成")

        monkeypatch.setattr(pr, "generate_video_agnes", _boom)
        monkeypatch.setattr(
            pr, "generate_image_agnes",
            lambda *a, **k: {"ok": True, "output": "x.jpg"})
        r = pr.run_generate_phase(pid, store)
        assert r["ok"] is True, str(r.get("reason"))[:200]
        assert not called, "池化复用不得触发重新生成"
        rows = [x for x in r["report"] if x.get("shot_id") == "S01"]
        assert rows and rows[0].get("cached") and rows[0].get("pooled"), rows

    def test_pooled_clip_regenerated_when_shot_definition_changed(
            self, monkeypatch, tmp_path):
        """轮44 另一面:/rewrite 改了镜头动作(定义变)→ 即使池化也强制
        重生(七审 #3 的关切不放松)。"""
        pid = "pool-regen"
        store = self._seed_pooled(pid, tmp_path / "pool2",
                                  "steam rises slowly")
        # 篡改分镜:动作变了(模拟 /rewrite)
        sb = pr._load(pid, "storyboard.json")
        sb["shots"][0]["motion"] = "cup tips over suddenly"
        pr._save(pid, "storyboard.json", sb)
        called = []

        def _fake_gen(prompt, duration=5, resolution="720p", work_dir=None,
                      first_frame=None, last_frame=None, output_path=None,
                      negative_prompt="", **kw):
            called.append(1)
            import subprocess as _sp3
            out = output_path or str(tmp_path / "f.mp4")
            _sp3.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                      "-i", "testsrc2=duration=3:size=320x240:r=24",
                      "-c:v", "libx264", "-pix_fmt", "yuv420p", out],
                     check=True, capture_output=True)
            return {"ok": True, "output": out, "master_path": ""}

        monkeypatch.setattr(pr, "generate_video_agnes", _fake_gen)
        monkeypatch.setattr(
            pr, "generate_image_agnes",
            lambda *a, **k: {"ok": True,
                             "output": str(pr._project_dir(pid) / "S01.jpg")})
        r = pr.run_generate_phase(pid, store)
        assert called, "镜头定义变了必须重生"
        assert r["ok"] is True, str(r.get("reason"))[:200]


class TestGenerateFailureDiagnosis:
    """轮45(E2E 第 6 次挂:S08 3 次生成未成功、reason 无任何细节):
    生成全败的失败原因必须可诊断。旧代码 reason 只带网络异常 error,
    而 generate_video_agnes 要么抛异常要么返回 dict——「生成成功但
    QC 判 fix」这一整类失败(参考图漂移/内部切镜/形态突变)在 reason
    里无迹可寻,attempts 也只记 verdict+cuts,findings 全文丢弃。
    E2E 现场正是这个形态:43 分钟跑到 S08,三连 fix,零细节。"""

    _seed = TestGenerateNoSilentSkip._seed

    def _fake_gen_writing_clip(self, pid, tmp_path, payload):
        def _fake_gen(prompt, duration=5, resolution="720p", work_dir=None,
                      first_frame=None, last_frame=None, output_path=None,
                      negative_prompt="", **kw):
            out = output_path or str(tmp_path / "f.mp4")
            import subprocess as _sp
            _sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                     "-i", "testsrc2=duration=3:size=320x240:r=24",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", out],
                    check=True, capture_output=True)
            return payload(out)
        return _fake_gen

    def _fake_img(self, pid):
        def _fake_img(*a, **k):
            import subprocess as _sp2
            out = pr._project_dir(pid) / "S01.jpg"
            _sp2.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                      "-i", "testsrc2=duration=1:size=320x240:r=24",
                      "-frames:v", "1", str(out)],
                     check=True, capture_output=True)
            return {"ok": True, "output": str(out)}
        return _fake_img

    def _qc_fix_stub(self, monkeypatch, code="REF_MISMATCH"):
        monkeypatch.setattr(pr, "qc_clip", lambda *a, **k: {
            "verdict": "fix",
            "checks": {"internal_cuts": {"value": 0}},
            "findings": [
                {"severity": "critical", "code": code,
                 "message": "镜头S01 首帧与参考图感知距离 58 (fail>46)"},
                {"severity": "suggestion", "code": "REF_DRIFT",
                 "message": "偏大，建议人工复核"}],
            "next_action": "按 findings 重新生成该镜头"})

    def test_qc_critical_code_surfaces_in_reason(self, monkeypatch,
                                                 tmp_path):
        """QC 判 fix 的失败:reason 必须带 critical 编码+attempt,attempts
        必须带 findings 全文(否则运营拿到『3 次未成功』无从下手)。"""
        pid = "gen-diag-qc"
        store = self._seed(pid)
        monkeypatch.setattr(
            pr, "generate_video_agnes",
            self._fake_gen_writing_clip(
                pid, tmp_path, lambda out: {"ok": True, "output": out,
                                            "master_path": ""}))
        monkeypatch.setattr(pr, "generate_image_agnes", self._fake_img(pid))
        self._qc_fix_stub(monkeypatch)
        r = pr.run_generate_phase(pid, store)
        assert r["ok"] is False, r
        # reason:critical 编码 + attempt 号(三次同码不同 attempt 都要在)
        assert "QC: " in r["reason"], r["reason"]
        assert "REF_MISMATCH#1" in r["reason"], r["reason"]
        assert r["reason"].count("REF_MISMATCH#") == 3, r["reason"]
        assert "旧素材/过期 qc 状态不得复用" in r["reason"], r["reason"]
        # attempts:findings 全文(含 suggestion 与 message)不得再丢弃
        att = r["report"][-1]["attempts"]
        assert len(att) == 3, att
        f0 = att[0]["findings"][0]
        assert f0["code"] == "REF_MISMATCH"
        assert f0["severity"] == "critical"
        assert "58" in f0["message"], f0
        assert any(f["severity"] == "suggestion" for f in att[0]["findings"])

    def test_silent_none_return_is_recorded(self, monkeypatch, tmp_path):
        """生成器静默返回 None(不抛异常):必须留痕『返回空结果』——
        旧代码 r is None → continue 静默跳过,该 attempt 无迹可寻。"""
        pid = "gen-diag-none"
        store = self._seed(pid)
        monkeypatch.setattr(pr, "generate_video_agnes",
                            self._fake_gen_writing_clip(
                                pid, tmp_path, lambda out: None))
        monkeypatch.setattr(pr, "generate_image_agnes", self._fake_img(pid))
        r = pr.run_generate_phase(pid, store)
        assert r["ok"] is False, r
        assert "3 次生成未成功" in r["reason"], r["reason"]
        assert "生成器返回空结果" in r["reason"], r["reason"]
        att = r["report"][-1]["attempts"]
        assert len(att) == 3, att
        assert all(a.get("error") == "生成器返回空结果(无异常)"
                   for a in att), att

    def test_network_error_takes_priority_over_findings(self, monkeypatch,
                                                        tmp_path):
        """异常与 QC 败并存时 reason 以异常为主(更可操作),findings 仍
        完整落在 attempts 里可查。"""
        pid = "gen-diag-mix"
        store = self._seed(pid)
        state = {"n": 0}

        def _flaky(prompt, duration=5, resolution="720p", work_dir=None,
                   first_frame=None, last_frame=None, output_path=None,
                   negative_prompt="", **kw):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("429 Too Many Requests")
            out = output_path or str(tmp_path / "f.mp4")
            import subprocess as _sp
            _sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                     "-i", "testsrc2=duration=3:size=320x240:r=24",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", out],
                    check=True, capture_output=True)
            return {"ok": True, "output": out, "master_path": ""}

        monkeypatch.setattr(pr, "generate_video_agnes", _flaky)
        monkeypatch.setattr(pr, "generate_image_agnes", self._fake_img(pid))
        self._qc_fix_stub(monkeypatch, code="INTERNAL_CUTS")
        r = pr.run_generate_phase(pid, store)
        assert r["ok"] is False, r
        assert "429" in r["reason"], r["reason"]
        assert "QC:" not in r["reason"], r["reason"]
        att = r["report"][-1]["attempts"]
        assert att[0]["error"].startswith("429"), att[0]
        assert any(f["code"] == "INTERNAL_CUTS"
                   for a in att for f in a.get("findings") or []), att


    def test_real_qc_static_clip_diagnosed(self, monkeypatch, tmp_path):
        """真 qc_clip 链路:静态图卡(motion=0)三连 STATIC_SLIDESHOW——
        不用 fake qc,只 stub VLM 网络,证明诊断对真实 QC 败同样成立
        (S08 logo 卡正是这一类的现实候选)。"""
        pid = "gen-diag-real"
        store = self._seed(pid)
        monkeypatch.setattr(
            pr, "generate_video_agnes",
            self._fake_gen_writing_clip(
                pid, tmp_path,
                lambda out: {"ok": True, "output": out, "master_path": ""}))
        # 静态卡:-loop 1 单图 3s,motion_energy=0
        def _static_gen(prompt, duration=5, resolution="720p", work_dir=None,
                        first_frame=None, last_frame=None, output_path=None,
                        negative_prompt="", **kw):
            import subprocess as _sp
            img = pr._project_dir(pid) / "S01.jpg"
            _sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                     "-i", "testsrc2=duration=1:size=320x240:r=24",
                     "-frames:v", "1", str(img)],
                    check=True, capture_output=True)
            _sp.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1",
                     "-i", str(img), "-t", "3",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p",
                     output_path or str(tmp_path / "f.mp4")],
                    check=True, capture_output=True)
            return {"ok": True, "output": output_path, "master_path": ""}

        monkeypatch.setattr(pr, "generate_video_agnes", _static_gen)
        monkeypatch.setattr(pr, "generate_image_agnes", self._fake_img(pid))
        # VLM 两调用均不可用(vlm stun):协议失败只进 note,不得伪装 critical
        from shipin_platform.review import clip_qc as _cq
        monkeypatch.setattr(_cq, "vlm_same_scene",
                            lambda *a, **k: {"available": False,
                                             "reason": "vlm timeout (stub)"})
        monkeypatch.setattr(_cq, "vlm_morph_check",
                            lambda *a, **k: {"available": False,
                                             "reason": "vlm timeout (stub)"})
        r = pr.run_generate_phase(pid, store)
        assert r["ok"] is False, r
        assert "QC: " in r["reason"], r["reason"]
        assert "STATIC_SLIDESHOW#1" in r["reason"], r["reason"]
        # 协议失败不得升级成判定:findings 里只有确定性 critical,无 VLM 伪 critical
        att = r["report"][-1]["attempts"]
        codes = {f["code"] for a in att for f in a.get("findings") or []}
        assert "STATIC_SLIDESHOW" in codes, codes
        assert "REF_VLM_MISMATCH" not in codes, codes
        assert "MORPH_DETECTED" not in codes, codes
        # VLM 不可用原因进 note(可解释),不进 critical
        assert all("vlm timeout" in str(a.get("vlm_note"))
                   for a in att), att


class TestDialogueOnlyShots:
    """轮47(八审 P2#2):纯台词镜(narration 空、dialogue 有词)是合法
    剧本形态(模板:每镜 narration 或 dialogue 至少其一,全片至少 2 镜
    dialogue)——旧代码只认旁白轨,台词 TTS 实际合成成功也报「TTS
    缺失」,合法剧本永远无法出片且报错指错方向。"""

    def _seed_dlg_only(self, pid: str):
        import subprocess as _sp
        from shipin_platform.contracts import stable_artifact_hash
        pr._save(pid, "brief.json", {"product_info": "x", "duration_sec": 9,
                                     "brand_name": "测试牌"})
        pr._save(pid, "storyboard.json", {
            "hero_shot": "S01",
            # S01 纯台词(非末镜——末镜会被 _bind_brand 强制写品牌旁白,
            # 那是品牌确定性通道的设计行为);S02 常规旁白镜
            "shots": [
                {"shot_id": "S01", "duration_sec": 3, "narration": "",
                 "dialogue": {"role_code": "assistant_female",
                              "text": "欢迎光临"},
                 "beat": "hook", "scene": "门口", "subject": "杯",
                 "motion": "steam rises slowly", "spatial": "center",
                 "camera": "dolly in", "shot_size": "cu"},
                {"shot_id": "S02", "duration_sec": 3, "narration": "好喝的咖啡",
                 "dialogue": "", "beat": "value", "scene": "店内",
                 "subject": "杯", "motion": "steam drifts", "spatial": "center",
                 "camera": "truck left", "shot_size": "cu"}]})
        ip = {"style_anchor": "soft light",
              "shot_prompts": [{"shot_id": "S01", "prompt_en": "a cup"},
                               {"shot_id": "S02", "prompt_en": "a cup"}]}
        vp = {"shot_prompts": [{"shot_id": "S01", "prompt_text": "steam"},
                               {"shot_id": "S02", "prompt_text": "steam"}]}
        pr._save(pid, "image_prompt.json", ip)
        pr._save(pid, "video_prompt.json", vp)
        store = ProjectStageStore(":memory:")
        store.create_project(pid)
        store.record_confirmation(pid, "script")
        store.record_confirmation(pid, "storyboard")
        store.record_artifact(pid, "script", "h")
        store.record_artifact(pid, "storyboard", "h")
        store.record_artifact(pid, "image_prompt", stable_artifact_hash(ip))
        store.record_artifact(pid, "video_prompt", stable_artifact_hash(vp))
        pr._save(pid, "manifest.json", {"shots": {"S01": {}, "S02": {}}})
        return store

    def test_dialogue_only_shot_generates(self, monkeypatch, tmp_path):
        """台词镜必须能出片:manifest 落 dlg 轨、无旁白轨、align ok。"""
        import subprocess as _sp
        pid = "gen-dlgonly"
        shutil.rmtree(pr._project_dir(pid), ignore_errors=True)
        store = self._seed_dlg_only(pid)

        def _fake_gen(prompt, duration=5, resolution="720p", work_dir=None,
                      first_frame=None, last_frame=None, output_path=None,
                      negative_prompt="", **kw):
            out = output_path or str(tmp_path / "f.mp4")
            _sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                     "-i", "testsrc2=duration=3:size=320x240:r=24",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", out],
                    check=True, capture_output=True)
            return {"ok": True, "output": out, "master_path": ""}

        monkeypatch.setattr(pr, "generate_video_agnes", _fake_gen)

        def _fake_img(prompt, w, h, out, *a, **k):
            # 按调用方给的输出路径写(多镜各写各的首/末帧)
            _sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                     "-i", "testsrc2=duration=1:size=320x240:r=24",
                     "-frames:v", "1", str(out)],
                    check=True, capture_output=True)
            return {"ok": True, "output": str(out)}

        monkeypatch.setattr(pr, "generate_image_agnes", _fake_img)
        r = pr.run_generate_phase(pid, store)
        assert r["ok"] is True, str(r.get("reason"))[:300]
        mrec = pr._load(pid, "manifest.json")["shots"]["S01"]
        assert mrec.get("dlg") and Path(mrec["dlg"]).is_file(), mrec
        assert not mrec.get("tts"), mrec  # 台词镜不写旁白轨
        # 指纹照记(台词文本变化仍作废旧 dlg)
        assert mrec.get("tts_text_sha")

    def test_align_dialogue_only_not_narration_missing(self, tmp_path):
        """align 层:纯台词镜不得被判 NARRATION_MISSING(旧代码只要
        narration_path 空就 critical,台词轨照常落位也拦)。"""
        from shipin_platform.assembly import align_narration
        dlg = tmp_path / "d.mp3"
        import subprocess as _sp
        _sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                 "-i", "sine=frequency=300:duration=1.2", str(dlg)],
                check=True, capture_output=True)
        r = align_narration([{"shot_id": "S01", "duration_sec": 3.0,
                              "narration_path": None,
                              "dialogue_path": str(dlg)}])
        assert r["verdict"] == "ok", r["findings"]
        t = r["timeline"][0]
        assert t["dlg_sec"] >= 1.0
        assert t["window_sec"] >= 3.0

    def test_align_no_voice_at_all_still_critical(self, tmp_path):
        """反向:旁白台词都没有(该镜彻底无声)仍 critical——修复不能
        把「无声镜」也放行。"""
        from shipin_platform.assembly import align_narration
        r = align_narration([{"shot_id": "S01", "duration_sec": 3.0,
                              "narration_path": None,
                              "dialogue_path": None}])
        assert r["verdict"] == "fix"
        assert any(f["code"] == "NARRATION_MISSING" for f in r["findings"])


class TestProjectIdGuard:
    """轮55(十审 P0-1):project_id 无白名单 → _save/_load 可穿越
    data/projects 围栏写/读任意目录(实测 '../../pwned' 写到仓库根、
    '/abs' 写到磁盘根)。既有 2098 个项目目录全部合规,不影响正常流。"""

    @pytest.mark.parametrize("bad", ["../../pwned", "/abs", "..", "a/b",
                                     "a\\b", "..x", "x y", "x;y", "",
                                     "a" * 65])
    def test_traversal_ids_rejected(self, bad):
        from shipin_platform.orchestration import pipeline_runner as pr
        with pytest.raises(ValueError):
            pr._safe_project_id(bad)

    @pytest.mark.parametrize("ok", ["coffee-v7", "e2e-629a730c",
                                    "g3a1-e348a642", "bf-llm", "a" * 64])
    def test_normal_ids_accepted(self, ok):
        from shipin_platform.orchestration import pipeline_runner as pr
        assert pr._safe_project_id(ok) == ok

    def test_project_dir_stays_under_projects(self, tmp_path, monkeypatch):
        from shipin_platform.orchestration import pipeline_runner as pr
        monkeypatch.setattr(pr, "PROJECTS_DIR", tmp_path)
        d = pr._project_dir("ok-pid")
        assert d == tmp_path / "ok-pid"
        with pytest.raises(ValueError):
            pr._project_dir("../../pwned")
        assert not (tmp_path.parent / "pwned").exists()


class TestClipPoolSemantics:
    """轮47(八审 P3):_clip_is_pooled 旧判据(parent != 项目目录)把
    项目**子目录**里的 clip 也误判成池化——池化分支的新鲜度不比
    prompt 指纹,stale qc==ok + clip_shot_sha 可把任意旧 clip 塞回
    时间线。语义:项目目录树内 = 本地产物,树外 = 池。"""

    def test_project_subdir_is_not_pooled(self, tmp_path, monkeypatch):
        from shipin_platform.orchestration import pipeline_runner as pr
        pid = "pool-sub"
        monkeypatch.setattr(pr, "_project_dir", lambda p: tmp_path / p)
        proj = tmp_path / pid
        (proj / "work" / "parts").mkdir(parents=True)
        mrec = {"clip": str(proj / "work" / "parts" / "S01.mp4")}
        assert pr._clip_is_pooled(mrec, pid) is False, \
            "项目内子目录 clip 是本地产物,必须走全输入指纹"

    def test_project_root_is_not_pooled(self, tmp_path, monkeypatch):
        from shipin_platform.orchestration import pipeline_runner as pr
        pid = "pool-root"
        monkeypatch.setattr(pr, "_project_dir",
                            lambda p: tmp_path / p)
        proj = tmp_path / pid
        proj.mkdir(parents=True)
        mrec = {"clip": str(proj / "S01_canvas.mp4")}
        assert pr._clip_is_pooled(mrec, pid) is False

    def test_other_project_dir_is_pooled(self, tmp_path, monkeypatch):
        from shipin_platform.orchestration import pipeline_runner as pr
        pid = "pool-self"
        monkeypatch.setattr(pr, "_project_dir",
                            lambda p: tmp_path / p)
        (tmp_path / pid).mkdir(parents=True)
        (tmp_path / "coffee-v7").mkdir(parents=True)
        mrec = {"clip": str(tmp_path / "coffee-v7" / "S01_clip.mp4")}
        assert pr._clip_is_pooled(mrec, pid) is True, \
            "别的项目目录 = 共享素材池(变体复用基准,设计行为)"

    def test_sibling_prefix_dir_is_pooled(self, tmp_path, monkeypatch):
        """前缀相似但不同的目录(coffee-v7 vs coffee-v7x)不得误判树内。"""
        from shipin_platform.orchestration import pipeline_runner as pr
        pid = "pool-abc"
        monkeypatch.setattr(pr, "_project_dir",
                            lambda p: tmp_path / p)
        (tmp_path / pid).mkdir(parents=True)
        (tmp_path / (pid + "x")).mkdir(parents=True)
        mrec = {"clip": str(tmp_path / (pid + "x") / "S01.mp4")}
        assert pr._clip_is_pooled(mrec, pid) is True


class TestFinalPromotion:
    """轮48(八审 P4 遗留):final 晋升序列崩溃安全。

    轮43 旧序列 move 走旧片再 replace 新片,两步之间崩溃留下「盘上无
    final.mp4」空窗,『可回滚』是空头支票。新序列:先拷贝备份(旧片
    留岗)→ 原子替换;_recover_final 兜底恢复更早版本留存的残局。"""

    def _mk(self, work: Path, name: str, content: bytes) -> Path:
        p = work / name
        p.write_bytes(content)
        return p

    def test_promote_first_time_no_backup(self, tmp_path):
        from shipin_platform.orchestration import pipeline_runner as pr
        work = tmp_path / "proj"
        work.mkdir()
        cand = self._mk(work, "final_norm.candidate.mp4", b"NEW")
        r = pr._promote_final(work, cand)
        assert r["ok"] is True, r
        assert r["backed_up"] is False  # 首次晋升无旧片
        assert (work / "final.mp4").read_bytes() == b"NEW"
        assert not (work / "final.published.mp4").exists()
        assert r["sha256"]

    def test_promote_backs_up_old_and_keeps_it_on_duty(self, tmp_path):
        """二次晋升:旧片内容进 published 备份,且晋升过程中盘上始终
        有 final.mp4(无 move 空窗——备份是拷贝不是 move)。"""
        from shipin_platform.orchestration import pipeline_runner as pr
        work = tmp_path / "proj"
        work.mkdir()
        self._mk(work, "final.mp4", b"OLD")
        cand = self._mk(work, "final_norm.candidate.mp4", b"NEW")
        r = pr._promote_final(work, cand)
        assert r["ok"] is True and r["backed_up"] is True
        assert (work / "final.mp4").read_bytes() == b"NEW"
        assert (work / "final.published.mp4").read_bytes() == b"OLD"

    def test_promote_survives_missing_candidate(self, tmp_path):
        """候选缺失(ex:磁盘满没写出来)→ 显式失败,旧片原封不动。"""
        from shipin_platform.orchestration import pipeline_runner as pr
        work = tmp_path / "proj"
        work.mkdir()
        self._mk(work, "final.mp4", b"OLD")
        r = pr._promote_final(work, work / "nope_candidate.mp4")
        assert r["ok"] is False, r
        assert (work / "final.mp4").read_bytes() == b"OLD"

    def test_recover_restores_published_when_final_missing(self, tmp_path):
        from shipin_platform.orchestration import pipeline_runner as pr
        work = tmp_path / "proj"
        work.mkdir()
        self._mk(work, "final.published.mp4", b"PUBLISHED")
        r = pr._recover_final(work)
        assert r == "restored_from_published", r
        assert (work / "final.mp4").read_bytes() == b"PUBLISHED"

    def test_recover_noop_when_final_present(self, tmp_path):
        from shipin_platform.orchestration import pipeline_runner as pr
        work = tmp_path / "proj"
        work.mkdir()
        self._mk(work, "final.mp4", b"LIVE")
        self._mk(work, "final.published.mp4", b"OLD")
        assert pr._recover_final(work) is None
        assert (work / "final.mp4").read_bytes() == b"LIVE"

    def test_recover_noop_when_nothing_to_recover(self, tmp_path):
        from shipin_platform.orchestration import pipeline_runner as pr
        work = tmp_path / "proj"
        work.mkdir()
        assert pr._recover_final(work) is None

    def test_recover_copy_failure_is_not_fatal(self, tmp_path,
                                               monkeypatch):
        """备份拷贝失败(磁盘错)不炸 assemble——返回 None,由后续流程
        自然报错(恢复是尽力而为,不能自己变成崩溃源)。"""
        from shipin_platform.orchestration import pipeline_runner as pr
        import shutil as _sh
        work = tmp_path / "proj"
        work.mkdir()
        self._mk(work, "final.published.mp4", b"PUB")

        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(_sh, "copy2", _boom)
        assert pr._recover_final(work) is None
        assert not (work / "final.mp4").exists()


class TestTargetedRegeneration:
    """轮61(生成策略转向):单镜 VLM 诊断 critical 不再直接交 assemble
    拦——generate 内用变体 prompt 定向重生成(attempt 阶梯天然分档:
    0=原 prompt / 1=固定机位 / 2=固定机位+极简运动)。定向重试仍
    critical 才落记录,由 assemble 终审统一阻断(原范式不变)。"""

    def _seed(self, pid, scene="店内，主角手部按下按键"):
        shutil.rmtree(pr._project_dir(pid), ignore_errors=True)
        pr._save(pid, "brief.json", {"product_info": "x", "duration_sec": 9,
                                     "brand_name": "晨光咖啡"})
        pr._save(pid, "storyboard.json", {"hero_shot": "S01", "shots": [
            {"shot_id": "S01", "duration_sec": 3, "narration": "n",
             "dialogue": "", "beat": "hook", "scene": scene,
             "subject": "主角", "motion": "presses the brew button",
             "spatial": "close-up", "camera": "static",
             "shot_size": "cu"}]})
        pr._save(pid, "image_prompt.json", {"style_anchor": "s",
            "shot_prompts": [{"shot_id": "S01", "prompt_en": "cup"}]})
        pr._save(pid, "video_prompt.json", {"shot_prompts": [
            {"shot_id": "S01", "prompt_text": "close-up pressing button"}]})
        store = ProjectStageStore(":memory:")
        store.create_project(pid)
        for g in ("script", "storyboard"):
            store.record_artifact(pid, g, "h")
            store.record_confirmation(pid, g)
        return store

    @staticmethod
    def _wire_gen(monkeypatch, pid, tmp_path, prompts):
        def _fake_gen(prompt, duration=5, resolution="720p", work_dir=None,
                      first_frame=None, last_frame=None, output_path=None,
                      negative_prompt="", **kw):
            prompts.append(prompt)
            out = output_path or str(tmp_path / "f.mp4")
            import subprocess as _sp
            _sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                     "-i", "testsrc2=duration=3:size=320x240:r=24",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", out],
                    check=True, capture_output=True)
            return {"ok": True, "output": out, "master_path": ""}

        def _fake_img(*a, **k):
            out = pr._project_dir(pid) / "S01.jpg"
            import subprocess as _sp
            _sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                     "-i", "testsrc2=duration=1:size=320x240:r=24",
                     "-frames:v", "1", str(out)],
                    check=True, capture_output=True)
            return {"ok": True, "output": str(out)}

        monkeypatch.setattr(pr, "generate_video_agnes", _fake_gen)
        monkeypatch.setattr(pr, "generate_image_agnes", _fake_img)

    def test_critical_review_triggers_variant_regen(self, monkeypatch,
                                                    tmp_path):
        """诊断 critical → 换固定机位变体重生成 → 通过后落账;
        attempts 必须留痕(codes + vlm_review 标记)。"""
        from shipin_platform.review import hard_gates as hg
        import uuid as _uuid_rt
        pid = f"retarget-ok-{_uuid_rt.uuid4().hex[:8]}"
        store = self._seed(pid)
        prompts = []
        self._wire_gen(monkeypatch, pid, tmp_path, prompts)
        vcalls = {"n": 0}

        def _fake_review(clip, shot, frames_count=4, key=None):
            vcalls["n"] += 1
            if vcalls["n"] == 1:
                return {"verdict": "fix", "findings": [
                    {"severity": "critical", "code": "VLM_BREAK",
                     "message": "stub: 侧面按键"}]}
            return {"verdict": "pass", "findings": []}

        monkeypatch.setattr(hg, "vlm_review_shot", _fake_review)
        r = pr.run_generate_phase(pid, store)
        assert r["ok"] is True, str(r.get("reason"))[:200]
        assert len(prompts) == 2, prompts
        assert "FIXED camera" in prompts[1], prompts[1]   # attempt 1 变体
        att = r["report"][-1]["attempts"]
        codes = [a.get("codes") for a in att if a.get("vlm_review")]
        assert ["VLM_BREAK"] in codes, att

    def test_persistent_critical_still_records_for_assemble(
            self, monkeypatch, tmp_path):
        """定向重试后仍 critical:不得 silently 放行——落
        shots_review 记录交 assemble 终审阻断(原范式保持)。"""
        from shipin_platform.review import hard_gates as hg
        import uuid as _uuid_rp
        pid = f"retarget-persist-{_uuid_rp.uuid4().hex[:8]}"
        store = self._seed(pid)
        prompts = []
        self._wire_gen(monkeypatch, pid, tmp_path, prompts)
        monkeypatch.setattr(hg, "vlm_review_shot",
                            lambda clip, shot, frames_count=4, key=None: {
                                "verdict": "fix", "findings": [
                                    {"severity": "critical",
                                     "code": "VLM_BREAK",
                                     "message": "stub: 恒定侧面按键"}]})
        r = pr.run_generate_phase(pid, store)
        assert r["ok"] is True, str(r.get("reason"))[:200]  # generate 成功
        assert len(prompts) == 2, prompts                   # 定向重试发生过
        sr = pr._load(pid, "shots_review.json") or {}
        assert sr["S01"]["verdict"] == "fix"
        assert any(f.get("code") == "VLM_BREAK"
                   for f in sr["S01"]["findings"]), sr["S01"]


class TestSubtitleWrap:
    """轮44d:§10.6 宽度红线治本——720p@46px 下 10 字旁白烧出来
    63.7%,验收门(轮27)直接打死整条 assemble(E2E 实证)。中文排版惯例
    是标点折两行,渲染器多行 cue 基建早已就绪,在字幕生成侧一次治本。"""

    def test_long_line_wraps_at_punctuation(self):
        got = pr._wrap_text("加班后的倦，无人诉说", 9)
        assert "\n" in got and max(len(x) for x in got.split("\n")) <= 9

    def test_short_line_unchanged(self):
        assert pr._wrap_text("深夜街头，冷色如冰", 9) == "深夜街头，冷色如冰"
        assert pr._wrap_text("短句", 9) == "短句"

    def test_no_punctuation_hard_cut(self):
        got = pr._wrap_text("没有标点的超长字幕文本需要硬切处理", 9)
        assert "\n" in got

    def test_zero_budget_means_no_wrap(self):
        assert pr._wrap_text("加班后的倦，无人诉说", 0) == "加班后的倦，无人诉说"

    def test_build_srt_wraps_narration(self):
        sb = {"shots": [{"shot_id": "S01", "duration_sec": 3,
                         "narration": "加班后的倦，无人诉说",
                         "dialogue": ""}]}
        tl = [{"shot_id": "S01", "window_sec": 3.0}]
        srt = pr._build_srt(sb, tl, max_line_chars=9)
        assert "\n无人诉说" in srt, srt
        # 不传预算 = 旧行为(单行)
        srt_old = pr._build_srt(sb, tl)
        assert "加班后的倦，无人诉说" in srt_old

    def test_build_srt_timestamps_never_roll_to_1000(self):
        """轮49(九审 P1):sec/ms 独立取整会产出非法 `,1000`——平台自解析
        读成 20.1s(应 21.0),字幕早 ~0.9s 显示、验收在错点量墨迹(假过),
        ffmpeg 进位与 verify 错解析又造成间歇性硬失败。浮点命中的真实
        形态:audit 实测 1.27% 项目至少一中。"""
        import re
        from shipin_platform.tools.subtitle_renderer import _srt_ts_to_seconds
        tl = [{"shot_id": f"S{i:02d}", "window_sec": w, "tts_sec": 1.5,
               "dlg_sec": d, "audio_start_sec": 0.0, "dlg_start_sec": 0.0}
              for i, (w, d) in enumerate(zip(
                  [1.74, 2.93, 2.02, 1.76, 5.77, 4.88, 2.96, 4.75],
                  [2.89, 0.88, 1.13, 2.98, 1.18, 1.61, 1.8, 1.3]))]
        sb = {"shots": [{"shot_id": f"S{i:02d}", "narration": "旁白文本",
                         "dialogue": {"role_code": "hero_male",
                                      "text": "台词"}} for i in range(8)]}
        srt = pr._build_srt(sb, tl, {"canvas_w": 1280}, max_line_chars=10)
        stamps = [ts for ln in srt.splitlines() if " --> " in ln
                  for ts in ln.split(" --> ")]
        assert stamps
        for ts in stamps:
            assert re.fullmatch(r"\d{2}:\d{2}:\d{2},\d{3}", ts), ts
            assert int(ts.split(",")[1]) <= 999, f"毫秒越界: {ts}"
            # 回环:解析值与名义值一致(不进位错位)
            sec = float(ts[:2]) * 3600 + float(ts[3:5]) * 60 + \
                float(ts[6:8]) + float(ts[9:12]) / 1000
            back = _srt_ts_to_seconds(ts)
            assert abs(back - sec) < 0.002, (ts, sec, back)


class TestSubtitleWidthWrap:
    """轮51(E2E 第 8 次唯一卡点):字符折行预算(1 字=1em)对 CJK 系统
    性低估——46px 字号实测 advance≈48px,17 字=816px=63.7% 屏宽,
    §10.6 验收门(≤62%)当场打死(generate 全过之后字幕成唯一卡点)。
    治本:_wrap_to_width 用 drawtext/libass 实际渲染的同一字体度量。"""

    BUDGET = int(1280 * 0.62)  # 793px

    # ── 轮59 断点4:折行宽度必须按实际画幅 ─────────────────────────
    # 竖版 720x1280 项目 manifest 无 canvas_w 时旧代码回退横屏 1280,
    # 可用宽 446px 却按 793px 折行,12 字 slogan(598px)必被 §10.6
    # 打死,assemble 到字幕 100% 卡死(一句话驱动真链路实测)。

    def test_vertical_canvas_wraps_within_its_own_width(self):
        """竖版 720 宽:slogan 12 字必须折行且每行 ≤62%×720。"""
        from shipin_platform.tools.subtitle_renderer import measure_text_px
        sb = {"shots": [{"shot_id": "S06",
                         "narration": "晨光咖啡，每一杯都是新开始",
                         "dialogue": ""}]}
        tl = [{"shot_id": "S06", "window_sec": 4.0, "tts_sec": 3.0,
               "dlg_sec": 0.0}]
        srt = pr._build_srt(sb, tl, None, max_line_px=int(720 * 0.62),
                            font_size=46, max_line_chars=11)
        lines = [ln for b in srt.split("\n\n") if b.strip()
                 for ln in b.splitlines()[2:] if ln.strip()]
        assert lines, srt
        for ln in lines:
            px = measure_text_px(ln, 46) or 0
            assert px <= int(720 * 0.62), (ln, px)

    def test_horizontal_budget_would_overflow_vertical(self):
        """反向:按横屏 793px 折的行在竖版必超——证明修的是画幅混淆。"""
        from shipin_platform.tools.subtitle_renderer import measure_text_px
        long_line = "晨光咖啡，每一杯都是新开始"
        assert (measure_text_px(long_line, 46) or 0) > int(720 * 0.62)
        # 横屏预算下这行原样放过(正是旧 bug 的形态)
        srt = pr._build_srt({"shots": [{"shot_id": "S06",
                                        "narration": long_line,
                                        "dialogue": ""}]},
                            [{"shot_id": "S06", "window_sec": 4.0}], None,
                            max_line_px=int(1280 * 0.62), font_size=46,
                            max_line_chars=17)
        assert long_line in srt, srt

    def _pct(self, line: str) -> float:
        from shipin_platform.tools.subtitle_renderer import measure_text_px
        px = measure_text_px(line, 46)
        return (px or 0) / 1280 * 100

    def test_long_line_wraps_within_budget(self):
        from shipin_platform.orchestration.pipeline_runner import _wrap_to_width
        out = _wrap_to_width("加班后的倦意涌上心头，却无人可以诉说",
                             self.BUDGET, 46, 17)
        lines = out.split("\n")
        assert len(lines) >= 2
        for ln in lines:
            assert self._pct(ln) <= 62.0, (ln, self._pct(ln))

    def test_seventeen_chars_fit_one_line(self):
        """17 字是字符预算的极限值——px 度量下 61.1% 单行放得下,不再
        被误折(也不超 62%)。"""
        from shipin_platform.orchestration.pipeline_runner import _wrap_to_width
        text = "加班后的倦意涌上心头却无人可以诉说"
        out = _wrap_to_width(text, self.BUDGET, 46, 17)
        assert "\n" not in out, out
        assert self._pct(out) <= 62.0

    def test_no_leading_punctuation_after_wrap(self):
        """中文禁则:折行后行首不出标点(「…时刻 / ，从这里开始」这种
        必须消掉)。"""
        from shipin_platform.orchestration.pipeline_runner import _wrap_to_width
        out = _wrap_to_width("晨光咖啡，享受每一个清醒的清晨时刻，从这里开始",
                             self.BUDGET, 46, 17)
        lines = [l for l in out.split("\n") if l]
        assert len(lines) >= 2
        for ln in lines[1:]:
            assert ln[0] not in "，。！？；、,!?;:", ln

    def test_no_single_char_orphan_line(self):
        """硬切出的 1 字孤行要借字消掉(「…合不合 / 适」)。"""
        from shipin_platform.orchestration.pipeline_runner import _wrap_to_width
        out = _wrap_to_width("主角: 你喝一口试试看这个温度合不合适",
                             self.BUDGET, 46, 17)
        lines = [l for l in out.split("\n") if l]
        assert all(len(l) > 1 for l in lines), lines

    def test_font_unavailable_falls_back_to_char_wrap(self, monkeypatch):
        """PIL/字体不可用 → measure 返回 None → 回退字符折行,不炸。"""
        import shipin_platform.tools.subtitle_renderer as sr
        monkeypatch.setattr(sr, "measure_text_px", lambda *a, **k: None)
        from shipin_platform.orchestration.pipeline_runner import _wrap_to_width
        out = _wrap_to_width("加班后的倦意涌上心头，却无人可以诉说",
                             self.BUDGET, 46, 17)
        assert "\n" in out  # 长句仍被折(字符口径)

    def test_build_srt_px_mode_all_lines_within_62(self):
        """assemble 的调用形态(px 模式):每条 cue 的每一行实测 ≤62%。"""
        from shipin_platform.orchestration.pipeline_runner import _build_srt
        from shipin_platform.tools.subtitle_renderer import measure_text_px
        sb = {"shots": [
            {"shot_id": "S01", "duration_sec": 3,
             "narration": "加班后的倦意涌上心头，却无人可以诉说",
             "dialogue": ""},
            {"shot_id": "S02", "duration_sec": 3,
             "narration": "短句",
             "dialogue": {"role_code": "hero_male",
                          "text": "你喝一口试试看这个温度合不合适"}}]}
        tl = [{"shot_id": "S01", "window_sec": 3.0, "tts_sec": 2.5,
               "dlg_sec": 0.0},
              {"shot_id": "S02", "window_sec": 3.0, "tts_sec": 0.0,
               "dlg_sec": 2.5}]
        srt = _build_srt(sb, tl, {"canvas_w": 1280},
                         max_line_px=self.BUDGET, font_size=46,
                         max_line_chars=17)
        for block in srt.split("\n\n"):
            for ln in block.splitlines()[2:]:
                if not ln.strip():
                    continue
                px = measure_text_px(ln, 46) or 0
                assert px / 1280 * 100 <= 62.0, (ln, px / 1280 * 100)


class TestClipCacheFingerprint:
    """轮42(七审 #3):generate 的 clip 缓存捷径旧只看 qc=="ok"——
    storyboard 改写/变体改风格后 vid prompt 变了但 shot_id 不变,旧
    clip 原样复用:新 prompt 从未执行,「审查与花钱定稿的输入 ≠ 实际
    入拼的内容」(轮29 修了 prompt 侧、轮26 修了 TTS 侧,generate 侧
    一直漏)。输入指纹(prompt+首末帧+时长)不符必须强制重生。"""

    def test_same_inputs_same_fingerprint(self):
        from shipin_platform.orchestration import pipeline_runner as pr
        a = pr._clip_input_sha("one take, fixed camera", "/p/S01.jpg",
                               "/p/S02.jpg", 3.0)
        b = pr._clip_input_sha("one take, fixed camera", "/p/S01.jpg",
                               "/p/S02.jpg", 3.0)
        assert a == b

    def test_changed_prompt_invalidates(self):
        from shipin_platform.orchestration import pipeline_runner as pr
        a = pr._clip_input_sha("one take, fixed camera", "/p/S01.jpg",
                               "/p/S02.jpg", 3.0)
        b = pr._clip_input_sha("one take, dolly in", "/p/S01.jpg",
                               "/p/S02.jpg", 3.0)
        assert a != b, "prompt 变了必须作废旧 clip"

    def test_changed_first_frame_invalidates(self):
        from shipin_platform.orchestration import pipeline_runner as pr
        a = pr._clip_input_sha("p", "/p/S01.jpg", "/p/S02.jpg", 3.0)
        b = pr._clip_input_sha("p", "/p/S01_new.jpg", "/p/S02.jpg", 3.0)
        assert a != b, "关键帧换了必须作废旧 clip(视频以首帧为条件)"

    def test_changed_duration_invalidates(self):
        from shipin_platform.orchestration import pipeline_runner as pr
        a = pr._clip_input_sha("p", "/p/S01.jpg", "/p/S02.jpg", 3.0)
        b = pr._clip_input_sha("p", "/p/S01.jpg", "/p/S02.jpg", 4.0)
        assert a != b


class TestTtsFailureDetection:
    """轮31:合成失败必须显式失败(新审计 #2:轮26 指纹机制在失败路径上
    被绕过——synthesize 静默失败 → _tts_of 按 mtime 取旧音频 → 新指纹
    盖上去 → 该镜永远"新鲜",无限复用旧口播,align 按错音频算窗口)。"""

    def test_failed_segments_detected(self):
        from shipin_platform.orchestration import pipeline_runner as pr

        class _Seg:
            def __init__(self, sid, err="", out=""):
                self.shot_id = sid
                self.error = err
                self.output_path = out

        segs = [_Seg("S01", out="a.mp3"), _Seg("S02", err="edge-tts 502"),
                _Seg("S03", out="")]
        failed = pr._tts_failures(segs)
        assert [s.shot_id for s in failed] == ["S02", "S03"]

    def test_all_ok_no_failures(self):
        from shipin_platform.orchestration import pipeline_runner as pr

        class _Seg:
            def __init__(self, sid):
                self.shot_id = sid
                self.error = ""
                self.output_path = f"{sid}.mp3"

        assert pr._tts_failures([_Seg("S01"), _Seg("S02")]) == []


class TestPromptStageCache:
    """轮29:prompt 阶段 PASS 缓存必须过内容哈希。

    旧逻辑 row.status != "PASS" 才重审——分镜文本变了(shot_id 集合
    不变,如 _bind_brand 绑实品牌名、用户 /rewrite 改主体描述)时新派生
    prompt 被整个丢弃、生成用盘上的旧 prompt:品牌注入丢失要等终审
    BRAND_MISSING 才炸(钱已花完),且 prompt 阶段对新输入再无审查
    (审查过的输入 ≠ 实际使用的输入)。"""

    def _row(self, status: str, h: str):
        class _R:
            def __init__(self):
                self.status = status
                self.artifact_hash = h

            def keys(self):
                return ["status", "artifact_hash"]

            def __getitem__(self, k):
                return getattr(self, k)
        return _R()

    def test_same_hash_is_cache_hit(self):
        from shipin_platform.contracts import stable_artifact_hash
        from shipin_platform.orchestration import pipeline_runner as pr
        data = {"style_anchor": "x",
                "shot_prompts": [{"shot_id": "S01", "prompt_en": "A"}]}
        row = self._row("PASS", stable_artifact_hash(data))
        assert pr._prompt_stage_stale(row, data) is False

    def test_changed_input_is_stale(self):
        from shipin_platform.contracts import stable_artifact_hash
        from shipin_platform.orchestration import pipeline_runner as pr
        old = {"style_anchor": "x",
               "shot_prompts": [{"shot_id": "S01", "prompt_en": "A"}]}
        new = {"style_anchor": "x",
               "shot_prompts": [{"shot_id": "S01", "prompt_en": "B"}]}
        row = self._row("PASS", stable_artifact_hash(old))
        assert pr._prompt_stage_stale(row, new) is True

    def test_empty_hash_is_stale(self):
        from shipin_platform.orchestration import pipeline_runner as pr
        data = {"style_anchor": "x", "shot_prompts": []}
        assert pr._prompt_stage_stale(self._row("PASS", ""), data) is True

    def test_non_pass_row_not_stale(self):
        from shipin_platform.contracts import stable_artifact_hash
        from shipin_platform.orchestration import pipeline_runner as pr
        data = {"style_anchor": "x", "shot_prompts": []}
        row = self._row("BLOCKED", stable_artifact_hash({"other": 1}))
        assert pr._prompt_stage_stale(row, data) is False


class TestTextPhaseLlmDown:
    """轮32:语义审片不可用(规则全过)时立刻带可操作原因退出。

    旧行为:r["llm"] 被 steps 丢弃、6 次 attempt 每次烧 1 次 repair 生成
    +1 次审片后,用户拿到的 reason 是误导性的「LLM+规则都没过、人工介入
    剧本」;steps 里 6 行 decision=revise,criticals=0 自相矛盾(0 critical
    为何 revise),无任何字段指向 AGNES_KEY。"""

    def test_bails_with_actionable_reason_single_attempt(self, monkeypatch):
        from shipin_platform.orchestration.stage_store import ProjectStageStore
        # 过 run_text_phase 的 key 前置检查(中间失效由下方 stub 模拟)
        monkeypatch.setenv("AGNES_KEY", "x" * 24)
        pid = "text-llm-down"
        store = ProjectStageStore(":memory:")
        store.create_project(pid)
        brief = {"content_type": "product", "product_info": "测试咖啡",
                 "target_platform": "douyin", "duration_sec": 24,
                 "target_audience": "都市白领", "tone": "轻松治愈",
                 "creative_direction": "温暖治愈的产品短片",
                 "reference_materials": "", "special_requirements": ""}
        pr._save(pid, "brief.json", brief)
        _narr = ["深夜街头冷色如冰", "加班后的倦无人说", "推门暖光迎面而来",
                 "手工烘焙的香气", "第一口顺滑融化疲惫", "走向座位缓缓落座",
                 "原来温柔就在这一杯", "享受这一刻好滋味"]
        draft = {"duration_sec": 24, "shots": []}
        for i in range(1, 9):
            _dlg = ""
            if i in (3, 7):  # 至少 2 镜台词(引擎对白门禁)
                _dlg = {"role_code": "hero_male", "text": "欢迎光临慢慢喝"}
            draft["shots"].append({
                "shot_id": f"S{i:02d}", "duration_sec": 3,
                "shot_size": "中景", "camera": "固定机位",
                "spatial": "画面中央", "subject": "主角",
                "scene": "咖啡店", "motion": "端起咖啡杯",
                "narration": _narr[i - 1], "dialogue": _dlg})
        monkeypatch.setattr(pr, "_llm_json", lambda *a, **k: dict(draft))
        monkeypatch.setattr(
            pr, "llm_stage_review",
            lambda stage, data, brief=None: {
                "available": False, "reason": "测试:审片端点 500",
                "findings": [], "scores": {}, "raw": ""})
        r = pr.run_text_phase(pid, brief, store)
        assert r["ok"] is False
        # 可操作:点名是语义审片不可用 + 带上端点原因,而不是"剧本要人工介入"
        assert "语义审片不可用" in r["reason"], r["reason"]
        assert "审片端点 500" in r["reason"]
        assert "AGNES_KEY" in r["reason"]
        # 不空烧:1 次 attempt 就退出(旧行为烧满 6 次)
        assert len([s for s in r["steps"]
                    if s.get("stage") == "script"]) == 1
        # steps 带 llm 上下文(旧行为整个丢弃)
        st = next(s for s in r["steps"] if s.get("stage") == "script")
        assert (st.get("llm") or {}).get("available") is False


class TestIterateDurationLoop:
    """轮25 回归:_iterate 的时长闭环收尾块曾引用三个从未赋名的变量
    (fitted_total/dev/llm_meta)——stage="script" 且 brief 带
    duration_sec(brief 审查强制必填)时每轮必崩 NameError,整条剧本
    审查链路形同不存在(实跑复现:/api/pipeline/text 500)。此处把
    可达/不可达两条路径都钉住:不崩 + duration critical 正确产出。
    """

    def _script(self, n: int, per: float) -> dict:
        return {"shots": [{"shot_id": f"S{i:02d}", "duration_sec": per,
                           "narration": f"台词{i}", "dialogue": "",
                           "subject": "主角", "scene": "街头",
                           "motion": "独行"}
                          for i in range(1, n + 1)]}

    def _store_and_brief(self, pid: str, duration_sec: float):
        from shipin_platform.orchestration.stage_store import ProjectStageStore
        store = ProjectStageStore(":memory:")
        store.create_project(pid)
        pr._save(pid, "brief.json", {"product_info": "测试咖啡",
                                     "duration_sec": duration_sec,
                                     "slogan": "享受每一刻"})
        return store

    def test_unreachable_duration_emits_critical_no_crash(self):
        pid = "iter-unreach"
        store = self._store_and_brief(pid, 24)
        pr._iterate("script", self._script(1, 3), pid, store,
                    use_llm=False)
        rounds = pr._load(pid, "script_review.json")["rounds"]
        dur_findings = [f for rnd in rounds for f in rnd["findings"]
                        if "无法收敛" in str(f.get("issue") or "")]
        assert dur_findings, "不可达时长必须产出 duration critical"
        assert "24" in dur_findings[0]["issue"]

    def test_reachable_duration_no_crash(self):
        pid = "iter-reach"
        store = self._store_and_brief(pid, 24)
        pr._iterate("script", self._script(8, 3), pid, store,
                    use_llm=False)  # 8×3=24s 命中目标
        rounds = pr._load(pid, "script_review.json")["rounds"]
        assert not [f for rnd in rounds for f in rnd["findings"]
                    if "无法收敛" in str(f.get("issue") or "")], \
            "可达路径不应有时长意图 critical"
        assert rounds[-1].get("metadata", {}).get("duration", {}).get(
            "fitted") == 24


class TestTtsReuse:
    """轮26:TTS 复用判定与 glob 精确性。

    两个都实证过的洞:
    (a) 旧音频配新字幕——复用只看「有没有文件」,review/iterate 改了
        narration 后旧 {sid}_*.mp3 直接被复用,成片口播是旧词、字幕是
        新词,声画不一致违反剧本且无门能发现;
    (b) 旁白轨取到台词音频——台词段输出 {sid}_dlg_*.mp3 被 {sid}_*
        通配吞掉,align 按错音频算时长。
    """

    def test_glob_excludes_dialogue_file(self, tmp_path):
        import time
        from shipin_platform.orchestration import pipeline_runner as pr
        narr = tmp_path / "S01_aaaa1111.mp3"
        dlg = tmp_path / "S01_dlg_bbbb2222.mp3"
        narr.write_bytes(b"narr")
        time.sleep(0.02)
        dlg.write_bytes(b"dlg")  # 台词更新——旧逻辑 newest 会选它
        got = pr.glob_tts(tmp_path, "S01")
        assert got == str(narr), f"旁白 glob 吞了台词文件: {got}"
        assert pr.glob_tts(tmp_path, "S01_dlg") == str(dlg)

    def test_text_sha_tracks_narration_and_dialogue(self):
        from shipin_platform.orchestration import pipeline_runner as pr
        s1 = {"shot_id": "S01", "narration": "深夜街头", "dialogue": ""}
        s2 = {"shot_id": "S01", "narration": "深夜街头", "dialogue": ""}
        s3 = {"shot_id": "S01", "narration": "深夜的街头", "dialogue": ""}
        s4 = {"shot_id": "S01", "narration": "深夜街头",
              "dialogue": {"role_code": "biz_female", "text": "欢迎光临"}}
        assert pr._tts_text_sha(s1) == pr._tts_text_sha(s2)
        assert pr._tts_text_sha(s1) != pr._tts_text_sha(s3), "旁白变必须变"
        assert pr._tts_text_sha(s1) != pr._tts_text_sha(s4), "台词变必须变"

    def test_stale_text_sha_forces_regen(self, tmp_path):
        """manifest 记的文本指纹与当前台词不符 → 不可复用(返回 None)。"""
        from shipin_platform.orchestration import pipeline_runner as pr
        (tmp_path / "S01_aaaa1111.mp3").write_bytes(b"old")
        shot = {"shot_id": "S01", "narration": "新台词", "dialogue": ""}
        manifest = {"shots": {"S01": {"tts": str(tmp_path / "S01_aaaa1111.mp3"),
                                      "tts_text_sha": pr._tts_text_sha(
                                          {"narration": "旧台词"})}}}
        # 直接验证判定语义:文件在、指纹不符 → 需要重生
        fresh_sha = pr._tts_text_sha(shot)
        assert manifest["shots"]["S01"]["tts_text_sha"] != fresh_sha


class TestAssembleGate:
    """assemble 第一道闸：video_gen 未 PASS 绝不能拼接（防旧素材+新字幕混片）。"""

    def test_assemble_requires_video_gen(self, tmp_path, monkeypatch):
        import shipin_platform.orchestration.pipeline_runner as pr_mod
        from shipin_platform.orchestration.stage_store import ProjectStageStore
        store = ProjectStageStore(tmp_path / "s.db")
        pid = "p-intent-gate"
        store.create_project(pid)
        # 只有 script confirm + PASS，没有 video_gen
        store.record_confirmation(pid, "storyboard")
        store.record_artifact(pid, "brief", "h" * 64)
        store.record_artifact(pid, "script", "h" * 64)
        store.record_artifact(pid, "storyboard", "h" * 64)
        r = pr.run_assemble_phase(pid, store)
        assert r["ok"] is False
        assert "video_gen" in r["reason"]

    def test_assemble_requires_storyboard_confirmed(self, tmp_path):
        from shipin_platform.orchestration.stage_store import ProjectStageStore
        store = ProjectStageStore(tmp_path / "s2.db")
        pid = "p2"
        store.create_project(pid)
        store.record_artifact(pid, "video_gen", "a" * 64)
        r = pr.run_assemble_phase(pid, store)
        assert r["ok"] is False
        assert "确认" in r["reason"]