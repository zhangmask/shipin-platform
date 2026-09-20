"""真实视频端到端验收：text → image_gen → video_gen → qc → card。

用画布 API（仅本机回环验收）走完整链路，视频生成真调 AGNES（约 5s）。
请求层强制白名单：协议 http、主机只允许 127.0.0.1/localhost 的解析 IP、
路径必须 /api/ 前缀、禁重定向 —— 杜绝任何 SSRF 面（本脚本是本地开发
验收工具，安全策略与运行时 guard 一致）。
"""
import json
import os
import socket
import time
import urllib.parse
import urllib.request

LOCAL_HOST = "127.0.0.1"
PORT = int(os.environ.get("SHIPIN_E2E_PORT", "8766"))
API_PREFIX = "/api"
GID = f"ve2e-{int(time.time())}"


class _LocalOnlyHandler(urllib.request.HTTPRedirectHandler):
    """禁重定向：本平台验收只接受直接响应，跟随跳转即拒绝。"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, "redirect not allowed", headers, fp)


def _local_url(path: str) -> str:
    """构造并校验 URL：只接受本地回环 + /api 前缀的固定端点。"""
    if not isinstance(path, str) or not path.startswith(API_PREFIX):
        raise ValueError(f"只允许 /api 端点: {path!r}")
    url = f"http://{LOCAL_HOST}:{PORT}{path}"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or parsed.hostname is None:
        raise ValueError(f"协议仅 http: {url}")
    # 解析后 IP 校验（防 DNS rebinding / 例外主机）：必须全部落在 127.0.0.0/8
    try:
        addrs = socket.getaddrinfo(parsed.hostname, parsed.port or 80)
    except OSError as e:
        raise ValueError(f"解析失败: {e}")
    if not addrs:
        raise ValueError("无可解析地址")
    for fam, _, _, _, sockaddr in addrs:
        ip = sockaddr[0]
        if not ip.startswith("127."):
            raise ValueError(f"解析到非环回地址: {ip}")
    return url


def call(method: str, path: str, body: dict | None = None, timeout: int = 120):
    url = _local_url(path)
    opener = urllib.request.build_opener(_LocalOnlyHandler)
    req = urllib.request.Request(url, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    with opener.open(req, data=data, timeout=timeout) as r:
        return json.loads(r.read().decode())


def save_edges(gid: str, edges: list[dict]):
    """PUT 是全量保存：读图合并（现有边 + 新边按端键去重）后整体提交。"""
    g = call("GET", f"/api/graphs/{gid}")["graph"]
    cur = g.get("edges", [])
    key = lambda e: (e.get("from"), e.get("from_port"),
                     e.get("to"), e.get("to_port"))
    merged = {key(e): dict(e) for e in cur}
    for e in edges:
        merged[key(e)] = dict(e)
    call("PUT", f"/api/graphs/{gid}",
         {"name": g.get("name"), "nodes": g.get("nodes", []),
          "edges": list(merged.values())})


def main():
    gid = call("POST", "/api/graphs", {"name": "视频端到端验收"})["id"]
    print(f"[graph] created {gid}")
    n_script = call("POST", f"/api/graphs/{gid}/nodes", {"type": "script",
                     "x": 60, "y": 80})["node"]
    call("POST", f"/api/graphs/{gid}/nodes/{n_script['id']}/params", {
        "params": {"content": "镜头1：暖色茶饮特写，雾气升腾，品牌杯身在光中旋转"}})
    n_fp = call("POST", f"/api/graphs/{gid}/nodes", {"type": "frame_prompts",
                "x": 320, "y": 80})["node"]
    n_img = call("POST", f"/api/graphs/{gid}/nodes", {"type": "image_gen",
                "x": 580, "y": 80})["node"]
    n_img_id = n_img["id"]
    save_edges(gid, [
        {"from": n_script["id"], "from_port": "progress",
         "to": n_fp["id"], "to_port": "board", "order": 0},
        {"from": n_fp["id"], "from_port": "first_prompt",
         "to": n_img_id, "to_port": "prompt", "order": 0},
    ])
    print(f"[nodes] script={n_script['id']} frame_prompts={n_fp['id']} image_gen={n_img_id}")
    for nid, tag in ((n_script["id"], "script"), (n_fp["id"], "frame_prompts")):
        r = call("POST", f"/api/graphs/{gid}/run", {"node_id": nid}, timeout=300)
        assert r["ok"], r
        print(f"[1-2] {tag} run OK")
    r = call("POST", f"/api/graphs/{gid}/run", {"node_id": n_img_id}, timeout=300)
    assert r["ok"], r
    print(f"[3] image_gen OK → {r['state']['outputs']['image']['value']}")
    n_vid = call("POST", f"/api/graphs/{gid}/nodes", {"type": "video_gen",
                "x": 840, "y": 80})["node"]
    n_vid_id = n_vid["id"]
    save_edges(gid, [
        {"from": n_fp["id"], "from_port": "first_prompt",
         "to": n_vid_id, "to_port": "prompt", "order": 0},
        {"from": n_img_id, "from_port": "image",
         "to": n_vid_id, "to_port": "first_frame", "order": 1},
    ])
    print(f"[4] video_gen {n_vid_id} 真实 AGNES 出片 …")
    t0 = time.time()
    r = call("POST", f"/api/graphs/{gid}/run", {"node_id": n_vid_id}, timeout=360)
    elapsed = time.time() - t0
    assert r["ok"], r
    print(f"[4] video_gen OK ({elapsed:.0f}s) → {r['state']['outputs']['video']['value']} "
          f"meta={r['state']['outputs']['video'].get('meta')}")
    n_qc = call("POST", f"/api/graphs/{gid}/nodes", {"type": "qc",
                "x": 1100, "y": 80})["node"]
    qc_id = n_qc["id"]
    save_edges(gid, [
        {"from": n_vid_id, "from_port": "video", "to": qc_id,
         "to_port": "video", "order": 0},
    ])
    r = call("POST", f"/api/graphs/{gid}/run", {"node_id": qc_id}, timeout=300)
    print(f"[5] QC ok={r['ok']} state={r.get('state', {})}")
    n_card = call("POST", f"/api/graphs/{gid}/nodes", {"type": "card",
                 "x": 1360, "y": 80,
                 "params": {"title": "暖茶时光", "subtitle": "一镜到底",
                            "tags": "茶,静物"}})["node"]
    card_id = n_card["id"]
    save_edges(gid, [
        {"from": n_vid_id, "from_port": "video", "to": card_id,
         "to_port": "final_video", "order": 0},
    ])
    r = call("POST", f"/api/graphs/{gid}/run", {"node_id": card_id}, timeout=300)
    assert r["ok"], r
    meta = r["state"]["outputs"]["card"]["meta"]
    print(f"[6] card OK → {meta['title']} | {meta['subtitle']} | "
          f"{meta['duration_sec']}s | tags={meta['tags']}")
    print("\nPASS ✅ 全链路真实跑通")


if __name__ == "__main__":
    main()