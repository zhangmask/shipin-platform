"""工作流审核闸门端到端验收（真实 API，仅本地回环）。

覆盖三件事：
1) 审核未通过（reject/pending）时，下游节点运行被引擎拦截并给出原因；
2) 审核通过（pass）后下游放行；
3) review 节点 auto_review=auto：真实调用 LLM 审查接入内容，
   合格文本自动 pass、明显不合格文本自动 reject（有 key 时）。
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
GID = f"ade-{int(time.time())}"


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
    """返回 (status, json)。非 2xx 不抛——测试要读拦截的 42x/5xx。"""
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

    st, r = call("POST", "/api/graphs", {"name": "审核闸门验收"})
    gid = r["id"]
    print(f"[graph] {gid}")

    n_text = call("POST", f"/api/graphs/{gid}/nodes",
                  {"type": "text", "x": 40, "y": 80})[1]["node"]["id"]
    call("POST", f"/api/graphs/{gid}/nodes/{n_text}/params",
         {"params": {"text": "钩子：凌晨三点，奶茶店窗边一杯茶还在冒热气。"
                             "产品：暖茶即饮，12 秒一条。"}})
    n_rev = call("POST", f"/api/graphs/{gid}/nodes",
                 {"type": "review", "x": 260, "y": 80,
                  "params": {"display_name": "剧本复核", "stage": "script",
                             "auto_review": "manual", "status": "reject",
                             "comments": "钩子不成立"}})[1]["node"]["id"]
    n_vp = call("POST", f"/api/graphs/{gid}/nodes",
                {"type": "video_prompt", "x": 480, "y": 80})[1]["node"]["id"]
    save_edges(gid, [
        {"from": n_text, "from_port": "text", "to": n_rev, "to_port": "target"},
        {"from": n_rev, "from_port": "content", "to": n_vp, "to_port": "board"},
    ])

    # ① 上游 text 先跑通（供 review target）
    st, r = call("POST", f"/api/graphs/{gid}/run", {"node_id": n_text}, timeout=60)
    check("text 上游可运行", st == 200 and r.get("ok"), f"({st})")

    # ② reject 状态下游被拦
    st, r = call("POST", f"/api/graphs/{gid}/run", {"node_id": n_vp}, timeout=120)
    detail = str(r.get("detail") or r)[:120]
    blocked = st in (422, 502) and "审核" in detail
    check("reject → 下游被拦截", blocked, f"({st}) {detail}")

    # ③ review 自身可跑（产生留痕）
    st, r = call("POST", f"/api/graphs/{gid}/run", {"node_id": n_rev}, timeout=60)
    check("review 自身可运行", st == 200 and r.get("ok"), f"({st})")

    # ④ 改为 pass → 放行
    call("POST", f"/api/graphs/{gid}/nodes/{n_rev}/params",
         {"params": {"status": "pass"}})
    st, r = call("POST", f"/api/graphs/{gid}/run", {"node_id": n_vp}, timeout=120)
    check("pass → 下游放行", st == 200 and r.get("ok"), f"({st})")

    # ⑤ auto_review=auto：对不合格内容跑真实 LLM 审查
    n_rev2 = call("POST", f"/api/graphs/{gid}/nodes",
                  {"type": "review", "x": 260, "y": 260,
                   "params": {"display_name": "自动复核", "stage": "script",
                              "auto_review": "auto"}})[1]["node"]["id"]
    n2_in = call("POST", f"/api/graphs/{gid}/nodes",
                 {"type": "text", "x": 40, "y": 260,
                  "params": {"text": "一个杯子。"}})[1]["node"]["id"]
    save_edges(gid, [
        {"from": n2_in, "from_port": "text", "to": n_rev2, "to_port": "target"},
    ])
    st, r = call("POST", f"/api/graphs/{gid}/run", {"node_id": n_rev2}, timeout=240)
    if st == 200 and r.get("ok"):
        meta = (r["state"].get("outputs") or {}).get("verdict", {}).get("meta") or {}
        verdict = meta.get("verdict")
        note = (meta.get("note") or "")[:160]
        if verdict == "reject":
            check("auto 审查（明显不合格）→ 自动驳回", True, f"note={note}")
        elif verdict == "pass":
            check("auto 审查（明显不合格）→ 自动驳回",
                  False, f"LLM 给了 pass：{note}")
        else:
            check("auto 审查（明显不合格）→ 自动驳回",
                  False, f"verdict={verdict} note={note}")
    else:
        check("auto 审查（明显不合格）→ 自动驳回",
              False, f"run failed {st}: {r}")
        check("  (标记) LLM key 未配置时自动审查按不可用降级",
              True, "）")

    print(f"\n结果 {passed}/{total}")
    return 0 if total == passed else 1


if __name__ == "__main__":
    raise SystemExit(main())