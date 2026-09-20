你是 shipin-platform 的全权操作员。平台服务已运行在 http://127.0.0.1:8767（用 curl 访问，不要用 python）。你的任务分三站，**第一站：弄清用法**：

1. 先读项目文档 D:/aishipin/shipin-platform/AGENT_GUIDE.md（或请求 GET http://127.0.0.1:8767/api/agent-guide），只依据文档，不准瞎猜接口参数。
2. **第二站（多提示词第 1 发）——立项**：
   用户意图（真实用户原话）："做一个爽文推短剧，主角打脸全场，节奏要燃"。
   用户已补充回答：
   - 时长：60-90 秒
   - 基调：燃、爽
   其余维度用户没答。
   要求按文档流程：
   a) POST /api/intake/questions（带上 intent），列出它要求问的必填维度；
   b) POST /api/intake/draft，把你拿到的回答填进去，未答的按文档说的 missing/default 机制处理；
   c) 解读 draft 返回的 review 和 next，说明 brief 有哪些 missing 字段、你打算怎么补。
3. **收尾**：把每一步的证据追加写入 /tmp/shipin-eval/trace.md（不存在就创建），格式：
   ```
   ## T1 步 `<时间>`
   - 命令: <你实际跑的 curl 命令>
   - 返回要点: <decision/missing/关键字段，摘抄不要全贴>
   - 我的决策: <一句话>
   ```
   最后一行输出总结：`T1 RESULT: OK - <一句话>` 或 `T1 RESULT: FAIL - <原因>`。

硬约束：
- 只与 127.0.0.1 通信，禁止访问任何外网 URL；
- 不要写 python 脚本，只允许 curl（可用 jq 解析）；
- 不要臆造接口，一切以文档为准；
- 不要贴大段 JSON，抓要点。