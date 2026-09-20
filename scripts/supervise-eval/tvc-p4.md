# 阶段 4 任务书 · 生成提示词（image/video prompt）

基于阶段 3 过审的分镜（test_out/tvc_storyboard.json），为关键镜头写 AI 生成提示词包。

1. 读 test_out/tvc_storyboard.json。
2. 选出最高价值的 3 个镜头（建议：产品登场特写、开盖 AI 瞬间、收束广角）写两套提示词：
   - image_prompt（AI 分镜图）：每个含 主体/动作/情绪/光线/构图/一致性约束
     （ThinkPad 外观、主机位色调、场景三不变，服饰人物在舞台内必须一致）
   - video_prompt（AI 视频段）：按 /api/agent-guide 合同字段，写 动作流/运镜/时长/音效位，
     明确禁止 zoom 类词。
3. 产品一致性描述必须来自 brief 的产品事实（1.1kg、键盘转轴细节），不出现品牌 LOGO 特写
   或可识别的真实发布会素材（避免版权风险，用“黑色哑光商务本、红点呼吸灯”这类可生成描述）。
4. 存 test_out/tvc_prompts.json（修复后 data）后，POST /api/review/iterate
   （stage=image_prompt 与 stage=video_prompt 各走一遍，max_rounds=3），循环至过审或 stall。

完成标准：存在 test_out/tvc_prompts.json，TRACE 记录两个 stage 每轮 decision/manual_modes。