document.documentElement.classList.add("collections-ready");

const metadataButton = document.querySelector("#load-metadata");
const metadataPanel = document.querySelector("#collection-metadata");

metadataButton?.addEventListener("click", async () => {
  metadataButton.disabled = true;
  metadataPanel.setAttribute("aria-busy", "true");
  try {
    const response = await fetch("/api/collections/environmental-signals");
    if (!response.ok) throw new Error(`Metadata request failed: ${response.status}`);
    const metadata = await response.json();
    metadataPanel.innerHTML = `
      <dl>
        <div><dt>Curator</dt><dd>${metadata.curator}</dd></div>
        <div><dt>Coverage</dt><dd>${metadata.coverage}</dd></div>
        <div><dt>Methods</dt><dd>${metadata.methods.join(", ")}</dd></div>
      </dl>`;
  } catch (error) {
    metadataPanel.textContent = "Collection details could not be loaded.";
    metadataButton.disabled = false;
  } finally {
    metadataPanel.removeAttribute("aria-busy");
  }
});

let galleryArmed = false;
const galleryImages = [...document.querySelectorAll(".lazy-gallery img[data-src]")];
const galleryObserver = new IntersectionObserver(
  (entries, observer) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const image = entry.target;
      const revealImage = () => image.classList.add("loaded");
      image.addEventListener("load", revealImage, { once: true });
      image.src = image.dataset.src;
      image.removeAttribute("data-src");
      if (image.complete && image.naturalWidth > 0) revealImage();
      observer.unobserve(image);
    }
  },
  { rootMargin: "120px 0px" },
);

window.addEventListener(
  "scroll",
  () => {
    if (galleryArmed) return;
    galleryArmed = true;
    galleryImages.forEach((image) => galleryObserver.observe(image));
  },
  { passive: true },
);

const mapButton = document.querySelector("#open-map");
const mapFrame = document.querySelector("#map-frame");
mapButton?.addEventListener("click", () => {
  if (mapFrame.querySelector("iframe")) return;
  const frame = document.createElement("iframe");
  frame.title = "Regional observation map";
  frame.loading = "eager";
  frame.src = "/embed/map";
  mapFrame.append(frame);
  mapFrame.hidden = false;
  mapButton.disabled = true;
});
