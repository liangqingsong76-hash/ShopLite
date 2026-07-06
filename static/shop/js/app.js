(function () {
    "use strict";

    const searchInput = document.getElementById("searchInput");
    const searchDropdown = document.getElementById("searchDropdown");
    const searchForm = document.getElementById("searchForm");
    const searchHistory = document.getElementById("searchHistory");
    const searchSuggestGroup = document.getElementById("searchSuggestGroup");
    const searchSuggestResults = document.getElementById("searchSuggestResults");
    let suggestTimer = null;
    let selectedSuggestIndex = -1;

    function saveHistory(keyword) {
        if (!keyword) return;
        let history = localStorage.getItem("searchHistory");
        history = history ? JSON.parse(history) : [];
        history = history.filter((item) => item !== keyword);
        history.unshift(keyword);
        if (history.length > 10) history = history.slice(0, 10);
        localStorage.setItem("searchHistory", JSON.stringify(history));
    }

    function loadHistory() {
        const history = localStorage.getItem("searchHistory");
        const items = history ? JSON.parse(history) : [];
        if (!items.length) {
            searchHistory.innerHTML = '<div style="color:var(--muted);font-size:13px;padding:6px 10px;">暂无搜索记录</div>';
            return;
        }

        searchHistory.innerHTML = items.map((item) =>
            '<div class="search-history-item" onclick="fillSearch(\'' + item.replace(/'/g, "\\'") + '\')">' +
                "<span>🕐</span><span>" + item + "</span>" +
            "</div>"
        ).join("");
    }

    function showDropdown() {
        searchDropdown.classList.add("show");
        loadHistory();
    }

    function hideDropdown() {
        searchDropdown.classList.remove("show");
        selectedSuggestIndex = -1;
    }

    function fillSearch(keyword) {
        if (!searchInput || !searchForm) return;
        searchInput.value = keyword;
        saveHistory(keyword);
        searchForm.submit();
    }

    function clearHistory() {
        localStorage.removeItem("searchHistory");
        if (searchHistory) loadHistory();
    }

    function highlightMatch(text, query) {
        if (!query) return text;
        const idx = text.toLowerCase().indexOf(query.toLowerCase());
        if (idx < 0) return text;
        return text.substring(0, idx) + "<em>" + text.substring(idx, idx + query.length) + "</em>" + text.substring(idx + query.length);
    }

    function fetchSuggestions(q) {
        fetch("/api/search/suggest/?q=" + encodeURIComponent(q))
            .then((resp) => resp.json())
            .then((data) => {
                if (data.results && data.results.length > 0) {
                    searchSuggestGroup.style.display = "";
                    searchSuggestResults.innerHTML = data.results.map((p) =>
                        '<a class="search-suggest-item" href="/product/' + p.id + '/">' +
                            '<span class="search-suggest-img">' + (p.image ? '<img src="' + p.image + '" alt="">' : '<span class="suggest-placeholder"></span>') + "</span>" +
                            '<span class="search-suggest-info">' +
                                '<span class="search-suggest-name">' + highlightMatch(p.name, q) + "</span>" +
                                '<span class="search-suggest-price">¥' + p.price + "</span>" +
                            "</span></a>"
                    ).join("");
                } else {
                    searchSuggestGroup.style.display = "none";
                }
            })
            .catch(() => {
                searchSuggestGroup.style.display = "none";
            });
    }

    function toggleDropdown(dropdownId) {
        const dropdown = document.getElementById(dropdownId);
        if (!dropdown) return;

        document.querySelectorAll(".action-dropdown").forEach((item) => {
            if (item.id !== dropdownId) item.classList.remove("show");
        });
        dropdown.classList.toggle("show");
    }

    if (searchInput && searchDropdown && searchForm && searchHistory && searchSuggestGroup && searchSuggestResults) {
        searchInput.addEventListener("focus", showDropdown);
        searchInput.addEventListener("click", showDropdown);
        searchInput.addEventListener("input", function () {
            clearTimeout(suggestTimer);
            const q = this.value.trim();
            if (q.length >= 1) {
                suggestTimer = setTimeout(() => fetchSuggestions(q), 250);
            } else {
                searchSuggestGroup.style.display = "none";
                selectedSuggestIndex = -1;
            }
        });

        document.addEventListener("click", (event) => {
            if (!searchForm.contains(event.target)) hideDropdown();
        });

        searchForm.addEventListener("submit", () => {
            const keyword = searchInput.value.trim();
            if (keyword) saveHistory(keyword);
        });
    }

    document.addEventListener("click", (event) => {
        const headerActions = document.querySelector(".header-actions");
        if (headerActions && !headerActions.contains(event.target)) {
            document.querySelectorAll(".action-dropdown").forEach((item) => item.classList.remove("show"));
        }
    });

    window.fillSearch = fillSearch;
    window.clearHistory = clearHistory;
    window.toggleDropdown = toggleDropdown;
}());
