# 阶段 2 任务书 · 剧本（script）

基于阶段 1 过审的 brief（test_out/tvc_brief.json），以 TVC 导演+编剧身份写 60 秒品牌片剧本。

1. 读 test_out/tvc_brief.json，严格贴合里头的产品事实、基调、时长与创意方向。
2. 按广播级 TVC 结构写 script JSON（全中文，字段按 AGENT_GUIDE 里该阶段的合同形状：
   shots[] 或按 /api/agent-guide 返回的字段表为准）：
   - 0-3s   钩子：特写慢镜，指尖挑起笔记本边缘，机身悬空 3 厘米、屏幕不晃（全片唯一记忆点，
     只出现一次）
   - 4-20s  痛点对照：三台设备+背包的慌乱快剪 → 一台 ThinkPad 合盖静置，一声“啪”后世界安静
   - 21-44s 产品价值三连（各 ≤3 秒）：① 单指拎起合盖机身（1.1kg 重量）② 开盖即 AI 摘要
     “3 场会议 → 1 页决策”（不出现“云端”字样）③ 会议降噪压住嘈杂（镜头给对方惊讶微表情）。
     旁白仅 2 句：“别人背的是会议，你背的是决定。”
   - 45-58s 升格收束：他合上电脑走进会场，手里只有一台机器；空镜收在键盘与指尖
   - 59-60s 品牌卡：ThinkPad logo + 红点呼吸灯；slogan 一律不编造（官方未发布则不写），
     使用原创副字幕“轻，是把整个公司装进口袋的底气。”并标注“拟”
3. 每段注明：画面动作 + 旁白/台词 + 时长秒数。
4. 存 test_out/tvc_script.json 后立即 POST /api/review/iterate（stage=script, max_rounds=3），
   同样循环至过审或 stall（口径同阶段 1）。

完成标准：存在 test_out/tvc_script.json（修复后 data），TRACE 记录每轮 decision/manual_modes。