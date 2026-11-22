/**
 * waveform.js - Reusable ECG Waveform Modal and shared Chart.js config builder
 * for Patient and Doctor/Admin ECG views.
 * - Draws AI attention as a scatter-dot overlay on the same axes as the ECG line
 *   (doctor/admin only; patients never see attention).
 * - Fixes RTL minus-sign reversal on numeric axis ticks via LRM (\u200E).
 * - Always destroys any previous Chart instance before creating a new one.
 */
import { apiFetch, escapeHtml, formatDateTime } from "./api.js";
import { t } from "./i18n.js";

export const ATTENTION_DOT_THRESHOLD = 0.6;
export const ATTENTION_DOT_MIN_SPACING_SAMPLES = 8;

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
 * Wrap numeric tick labels with Left-to-Right Mark (\u200E) so negative signs
 * stay on the left (e.g. "-0.5" instead of "0.5-") in Arabic RTL mode.
 */
export function formatLtrTick(value, decimals = null, suffix = "") {
    const num = Number(value);
    if (!Number.isFinite(num)) return String(value ?? "");
    const text = decimals !== null ? num.toFixed(decimals) : String(Number(num.toFixed(4)));
    return `\u200E${text}${suffix}`;
}

/**
 * Build thinned scatter dot points lying on the ECG waveform where attention >= threshold.
 * - Verifies 1-to-1 alignment between samples[i] and attention[i].
 * - Normalizes attention into [0, 1] only if finite values fall outside [0, 1].
 * - Peak-priority thinning guarantees at least `minSpacing` samples between any two dots
 *   while keeping the highest-attention sample inside every run.
 */
export function buildAttentionDotPoints(
    samples,
    attention,
    timeValues,
    threshold = ATTENTION_DOT_THRESHOLD,
    minSpacing = ATTENTION_DOT_MIN_SPACING_SAMPLES
) {
    if (
        !Array.isArray(samples) ||
        !Array.isArray(attention) ||
        !Array.isArray(timeValues) ||
        samples.length === 0 ||
        samples.length !== attention.length ||
        samples.length !== timeValues.length
    ) {
        return [];
    }

    let minVal = Infinity;
    let maxVal = -Infinity;
    for (let i = 0; i < attention.length; i++) {
        const v = Number(attention[i]);
        if (Number.isFinite(v)) {
            if (v < minVal) minVal = v;
            if (v > maxVal) maxVal = v;
        }
    }
    if (!Number.isFinite(minVal) || !Number.isFinite(maxVal)) {
        return [];
    }

    const needsNormalize = minVal < -1e-6 || maxVal > 1.0 + 1e-6;
    const span = maxVal - minVal;

    const candidates = [];
    for (let i = 0; i < attention.length; i++) {
        const raw = Number(attention[i]);
        const yVal = Number(samples[i]);
        const xVal = Number(timeValues[i]);
        if (!Number.isFinite(raw) || !Number.isFinite(yVal) || !Number.isFinite(xVal)) continue;

        const normAtt = needsNormalize ? (span > 1e-8 ? (raw - minVal) / span : 0) : raw;
        if (normAtt >= threshold) {
            candidates.push({ index: i, x: xVal, y: yVal, attention: normAtt });
        }
    }

    if (!candidates.length) return [];

    // Peak-priority thinning: sort by attention descending, keep if >= minSpacing from all kept indices
    const byPriority = [...candidates].sort((a, b) =>
        b.attention !== a.attention ? b.attention - a.attention : a.index - b.index
    );

    const kept = [];
    const spacing = Math.max(1, Number(minSpacing) || 1);
    for (let k = 0; k < byPriority.length; k++) {
        const cand = byPriority[k];
        let tooClose = false;
        for (let j = 0; j < kept.length; j++) {
            if (Math.abs(cand.index - kept[j].index) < spacing) {
                tooClose = true;
                break;
            }
        }
        if (!tooClose) {
            kept.push(cand);
        }
    }

    kept.sort((a, b) => a.index - b.index);
    return kept.map((p) => ({ x: p.x, y: p.y, attention: p.attention }));
}

/**
 * Build a unified Chart.js configuration for stored ECG waveforms (history modal & doctor case review).
 */
export function buildEcgChartConfig({
    samples = [],
    attention = null,
    durationS = 10,
    isClinical = false,
    isLight = false,
    showYAxisTitle = true,
    showYAxisTicks = true,
} = {}) {
    const sigColor = isLight ? "#0891b2" : "#06b6d4";
    const gridColor = isLight ? "rgba(15,23,42,0.08)" : "rgba(255,255,255,0.08)";
    const tickColor = isLight ? "rgba(51,65,85,0.85)" : "rgba(180,190,205,0.85)";
    const dotBorder = isLight ? "rgba(255,255,255,0.95)" : "rgba(248,250,252,0.92)";

    const safeDuration = Number.isFinite(durationS) && durationS > 0 ? durationS : 10;
    const denom = Math.max(samples.length - 1, 1);
    const timeValues = samples.map((_, i) => (i / denom) * safeDuration);
    const ecgPoints = samples.map((y, i) => ({ x: timeValues[i], y }));

    const hasAttention =
        Boolean(isClinical) &&
        Array.isArray(attention) &&
        attention.length === samples.length &&
        samples.length > 0;

    const datasets = [
        {
            type: "line",
            label: t("legendEcg"),
            data: ecgPoints,
            borderColor: sigColor,
            backgroundColor: sigColor,
            borderWidth: 1.5,
            pointRadius: 0,
            pointHoverRadius: 3,
            pointStyle: "line",
            tension: 0.0,
            borderJoinStyle: "miter",
            fill: false,
            order: 2,
        },
    ];

    if (hasAttention) {
        const dotPoints = buildAttentionDotPoints(samples, attention, timeValues);
        datasets.push({
            type: "scatter",
            isAttentionDots: true,
            label: t("legendAttentionDots"),
            data: dotPoints,
            showLine: false,
            pointStyle: "circle",
            pointRadius: 3.5,
            pointHoverRadius: 5.5,
            backgroundColor: "rgba(239, 68, 68, 0.92)",
            borderColor: dotBorder,
            borderWidth: 1.25,
            order: 1,
        });
    }

    const plugins = {
        legend: {
            display: hasAttention,
            position: "top",
            align: "end",
            labels: {
                usePointStyle: true,
                color: tickColor,
                boxWidth: 10,
                boxHeight: 10,
                padding: 12,
                font: { size: 12 },
            },
        },
        tooltip: {
            enabled: true,
            mode: "nearest",
            intersect: false,
            callbacks: {
                title: (items) => {
                    if (!items || !items.length) return "";
                    const first = items[0];
                    if (first.dataset && first.dataset.isAttentionDots) {
                        return t("legendAttentionDots");
                    }
                    const xVal = first.parsed && Number.isFinite(first.parsed.x) ? first.parsed.x : 0;
                    return `${t("tooltipTime")}: ${formatLtrTick(xVal, 2, ` ${t("unitSecShort")}`)}`;
                },
                label: (ctx) => {
                    if (ctx.dataset && ctx.dataset.isAttentionDots) {
                        const raw = ctx.raw || {};
                        const xVal = Number.isFinite(raw.x) ? raw.x : ctx.parsed.x;
                        const attVal = Number.isFinite(raw.attention) ? raw.attention : 0;
                        return `${t("tooltipTime")}: ${formatLtrTick(
                            xVal,
                            2,
                            ` ${t("unitSecShort")}`
                        )}  •  ${t("tooltipAttention")}: ${formatLtrTick(attVal, 2)}`;
                    }
                    const yVal = ctx.parsed && Number.isFinite(ctx.parsed.y) ? ctx.parsed.y : 0;
                    return `${ctx.dataset.label || t("legendEcg")}: ${formatLtrTick(yVal, 2)}`;
                },
            },
        },
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

    return {
        type: "line",
        data: { datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            parsing: false,
            devicePixelRatio: Math.max(window.devicePixelRatio || 1, 2),
            plugins,
            scales: {
                x: {
                    type: "linear",
                    min: 0,
                    max: safeDuration,
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
                        callback: (value) => formatLtrTick(value, 1, "s"),
                    },
                },
                y: {
                    title: {
                        display: showYAxisTitle,
                        text: t("axisAmplitude"),
                        color: tickColor,
                        font: { size: 11 },
                    },
                    grid: { color: gridColor },
                    ticks: {
                        display: showYAxisTicks,
                        color: tickColor,
                        maxTicksLimit: 6,
                        callback: (value) => formatLtrTick(value),
                    },
                },
            },
        },
    };
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

        if (loadingEl) loadingEl.style.display = "none";
        if (chartBox) chartBox.style.display = "block";

        const canvas = document.getElementById("waveformModalCanvas");
        if (!canvas) return;

        _destroyWaveformChart();
        const isLight = document.body.classList.contains("light-theme");
        const config = buildEcgChartConfig({
            samples,
            attention: isClinical ? data.attention : null,
            durationS,
            isClinical,
            isLight,
            showYAxisTitle: true,
            showYAxisTicks: true,
        });
        _waveformChart = new Chart(canvas.getContext("2d"), config);
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
