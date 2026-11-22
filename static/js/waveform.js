/**
 * waveform.js - Reusable ECG Waveform Modal for Patient and Doctor/Admin history views.
 * Fetches waveforms lazily from GET /api/history/<id>/waveform, renders Chart.js line chart
 * with time axis in seconds, supports doctor zoom/pan + AI attention overlay + Reset Zoom,
 * and destroys any previous Chart instance before creating a new one.
 */
import { apiFetch, escapeHtml, formatDateTime } from "./api.js";
import { t } from "./i18n.js";

let _waveformChart = null;
let _activeRequestToken = 0;

export function getWaveformModalChart() {
    return _waveformChart;
}

function _destroyWaveformChart() {
    if (_waveformChart) {
        _waveformChart.destroy();
        _waveformChart = null;
    }
}

export function resetWaveformModalZoom() {
    if (_waveformChart && typeof _waveformChart.resetZoom === "function") {
        _waveformChart.resetZoom();
    }
}

export function closeWaveformModal() {
    _activeRequestToken += 1;
    _destroyWaveformChart();
    const modal = document.getElementById("waveformModal");
    if (modal) modal.classList.remove("open");
}

/**
 * Open the ECG waveform modal for a history entry.
 * @param {object} entry - History row { id, prediction, status, bpm, rate_status, ts }
 * @param {object} options - { isClinical: boolean }
 */
export async function openWaveformModal(entry, { isClinical = false } = {}) {
    const modal = document.getElementById("waveformModal");
    const metaEl = document.getElementById("waveformModalMeta");
    const loadingEl = document.getElementById("waveformModalLoading");
    const errorEl = document.getElementById("waveformModalError");
    const chartBox = document.getElementById("waveformModalChartBox");
    const legendEl = document.getElementById("waveformModalLegend");
    const resetBtn = document.getElementById("btnResetWaveformZoom");

    if (!modal || !entry || !entry.id) return;

    const reqToken = ++_activeRequestToken;
    _destroyWaveformChart();

    const predLabel =
        entry.prediction && entry.prediction !== "N/A"
            ? `${entry.status} • ${entry.prediction}`
            : entry.status || "--";
    if (metaEl) {
        metaEl.innerHTML = `
            <span class="badge badge-info">${escapeHtml(predLabel)}</span>
            <span><strong>${escapeHtml(entry.bpm ?? "--")} BPM</strong> (${escapeHtml(
            entry.rate_status || "--"
        )})</span>
            <span style="color:var(--white-muted);">⏱ ${escapeHtml(formatDateTime(entry.ts))}</span>
        `;
    }

    if (loadingEl) {
        loadingEl.textContent = t("waveformLoading");
        loadingEl.style.display = "block";
    }
    if (errorEl) {
        errorEl.textContent = "";
        errorEl.style.display = "none";
    }
    if (chartBox) chartBox.style.display = "none";
    if (legendEl) legendEl.style.display = "none";
    if (resetBtn) resetBtn.style.display = isClinical ? "inline-flex" : "none";

    modal.classList.add("open");

    try {
        const maxPoints = isClinical ? 3000 : 1500;
        const data = await apiFetch(
            `/api/history/${encodeURIComponent(entry.id)}/waveform?max_points=${maxPoints}`
        );
        if (reqToken !== _activeRequestToken) return;

        const samples = Array.isArray(data.samples) ? data.samples : [];
        if (!samples.length) {
            throw new Error(t("noWaveformStored"));
        }

        const durationS =
            typeof data.duration_s === "number" && data.duration_s > 0
                ? data.duration_s
                : samples.length / (data.sample_rate_hz || 300);
        const denom = Math.max(samples.length - 1, 1);
        const timeLabels = samples.map((_, i) => Number(((i / denom) * durationS).toFixed(3)));

        const isLight = document.body.classList.contains("light-theme");
        const sigColor = isLight ? "#0891b2" : "#06b6d4";
        const gridColor = isLight ? "rgba(15,23,42,0.08)" : "rgba(255,255,255,0.08)";
        const tickColor = isLight ? "rgba(51,65,85,0.8)" : "rgba(180,190,205,0.8)";

        const hasAttention =
            isClinical &&
            Array.isArray(data.attention) &&
            data.attention.length === samples.length;

        const datasets = [
            {
                label: t("legendEcg"),
                data: samples,
                yAxisID: "y",
                borderColor: sigColor,
                borderWidth: 1.5,
                pointRadius: 0,
                tension: 0.0,
                borderJoinStyle: "miter",
                fill: false,
                order: 1,
            },
        ];

        if (hasAttention) {
            datasets.push({
                label: t("legendAttention"),
                data: data.attention,
                yAxisID: "yAtt",
                borderColor: "rgba(239, 68, 68, 0.55)",
                backgroundColor: "rgba(239, 68, 68, 0.22)",
                borderWidth: 1,
                pointRadius: 0,
                tension: 0.15,
                fill: true,
                order: 2,
            });
        }

        if (loadingEl) loadingEl.style.display = "none";
        if (chartBox) chartBox.style.display = "block";
        if (legendEl) {
            legendEl.style.display = hasAttention ? "flex" : "none";
        }

        const canvas = document.getElementById("waveformModalCanvas");
        if (!canvas) return;

        _destroyWaveformChart();
        const scales = {
            x: {
                title: {
                    display: true,
                    text: t("axisTimeSec"),
                    color: tickColor,
                    font: { size: 11 },
                },
                grid: { color: gridColor },
                ticks: {
                    color: tickColor,
                    maxTicksLimit: 11,
                    callback: function (value) {
                        const lbl = this.getLabelForValue(value);
                        return `${Number(lbl).toFixed(1)}s`;
                    },
                },
            },
            y: {
                title: {
                    display: true,
                    text: t("axisAmplitude"),
                    color: tickColor,
                    font: { size: 11 },
                },
                grid: { color: gridColor },
                ticks: { color: tickColor, maxTicksLimit: 6 },
            },
        };

        if (hasAttention) {
            scales.yAtt = {
                position: "right",
                min: 0,
                max: 1,
                display: false,
                grid: { drawOnChartArea: false },
            };
        }

        const plugins = {
            legend: { display: false },
            tooltip: { enabled: true, mode: "index", intersect: false },
        };

        if (isClinical) {
            plugins.zoom = {
                pan: { enabled: true, mode: "x" },
                zoom: {
                    wheel: { enabled: true },
                    pinch: { enabled: true },
                    mode: "x",
                },
            };
        }

        _waveformChart = new Chart(canvas.getContext("2d"), {
            type: "line",
            data: {
                labels: timeLabels,
                datasets,
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                devicePixelRatio: Math.max(window.devicePixelRatio || 1, 2),
                plugins,
                scales,
            },
        });
    } catch (err) {
        if (reqToken !== _activeRequestToken) return;
        if (loadingEl) loadingEl.style.display = "none";
        if (chartBox) chartBox.style.display = "none";
        if (errorEl) {
            errorEl.textContent = (err && err.message) || t("waveformError");
            errorEl.style.display = "block";
        }
    }
}
