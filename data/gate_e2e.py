"""质量门 + 运行全部 端到端验收（真实 API，仅本地回环）。

覆盖三件事：
1) QC 未通过（报告 verdict≠ok / 未运行）时，消费同一视频的下游被拦截；
2) /run-all 拓扑序跑全图：review/qc 门照常生效，被拦节点带原因返回；
3) review 节点新增的 suggestion 输出端口存在并有内容（auto 审查时）。
"""
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

LOCAL_HOST = "127.0.0.1"
PORT = int(os.environ.get("SHIPIN_E2E_PORT", "8766"))
API_PREFIX = "/api"
GID = f"gate-{int(time.time())}"


class _LocalOnlyHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, "redirect not allowed", headers, fp)


def _local_url(path: str) -> str:
    if not path.startswith(API_PREFIX):
        raise ValueError(f"只允许 /api 端点: {path!r}")
    url = f"http://{LOCAL_HOST}:{PORT}{path}"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or parsed.hostname is None:
        raise ValueError(f"协议仅 http: {url}")
    for fam, _, _, _, sockaddr in socket.getaddrinfo(parsed.hostname, parsed.port or 80):
        if not sockaddr[0].startswith("127."):
            raise ValueError(f"解析到非环回地址: {sockaddr[0]}")
    return url


def call(method: str, path: str, body: dict | None = None, timeout: int = 120):
    url = _local_url(path)
    opener = urllib.request.build_opener(_LocalOnlyHandler)
    req = urllib.request.Request(url, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with opener.open(req, data=data, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"detail": raw[:300]}


def save_edges(gid: str, edges: list[dict]):
    g = call("GET", f"/api/graphs/{gid}")[1]["graph"]
    key = lambda e: (e.get("from"), e.get("from_port"),
                     e.get("to"), e.get("to_port"))
    merged = {key(e): dict(e) for e in g.get("edges", [])}
    for e in edges:
        merged[key(e)] = dict(e)
    call("PUT", f"/api/graphs/{gid}",
         {"name": g.get("name"), "nodes": g.get("nodes", []),
          "edges": list(merged.values())})


def main():
    total, passed = 0, 0

    def check(name: str, ok: bool, extra: str = ""):
        nonlocal total, passed
        total += 1
        if ok:
            passed += 1
            print(f"  ✅ {name} {extra}")
        else:
            print(f"  ❌ {name} {extra}")

    gid = call("POST", "/api/graphs",
               {"name": "质量门+run-all 验收"})[1]["id"]
    print(f"[graph] {gid}")

    # --- ① 质量门：video_gen 视频 → qc + assemble；qc 未运行 → assemble 被拦
    n_vg = call("POST", f"/api/graphs/{gid}/nodes",
                {"type": "video_gen", "x": 60, "y": 80,
                 "params": {"prompt": "测试镜头", "duration": 5}})[1]["node"]["id"]
    n_qc = call("POST", f"/api/graphs/{gid}/nodes",
                {"type": "qc", "x": 300, "y": 80,
                 "params": {"display_name": "质检", "expected_duration": 5}})[1]["node"]["id"]
    n_as = call("POST", f"/api/graphs/{gid}/nodes",
                {"type": "assemble", "x": 540, "y": 80})[1]["node"]["id"]
    save_edges(gid, [
        {"from": n_vg, "from_port": "video", "to": n_qc, "to_port": "video"},
        {"from": n_vg, "from_port": "video", "to": n_as, "to_port": "clips"},
    ])

    # 质检未运行 → 下游 assemble 应被质量门拦下（不落执行）
    st, r = call("POST", f"/api/graphs/{gid}/run", {"node_id": n_as}, timeout=10)
    detail = str(r.get("detail") or r)[:120]
    blocked = st in (422, 502) and ("质检" in detail or "质量门" in detail)
    check("QC 未运行 → 下游被质量门拦截", blocked, f"({st}) {detail}")

    # --- ② run-all：同一张图，全量执行为什么被拦、哪个节点失败要逐条可见 ----
    # 直接跑（video_gen 因缺首帧/网络必然失败，但 run-all 必须给逐节点结果）
    st, r = call("POST", f"/api/graphs/{gid}/run-all", {"force": False}, timeout=30)
    if st == 200:
        nodes = (r.get("results") or {}).get("nodes") or {}
        has = {k: bool(v.get("ok")) for k, v in nodes.items()}
        as_node = nodes.get(n_as, {})
        blocked = (not has.get(n_as)) and (
            "质检" in str(as_node.get("error") or "")
            or "质量门" in str(as_node.get("error") or "")
        )
        check("run-all 拓扑序执行，assemble 被质量门拦截", blocked,
              f"vg={has.get(n_vg)} qc={has.get(n_qc)} as={has.get(n_as)}")
    else:
        check("run-all 返回逐节点结果", False, f"({st}) {r}")

    # --- ③ review suggestion 端口（defs 中存在） ---
    st, defs = call("GET", "/api/graphs/kit/definitions")
    rev_out = [o.get("name") for o in (defs.get("nodes") or {})
               .get("review", {}).get("outputs", [])]
    check("review 定义带有 suggestion 输出端口",
          st == 200 and "suggestion" in rev_out, f"outputs={rev_out}")

    # --- ④ auto 审查后 suggestion 有真实修改建议（真实 LLM，有 key 时） ---
    n_src = call("POST", f"/api/graphs/{gid}/nodes",
                 {"type": "text", "x": 60, "y": 300,
                  "params": {"text": "我们是一支即饮咖啡，请你把上面的文案"
                                      "改得更口语，然后不要出现切割镜头"}})[1]["node"]["id"]
    n_rev2 = call("POST", f"/api/graphs/{gid}/nodes",
                  {"type": "review", "x": 260, "y": 300,
                   "params": {"display_name": "自动复核", "stage": "video_prompt",
                              "auto_review": "auto"}})[1]["node"]["id"]
    save_edges(gid, [
        {"from": n_src, "from_port": "text", "to": n_rev2, "to_port": "target"},
    ])
    st, r = call("POST", f"/api/graphs/{gid}/run", {"node_id": n_rev2}, timeout=240)
    if st == 200 and r.get("ok"):
        outs = r["state"].get("outputs") or {}
        sug = outs.get("suggestion") or {}
        rev_meta = outs.get("verdict", {}).get("meta") or {}
        verdict = rev_meta.get("verdict")
        if sug.get("kind") == "text" and str(sug.get("value") or "").strip():
            check("auto 审查产出 suggestion 建议",
                  True, f"verdict={verdict} 建议已生成")
        else:
            check("auto 审查产出 suggestion 建议",
                  False, f"verdict={verdict} value={str(sug.get('value'))[:80]}")
    else:
        check("auto 审查产出 suggestion 建议",
              False, f"run failed {st}: {str(r)[:200]}")

    print(f"\n结果 {passed}/{total}")
    return 0 if total == passed else 1


if __name__ == "__main__":
    raise SystemExit(main())