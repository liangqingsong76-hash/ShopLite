/* 文件职责：智能找商品页面交互；只提交用户主动选择的输入并安全渲染 discovery API 结果。 */
(function () {
    "use strict";

    const app = document.getElementById("discoveryApp");
    if (!app) return;

    const textForm = document.getElementById("discoveryTextForm");
    const imageForm = document.getElementById("discoveryImageForm");
    const queryInput = document.getElementById("discoveryQuery");
    const voiceButton = document.getElementById("voiceSearchButton");
    const imageInput = document.getElementById("discoveryImage");
    const statusBox = document.getElementById("discoveryStatus");
    const resultsSection = document.getElementById("discoveryResultsSection");
    const resultsGrid = document.getElementById("discoveryResults");
    const intentSummary = document.getElementById("discoveryIntentSummary");
    const csrfInput = textForm.querySelector("input[name='csrfmiddlewaretoken']");
    const resultsHeading = document.getElementById("discoveryResultsHeading");
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let recognition = null;
    let pendingVoiceTranscript = "";
    let activeRequest = null;
    let requestSequence = 0;

    /** 更新一个可被屏幕阅读器播报的状态区域。 */
    function setStatus(message, state) {
        statusBox.textContent = message;
        statusBox.className = "discovery-status";
        if (state) statusBox.classList.add(`is-${state}`);
    }

    /** 从服务端响应读取可公开展示的错误，不暴露响应内部结构。 */
    async function responsePayload(response) {
        let payload = {};
        try {
            payload = await response.json();
        } catch (error) {
            throw new Error("服务返回了无法读取的内容，请稍后重试。");
        }
        if (!response.ok) {
            const message = payload.error && payload.error.message;
            throw new Error(message || "暂时无法查找商品，请稍后重试。");
        }
        return payload;
    }

    /** 发送 JSON 搜索请求；CSRF 令牌来自当前受保护页面。 */
    async function postJson(url, body, signal) {
        const response = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfInput ? csrfInput.value : "",
            },
            body: JSON.stringify(body),
            signal,
        });
        return responsePayload(response);
    }

    /** 取消旧搜索并创建本次请求标识，防止较慢响应覆盖用户后发起的结果。 */
    function beginRequest() {
        cancelActiveRequest();
        const request = {
            id: ++requestSequence,
            controller: new AbortController(),
        };
        activeRequest = request;
        app.setAttribute("aria-busy", "true");
        return request;
    }

    /** 用户开始新的输入动作时取消旧请求，避免旧结果覆盖尚未提交的新意图。 */
    function cancelActiveRequest() {
        if (!activeRequest) return;
        activeRequest.controller.abort();
        activeRequest = null;
        app.setAttribute("aria-busy", "false");
    }

    /** 仅允许当前最新请求更新页面，旧请求完成时静默丢弃。 */
    function isCurrentRequest(request) {
        return activeRequest && activeRequest.id === request.id;
    }

    /** 在当前请求结束时解除忙碌状态，不干扰已开始的下一次请求。 */
    function finishRequest(request) {
        if (!isCurrentRequest(request)) return;
        activeRequest = null;
        app.setAttribute("aria-busy", "false");
    }

    /** 用后端结构化意图生成简短、可核对的查找条件说明。 */
    function describeIntent(intent) {
        if (!intent) return "";
        const parts = [];
        if (intent.keywords && intent.keywords.length) parts.push(`关键词：${intent.keywords.join("、")}`);
        if (intent.attributes && intent.attributes.length) parts.push(`特点：${intent.attributes.join("、")}`);
        if (intent.brand) parts.push(`品牌：${intent.brand}`);
        if (intent.category) parts.push(`分类：${intent.category}`);
        if (intent.price_min || intent.price_max) {
            const minimum = intent.price_min ? `${intent.price_min} 元` : "不限";
            const maximum = intent.price_max ? `${intent.price_max} 元` : "不限";
            parts.push(`预算：${minimum} 至 ${maximum}`);
        }
        return parts.join("；");
    }

    /** 按 Django 生成的 URL 模板构造商品详情地址。 */
    function productUrl(productId) {
        return app.dataset.productUrlTemplate.replace("/0/", `/${encodeURIComponent(productId)}/`);
    }

    /** 创建一个结果卡片；所有接口内容都通过 textContent 写入。 */
    function createProductCard(product) {
        const article = document.createElement("article");
        article.className = "discovery-card";

        const imageLink = document.createElement("a");
        imageLink.className = "discovery-card-image";
        imageLink.href = productUrl(product.id);
        imageLink.setAttribute("aria-label", `查看${product.name}详情`);
        if (product.image) {
            const image = document.createElement("img");
            image.src = product.image;
            image.alt = product.name;
            image.loading = "lazy";
            imageLink.appendChild(image);
        } else {
            imageLink.textContent = "暂无商品图片";
        }

        const body = document.createElement("div");
        body.className = "discovery-card-body";

        const title = document.createElement("h3");
        title.textContent = product.name;
        const meta = document.createElement("p");
        meta.className = "discovery-card-meta";
        meta.textContent = [product.brand, product.category].filter(Boolean).join(" · ") || "商品信息";
        const price = document.createElement("p");
        price.className = "discovery-card-price";
        price.textContent = `¥${product.price}`;

        const reasons = document.createElement("ul");
        reasons.className = "discovery-card-reasons";
        (product.reasons || []).slice(0, 3).forEach((reason) => {
            const item = document.createElement("li");
            item.textContent = reason;
            reasons.appendChild(item);
        });

        const link = document.createElement("a");
        link.className = "discovery-card-link";
        link.href = productUrl(product.id);
        link.textContent = "查看商品详情";
        body.append(title, meta, price, reasons, link);
        article.append(imageLink, body);
        return article;
    }

    /** 清空旧结果并展示本次只读商品匹配结果。 */
    function renderResults(payload) {
        resultsGrid.replaceChildren();
        intentSummary.textContent = describeIntent(payload.intent);
        (payload.results || []).forEach((product) => {
            resultsGrid.appendChild(createProductCard(product));
        });
        resultsSection.hidden = false;
        if (payload.count) {
            setStatus(`已找到 ${payload.count} 件符合条件的商品。`, "success");
        } else {
            setStatus("暂时没有找到符合全部条件的商品，请减少条件或换一种说法。", "error");
        }
        try {
            resultsHeading.focus({preventScroll: true});
        } catch (error) {
            resultsHeading.focus();
        }
        resultsSection.scrollIntoView({
            behavior: reduceMotion.matches ? "auto" : "smooth",
            block: "start",
        });
    }

    /** 提交文字或语音转写文本，并统一处理加载与错误状态。 */
    async function searchByText(source) {
        const query = queryInput.value.trim();
        if (!query) {
            setStatus("请先输入或说出您想买的商品。", "error");
            queryInput.focus();
            return;
        }
        const request = beginRequest();
        setStatus("正在理解您的需求并匹配商品，请稍候……", "loading");
        try {
            const url = source === "voice" ? app.dataset.voiceUrl : app.dataset.textUrl;
            const body = source === "voice" ? {transcript: query} : {query};
            const payload = await postJson(url, body, request.controller.signal);
            if (isCurrentRequest(request)) renderResults(payload);
        } catch (error) {
            if (error.name !== "AbortError" && isCurrentRequest(request)) {
                setStatus(error.message, "error");
            }
        } finally {
            finishRequest(request);
        }
    }

    /** 初始化浏览器语音识别；原始音频不提交给 ShopLite 后端。 */
    function createRecognition() {
        const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!Recognition) return null;
        const instance = new Recognition();
        instance.lang = "zh-CN";
        instance.interimResults = false;
        instance.maxAlternatives = 1;
        instance.addEventListener("start", () => {
            cancelActiveRequest();
            voiceButton.setAttribute("aria-pressed", "true");
            voiceButton.textContent = "正在听，请说话…";
            setStatus("正在听您说话，说完后请确认文字，再点击“查找商品”。", "loading");
        });
        instance.addEventListener("result", (event) => {
            const transcript = event.results[0][0].transcript.trim();
            queryInput.value = transcript;
            pendingVoiceTranscript = transcript;
            setStatus(`听到：“${transcript}”。请确认或修改文字，再点击“查找商品”。`, "success");
            queryInput.focus();
        });
        instance.addEventListener("error", (event) => {
            const message = event.error === "not-allowed"
                ? "没有获得麦克风权限，请允许后重试，或直接打字查找。"
                : "没有听清楚，请重试或直接打字查找。";
            setStatus(message, "error");
        });
        instance.addEventListener("end", () => {
            voiceButton.setAttribute("aria-pressed", "false");
            voiceButton.textContent = "🎙 说出需求";
        });
        return instance;
    }

    /** 校验并提交用户主动选择的图片；前端校验不能替代服务端校验。 */
    async function searchByImage(event) {
        event.preventDefault();
        const image = imageInput.files && imageInput.files[0];
        if (!image) {
            setStatus("请先选择一张商品图片。", "error");
            return;
        }
        if (image.size > 2 * 1024 * 1024) {
            setStatus("图片不能超过 2 MB，请选择更小的图片。", "error");
            return;
        }
        const request = beginRequest();
        setStatus("正在识别图片并匹配商品，请稍候……", "loading");
        const formData = new FormData();
        formData.append("image", image);
        try {
            const response = await fetch(app.dataset.imageUrl, {
                method: "POST",
                credentials: "same-origin",
                headers: {"X-CSRFToken": csrfInput ? csrfInput.value : ""},
                body: formData,
                signal: request.controller.signal,
            });
            const payload = await responsePayload(response);
            if (isCurrentRequest(request)) renderResults(payload);
        } catch (error) {
            if (error.name !== "AbortError" && isCurrentRequest(request)) {
                setStatus(error.message, "error");
            }
        } finally {
            finishRequest(request);
        }
    }

    textForm.addEventListener("submit", (event) => {
        event.preventDefault();
        const query = queryInput.value.trim();
        const source = pendingVoiceTranscript && query === pendingVoiceTranscript
            ? "voice"
            : "text";
        pendingVoiceTranscript = "";
        searchByText(source);
    });

    queryInput.addEventListener("input", () => {
        cancelActiveRequest();
        if (queryInput.value.trim() !== pendingVoiceTranscript) {
            pendingVoiceTranscript = "";
        }
    });

    document.querySelectorAll("[data-discovery-example]").forEach((button) => {
        button.addEventListener("click", () => {
            cancelActiveRequest();
            queryInput.value = button.dataset.discoveryExample || "";
            pendingVoiceTranscript = "";
            queryInput.focus();
        });
    });

    voiceButton.addEventListener("click", () => {
        if (!recognition) recognition = createRecognition();
        if (!recognition) {
            setStatus("当前浏览器不支持语音识别，请使用 Chrome、Edge 或直接打字查找。", "error");
            return;
        }
        try {
            recognition.start();
        } catch (error) {
            setStatus("语音识别正在运行，请说出您的需求。", "loading");
        }
    });

    if (app.dataset.imageEnabled === "true") {
        imageForm.addEventListener("submit", searchByImage);
    }
}());
