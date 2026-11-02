document.documentElement.classList.add("report-ready");

const reportButton = document.querySelector("#open-report");
const reportViewer = document.querySelector("#report-viewer");

reportButton?.addEventListener("click", () => {
  if (reportViewer.querySelector("iframe")) return;
  const frame = document.createElement("iframe");
  frame.title = "Preserving web evidence report";
  frame.src = "/documents/web-evidence.pdf";
  reportViewer.append(frame);
  reportViewer.hidden = false;
  reportButton.disabled = true;
});
