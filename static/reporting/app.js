function getCookie(name) {
  const found = document.cookie.split(";").map((v) => v.trim()).find((v) => v.startsWith(`${name}=`));
  return found ? decodeURIComponent(found.split("=").slice(1).join("=")) : "";
}

function getCsrfToken() {
  const formToken = document.querySelector('input[name="csrfmiddlewaretoken"]');
  return formToken?.value || getCookie("csrftoken");
}

async function postJson(url, payload = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-CSRFToken": getCsrfToken()},
    body: JSON.stringify(payload),
    credentials: "same-origin",
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const fallback = response.status === 403
      ? "La session de sécurité a expiré. Rechargez la page puis réessayez."
      : "La demande a échoué.";
    throw new Error(data.error || data.message || fallback);
  }
  return data;
}

async function pollSync(id, banner) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    const response = await fetch(`/api/v1/syncs/${id}`, {credentials: "same-origin"});
    const data = await response.json();
    if (banner) banner.querySelector("div").textContent = data.message || `Synchronisation ${data.status}`;
    if (["success", "partial", "failed"].includes(data.status)) return data;
  }
  throw new Error("La synchronisation continue en arrière-plan.");
}

document.querySelectorAll("[data-sync-now]").forEach((button) => {
  button.addEventListener("click", async () => {
    const initial = button.textContent;
    const banner = document.querySelector("[data-sync-banner]");
    button.disabled = true;
    button.textContent = "Démarrage…";
    try {
      const data = await postJson(button.dataset.api, {
        start: button.dataset.startDate || button.dataset.endDate,
        end: button.dataset.endDate,
        levels: ["account", "campaign", "adset", "ad"],
      });
      button.textContent = "Synchronisation…";
      const completed = await pollSync(data.id, banner);
      if (completed.status === "failed") {
        throw new Error(completed.message || "La synchronisation a échoué.");
      }
      if (button.dataset.showLatest === "true") {
        window.location.assign("/");
      } else {
        window.location.reload();
      }
    } catch (error) {
      button.textContent = "Échec — réessayer";
      if (banner) banner.querySelector("div").textContent = error.message;
      setTimeout(() => { button.textContent = initial; button.disabled = false; }, 4000);
    }
  });
});

const testButton = document.querySelector("[data-test-meta]");
if (testButton) {
  testButton.addEventListener("click", async () => {
    const result = document.querySelector("[data-test-result]");
    testButton.disabled = true;
    result.textContent = "Test en cours…";
    try {
      const data = await postJson(testButton.dataset.api);
      result.className = "inline-result success";
      result.textContent = data.message;
    } catch (error) {
      result.className = "inline-result error";
      result.textContent = error.message;
    } finally {
      testButton.disabled = false;
    }
  });
}

const actionInput = document.getElementById("id_mapping-action_type");
if (actionInput) actionInput.setAttribute("list", "actions-list");

function initializeTrendChart() {
  const canvas = document.getElementById("trend-chart");
  const source = document.getElementById("trend-data");
  if (!canvas || !source || typeof Chart === "undefined") return;
  const data = JSON.parse(source.textContent || "[]");
  new Chart(canvas, {
    data: {
      labels: data.map((row) => row.label),
      datasets: [
        {type: "bar", label: "Dépenses", data: data.map((row) => row.spend), backgroundColor: "rgba(10,123,97,.20)", borderColor: "#0a7b61", borderWidth: 1, borderRadius: 5, yAxisID: "y"},
        {type: "line", label: "Résultats", data: data.map((row) => row.results), borderColor: "#f26b51", backgroundColor: "#f26b51", borderWidth: 2.5, pointRadius: 2.5, tension: .32, spanGaps: false, yAxisID: "y1"},
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {mode: "index", intersect: false},
      plugins: {legend: {display: false}, tooltip: {backgroundColor: "#14302a", padding: 10}},
      scales: {
        x: {grid: {display: false}, ticks: {color: "#7d8a86", maxRotation: 0}},
        y: {beginAtZero: true, grid: {color: "#edf1ef"}, ticks: {color: "#7d8a86"}},
        y1: {beginAtZero: true, position: "right", grid: {drawOnChartArea: false}, ticks: {color: "#f26b51"}},
      },
    },
  });
}

window.addEventListener("load", initializeTrendChart);
