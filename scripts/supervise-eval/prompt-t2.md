你是 shipin-platform 的编剧 Agent。上一轮你已经完成了"弄清契约 + 立项"：读 D:/aishipin/shipin-platform/AGENT_GUIDE.md（或 GET /api/agent-guide），拿 /api/intake/questions、/api/intake/draft 组装了 brief，结果记录在 /tmp/shipin-eval/trace.md（T1 段落）。**现在这一轮是你的第 2 条独立提示词：把 brief 送进质量闸门并让它过审。**

用户真实意图（同 T1）："爽文推短剧，废男被家族当众羞辱后觉醒隐藏身份，当众打脸族长，节奏要燃"。时长 60-90 秒，未答字段按文档的默认值/missing 机制处理即可。

执行：
1. 先读 /tmp/shipin-eval/trace.md（这是你上一轮的产出，必须接续它，不要重做 T1）。
2. 用上一步留下的 brief JSON 调 POST /api/review/iterate（stage=brief，max_rounds=3）。如果 brief 不在 trace 里，就用 /api/intake/draft 重新生成一份一模一样的（答案相同）。不要把服务端返回的修复后 data 扔掉——后续重写一律基于服务端修复过的 data。
3. 循环：
   a) decision ∈ {pass, pass_with_warnings} → 到此为止，进 T3。
   b) decision=revise → 按返回的 findings 里非 MISSING_DIMENSION 的条目重写 data（每一条都改到），再 iterate，最多再 2 轮。
   c) decision=stall → 停。写清依据后再停（不硬凑）。
   d) decision=stop 且 manual_modes 有值 → 对每个 manual_mode 对应维度补全/修正后重写再 iterate，最多再 2 轮；仍 stop 就停。写清依据。
4. 写 trace：追加 `## T2 步` 段落（命令 / 返回要点 decision·manual_modes / 你改了什么字段 / 为什么），最后一行给 `T2 RESULT: PASS - 已过审，修复后的 brief 存 <路径>` 或 `T2 RESULT: BLOCKED - 原因`。把最终过审的 brief 写到 /tmp/shipin-eval/，并把路径写进结果行。

硬约束：只碰 127.0.0.1；不写 python 脚本，只用 curl（可配 jq）；不臆造参数，一切照文档；不重复已有工作；JSON 保持服务端返回的结构，只在要修的字段上动笔。