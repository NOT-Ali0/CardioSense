/**
 * ble.js - Web Bluetooth GATT streaming and live patient ECG oscilloscope Chart.js.
 */
import { showToast } from "./api.js";
import { t } from "./i18n.js";

const SERVICE_UUID = "4fa2c232-e1e3-4b10-a1e1-30a629b61a7b";
const CHARACTERISTIC_UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8";
const MAX_POINTS = 600;

let bleDevice = null;
let bleCharacteristic = null;
let bleStreamBuffer = "";
let rawSignalBuffer = [];
let isAcquisitionComplete = false;
let isBleConnected = false;
let startTime = null;
let countdownInterval = null;
let _renderQueued = false;
let ecgChart = null;
let chartData = null;

export function getRawSignalBuffer() {
    return rawSignalBuffer;
}

export function getEcgChart() {
    return ecgChart;
}

export function initPatientEcgChart() {
    const canvas = document.getElementById("ecgChart");
    if (!canvas || ecgChart) return;
    const ctx = canvas.getContext("2d");

    chartData = {
        labels: Array.from({ length: MAX_POINTS }, () => ""),
        datasets: [
            {
                data: Array(MAX_POINTS).fill(null),
                borderColor: "#06b6d4",
                borderWidth: 2,
                pointRadius: 0,
                tension: 0.0,
                fill: false,
                borderCapStyle: "square",
                borderJoinStyle: "miter",
            },
        ],
    };

    ecgChart = new Chart(ctx, {
        type: "line",
        data: chartData,
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            devicePixelRatio: Math.max(window.devicePixelRatio || 1, 2),
            plugins: { legend: { display: false }, tooltip: { enabled: false } },
            scales: {
                x: { display: false },
                y: { display: false, suggestedMin: 0, suggestedMax: 4095 },
            },
            layout: { padding: { top: 20, bottom: 20, left: 10, right: 10 } },
            events: [],
        },
    });
}

function _flushChart() {
    _renderQueued = false;
    if (ecgChart) ecgChart.update("none");
}

export function processDataChunk(values) {
    if (isAcquisitionComplete || !values.length) return;
    if (startTime === null) {
        startTime = Date.now();
        const wrap = document.getElementById("progressBarWrapper");
        if (wrap) wrap.style.display = "block";
        startAcquisitionTimer();
    }

    rawSignalBuffer.push(...values);
    if (chartData) {
        const targetData = chartData.datasets[0].data;
        targetData.splice(0, values.length);
        targetData.push(...values);
        if (!_renderQueued) {
            _renderQueued = true;
            requestAnimationFrame(_flushChart);
        }
    }
}

function startAcquisitionTimer() {
    if (countdownInterval) clearInterval(countdownInterval);
    countdownInterval = setInterval(() => {
        const progress = (rawSignalBuffer.length / 3000) * 100;
        const bar = document.getElementById("signalProgressBar");
        if (bar) bar.style.width = `${Math.min(progress, 100)}%`;
        if (rawSignalBuffer.length >= 3000) {
            clearInterval(countdownInterval);
            countdownInterval = null;
            rawSignalBuffer = rawSignalBuffer.slice(0, 3000);
            executeSignalCompletion();
        }
    }, 30);
}

function executeSignalCompletion() {
    isAcquisitionComplete = true;
    const wrap = document.getElementById("progressBarWrapper");
    const diagBtn = document.getElementById("diagnoseBtn");
    const resetBtn = document.getElementById("resetBtn");
    const aiLbl = document.getElementById("aiDiagnosisLabel");
    if (wrap) wrap.style.display = "none";
    if (diagBtn) diagBtn.style.display = "inline-flex";
    if (resetBtn) resetBtn.style.display = "inline-flex";
    if (aiLbl) aiLbl.textContent = "DATA ACQUISITION COMPLETE • READY FOR INFERENCE";
}

export function resetAcquisitionState() {
    isAcquisitionComplete = false;
    rawSignalBuffer = [];
    startTime = null;
    bleStreamBuffer = "";
    if (countdownInterval) {
        clearInterval(countdownInterval);
        countdownInterval = null;
    }
    if (chartData && ecgChart) {
        chartData.datasets[0].data.fill(null);
        ecgChart.update("none");
    }
}

export function updateBleStatusLabel() {
    const lbl = document.getElementById("statusLabel");
    if (!lbl) return;
    lbl.textContent = isBleConnected ? t("statusBnd") : t("statusDisc");
}

function updateBleButtons(connected) {
    isBleConnected = connected;
    const connectBtn = document.getElementById("connectBtn");
    const disconnectBtn = document.getElementById("disconnectBtn");
    const statusBadge = document.getElementById("statusLabel");
    if (connectBtn) connectBtn.style.display = connected ? "none" : "inline-flex";
    if (disconnectBtn) disconnectBtn.style.display = connected ? "inline-flex" : "none";
    if (statusBadge) statusBadge.className = connected ? "status-badge status-connected" : "status-badge";
    updateBleStatusLabel();
}

export async function connectBluetooth(onResetCallback) {
    try {
        if (!navigator.bluetooth) {
            showToast("Web Bluetooth is not supported in this browser.", "error");
            return;
        }
        bleDevice = await navigator.bluetooth.requestDevice({ filters: [{ services: [SERVICE_UUID] }] });
        const server = await bleDevice.gatt.connect();
        const service = await server.getPrimaryService(SERVICE_UUID);
        bleCharacteristic = await service.getCharacteristic(CHARACTERISTIC_UUID);
        await bleCharacteristic.startNotifications();
        updateBleButtons(true);
        if (onResetCallback) onResetCallback();

        bleStreamBuffer = "";
        bleCharacteristic.addEventListener("characteristicvaluechanged", (event) => {
            const chunkStr = new TextDecoder("utf-8").decode(event.target.value);
            bleStreamBuffer += chunkStr;
            const lines = bleStreamBuffer.split("\n");
            bleStreamBuffer = lines.pop();
            const validValues = [];
            for (let i = 0; i < lines.length; i++) {
                const val = parseFloat(lines[i].trim());
                if (!Number.isNaN(val)) validValues.push(val);
            }
            processDataChunk(validValues);
        });
    } catch (error) {
        updateBleButtons(false);
        showToast("Bluetooth Error: " + error.message, "error");
    }
}

export function disconnectBluetooth(onResetCallback) {
    if (bleDevice && bleDevice.gatt && bleDevice.gatt.connected) {
        bleDevice.gatt.disconnect();
    }
    updateBleButtons(false);
    if (onResetCallback) onResetCallback();
}
