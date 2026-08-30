(function () {
  "use strict";

  var demo = window.EZTRIP_DEMO;
  var STORAGE_KEY = "eztrip-interactive-demo-state-v3";
  var root = document.getElementById("app-root");
  var progressRoot = document.getElementById("progress-root");
  var liveRegion = document.getElementById("status-live");
  var evidenceDialog = document.getElementById("evidence-dialog");
  var pendingTimer = null;
  var transientScreen = null;
  var DETECT_DELAY = 1200;
  var PLAN_DELAY = 800;
  var UPDATE_DELAY = 800;

  var iconPaths = {
    refresh:
      '<path d="M20 11a8 8 0 1 0 2 5.5"/><path d="M20 4v7h-7"/>',
    document:
      '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h6"/>',
    close: '<path d="m6 6 12 12M18 6 6 18"/>',
    external:
      '<path d="M15 3h6v6M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    chevron: '<path d="m9 18 6-6-6-6"/>',
    rain:
      '<path d="M16 13a4 4 0 0 0-7.7-1.5A3.5 3.5 0 0 0 8.5 18H17a3 3 0 0 0-1-5Z"/><path d="m8 21-1 2m5-2-1 2m5-2-1 2"/>',
    ticket:
      '<path d="M2 9a3 3 0 0 0 0 6v4h20v-4a3 3 0 0 0 0-6V5H2z"/><path d="M13 5v14"/>',
    route:
      '<circle cx="6" cy="19" r="2"/><circle cx="18" cy="5" r="2"/><path d="M8 19c6 0 3-14 8-14"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    user:
      '<path d="M20 21a8 8 0 0 0-16 0"/><circle cx="12" cy="7" r="4"/>',
    wallet:
      '<path d="M20 7V5a2 2 0 0 0-2-2H5a3 3 0 0 0 0 6h16v10a2 2 0 0 1-2 2H5a3 3 0 0 1-3-3V6"/><path d="M16 14h2"/>',
    clock:
      '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    home:
      '<path d="m3 11 9-8 9 8"/><path d="M5 10v10h14V10M9 20v-6h6v6"/>',
    protect:
      '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/>',
  };

  function iconSvg(name) {
    var paths = iconPaths[name] || iconPaths.chevron;
    return (
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      paths +
      "</svg>"
    );
  }

  function hydrateIcons(scope) {
    var target = scope || document;
    target.querySelectorAll("[data-icon]").forEach(function (node) {
      node.innerHTML = iconSvg(node.getAttribute("data-icon"));
    });
  }

  function defaultState() {
    return {
      screen: "request",
      version: "v1",
      selectedProposal: true,
      openItem: null,
    };
  }

  function loadState() {
    try {
      var saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
      if (!saved || typeof saved !== "object") {
        return defaultState();
      }
      return Object.assign(defaultState(), saved);
    } catch (error) {
      return defaultState();
    }
  }

  var state = loadState();

  function saveState() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    if (state.screen === "request") {
      history.replaceState(null, "", location.pathname + location.search);
    } else {
      history.replaceState(null, "", "#" + state.screen);
    }
  }

  function announce(message) {
    liveRegion.textContent = "";
    window.setTimeout(function () {
      liveRegion.textContent = message;
    }, 20);
  }

  function formatMoney(value) {
    return "¥" + Number(value).toLocaleString("zh-CN");
  }

  function progressMarkup() {
    var labels = ["旅行需求", "行程方案", "雨天调整", "确认调整"];
    var visibleScreen =
      transientScreen === "planning"
        ? "plan"
        : transientScreen === "updating"
          ? "hitl"
          : state.screen;
    var activeStep = {
      request: 1,
      review: 1,
      plan: 2,
      rain: 3,
      hitl: 4,
      completed: 4,
    }[visibleScreen];

    return (
      '<div class="progress-shell"><nav class="progress-inner" aria-label="演示进度"><ol class="progress-list">' +
      labels
        .map(function (label, index) {
          var step = index + 1;
          var complete =
            visibleScreen === "completed" ? step <= activeStep : step < activeStep;
          var active = step === activeStep;
          var classes =
            "progress-step" +
            (complete ? " is-complete" : "") +
            (active ? " is-active" : "");
          var indexContent = complete
            ? '<span data-icon="check" aria-hidden="true"></span>'
            : String(step);
          return (
            '<li class="' +
            classes +
            '"' +
            ' aria-label="步骤 ' +
            step +
            "：" +
            label +
            '"' +
            (active ? ' aria-current="step"' : "") +
            '><span class="progress-index">' +
            indexContent +
            "</span><span>" +
            label +
            "</span></li>"
          );
        })
        .join("") +
      "</ol></nav></div>"
    );
  }

  function requestMarkup() {
    return (
      '<section class="request-shell request-entry-shell" aria-labelledby="request-title">' +
      '<div class="request-intro">' +
      '<h1 id="request-title">说说你想要的<span>旅行方式</span></h1>' +
      "<p>确认本次旅行的日期、预算和偏好后，即可生成行程。</p>" +
      "</div>" +
      '<form class="request-form request-input-form" id="request-form">' +
      '<div class="request-input-header"><label for="trip-request">旅行需求</label></div>' +
      '<div class="request-input-body"><textarea id="trip-request" readonly aria-readonly="true">' +
      demo.request.rawInput +
      "</textarea></div>" +
      '<div class="form-footer"><button class="button button-primary button-full" type="submit">生成行程 <span data-icon="chevron" aria-hidden="true"></span></button></div>' +
      "</form></section>"
    );
  }

  function requirementTableMarkup() {
    return (
      '<table class="requirement-table"><caption class="sr-only">本次旅行需求</caption><tbody>' +
      '<tr><th scope="row">出发地</th><td>上海</td><th scope="row">目的地</th><td>北京</td></tr>' +
      '<tr><th scope="row">旅行日期</th><td>9月3日—4日</td><th scope="row">同行人数</th><td>2 位成人</td></tr>' +
      '<tr><th scope="row">整趟预算</th><td><strong>¥3,000</strong></td><th scope="row">行程节奏</th><td>轻松</td></tr>' +
      '<tr><th scope="row">兴趣偏好</th><td><span class="requirement-tag">历史文化</span></td><th scope="row">步行偏好</th><td><span class="requirement-tag">尽量少步行</span></td></tr>' +
      "</tbody></table>"
    );
  }

  function reviewMarkup() {
    return (
      '<section class="request-shell review-shell" aria-labelledby="review-title">' +
      '<div class="review-source"><p class="section-label">旅行需求</p><h1 id="review-title">确认旅行需求</h1><p>以下内容将用于生成本次行程。</p><blockquote>' +
      demo.request.rawInput +
      "</blockquote></div>" +
      '<form class="request-form review-card" id="review-form"><div class="review-card-header"><div><span class="review-status"><span data-icon="check" aria-hidden="true"></span>待确认</span><h2>旅行需求表</h2></div><p>8 项条件</p></div>' +
      requirementTableMarkup() +
      '<div class="review-footnote"><span data-icon="rain" aria-hidden="true"></span><span>如遇降雨，会先给出替换建议，由你确认后再调整。</span></div>' +
      '<div class="form-footer"><button class="button button-primary button-full" data-action="confirm-requirements" type="button">确认需求，生成行程 <span data-icon="chevron" aria-hidden="true"></span></button><button class="button button-secondary button-full" data-action="back-request" type="button">返回查看输入</button></div></form></section>'
    );
  }

  function loadingMarkup() {
    var isUpdating = transientScreen === "updating";
    var isDetecting = transientScreen === "detecting";
    return (
      '<section class="loading-shell" aria-labelledby="loading-title" aria-busy="true">' +
      '<div class="loading-card"><span class="loading-spinner" aria-hidden="true"></span>' +
      '<h1 id="loading-title">' +
      (isUpdating
        ? "正在更新第 2 天…"
        : isDetecting
          ? "正在整理旅行需求…"
          : "正在整理行程…") +
      "</h1>" +
      (isDetecting
        ? '<ul class="thinking-steps"><li>确认出发地、日期和人数</li><li>核对预算与行程节奏</li><li>整理兴趣与步行偏好</li></ul>'
        : "<p>马上就好</p>") +
      '<button class="button button-primary" type="button" disabled>' +
      (isUpdating ? "正在更新" : isDetecting ? "正在整理" : "正在生成") +
      "</button></div></section>"
    );
  }

  function versionControlsMarkup() {
    if (state.screen !== "completed") {
      return (
        '<div class="version-controls" aria-label="行程版本"><button class="version-button is-active" type="button" disabled>原方案</button></div>'
      );
    }
    return (
      '<div class="version-controls" aria-label="行程版本">' +
      '<button class="version-button' +
      (state.version === "v1" ? " is-active" : "") +
      '" data-action="switch-version" data-version="v1" type="button">原方案</button>' +
      '<button class="version-button' +
      (state.version === "v2" ? " is-active" : "") +
      '" data-action="switch-version" data-version="v2" type="button">调整后</button>' +
      "</div>"
    );
  }

  function activityMarkup(item) {
    var isOpen = state.openItem === item.id;
    var classes =
      "activity-row" +
      (item.affectedByRain ? " is-affected" : "") +
      (item.replacementFor ? " is-replacement" : "");
    var detail = isOpen
      ? '<div class="activity-detail"><div><strong>推荐理由</strong><span>' +
        item.reason +
        "</span></div><div><strong>路线参考</strong><span>" +
        item.route +
        "</span></div><div><strong>数据来源</strong><span>" +
        item.source +
        "</span></div></div>"
      : "";

    return (
      '<article class="' +
      classes +
      '" data-item-id="' +
      item.id +
      '">' +
      '<span class="activity-marker" aria-hidden="true"></span>' +
      '<div class="activity-time">' +
      item.time +
      "</div>" +
      '<div class="activity-copy"><h3>' +
      item.title +
      "</h3><p>" +
      item.description +
      '</p><div class="activity-meta"><span class="meta-item">' +
      item.environment +
      '</span><span class="meta-item"><span data-icon="ticket" aria-hidden="true"></span>' +
      item.ticket +
      '</span><span class="meta-item"><span data-icon="route" aria-hidden="true"></span>' +
      item.district +
      "</span></div></div>" +
      '<button class="activity-toggle" data-action="toggle-item" data-item="' +
      item.id +
      '" type="button" aria-expanded="' +
      String(isOpen) +
      '">详情 <span data-icon="chevron" aria-hidden="true"></span></button>' +
      detail +
      "</article>"
    );
  }

  function timelineMarkup(versionData) {
    return (
      '<section class="timeline-panel" aria-labelledby="timeline-title">' +
      '<div class="timeline-header"><div><h2 id="timeline-title">行程时间线</h2><p>点击活动可查看推荐理由、路线参考和数据来源。</p></div><span class="timeline-status">' +
      versionData.status +
      "</span></div>" +
      versionData.days
        .map(function (day) {
          return (
            '<section class="day-block" aria-labelledby="' +
            day.id +
            '-title"><div class="day-meta"><div><strong>DAY ' +
            day.number +
            '</strong><span id="' +
            day.id +
            '-title">' +
            day.date +
            day.weekday +
            '</span></div><div class="day-weather">' +
            day.weather.label +
            "<br />" +
            day.weather.impact +
            '</div></div><div class="day-timeline"><div class="departure-note">' +
            day.departure +
            "</div>" +
            day.items.map(activityMarkup).join("") +
            "</div></section>"
          );
        })
        .join("") +
      "</section>"
    );
  }

  function budgetState(budget, limit) {
    if (limit >= budget.maximum) {
      return {
        className: "is-within",
        label:
          "按参考上限计算，仍有约 " +
          formatMoney(limit - budget.maximum) +
          " 余量。",
      };
    }
    if (limit >= budget.midpoint) {
      return {
        className: "is-warning",
        label:
          "中位参考值在预算内，但参考上限可能超出 " +
          formatMoney(budget.maximum - limit) +
          "。",
      };
    }
    return {
      className: "is-over",
      label:
        "中位参考值预计超出预算约 " +
        formatMoney(budget.midpoint - limit) +
        "，需要调整行程。",
    };
  }

  function budgetMarkup(version) {
    var budget = demo.budgets[version];
    var result = budgetState(budget, demo.request.budget);
    return (
      '<section class="panel budget-panel" aria-labelledby="budget-title">' +
      '<div class="budget-head"><div><p class="section-label">预算参考</p><h2 id="budget-title" class="budget-total">约 ' +
      formatMoney(budget.midpoint) +
      '</h2><p class="budget-range">参考范围 ' +
      formatMoney(budget.minimum) +
      "–" +
      formatMoney(budget.maximum) +
      '</p></div><div class="budget-limit">你的预算<br /><strong>' +
      formatMoney(demo.request.budget) +
      "</strong></div></div>" +
      '<div class="budget-status ' +
      result.className +
      '">' +
      result.label +
      '</div><div class="budget-list">' +
      budget.items
        .map(function (item) {
          return (
            '<div class="budget-row"><div><strong>' +
            item.category +
            "</strong><span>" +
            item.detail +
            "</span></div><b>" +
            formatMoney(item.minimum) +
            "–" +
            formatMoney(item.maximum) +
            "</b></div>"
          );
        })
        .join("") +
      '</div><p class="boundary-note">价格仅供行程预算参考，不代表实时成交价；未包含往返大交通、购物或预订手续费。</p></section>'
    );
  }

  function weatherMarkup() {
    return (
      '<section class="panel weather-panel" aria-labelledby="weather-title"><div class="weather-summary"><span class="weather-icon"><span data-icon="rain" aria-hidden="true"></span></span><div><p class="section-label">天气提醒</p><strong id="weather-title">9月4日有阵雨</strong><span>2 项户外活动建议调整</span></div></div>' +
      '<div class="weather-actions"><button class="button button-amber button-full" data-action="view-rain" type="button">查看雨天方案 <span data-icon="chevron" aria-hidden="true"></span></button></div></section>'
    );
  }

  function rainProposalMarkup() {
    var proposal = demo.rainProposal;
    return (
      '<section class="panel hitl-panel" aria-labelledby="proposal-title"><div class="panel-heading"><div><p class="section-label">雨天推荐</p><h2 id="proposal-title">' +
      proposal.title +
      "</h2><p>" +
      proposal.summary +
      "</p></div></div>" +
      '<label class="proposal-choice"><input id="rain-proposal" type="radio" name="rain-proposal" checked /><span class="proposal-card"><strong>采用第 2 天全天室内方案</strong><span>' +
      proposal.scope +
      '</span><span class="replacement-list">' +
      proposal.replacements
        .map(function (replacement) {
          return (
            '<span class="replacement"><b>' +
            replacement.from +
            " → " +
            replacement.to +
            "</b><span>" +
            replacement.reason +
            "</span></span>"
          );
        })
        .join("") +
      "</span></span></label>" +
      '<div class="hitl-actions"><button class="button button-primary" data-action="continue-hitl" type="button">查看调整内容</button><button class="button button-secondary" data-action="keep-original" type="button">保留原方案</button></div></section>'
    );
  }

  function completedPanelMarkup() {
    return (
      '<section class="panel hitl-panel" aria-labelledby="completed-title"><div class="panel-heading"><div><p class="section-label">调整结果</p><h2 id="completed-title">第 2 天已更新</h2><p>仅调整雨天活动，第 1 天保持不变。</p></div></div><div class="weather-actions"><button class="button button-secondary button-full" data-action="open-hitl" type="button">查看调整前后</button></div></section>'
    );
  }

  function planMarkup() {
    var version = state.screen === "completed" ? state.version : "v1";
    var versionData = demo.versions[version];
    var completedBanner =
      state.screen === "completed"
        ? '<div class="success-banner"><strong>行程已更新</strong><span>已调整 1 个日期 · 当前查看' +
          versionData.label +
          "</span></div>"
        : "";
    var sidebarTop =
      state.screen === "rain"
        ? rainProposalMarkup()
        : state.screen === "completed"
          ? completedPanelMarkup()
          : weatherMarkup();

    return (
      '<section class="page-shell"><header class="plan-heading"><div><p class="section-label">行程方案</p><h1>北京 · 2 天行程</h1><p>' +
      versionData.status +
      " · 2 位成人 · 轻松节奏</p></div>" +
      versionControlsMarkup() +
      "</header>" +
      completedBanner +
      '<div class="workbench">' +
      timelineMarkup(versionData) +
      '<aside class="decision-column" aria-label="预算、天气与调整选项">' +
      sidebarTop +
      budgetMarkup(version) +
      "</aside></div></section>"
    );
  }

  function compareItemMarkup(item) {
    return (
      '<article class="compare-item"><span class="compare-dot" aria-hidden="true"></span><span class="compare-time">' +
      item.time +
      "</span><h3>" +
      item.title +
      "</h3><p>" +
      item.environment +
      " · " +
      item.ticket +
      "</p></article>"
    );
  }

  function hitlMarkup() {
    var oldDay = demo.versions.v1.days[1];
    var newDay = demo.versions.v2.days[1];
    var v2Budget = demo.budgets.v2;
    var v2BudgetResult = budgetState(v2Budget, demo.request.budget);
    return (
      '<section class="compare-shell"><header class="compare-heading"><div><p class="section-label">确认调整</p><h1>确认第 2 天的雨天安排</h1><p>第 1 天保持不变；第 2 天替换 2 项受降雨影响的户外活动。</p></div></header>' +
      '<div class="change-strip"><strong>仅调整 DAY 2</strong><span>DAY 1 保持不变</span></div>' +
      '<div class="compare-layout"><section class="comparison-stage" aria-label="调整前后行程对比">' +
      '<div class="compare-stage-header"><div><strong>原方案</strong><span>第 2 天</span></div><div><strong>调整后</strong><span>雨天推荐 · 等待确认</span></div></div>' +
      '<div class="compare-grid"><section class="compare-day"><h2>DAY 2 · 9月4日（原计划）</h2>' +
      oldDay.items.map(compareItemMarkup).join("") +
      '</section><section class="compare-day is-new"><h2>DAY 2 · 9月4日（雨天调整）</h2>' +
      newDay.items.map(compareItemMarkup).join("") +
      "</section></div>" +
      '<div class="compare-summary"><div class="removed"><strong>将被替换</strong><span>天坛公园<br />景山公园</span></div><div class="added"><strong>将被采用</strong><span>首都博物馆<br />北京天文馆</span></div></div>' +
      "</section>" +
      '<aside class="compare-side" aria-label="调整依据与确认操作"><section class="panel"><div class="panel-heading"><div><p class="section-label">调整依据</p><h2>为什么只改第 2 天</h2></div></div>' +
      '<ul class="reason-list"><li><span class="reason-icon"><span data-icon="home" aria-hidden="true"></span></span><div><strong>替换为室内活动</strong><span>两个地点都不受降雨影响。</span></div></li>' +
      '<li><span class="reason-icon"><span data-icon="protect" aria-hidden="true"></span></span><div><strong>保护未受影响日期</strong><span>第 1 天的候选、时间和费用保持不变。</span></div></li>' +
      '<li><span class="reason-icon"><span data-icon="wallet" aria-hidden="true"></span></span><div><strong>重新计算预算参考</strong><span>原方案约 ¥1,370；调整后约 ¥1,350。' +
      v2BudgetResult.label +
      "</span></div></li></ul></section>" +
      budgetMarkup("v2") +
      '<div class="hitl-actions is-sticky-mobile"><button class="button button-primary" data-action="confirm-v2" type="button"><span data-icon="check" aria-hidden="true"></span>确认调整</button><button class="button button-secondary" data-action="back-rain" type="button">返回雨天方案</button></div>' +
      "</aside></div></section>"
    );
  }

  function render() {
    progressRoot.innerHTML = progressMarkup();
    if (transientScreen) {
      root.innerHTML = loadingMarkup();
    } else if (state.screen === "request") {
      root.innerHTML = requestMarkup();
    } else if (state.screen === "review") {
      root.innerHTML = reviewMarkup();
    } else if (state.screen === "hitl") {
      root.innerHTML = hitlMarkup();
    } else {
      root.innerHTML = planMarkup();
    }
    hydrateIcons(document);
  }

  function updateState(updates, message) {
    state = Object.assign({}, state, updates);
    saveState();
    render();
    if (message) {
      announce(message);
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function startTransition(kind, delay, updates, startMessage, endMessage) {
    if (pendingTimer) {
      window.clearTimeout(pendingTimer);
    }
    transientScreen = kind;
    render();
    announce(startMessage);
    window.scrollTo({ top: 0, behavior: "smooth" });
    pendingTimer = window.setTimeout(function () {
      pendingTimer = null;
      transientScreen = null;
      updateState(updates, endMessage);
    }, delay);
  }

  root.addEventListener("submit", function (event) {
    if (event.target.id !== "request-form") {
      return;
    }
    event.preventDefault();
    startTransition(
      "detecting",
      DETECT_DELAY,
      { screen: "review", version: "v1", openItem: null },
      "正在整理旅行需求。",
      "旅行需求已整理，请确认。",
    );
  });

  root.addEventListener("click", function (event) {
    var control = event.target.closest("[data-action]");
    if (!control) {
      return;
    }
    var action = control.getAttribute("data-action");

    if (action === "toggle-item") {
      var item = control.getAttribute("data-item");
      state.openItem = state.openItem === item ? null : item;
      saveState();
      render();
      return;
    }

    if (action === "confirm-requirements") {
      startTransition(
        "planning",
        PLAN_DELAY,
        { screen: "plan", version: "v1", openItem: null },
        "正在整理行程。",
        "行程已经准备好。",
      );
      return;
    }

    if (action === "back-request") {
      updateState({ screen: "request" }, "已返回旅行需求输入。");
      return;
    }

    if (action === "view-rain") {
      updateState({ screen: "rain" }, "已打开第 2 天的雨天推荐。");
      return;
    }

    if (action === "continue-hitl" || action === "open-hitl") {
      updateState({ screen: "hitl" }, "请确认第 2 天的调整内容。");
      return;
    }

    if (action === "keep-original") {
      updateState({ screen: "plan", version: "v1" }, "已保留原安排。");
      return;
    }

    if (action === "back-rain") {
      updateState({ screen: "rain" }, "已返回雨天推荐。");
      return;
    }

    if (action === "confirm-v2") {
      startTransition(
        "updating",
        UPDATE_DELAY,
        { screen: "completed", version: "v2", openItem: null },
        "正在更新第 2 天。",
        "行程已更新，仅第 2 天发生变化。",
      );
      return;
    }

    if (action === "switch-version") {
      state.version = control.getAttribute("data-version");
      state.openItem = null;
      saveState();
      render();
      announce("当前查看 " + demo.versions[state.version].label + "。");
    }
  });

  document.getElementById("reset-demo").addEventListener("click", function () {
    if (pendingTimer) {
      window.clearTimeout(pendingTimer);
      pendingTimer = null;
    }
    transientScreen = null;
    localStorage.removeItem(STORAGE_KEY);
    state = defaultState();
    saveState();
    render();
    announce("演示已重置。");
    window.scrollTo({ top: 0, behavior: "smooth" });
  });

  document
    .getElementById("open-evidence")
    .addEventListener("click", function () {
      evidenceDialog.showModal();
    });

  document
    .getElementById("close-evidence")
    .addEventListener("click", function () {
      evidenceDialog.close();
    });

  evidenceDialog.addEventListener("click", function (event) {
    if (event.target === evidenceDialog) {
      evidenceDialog.close();
    }
  });

  saveState();
  render();
})();
