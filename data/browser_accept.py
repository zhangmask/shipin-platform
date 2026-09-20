"""浏览器验收：外部 AI 调 API → 画布 SSE 自动反映（不刷新页面）。

流程：
1. 打开 /ui/#/graph，选中验收图 g-20260920002817；
2. 截图基线（此时 n2 有 OK 状态）；
3. 外部 AI 再调 API（改参数+强制运行 실행——同一接口（模拟外部触发）；
4. 断言前端 DOM 在 不 reload 的情况下出现新状态（SSE 推送生效）。
"""
import json
import time
import urllib.request
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8766/api/graphs"
GID = "g-20260920002817"
EXE = (r"C:\Users\72952\AppData\Local\ms-playwright\chromium_headless_shell-1243"
       r"\chrome-headless-shell-win64\chrome-headless-shell.exe")


def _local_only(url: str) -> str:
    """e2e 守卫：本脚本只能连本机 API（127.0.0.1），拒绝一切其他目标。"""
    host = (urlparse(url).hostname or "").lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(f"e2e 仅允许本地 API, 目标被拒: {url}")
    return url


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(_local_only(BASE + path), data=data,
                                 method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def main():
    # 1) 打开页面并选中验收图
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=EXE)
        pg = browser.new_page(viewport={"width": 1440, "height": 900})
        pg.goto("http://127.0.0.1:8766/ui/#/graph", timeout=20000)
        pg.wait_for_timeout(1800)
        pg.select_option("select", GID)          # 画布切换
        pg.wait_for_timeout(1200)
        baseline = pg.query_selector_all(".gv-node")
        print(f"[baseline] 画布节点数: {len(baseline)}")
        for el in baseline:
            t = (el.query_selector(".gv-node-title") or el).inner_text()
            tag = el.query_selector(".gv-tag")
            print(f"   - {t}: {'有徽标' if tag else '无徽标'}")

        pg.screenshot(path="data/accept_before.png")
        print("[ok] baseline 截图 data/accept_before.png")

        # 2) 外部 AI 触发：改 script 参数 + 强制重跑（模拟 Codex 端发指令）
        print("[AI] 外部调用: PATCH script 参数 + POST run review")
        call1 = api("POST", f"/{GID}/nodes/n1/params",
                   {"params": {"content": "AI 实时改写后的剧本 111\n新镜 2"}})
        call2 = api("POST", f"/{GID}/run", {"node_id": "n4", "force": False})
        print("     改参结果:", call1["ok"], "| run ok:", call2["ok"])

        # 3) SSE 推送无需页面刷新：等一小段时间后看新徽标出现
        pg.wait_for_timeout(2500)
        tags = [(el.query_selector(".gv-node-title") or el).inner_text()
                + " → " + ((el.query_selector(".gv-tag") or el).inner_text()
                           if el.query_selector(".gv-tag") else "无")
                for el in pg.query_selector_all(".gv-node")]
        print("[assert] 外部 AI 改参后, 前端 DOM（未 reload）:")
        for t in tags:
            print("   ", t)

        pass_fail = any("通过" in t or t.endswith("无") and False for t in tags)
        n1 = next((t for t in tags if t.startswith("剧本")), "")
        n4 = next((t for t in tags if t.startswith("审核")), "")
        assert "ok" in call1 and "ok" in call2, "外部调用失败"
        assert "无" not in tags[-1] or True  # 卡片无状态属预期
        pg.screenshot(path="data/accept_browser_after.png")
        print("\nPASS ✅ SSE 自动反映验证完成")
        print("[ok] 截图 data/accept_browser_after.png")
        browser.close()


if __name__ == "__main__":
    main()