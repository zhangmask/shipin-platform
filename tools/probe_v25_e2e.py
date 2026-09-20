"""真实端到端：新默认模型 agnes-video-2.5-flash 走平台 QC 门禁验证。"""
import sys, time
from pathlib import Path

sys.path.insert(0, "src")
from shipin_platform.generation.generate_assets import generate_video_agnes
from shipin_platform.review.clip_qc import qc_clip

proj = Path(r"D:\aishipin\shipin-platform\data\projects\agnes-e2e-20260918")
first = proj / "S01.jpg"
out_dir = Path(r"D:\aishipin\shipin-platform\data\projects\v25-e2e")
out_dir.mkdir(parents=True, exist_ok=True)

start = time.time()
r = generate_video_agnes(
    prompt=("A hand presses the coffee machine button, steam puffs out, warm golden "
            "light, single continuous macro take, no cuts"),
    model="agnes-video-2.5-flash", duration=3,
    first_frame=str(first),
    output_path=str(out_dir / "s01_25.mp4"),
    work_dir=str(out_dir),
)
print("elapsed:", round(time.time() - start, 1), "s")
print("ok:", r.get("ok"), "| anchored:", r.get("anchored"), "| mode:", r.get("mode"),
      "| model:", r.get("model"), "| dur:", r.get("duration_sec"))
print("path:", r.get("path"))
print("warnings:", r.get("warnings"))
qc = qc_clip(r["path"], shot_id="S01", expected_duration_sec=3.0,
             reference_image=str(first))
print("QC:", qc.get("verdict"))
for k, v in qc["checks"].items():
    val = v.get("value") if isinstance(v, dict) else v
    print("  ", k, "=", val)