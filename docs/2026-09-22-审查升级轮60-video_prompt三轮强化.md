# 轮60：video_prompt 模板三轮强化（终审 SHOT_STORY_MISMATCH 收敛 7→4）

日期：2026-09-25。承接轮59 终审暴露的内容质量 finding，对 video_prompt
模板做三轮强化并**每轮真实重跑 tvc-07431 验证**。真链路：text→confirm
→generate×5/6(真 AGNES，含哈希对齐二次跑)→assemble×4。

## 强化一：景别词前移 + Logo 按场景命中（ execution ）

- 分镜表一直有 shot_size(ecu/cu/mcu/cs/ms/ws/ows)，旧模板景别
  硬编码 "medium close-up" 埋在 Camera 子句中段——S03(cu 特写)被
  生成成中景站姿。修:`_SIZE_TERMS` 词表，景别词**抬头强约束+否定式**
  (no wide establishing shot, no camera pull-back)。
- Logo 只注入首尾镜，但 S03/S04 的 scene 明确写「出水键/杯身 Logo」
  → 终审判「未出现品牌 Logo」。修:scene 命中「品牌|logo|吊牌」即补
  品牌指令。

## 强化二：品牌专有否定式 + 屏显指定 + 室内光线-动作一致性

- S04 实测杯身出**星巴克** Logo(模型发明竞品)→ 品牌专有否定式:
  "The only visible brand anywhere is '<brand>'. No other logos, no
  Starbucks or any third-party brand marks, no invented text..."。
- S05 实测屏幕显示 "25th year" 非品牌名 → "If a screen is visible it
  displays exactly '<brand>', nothing else."。
- S01 死结:scene「冷色黎明**室内**」× motion「sunlight moves slowly」
  物理矛盾(模型要么无阳光→finding 无阳光移动;要么加暖光→finding
  色温不符)→ 命中矛盾时代入等效可拍运动 "soft interior light
  drifting"。

## 强化三：交互位点专有约束

- S03 分镜「按下带品牌 Logo 的出水键」两轮被生成成「侧面通用按键」
  → 命中按键类 scene 追加:"The pressed control is the brew button on
  the machine's front panel with the '<brand>' logo printed directly
  beside it — not a side button, not a generic control."

## 连带修复：字数上限常量单一数据源（八审类回归）

- `_fix_prompt_word_count`(RevisionEngine)引用 `VIDEO_PROMPT_MAX_CHARS`
  但常量定义在 ReviewEngine → AttributeError 被 `fix()` 的 except
  吞掉落 manual → 每轮交 LLM → stall(轮59 恰同类未暴露,轮60 加属性
  后触发)。修:常量迁到 RevisionEngine,ReviewEngine 引用共用。
- 强化后 prompt 涨到 ~450-620 字必超 380 → 裁剪器加**保护段**:
  头部景别子句(第一句)与品牌否定式(尾部摘出前移)必保,中段可裁。
  实测 756→372 且三段全在。

## 实证收敛（同一 tvc-07431 项目逐轮重跑）

| 轮次 | critical | 消除 |
|---|---|---|
| 轮59 基线 | 7 | — |
| 强化一 | 3 | S03 景别、S01 光线死结、S05 屏显/人物、S04 竞品 |
| 强化二/三 | 4 | 模型随机性带来 S04 时序新 finding;S03 按键位置两轮依旧 |

**S03「侧面按键」判定为 AGNES 视频模型对按键位置指令遵循的能力
上限**——prompt 已给正面面板+Logo 旁+禁侧面的三重约束仍生成侧面
键。留作模型侧/换源(本地 MiniMax-H3 或换 prompt 策略)迭代项,
不再加 prompt 层补丁(已到边际)。

## 铁律印证

终审 remaining 4 条全部是审查门**忠实工作**的产物(KEYFRAME_MISMATCH/
VLM_BREAK/SHOT_STORY_MISMATCH),没有一条是门误报——内容生成质量与
审查质量在此分清:后者全程零假阳。

## 测试与回归

tests/test_engine.py(裁剪收敛+保头×2、常量单一源)、
test_pipeline_intent.py(竖版折行×2)。全套非 E2E **717 全绿**。
