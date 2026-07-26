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

    function textNode(tag, className, value) {
        const element = document.createElement(tag);
        if (className) element.className = className;
        element.textContent = value;
        return element;
    }

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
            searchHistory.replaceChildren(textNode("div", "search-empty", "暂无搜索记录"));
            return;
        }

        const nodes = items.map((item) => {
            const row = document.createElement("button");
            row.type = "button";
            row.className = "search-history-item";
            row.append(textNode("span", "", "🕐"), textNode("span", "", item));
            row.addEventListener("click", () => fillSearch(item));
            return row;
        });
        searchHistory.replaceChildren(...nodes);
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

    function fetchSuggestions(q) {
        fetch("/api/search/suggest/?q=" + encodeURIComponent(q))
            .then((resp) => resp.json())
            .then((data) => {
                if (data.results && data.results.length > 0) {
                    searchSuggestGroup.style.display = "";
                    const nodes = data.results.map((p) => {
                        const link = document.createElement("a");
                        link.className = "search-suggest-item";
                        link.href = "/product/" + encodeURIComponent(p.id) + "/";
                        const imageWrap = document.createElement("span");
                        imageWrap.className = "search-suggest-img";
                        if (p.image) {
                            const img = document.createElement("img");
                            img.src = p.image;
                            img.alt = "";
                            imageWrap.appendChild(img);
                        } else {
                            imageWrap.appendChild(textNode("span", "suggest-placeholder", ""));
                        }
                        const info = document.createElement("span");
                        info.className = "search-suggest-info";
                        info.append(textNode("span", "search-suggest-name", p.name), textNode("span", "search-suggest-price", "¥" + p.price));
                        link.append(imageWrap, info);
                        return link;
                    });
                    searchSuggestResults.replaceChildren(...nodes);
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
