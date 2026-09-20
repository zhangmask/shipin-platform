# 阶段 3 任务书 · 分镜（storyboard）

基于阶段 2 过审的脚本（test_out/tvc_script.json），拆成可拍摄的分镜 JSON。

1. 读 test_out/tvc_script.json，把每一段拆成具体镜头。总镜头数 8-12 个，覆盖 60 秒节奏。
2. 每镜头必须包含的结构化字段（按 /api/agent-guide 的 storyboard 合同字段表）：
   - 镜头号、时长秒、景别（特写/近景/中景/全景）、运镜（禁止用 zoom/推拉这类平台不接受的词，
     用 dolly/track/static 等合法词）、机位高低、画面内容（1-2 句可拍摄描述）、
     人物/产品在哪、光线与色调、声音设计（环境音/音乐/留白）
3. 叙事节奏设计：钩子镜头必须 3 秒内出现“非日常”元素（指尖挑边特写、金属反光、悬空
   临界态）；中段“产品三连”按定稿：单指拎起 → 开盖 AI 摘要 → 会议降噪，15 秒内产品露出三次。
4. 存 test_out/tvc_storyboard.json 后立即 POST /api/review/iterate（stage=storyboard,
   max_rounds=3），循环至过审或 stall（口径同前）。

完成标准：存在 test_out/tvc_storyboard.json（修复后 data），TRACE 记录每轮
decision/manual_modes。