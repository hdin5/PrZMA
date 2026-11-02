document.documentElement.classList.add("map-ready");

const detail = document.querySelector("#map-detail");
let sites = [];

async function loadMapData() {
  const response = await fetch("/api/map/observations");
  if (!response.ok) throw new Error(`Map request failed: ${response.status}`);
  const payload = await response.json();
  sites = payload.sites;
  detail.innerHTML = `<b>${sites.length} observation sites</b><span>Select a site to inspect the current series.</span>`;
}

document.querySelectorAll(".map-point").forEach((point, index) => {
  point.addEventListener("click", () => {
    const site = sites[index];
    if (!site) return;
    detail.innerHTML = `<b>${site.name}</b><span>Latest recorded value: ${site.latest}</span>`;
  });
});

loadMapData().catch(() => {
  detail.innerHTML = "<b>Map unavailable</b><span>Observation data could not be loaded.</span>";
});
