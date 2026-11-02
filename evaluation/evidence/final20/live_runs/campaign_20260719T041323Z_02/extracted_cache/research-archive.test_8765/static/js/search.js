document.documentElement.classList.add("search-ready");

const queryInput = document.querySelector("#search-query");
const parameters = new URLSearchParams(window.location.search);
if (queryInput && parameters.get("query")) {
  queryInput.value = parameters.get("query");
}

const applyButton = document.querySelector("#apply-filters");
const results = document.querySelector("#search-results");
const resultCount = document.querySelector("#result-count");

function escapeText(value) {
  const span = document.createElement("span");
  span.textContent = value;
  return span.innerHTML;
}

applyButton?.addEventListener("click", async () => {
  const query = queryInput.value.trim();
  if (!query) {
    queryInput.focus();
    return;
  }
  const requestParameters = new URLSearchParams({
    query,
    topic: document.querySelector("#topic-filter").value,
    year: document.querySelector("#year-filter").value,
    format: document.querySelector("#format-filter").value,
    sort: document.querySelector("#sort-filter").value,
  });
  applyButton.disabled = true;
  results.setAttribute("aria-busy", "true");
  try {
    const response = await fetch(`/api/search?${requestParameters}`);
    if (!response.ok) throw new Error(`Search request failed: ${response.status}`);
    const payload = await response.json();
    const heading = results.querySelector(".results-heading");
    results.replaceChildren(heading);
    for (const record of payload.results) {
      const article = document.createElement("article");
      article.className = "result-card";
      article.innerHTML = `
        <span class="result-type">${escapeText(record.format)} · ${escapeText(record.year)}</span>
        <h3>${escapeText(record.title)}</h3>
        <p>${escapeText(record.summary)}</p>`;
      results.append(article);
    }
    resultCount.textContent = `${payload.count} records`;
  } catch (error) {
    resultCount.textContent = "Search unavailable";
  } finally {
    results.removeAttribute("aria-busy");
    applyButton.disabled = false;
  }
});
