document.documentElement.classList.add("dataset-ready");

const previewButton = document.querySelector("#load-preview");
const preview = document.querySelector("#dataset-preview");

previewButton?.addEventListener("click", async () => {
  const parameters = new URLSearchParams({
    region: document.querySelector("#region-select").value,
    measure: document.querySelector("#measure-select").value,
  });
  previewButton.disabled = true;
  preview.setAttribute("aria-busy", "true");
  try {
    const response = await fetch(`/api/datasets/regional-observations?${parameters}`);
    if (!response.ok) throw new Error(`Preview request failed: ${response.status}`);
    const payload = await response.json();
    const table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>Date</th><th>Region</th><th>Measure</th><th>Value</th></tr></thead>";
    const body = document.createElement("tbody");
    for (const row of payload.rows) {
      const record = document.createElement("tr");
      record.innerHTML = `<td>${row.date}</td><td>${row.region}</td><td>${row.measure}</td><td>${row.value}</td>`;
      body.append(record);
    }
    table.append(body);
    preview.replaceChildren(table);
  } catch (error) {
    preview.textContent = "The selected preview could not be loaded.";
  } finally {
    preview.removeAttribute("aria-busy");
    previewButton.disabled = false;
  }
});
