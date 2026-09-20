# 阶段 1 任务书 · 立项（brief）

以资深广告策划视角完成立项，产出过审的 brief JSON。

1. POST /api/intake/questions（intent 用用户原话），先看它要问什么。
2. 用 /api/intake/draft 组装 brief，把下面“已定事实”填进 answers（其余维度一律走
   默认/missing 机制，不要自作主张补编）：
   - content_type: narrative
   - product_info: “联想 ThinkPad 年度旗舰，商用轻薄本，约 1.1kg，Intel Core Ultra 系列，
     AI PC 定位（本地 AI 加速、会议降噪、智能摘要），面向企业采购与个体创业者”
   - target_platform: douyin,weixin
   - duration_sec: 60
   - target_audience: 25-45 岁商务办公人群与创业者
   - tone: 高级、克制、专业、可信、全球化
   - creative_direction: 直接采用 tvc-main.md「创意」章节的定稿概念《轻，即从容》——核心视觉
     “指尖挑起笔记本边缘、机身悬空 3 厘米屏幕不晃”，原样写入，不得另造
   - style_anchor: 写一条可出图的风格短语（如 “cold morning airport, cinematic, teal&carbon”）
3. 把 draft 返回的 brief 原样存 D:/aishipin/shipin-platform/test_out/tvc_brief.json，
   然后立即 POST /api/review/iterate（stage=brief, max_rounds=3）。
4. 用返回口径循环：pass/pass_with_warnings → 完成；revise/MISSING → 按修复要求改数据重审
   （最多再 2 轮）；stall → 写明阻塞依据，进不了下一阶段。

完成标准：存在 test_out/tvc_brief.json（内容=最后一次修复后 data），且 TRACE 里记下
每轮 decision 与 manual_modes。