document.documentElement.classList.add("js-ready");

document.addEventListener(
  "click",
  async (event) => {
    const link = event.target.closest("a[download]");
    if (!link || link.dataset.cacheTransfer === "1") return;
    event.preventDefault();

    const response = await fetch(link.href, {
      cache: "default",
      credentials: "same-origin",
    });
    if (!response.ok) return;

    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const transfer = document.createElement("a");
    transfer.href = objectUrl;
    transfer.download =
      link.getAttribute("download") || new URL(link.href).pathname.split("/").pop();
    transfer.dataset.cacheTransfer = "1";
    transfer.hidden = true;
    document.body.append(transfer);
    transfer.click();
    transfer.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  },
  { capture: true },
);
