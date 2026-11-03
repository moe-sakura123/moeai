/**
 * MissAI 网站统计追踪脚本 v4.7.0 (自托管版)
 * 基于 invite_system/public/tracker.js v3.1.0
 * 核心功能：PV/UV 统计 + 注册转化追踪
 *
 * v4.0.0 变更：
 * - 改为同源部署，数据发送到同源代理 /api/v/c（避免第三方域名被 CORS/广告拦截器阻止）
 * - 移除 data-api 属性依赖
 *
 * 公共 API：
 * - window.MissAnalytics.trackConversion(type, data) - 追踪转化事件（注册/付费等）
 * - window.MissAnalytics.getVisitorId() - 获取访客 ID
 * - window.MissAnalytics.getSessionId() - 获取会话 ID
 * - window.MissAnalytics.getMarketingData() - 获取营销来源数据
 * - window.MissAnalytics.destroy() - 销毁追踪器（SPA 环境中使用）
 */
(function() {
  "use strict";

  // ==================== 开发环境检测 ====================
  var hostname = window.location.hostname;
  var IS_DEV = hostname === "localhost" || hostname === "127.0.0.1" || hostname.startsWith("192.168.");
  // layout 在 dev 下可注入 window.__MISSAI_ANALYTICS_REMOTE_DEV__，配合 NEXT_PUBLIC_ANALYTICS_REMOTE_IN_DEV 走真实 /api/v/c → 远程 collect
  var REMOTE_DEV = typeof window !== "undefined" && !!window.__MISSAI_ANALYTICS_REMOTE_DEV__;
  if (IS_DEV && !REMOTE_DEV) {
    // 开发环境下暴露空壳 API，不做任何网络请求
    window.MissAnalytics = {
      trackPageview: function() {},
      trackConversion: function() {},
      trackEvent: function() {},
      getVisitorId: function() { return "dev"; },
      getSessionId: function() { return "dev"; },
      getMarketingData: function() { return null; },
      destroy: function() {},
      version: "4.7.0-dev-stub",
    };
    return;  // 直接退出 IIFE
  }

  // ==================== 常量配置 ====================
  const CONSTANTS = {
    SESSION_TIMEOUT_MS: 30 * 60 * 1000,     // 会话超时：30分钟
    MAX_DURATION_SECONDS: 3600,             // 最大停留时间：1小时
    SPA_DEBOUNCE_MS: 300,                   // SPA 路由切换防抖：300ms
    TWA_DIAGNOSTIC_SAMPLE_RATE: 0.2,

    // 存储键名
    STORAGE_KEY_VISITOR: "ma_visitor_id",
    STORAGE_KEY_SESSION: "ma_session_id",
    STORAGE_KEY_SESSION_STATE: "ma_session_state_v1",
    STORAGE_KEY_TWA_PROCESS_SEEN: "missai_twa_process_seen_v1",
    STORAGE_KEY_MARKETING: "ma_marketing",
  };

  // ==================== 运行时配置 ====================
  // v4.0.0: 使用同源相对路径，避免跨域 CORS 和广告拦截问题
  const collectUrl = "/api/v/c";

  // 状态管理
  let isDestroyed = false;
  let homeApiTimingInstalled = false;
  let homeApiTimingCount = 0;
  const HOME_API_TIMING_MAX_EVENTS = 4;
  let guestChatApiTimingCount = 0;
  const GUEST_CHAT_API_TIMING_MAX_EVENTS = 4;
  let documentStartupType = "";
  let twaDiagnosticSampled;

  // ==================== 签名相关 ====================
  var SIGN_KEY = "ma_s1gn_2024";

  function simpleHash(str) {
    var h = 0;
    for (var i = 0; i < str.length; i++) {
      h = ((h << 5) - h + str.charCodeAt(i)) | 0;
    }
    return (h >>> 0).toString(36);
  }

  // ==================== 工具函数 ====================

  // 生成唯一ID
  function generateId() {
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function(c) {
      const r = Math.random() * 16 | 0;
      const v = c === "x" ? r : (r & 0x3 | 0x8);
      return v.toString(16);
    });
  }

  // 获取或创建访客ID（持久化存储，支持无痕浏览模式）
  // 存储优先级：浏览器指纹（device_fingerprint） > localStorage > sessionStorage > 内存
  // v4.5.0: 优先使用 browserFingerprint.ts 生成的复合指纹，比随机 UUID 更稳定
  function getVisitorId() {
    const key = CONSTANTS.STORAGE_KEY_VISITOR;

    // 0. 先检查内存缓存（最快，避免重复读取存储）
    if (window._ma_visitor_id) {
      return window._ma_visitor_id;
    }

    // 1. 优先从 localStorage 读取已有的 visitor ID
    try {
      var id = localStorage.getItem(key);
      if (id) {
        // 检查是否需要升级为指纹 ID（UUID 格式且指纹已可用时自动升级）
        try {
          var fp = localStorage.getItem("device_fingerprint");
          if (fp && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) {
            // 当前 ID 是 UUID 格式（xxxxxxxx-xxxx-...），升级为浏览器指纹
            id = fp;
            localStorage.setItem(key, id);
            try { sessionStorage.setItem(key, id); } catch (e3) {}
          }
        } catch (e2) { /* ignore */ }
        try { sessionStorage.setItem(key, id); } catch (e4) {}
        window._ma_visitor_id = id;
        return id;
      }
    } catch (e) {
      // localStorage 不可用（无痕模式等）
    }

    // 2. 从 sessionStorage 读取（同一会话内持久，无痕模式下可用）
    try {
      var id = sessionStorage.getItem(key);
      if (id) {
        window._ma_visitor_id = id;
        return id;
      }
    } catch (e) {
      // sessionStorage 也不可用
    }

    // 3. 生成新 ID：优先使用浏览器指纹，否则回退到随机 UUID
    var newId;
    try {
      var fp = localStorage.getItem("device_fingerprint");
      newId = fp || generateId();
    } catch (e) {
      newId = generateId();
    }

    // 尝试存储到 localStorage
    try {
      localStorage.setItem(key, newId);
    } catch (e) {
      // 无痕模式下 localStorage 可能抛异常，尝试 sessionStorage
      try {
        sessionStorage.setItem(key, newId);
      } catch (e2) {
        // 都不可用，仅使用内存存储
      }
    }

    // 4. 缓存到内存（确保同一页面内多次调用返回相同 ID）
    window._ma_visitor_id = newId;

    return newId;
  }

  // Short-lived session mirror used for analytics continuity across WebView hard navigations.
  function readSessionState() {
    try {
      var raw = localStorage.getItem(CONSTANTS.STORAGE_KEY_SESSION_STATE);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      if (!parsed || !parsed.id || !parsed.lastSeen) return null;
      if (Date.now() - Number(parsed.lastSeen) > CONSTANTS.SESSION_TIMEOUT_MS) return null;
      return parsed;
    } catch (e) {
      return null;
    }
  }

  function writeSessionState(id, landingPage) {
    if (!id) return;
    try {
      var existing = readSessionState();
      var state = {
        id: id,
        landingPage: landingPage || (existing && existing.id === id ? existing.landingPage : "") || "",
        lastSeen: Date.now(),
      };
      localStorage.setItem(CONSTANTS.STORAGE_KEY_SESSION_STATE, JSON.stringify(state));
    } catch (e) {}
  }

  function clearSessionState() {
    try { localStorage.removeItem(CONSTANTS.STORAGE_KEY_SESSION_STATE); } catch (e) {}
  }

  // Get or create a session ID. sessionStorage is canonical; localStorage is a short-lived mirror.
  function getSessionId() {
    const key = CONSTANTS.STORAGE_KEY_SESSION;
    try {
      let id = sessionStorage.getItem(key);
      if (!id) {
        var mirrored = readSessionState();
        id = mirrored && mirrored.id ? mirrored.id : generateId();
        sessionStorage.setItem(key, id);
      }
      writeSessionState(id, getLandingPage());
      return id;
    } catch (e) {
      var fallbackState = readSessionState();
      var fallbackId = fallbackState && fallbackState.id
        ? fallbackState.id
        : (window._ma_temp_session_id || (window._ma_temp_session_id = generateId()));
      writeSessionState(fallbackId, fallbackState && fallbackState.landingPage);
      return fallbackId;
    }
  }

  // 检测设备类型
  function getDeviceType() {
    const ua = navigator.userAgent.toLowerCase();
    if (/mobile/.test(ua)) return "mobile";
    if (/tablet|ipad/.test(ua)) return "tablet";
    return "desktop";
  }

  // 检测浏览器
  function getBrowser() {
    const ua = navigator.userAgent;
    if (/edg/i.test(ua)) return "Edge";
    if (/chrome/i.test(ua)) return "Chrome";
    if (/safari/i.test(ua) && !/chrome/i.test(ua)) return "Safari";
    if (/firefox/i.test(ua)) return "Firefox";
    if (/opera|opr/i.test(ua)) return "Opera";
    return "Other";
  }

  // 检测操作系统
  function getOS() {
    const ua = navigator.userAgent;
    if (/windows/i.test(ua)) return "Windows";
    if (/macintosh|mac os/i.test(ua)) return "macOS";
    if (/linux/i.test(ua) && !/android/i.test(ua)) return "Linux";
    if (/android/i.test(ua)) return "Android";
    if (/iphone|ipad|ipod/i.test(ua)) return "iOS";
    return "Other";
  }

  // ==================== 营销来源数据采集 ====================
  const MARKETING_DATA_KEY = "ma_marketing_data";

  // 安全敏感页面：URL 路径含一次性能力凭证（如换绑撤销链接 /email-revert/{userId}/{token}）。
  // 这些页面整页豁免一切埋点与营销归因采集，防止凭证进入统计库/localStorage。
  var SENSITIVE_PATH_PREFIXES = ["/email-revert"];
  function isSensitivePath() {
    try {
      var p = window.location.pathname || "";
      for (var i = 0; i < SENSITIVE_PATH_PREFIXES.length; i++) {
        if (p.indexOf(SENSITIVE_PATH_PREFIXES[i]) === 0) return true;
      }
    } catch (e) { /* ignore */ }
    return false;
  }

  // 采集并存储 URL 中的营销参数（Last Non-Direct Click 归因）
  function captureMarketingData() {
    try {
      const params = new URLSearchParams(window.location.search);

      // 检查是否有广告来源参数
      const hasUtmSource = params.has("utm_source");
      const hasClickId = params.has("click_id") || params.has("clickid") || params.has("conversions_tracking");

      // 检查 URL 是否包含邀请码
      const hasInviteCode = params.has("inviteCode");

      if (hasUtmSource || hasClickId) {
        // 发现新的广告来源，覆盖 localStorage 中的旧数据（Last Non-Direct Click）
        const marketingData = {
          // UTM 参数
          utm_source: params.get("utm_source") || "",
          utm_medium: params.get("utm_medium") || "",
          utm_campaign: params.get("utm_campaign") || "",
          utm_content: params.get("utm_content") || "",
          utm_term: params.get("utm_term") || "",
          // 广告平台参数（兼容多种命名）
          click_id: params.get("click_id") || params.get("clickid") || params.get("conversions_tracking") || "",
          camp_id: params.get("camp_id") || params.get("campaign_id") || "",
          ad_id: params.get("ad_id") || "",
          site_id: params.get("site_id") || "",
          zone_id: params.get("zone_id") || "",
          // ExoClick 特有参数
          variation_id: params.get("variation_id") || params.get("varid") || "",
          category_id: params.get("category_id") || params.get("catid") || "",
          exo_goal: params.get("exo_goal") || params.get("goal_id") || "",
          src_hostname: params.get("src_hostname") || params.get("source") || "",
          pricing_model: params.get("pricing_model") || "",
          format: params.get("format") || "",
          // 点击成本
          click_cost: parseFloat(params.get("cost") || params.get("actual_cost") || params.get("bid") || "0") || 0,
          // 落地页（含查询参数，保留 inviteCode 等）
          landing_page: window.location.pathname + window.location.search,
          // 记录时间
          recorded_at: Date.now(),
        };
        localStorage.setItem(MARKETING_DATA_KEY, JSON.stringify(marketingData));
      } else if (hasInviteCode) {
        // 没有广告参数但有邀请码时，更新 landing_page 为当前邀请码页面
        try {
          var currentPage = window.location.pathname + window.location.search;
          var existing = localStorage.getItem(MARKETING_DATA_KEY);
          if (existing) {
            var data = JSON.parse(existing);
            data.landing_page = currentPage;
            data.recorded_at = Date.now();
            localStorage.setItem(MARKETING_DATA_KEY, JSON.stringify(data));
          } else {
            localStorage.setItem(MARKETING_DATA_KEY, JSON.stringify({
              utm_source: "", utm_medium: "", utm_campaign: "", utm_content: "", utm_term: "",
              click_id: "", camp_id: "", ad_id: "", site_id: "", zone_id: "",
              click_cost: 0,
              landing_page: currentPage,
              recorded_at: Date.now(),
            }));
          }
        } catch (e2) {}
      }
    } catch (e) {
      // localStorage 不可用时忽略
    }
  }

  // 获取存储的营销数据
  function getMarketingData() {
    try {
      const data = localStorage.getItem(MARKETING_DATA_KEY);
      return data ? JSON.parse(data) : null;
    } catch (e) {
      return null;
    }
  }

  // ==================== 发送队列与重试机制 ====================

  var pendingQueue = [];  // 待发送队列
  var isSending = false;  // 发送锁
  var MAX_QUEUE_SIZE = 50;  // 队列最大长度
  var PENDING_QUEUE_KEY = "ma_pending_queue";
  var MAX_QUEUE_AGE_MS = 2 * 60 * 60 * 1000;  // 队列数据 2 小时过期

  // 从 localStorage 恢复未发送的数据（页面刷新/关闭后继续发送）
  function restorePendingQueue() {
    try {
      var saved = localStorage.getItem(PENDING_QUEUE_KEY);
      if (saved) {
        var parsed = JSON.parse(saved);
        if (parsed.savedAt && (Date.now() - parsed.savedAt > MAX_QUEUE_AGE_MS)) {
          localStorage.removeItem(PENDING_QUEUE_KEY);
          return;
        }
        var items = parsed.items || parsed;
        if (Array.isArray(items) && items.length > 0) {
          pendingQueue = items.slice(0, MAX_QUEUE_SIZE);
          processPendingQueue();
          return;
        }
      }
      saved = sessionStorage.getItem(PENDING_QUEUE_KEY);
      if (saved) {
        var parsed = JSON.parse(saved);
        if (Array.isArray(parsed) && parsed.length > 0) {
          pendingQueue = parsed.slice(0, MAX_QUEUE_SIZE);
          processPendingQueue();
        }
      }
    } catch (e) {
      try { localStorage.removeItem(PENDING_QUEUE_KEY); } catch (e2) {}
      try { sessionStorage.removeItem(PENDING_QUEUE_KEY); } catch (e2) {}
    }
  }

  // 保存待发送队列到 localStorage（页面关闭时也能保留）
  function savePendingQueue() {
    if (pendingQueue.length === 0) {
      try { localStorage.removeItem(PENDING_QUEUE_KEY); sessionStorage.removeItem(PENDING_QUEUE_KEY); } catch (e) {}
      return;
    }
    try {
      var data = {
        items: pendingQueue.slice(0, MAX_QUEUE_SIZE),
        savedAt: Date.now(),
      };
      localStorage.setItem(PENDING_QUEUE_KEY, JSON.stringify(data));
    } catch (e) {
      try {
        sessionStorage.setItem(PENDING_QUEUE_KEY, JSON.stringify(pendingQueue.slice(0, MAX_QUEUE_SIZE)));
      } catch (e2) {}
    }
  }

  // A 2xx transport response is insufficient: legacy overload also returned 202.
  function requireSavedResponse(response) {
    if (!response.ok) throw new Error("HTTP " + response.status);
    return response.json().then(function(body) {
      if (!body || body.success !== true || body.dropped === true || body.accepted === false) {
        throw new Error("Analytics not persisted");
      }
    });
  }

  function processPendingQueue() {
    if (isSending || pendingQueue.length === 0) return;
    var item = pendingQueue[0];
    if (!item.queuedAt) item.queuedAt = item.data._at || item.data._t * 1000 || Date.now();
    if (!item.data._at) item.data._at = item.queuedAt;
    if (!item.data.eventId) item.data.eventId = generateId();
    if (Date.now() - (item.queuedAt || item.data._at || Date.now()) > MAX_QUEUE_AGE_MS) {
      pendingQueue.shift();
      savePendingQueue();
      setTimeout(processPendingQueue, 0);
      return;
    }
    isSending = true;
    // Refresh the transport signature; preserve event identity and original event time.
    item.data._t = Math.floor(Date.now() / 1000);
    item.data._s = simpleHash(item.data.visitorId + item.data.sessionId + item.data._t + SIGN_KEY);
    savePendingQueue();
    fetch(collectUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(item.data),
      keepalive: true,
    }).then(requireSavedResponse).then(function() {
      pendingQueue.shift();
      isSending = false;
      savePendingQueue();
      if (pendingQueue.length > 0) setTimeout(processPendingQueue, 100);
    }).catch(function() {
      item.retries = (item.retries || 0) + 1;
      isSending = false;
      savePendingQueue();
      setTimeout(processPendingQueue, Math.min(60000, 1000 * Math.pow(2, Math.min(item.retries, 6))));
    });
  }

  function send(data) {
    if (data.type === "conversion" || data.type === "pageview") {
      if (!data.eventId) data.eventId = generateId();
      if (!data._at) data._at = Date.now();
      if (pendingQueue.length < MAX_QUEUE_SIZE) {
        pendingQueue.push({ data: data, retries: 0, queuedAt: Date.now() });
        savePendingQueue();
      }
      processPendingQueue();
      return;
    }
    fetch(collectUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
      keepalive: true,
    }).catch(function() {});
  }

  // 获取基础 payload（含请求签名）
  function getSearchParam(name) {
    try {
      return new URLSearchParams(window.location.search).get(name) || "";
    } catch (e) {
      return "";
    }
  }

  function getSessionValue(key) {
    try {
      return sessionStorage.getItem(key) || "";
    } catch (e) {
      return "";
    }
  }

  function isTwaRuntimeForAnalytics() {
    var source = getSearchParam("source");
    var twa = getSearchParam("twa");
    if (source === "twa" || twa === "1") {
      try { sessionStorage.setItem("missai_twa_runtime", "1"); } catch (e) {}
      return true;
    }
    return getSessionValue("missai_twa_runtime") === "1";
  }

  function getTwaMetaForAnalytics() {
    if (!isTwaRuntimeForAnalytics()) {
      return {
        clientChannel: "web",
        appRuntime: "",
        appVersion: "",
        appBuild: "",
        container: "",
      };
    }

    var appVersion = getSearchParam("appVersion") || getSessionValue("missai_twa_app_version");
    var appBuild = getSearchParam("appBuild") || getSessionValue("missai_twa_app_build");
    var container = getSearchParam("container") || getSessionValue("missai_twa_container") || "twa";
    try {
      if (appVersion) sessionStorage.setItem("missai_twa_app_version", appVersion);
      if (appBuild) sessionStorage.setItem("missai_twa_app_build", appBuild);
      if (container) sessionStorage.setItem("missai_twa_container", container);
    } catch (e) {}

    return {
      clientChannel: "twa_android",
      appRuntime: "twa",
      appVersion: appVersion,
      appBuild: appBuild,
      container: container,
    };
  }

  function getNetworkHints() {
    var connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    if (!connection) return {};

    var hints = {
      effectiveType: connection.effectiveType || "",
      downlink: typeof connection.downlink === "number" ? connection.downlink : -1,
      rtt: typeof connection.rtt === "number" ? connection.rtt : -1,
      saveData: Boolean(connection.saveData),
    };
    hints.networkBucket = classifyNetworkBucket(hints);
    return hints;
  }

  function classifyNetworkBucket(hints) {
    var effectiveType = String(hints.effectiveType || "").toLowerCase();
    var rtt = typeof hints.rtt === "number" && hints.rtt >= 0 ? hints.rtt : -1;
    var downlink = typeof hints.downlink === "number" && hints.downlink >= 0 ? hints.downlink : -1;
    var hasSignal = !!effectiveType || rtt >= 0 || downlink >= 0 || hints.saveData === true;
    if (!hasSignal) return "unknown";
    if (hints.saveData || effectiveType === "slow-2g" || effectiveType === "2g" || rtt >= 600 || (downlink >= 0 && downlink < 0.8)) {
      return "constrained";
    }
    if (effectiveType === "3g" || rtt >= 300 || (downlink >= 0 && downlink < 1.5)) {
      return "weak";
    }
    return "good";
  }

  function getDocumentStartupType() {
    if (documentStartupType) return documentStartupType;
    try {
      if (sessionStorage.getItem(CONSTANTS.STORAGE_KEY_TWA_PROCESS_SEEN)) {
        documentStartupType = "warm";
      } else {
        sessionStorage.setItem(CONSTANTS.STORAGE_KEY_TWA_PROCESS_SEEN, "1");
        documentStartupType = "cold";
      }
    } catch (e) {
      documentStartupType = "unknown";
    }
    return documentStartupType;
  }

  function isTwaDiagnosticSampled() {
    if (typeof twaDiagnosticSampled === "boolean") return twaDiagnosticSampled;
    try {
      var hashValue = parseInt(simpleHash(getSessionId() + "|twa-p4"), 36);
      twaDiagnosticSampled = Number.isFinite(hashValue) &&
        (hashValue % 1000) < CONSTANTS.TWA_DIAGNOSTIC_SAMPLE_RATE * 1000;
    } catch (e) {
      twaDiagnosticSampled = false;
    }
    return twaDiagnosticSampled;
  }

  function getCurrentUserIdForAnalytics() {
    try {
      var id = localStorage.getItem("__gva_2p5q_ui");
      if (id) return String(id).trim();

      var userData = localStorage.getItem("__gva_9x2k_ud");
      if (userData) {
        var parsed = JSON.parse(userData);
        var user = parsed && parsed.user;
        var userId = user && (user.id || user.userID || user.userId);
        if (userId) return String(userId).trim();
      }
    } catch (e) {}
    return "";
  }

  function getBasePayload() {
    var vid = getVisitorId();
    var sid = getSessionId();
    var ts = Math.floor(Date.now() / 1000);
    var twaMeta = getTwaMetaForAnalytics();
    var networkHints = getNetworkHints();
    return {
      visitorId: vid,
      sessionId: sid,
      pagePath: window.location.pathname + window.location.search,
      hostname: window.location.hostname,
      rawHostname: window.location.hostname,
      locationOrigin: window.location.origin,
      locationHref: window.location.href.slice(0, 240),
      userAgent: navigator.userAgent.slice(0, 240),
      effectiveType: networkHints.effectiveType,
      downlink: networkHints.downlink,
      rtt: networkHints.rtt,
      saveData: networkHints.saveData,
      networkBucket: networkHints.networkBucket || "unknown",
      startupType: getDocumentStartupType(),
      userId: getCurrentUserIdForAnalytics(),
      clientChannel: twaMeta.clientChannel,
      appRuntime: twaMeta.appRuntime,
      appVersion: twaMeta.appVersion,
      appBuild: twaMeta.appBuild,
      container: twaMeta.container,
      eventId: generateId(),
      _at: Date.now(),
      _t: ts,
      _s: simpleHash(vid + sid + ts + SIGN_KEY),
    };
  }

  // ==================== 页面访问追踪 ====================

  var pageStartTime = Date.now();
  var lastHeartbeat = Date.now();
  var isLandingPage = false;

  // 检查是否是会话首次访问（落地页）
  function checkIsLanding(sessionId) {
    var key = "ma_session_landing";
    try {
      var current = sessionStorage.getItem(key);
      if (current) {
        return false;
      }

      var mirrored = readSessionState();
      if (mirrored && mirrored.id === sessionId && mirrored.landingPage) {
        sessionStorage.setItem(key, mirrored.landingPage);
        writeSessionState(sessionId, mirrored.landingPage);
        return false;
      }

      var landing = window.location.pathname + window.location.search;
      sessionStorage.setItem(key, landing);
      writeSessionState(sessionId, landing);
      return true;
    } catch (e) {
      if (!window._ma_session_landed) {
        window._ma_session_landed = true;
        writeSessionState(sessionId, window.location.pathname + window.location.search);
        return true;
      }
      return false;
    }
  }

  // 获取会话的落地页路径
  function getLandingPage() {
    try {
      var landing = sessionStorage.getItem("ma_session_landing");
      if (landing) return landing;
      var mirrored = readSessionState();
      return mirrored && mirrored.landingPage ? mirrored.landingPage : "";
    } catch (e) {
      var fallbackState = readSessionState();
      return fallbackState && fallbackState.landingPage ? fallbackState.landingPage : "";
    }
  }

  function trackPageview() {
    pageStartTime = Date.now();

    var sessionId = getSessionId();
    isLandingPage = checkIsLanding(sessionId);

    send({
      type: "pageview",
      ...getBasePayload(),
      pageTitle: document.title,
      referrer: document.referrer,
      isLanding: isLandingPage ? 1 : 0,
      landingPage: getLandingPage(),
    });
  }

  function trackLeave() {
    const duration = Math.round((Date.now() - pageStartTime) / 1000);
    if (duration > 0 && duration < CONSTANTS.MAX_DURATION_SECONDS) {
      send({
        type: "leave",
        ...getBasePayload(),
        duration: duration,
      });
    }
  }

  // ==================== 转化追踪 ====================

  function trackConversion(conversionType, data) {
    if (!conversionType) {
      console.warn("[MissAnalytics] trackConversion: conversionType is required");
      return;
    }

    const marketingData = getMarketingData() || {};

    const extraData = {
      ...(data?.extra || {}),
      exo_goal: marketingData.exo_goal || "",
      variation_id: marketingData.variation_id || "",
      category_id: marketingData.category_id || "",
      src_hostname: marketingData.src_hostname || "",
      pricing_model: marketingData.pricing_model || "",
      format: marketingData.format || "",
    };

    const payload = {
      type: "conversion",
      ...getBasePayload(),
      conversionType: conversionType,
      conversionId: data?.conversionId || "",
      userId: data?.userId || getCurrentUserIdForAnalytics(),
      deviceType: getDeviceType(),
      browser: getBrowser(),
      os: getOS(),
      referrer: document.referrer,
      utmSource: marketingData.utm_source || "",
      utmMedium: marketingData.utm_medium || "",
      utmCampaign: marketingData.utm_campaign || "",
      utmContent: marketingData.utm_content || "",
      utmTerm: marketingData.utm_term || "",
      clickId: marketingData.click_id || "",
      campId: marketingData.camp_id || "",
      adId: marketingData.ad_id || "",
      siteId: marketingData.site_id || "",
      zoneId: marketingData.zone_id || "",
      clickCost: marketingData.click_cost || 0,
      landingPage: marketingData.landing_page || getLandingPage() || "",
      value: data?.value || 0,
      extraData: JSON.stringify(extraData),
    };

    send(payload);
  }

  // ==================== 自定义事件追踪 ====================

  function trackEvent(category, action, label, value) {
    if (!category || !action) return;

    var payload = {
      type: "event",
      ...getBasePayload(),
      category: category,
      action: action,
      label: label || "",
      value: value || 0,
    };

    var payloadStr = JSON.stringify(payload);
    if (navigator.sendBeacon) {
      var blob = new Blob([payloadStr], { type: "application/json" });
      navigator.sendBeacon(collectUrl, blob);
    } else {
      fetch(collectUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: payloadStr,
        keepalive: true,
      }).catch(function() {});
    }
  }

  // ==================== 初始化 ====================

  function getFetchUrl(input) {
    try {
      if (typeof input === "string") return input;
      if (input && typeof input.url === "string") return input.url;
    } catch (e) {}
    return "";
  }

  function isHomeApiUrl(rawUrl) {
    try {
      var url = new URL(rawUrl, window.location.origin);
      return url.origin === window.location.origin && url.pathname === "/api/home";
    } catch (e) {
      return false;
    }
  }

  function isGuestChatApiUrl(rawUrl) {
    try {
      var url = new URL(rawUrl, window.location.origin);
      return url.origin === window.location.origin && url.pathname === "/api/gva/narratium/guestChat/chatStream";
    } catch (e) {
      return false;
    }
  }

  function getHomeApiTimingLabel(status, ok, extra) {
    var pathGroup = window.location.search.indexOf("inviteCode=") >= 0 ? "invite" : "plain";
    var statusPart = status > 0 ? String(status) : "error";
    return "status=" + statusPart + "|ok=" + (ok ? "1" : "0") + "|path=" + pathGroup + (extra ? "|" + extra : "");
  }

  function getGuestChatApiTimingLabel(status, ok, extra) {
    var statusPart = status > 0 ? String(status) : "error";
    var sid = "";
    var vid = "";
    try {
      sid = getSessionId().slice(-8);
      vid = getVisitorId().slice(-8);
    } catch (e) {}
    return "status=" + statusPart +
      "|ok=" + (ok ? "1" : "0") +
      "|path=" + window.location.pathname +
      "|raw_host=" + window.location.hostname +
      (sid ? "|sid=" + sid : "") +
      (vid ? "|vid=" + vid : "") +
      (extra ? "|" + extra : "");
  }

  function reportHomeApiTiming(startedAt, status, ok, extra) {
    if (homeApiTimingCount >= HOME_API_TIMING_MAX_EVENTS) return;
    if (window.location.pathname !== "/home") return;
    homeApiTimingCount += 1;
    var duration = Math.round(Date.now() - startedAt);
    trackEvent("performance", "home_api_request_v1", getHomeApiTimingLabel(status, ok, extra), duration);
  }

  function reportGuestChatApiTiming(startedAt, status, ok, extra) {
    if (guestChatApiTimingCount >= GUEST_CHAT_API_TIMING_MAX_EVENTS) return;
    if (window.location.pathname !== "/profile") return;
    guestChatApiTimingCount += 1;
    var duration = Math.round(Date.now() - startedAt);
    trackEvent("guest", "guest_chat_api_request_v1", getGuestChatApiTimingLabel(status, ok, extra), duration);
  }

  function installFetchTimingDiagnostics() {
    if (homeApiTimingInstalled || typeof window.fetch !== "function") return;
    homeApiTimingInstalled = true;
    var originalFetch = window.fetch;
    window.fetch = function() {
      var rawUrl = getFetchUrl(arguments[0]);
      var isHomeApi = isHomeApiUrl(rawUrl);
      var isGuestChatApi = isGuestChatApiUrl(rawUrl);
      if (!isHomeApi && !isGuestChatApi) {
        return originalFetch.apply(this, arguments);
      }

      var startedAt = Date.now();
      return originalFetch.apply(this, arguments).then(function(response) {
        if (isHomeApi) {
          reportHomeApiTiming(startedAt, response.status || 0, response.ok === true, "");
        } else {
          reportGuestChatApiTiming(startedAt, response.status || 0, response.ok === true, "");
        }
        return response;
      }).catch(function(error) {
        var name = error && error.name ? String(error.name).slice(0, 40) : "fetch_error";
        if (isHomeApi) {
          reportHomeApiTiming(startedAt, 0, false, "error=" + name);
        } else {
          reportGuestChatApiTiming(startedAt, 0, false, "error=" + name);
        }
        throw error;
      });
    };
  }

  function reportTwaNavigationMetric(label, value) {
    if (!isTwaRuntimeForAnalytics() || window.location.pathname !== "/home") return;
    var duration = Math.round(Number(value));
    if (!Number.isFinite(duration) || duration < 0 || duration > 120000) return;
    trackEvent("performance", "twa_navigation_timing_v1", label, duration);
  }

  function installTwaNavigationTimingDiagnostics() {
    if (!isTwaRuntimeForAnalytics() || window.location.pathname !== "/home") return;
    var sent = {};
    function once(label, value) {
      if (sent[label]) return;
      sent[label] = true;
      reportTwaNavigationMetric(label, value);
    }
    function reportNavigation() {
      try {
        var navigation = performance.getEntriesByType("navigation")[0];
        if (!navigation) return;
        once("ttfb", navigation.responseStart);
        once("dom_interactive", navigation.domInteractive);
        if (navigation.loadEventEnd > 0) once("load", navigation.loadEventEnd);
      } catch (e) {}
    }
    function reportBufferedPaint() {
      try {
        var fcp = performance.getEntriesByName("first-contentful-paint")[0];
        if (fcp) once("fcp", fcp.startTime);
      } catch (e) {}
    }
    reportNavigation();
    reportBufferedPaint();
    if (document.readyState === "complete") {
      setTimeout(reportNavigation, 0);
    } else {
      window.addEventListener("load", reportNavigation, { once: true });
    }
    if (typeof PerformanceObserver !== "function") return;
    try {
      var paintObserver = new PerformanceObserver(function(list) {
        list.getEntries().forEach(function(entry) {
          if (entry.name === "first-contentful-paint") once("fcp", entry.startTime);
        });
      });
      paintObserver.observe({ type: "paint", buffered: true });
    } catch (e) {}
    try {
      var latestLcp = 0;
      var lcpObserver = new PerformanceObserver(function(list) {
        list.getEntries().forEach(function(entry) { latestLcp = entry.startTime; });
      });
      lcpObserver.observe({ type: "largest-contentful-paint", buffered: true });
      var flushLcp = function() {
        if (latestLcp > 0) once("lcp", latestLcp);
      };
      document.addEventListener("visibilitychange", function() {
        if (document.visibilityState === "hidden") flushLcp();
      });
      setTimeout(flushLcp, 10000);
    } catch (e) {}
  }

  function init() {
    // 敏感路径（如 /email-revert 撤销链接）整页不上报、不归因 —— URL 含一次性凭证
    if (isSensitivePath()) return;
    restorePendingQueue();
    captureMarketingData();
    installFetchTimingDiagnostics();
    if (isTwaDiagnosticSampled()) installTwaNavigationTimingDiagnostics();
    trackPageview();
    if (isTwaDiagnosticSampled() && isTwaRuntimeForAnalytics() && window.location.pathname === "/home") {
      trackEvent("performance", "twa_startup_context_v1", getDocumentStartupType(), 1);
    }

    document.addEventListener("visibilitychange", function() {
      if (document.visibilityState === "hidden") {
        trackLeave();
      } else if (document.visibilityState === "visible") {
        if (Date.now() - lastHeartbeat > CONSTANTS.SESSION_TIMEOUT_MS) {
          try {
            sessionStorage.removeItem(CONSTANTS.STORAGE_KEY_SESSION);
            sessionStorage.removeItem("ma_session_landing");
            clearSessionState();
          } catch (e) {
            window._ma_temp_session_id = generateId();
            clearSessionState();
          }
          trackPageview();
        }
      }
    });

    window.addEventListener("beforeunload", function() {
      savePendingQueue();
      trackLeave();
    });

    // SPA 路由变化支持（带防抖，避免短时间内重复上报）
    var originalPushState = history.pushState;
    var originalReplaceState = history.replaceState;
    var spaDebounceTimer = null;
    var lastTrackedPath = window.location.pathname + window.location.search;

    function debouncedRouteChange() {
      // 与 init 早退对齐：SPA 客户端导航进入敏感路径时同样整页豁免，
      // 避免 300ms 防抖窗口内把含一次性凭证的 URL 读进上报链路
      if (isSensitivePath()) return;
      var currentPath = window.location.pathname + window.location.search;
      if (currentPath === lastTrackedPath) return;  // 路径没变，跳过

      if (spaDebounceTimer) clearTimeout(spaDebounceTimer);
      spaDebounceTimer = setTimeout(function() {
        trackLeave();
        lastTrackedPath = window.location.pathname + window.location.search;
        trackPageview();
        spaDebounceTimer = null;
      }, CONSTANTS.SPA_DEBOUNCE_MS);
    }

    history.pushState = function() {
      originalPushState.apply(this, arguments);
      debouncedRouteChange();
    };

    history.replaceState = function() {
      originalReplaceState.apply(this, arguments);
    };

    window.addEventListener("popstate", function() {
      debouncedRouteChange();
    });
  }

  // ==================== 暴露公共 API ====================

  function destroy() {
    if (isDestroyed) return;
    isDestroyed = true;
    trackLeave();
  }

  window.MissAnalytics = {
    trackPageview: trackPageview,
    trackConversion: trackConversion,
    trackEvent: trackEvent,
    getVisitorId: getVisitorId,
    getSessionId: getSessionId,
    getMarketingData: getMarketingData,
    destroy: destroy,
    version: "4.7.0",
  };

  // DOM Ready 后初始化
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
