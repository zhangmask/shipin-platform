// P5：登录页 —— 录入 API key（P0 平台鉴权），只存 localStorage，不回显明文
import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, getApiKey, setApiKey } from "../lib";

export default function Login() {
  const nav = useNavigate();
  const [keyDraft, setKeyDraft] = useState(getApiKey());
  const [msg, setMsg] = useState(null);
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true);
    setMsg(null);
    setApiKey(keyDraft);
    try {
      const h = await api("/api/health");
      const auth = h.auth || {};
      setMsg({
        ok: true,
        text: `认证通过 · ${h.status} · v${h.version} · 鉴权模式 ${
          auth.mode || "?"}${auth.admin_key_configured ? " · admin" : ""}`,
      });
      setTimeout(() => nav("/"), 600);
    } catch (e) {
      setMsg({ ok: false, text: `health 验证未通过：${e.message}` });
      setApiKey(""); // 无效 key 不落盘
    } finally {
      setBusy(false);
    }
  };

  const skip = () => {
    setApiKey("");
    nav("/");
  };

  return (
    <div className="login-wrap">
      <form className="card login-card" onSubmit={(e) => { e.preventDefault(); save(); }}>
        <h1 style={{ marginTop: 0 }}>Shipin Platform · 登录</h1>
        <div className="fade">
          粘贴平台 API key（/api/platform/keys 签发的明文；库中只存 sha256 指纹，
          仅保存在浏览器 localStorage，可随时清除）。匿名（off）模式可跳过。
        </div>
        <label htmlFor="api-key-input">API Key</label>
        <input
          id="api-key-input"
          type="password"
          autoComplete="off"
          placeholder="shipin_…（留空=匿名模式）"
          value={keyDraft}
          onChange={(e) => setKeyDraft(e.target.value)}
        />
        {msg && (
          <div className={`banner ${msg.ok ? "ok" : "err"}`}
               style={{ marginTop: 10 }}>
            {msg.text}
          </div>
        )}
        <div className="row" style={{ marginTop: 14 }}>
          <button type="submit" className="primary" disabled={busy}
                  onClick={save}>
            {busy ? "验证中…" : "保存并进入"}
          </button>
          <button type="button" disabled={busy} onClick={skip}>
            跳过（匿名）
          </button>
        </div>
        <div className="fade" style={{ marginTop: 12 }}>
          需要平台级权限（全局预算 / 成本总览 / key 管理）时，服务端环境变量
          SHIPIN_ADMIN_KEY 由管理员配置，本页只负责把 key 带给服务端。
        </div>
      </form>
    </div>
  );
}