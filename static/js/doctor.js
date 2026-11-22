/**
 * doctor.js - Professional Doctor Dashboard:
 * - KPI strip (Pending, Urgent, My Patients, Reviewed Today)
 * - Prioritized Case Queue with search & filters (Active / Archive)
 * - Case Review workspace (zoomable ECG + XAI overlay, AI attention note,
 *   Patient Profile side panel, clinical decision panel)
 * - My Patients list with last test, status, inline BPM trend sparkline
 * - Add Patient modal & Edit Clinical Profile modal
 */
import { apiFetch, escapeHtml, formatDateTime, showToast } from "./api.js";
import { t, getLang } from "./i18n.js";
import { openWaveformModal } from "./waveform.js";

let _docChart = null;
let _sparklineCharts = [];
let _selectedCaseId = null;
let _selectedCasePatientId = null;
let _attentionPollInterval = null;
let _searchDebounce = null;

export function getDoctorChart() {
    return _docChart;
}

export function stopDoctorPolling() {
    if (_attentionPollInterval) {
        clearInterval(_attentionPollInterval);
        _attentionPollInterval = null;
    }
}

export function initDoctorChart() {
    const canvas = document.getElementById("doctorEcgChart");
    if (!canvas || _docChart) return;
    _docChart = new Chart(canvas.getContext("2d"), {
        type: "line",
        data: {
            labels: Array.from({ length: 3000 }, (_, i) => i),
            datasets: [
                {
                    label: t("legendEcg"),
                    data: [],
                    yAxisID: "y",
                    borderColor: "#06b6d4",
                    borderWidth: 1.5,
                    pointRadius: 0,
                    tension: 0.0,
                    borderJoinStyle: "miter",
                    fill: false,
                    order: 1,
                },
                {
                    label: t("legendAttention"),
                    data: [],
                    yAxisID: "yAtt",
                    borderColor: "rgba(239, 68, 68, 0.55)",
                    backgroundColor: "rgba(239, 68, 68, 0.22)",
                    borderWidth: 1,
                    pointRadius: 0,
                    tension: 0.15,
                    fill: true,
                    order: 2,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            devicePixelRatio: Math.max(window.devicePixelRatio || 1, 2),
            plugins: {
                legend: { display: false },
                zoom: {
                    pan: { enabled: true, mode: "x" },
                    zoom: { wheel: { enabled: true }, pinch: { enabled: true }, mode: "x" },
                },
            },
            scales: {
                x: {
                    grid: { color: "rgba(255,255,255,0.08)" },
                    ticks: {
                        color: "rgba(150,150,150,0.7)",
                        maxTicksLimit: 11,
                        callback: (value) => (value / 300).toFixed(1) + "s",
                    },
                },
                y: { grid: { color: "rgba(255,255,255,0.05)" }, ticks: { display: false } },
                yAtt: {
                    position: "right",
                    min: 0,
                    max: 1,
                    display: false,
                    grid: { drawOnChartArea: false },
                },
            },
        },
    });
}

export function resetDoctorChartZoom() {
    if (_docChart && typeof _docChart.resetZoom === "function") {
        _docChart.resetZoom();
    }
}

export async function initDoctorDashboard() {
    initDoctorChart();
    await Promise.all([fetchDoctorKpis(), fetchDoctorCases(), fetchDoctorPatients()]);
}

export function switchDoctorTab(tabId) {
    document.querySelectorAll("#doctorView .tab-btn").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.tab === tabId);
    });
    document.querySelectorAll("#doctorView .tab-content").forEach((panel) => {
        panel.classList.toggle("active", panel.id === tabId);
    });
    if (tabId === "docPatientsTab") {
        fetchDoctorPatients();
    } else {
        fetchDoctorCases();
    }
}

export async function fetchDoctorKpis() {
    try {
        const data = await apiFetch("/api/doctor/dashboard");
        const k = data.kpis || {};
        document.getElementById("kpi_pending").textContent = k.pending_cases ?? 0;
        document.getElementById("kpi_urgent").textContent = k.urgent_cases ?? 0;
        document.getElementById("kpi_patients").textContent = k.my_patients ?? 0;
        document.getElementById("kpi_reviewed_today").textContent = k.reviewed_today ?? 0;
    } catch {
        // handled by apiFetch
    }
}

export function onDoctorFilterChange() {
    if (_searchDebounce) clearTimeout(_searchDebounce);
    _searchDebounce = setTimeout(() => fetchDoctorCases(), 200);
}

export async function fetchDoctorCases() {
    try {
        const queueType = document.getElementById("docQueueType")?.value || "active";
        const isArchive = queueType === "archived";
        const search = document.getElementById("docCaseSearch")?.value.trim() || "";
        const priority = document.getElementById("docPriorityFilter")?.value || "";
        const rawPred = document.getElementById("docPredFilter")?.value || "";

        const params = new URLSearchParams();
        if (search) params.set("search", search);
        if (!isArchive && priority) params.set("priority", priority);
        if (!isArchive && rawPred) params.set("raw_prediction", rawPred);

        const endpoint = isArchive ? "/api/doctor/archive" : "/api/doctor/cases";
        const cases = await apiFetch(`${endpoint}?${params.toString()}`);

        const container = document.getElementById("casesContainer");
        if (!cases.length) {
            container.innerHTML = `<p style="color:var(--white-muted); padding:28px; font-size:13px; text-align:center;">🟢 ${escapeHtml(
                t("queueClear")
            )}</p>`;
            return;
        }

        container.innerHTML = cases
            .map((c) => {
                const prioClass =
                    c.priority === "URGENT"
                        ? "badge-urgent"
                        : c.priority === "HIGH"
                        ? "badge-high"
                        : "badge-normal";
                const predClass = c.raw_prediction === "AFIB" ? "badge-danger" : "badge-success";
                const selectedClass = c.id === _selectedCaseId ? "selected" : "";
                return `<div class="case-item ${selectedClass}" data-case-id="${escapeHtml(c.id)}">
                    <div class="case-item-top">
                        <strong>${escapeHtml(c.patient_name)}</strong>
                        <span class="badge ${prioClass}">${escapeHtml(c.priority)}</span>
                    </div>
                    <div class="case-item-meta">
                        <span class="badge ${predClass}">${escapeHtml(c.raw_prediction)} (${(
                    c.confidence * 100
                ).toFixed(0)}%)</span>
                        <span>${escapeHtml(c.bpm)} BPM</span>
                        <span>${escapeHtml(formatDateTime(c.created_at))}</span>
                    </div>
                    ${
                        c.complaint
                            ? `<div style="font-size:11px; color:var(--white-muted); margin-top:6px;">${escapeHtml(
                                  c.complaint
                              )}</div>`
                            : ""
                    }
                </div>`;
            })
            .join("");

        container.querySelectorAll(".case-item").forEach((el) => {
            el.addEventListener("click", () => selectCase(el.dataset.caseId, isArchive));
        });
    } catch (err) {
        showToast(err.message, "error");
    }
}

async function selectCase(caseId, isArchive) {
    stopDoctorPolling();
    _selectedCaseId = caseId;
    document.querySelectorAll("#casesContainer .case-item").forEach((el) => {
        el.classList.toggle("selected", el.dataset.caseId === caseId);
    });

    try {
        const c = await apiFetch(`/api/doctor/cases/${encodeURIComponent(caseId)}`);
        _selectedCasePatientId = c.patient_id;
        renderCaseDetail(c, isArchive);

        if (!isArchive && c.ai_attention_note && c.ai_attention_note.includes("جاري")) {
            _attentionPollInterval = setInterval(async () => {
                try {
                    const updated = await apiFetch(`/api/doctor/cases/${encodeURIComponent(caseId)}`);
                    if (updated.ai_attention_note && !updated.ai_attention_note.includes("جاري")) {
                        document.getElementById("doc_attention_note").textContent = updated.ai_attention_note;
                        stopDoctorPolling();
                    }
                } catch {
                    stopDoctorPolling();
                }
            }, 3000);
        }
    } catch (err) {
        showToast(err.message, "error");
    }
}

function renderCaseDetail(c, isArchive) {
    const genderLabel = c.gender === "M" ? t("optMale") : c.gender === "F" ? t("optFemale") : "--";
    document.getElementById("doc_p_name").textContent = c.patient_name || "--";
    document.getElementById("doc_p_age").textContent = c.age ?? "--";
    document.getElementById("doc_p_gender").textContent = genderLabel;
    document.getElementById("doc_p_bpm").textContent = `${c.bpm ?? "--"} BPM (${c.rate_status || "--"})`;
    document.getElementById("doc_p_pred").textContent = `${c.raw_prediction} (${(c.confidence * 100).toFixed(1)}%)`;
    document.getElementById("doc_complaint_text").textContent = c.complaint || t("noData");
    document.getElementById("doc_chronic_text").textContent = c.chronic || t("noData");

    const noteSec = document.getElementById("docAttentionNoteSection");
    if (c.ai_attention_note) {
        document.getElementById("doc_attention_note").textContent = c.ai_attention_note;
        noteSec.style.display = "block";
    } else {
        noteSec.style.display = "none";
    }

    // Patient Profile Side Panel
    const prof = c.patient_profile || {};
    const side = document.getElementById("docPatientSidePanel");
    side.innerHTML = `
        <div class="profile-kv"><span>${escapeHtml(t("lblFullName"))}</span><strong>${escapeHtml(c.patient_name)}</strong></div>
        <div class="profile-kv"><span>${escapeHtml(t("lblAge"))} / ${escapeHtml(t("lblGender"))}</span><strong>${escapeHtml(
        prof.age ?? c.age
    )} / ${escapeHtml(genderLabel)}</strong></div>
        <div class="profile-kv"><span>${escapeHtml(t("lblPhone"))}</span><span>${escapeHtml(prof.phone || t("noData"))}</span></div>
        <div class="profile-kv"><span>${escapeHtml(t("lblEmergencyName"))}</span><span>${escapeHtml(
        prof.emergency_contact_name || t("noData")
    )}</span></div>
        <div class="profile-kv"><span>${escapeHtml(t("lblEmergencyPhone"))}</span><span>${escapeHtml(
        prof.emergency_contact_phone || t("noData")
    )}</span></div>
        <div class="profile-kv"><span>${escapeHtml(t("lblChronic"))}</span><span>${escapeHtml(
        prof.chronic_conditions && prof.chronic_conditions.length
            ? prof.chronic_conditions.join("، ")
            : t("noData")
    )}</span></div>
        <div style="margin-top:10px; font-size:12px; color:var(--white-muted);">${escapeHtml(
            t("lblClinicalNotes")
        )}:</div>
        <div style="font-size:13px; margin-top:4px; margin-bottom:12px;">${escapeHtml(
            prof.clinical_notes || t("noData")
        )}</div>
        <div style="display:flex; gap:8px; flex-wrap:wrap;">
            <button class="btn btn-secondary btn-sm" id="btnSideEditClinical">${escapeHtml(
                t("btnEditClinical")
            )}</button>
            <button class="btn btn-secondary btn-sm" id="btnSideViewHistory">${escapeHtml(
                t("btnViewHistory")
            )}</button>
        </div>
    `;
    document.getElementById("btnSideEditClinical").onclick = () => openEditClinicalModal(c.patient_id);
    document.getElementById("btnSideViewHistory").onclick = () => openPatientHistoryModal(c.patient_id, c.patient_name);

    // Decision panel state
    const decBox = document.getElementById("docDecisionBox");
    const disabled = isArchive || c.status !== "ESCALATED";
    decBox.style.opacity = disabled ? "0.5" : "1";
    document.getElementById("doc_notes").value = c.doctor_note || "";
    document.getElementById("doc_notes").disabled = disabled;
    ["btnActionNoisy", "btnActionHealthy", "btnActionInfected"].forEach((id) => {
        document.getElementById(id).disabled = disabled;
    });

    // ECG Waveform + semi-transparent AI Attention overlay
    const legendEl = document.getElementById("docCaseLegend");
    if (_docChart) {
        const sig = Array.isArray(c.signal) ? c.signal : [];
        const sr = c.sample_rate_hz || 300;
        _docChart.data.labels = Array.from({ length: sig.length }, (_, i) => i);
        _docChart.options.scales.x.ticks.callback = (value) => (value / sr).toFixed(1) + "s";
        _docChart.data.datasets[0].data = sig;

        const hasAtt = Array.isArray(c.xai_weights) && c.xai_weights.length === sig.length && sig.length > 0;
        if (_docChart.data.datasets[1]) {
            _docChart.data.datasets[1].data = hasAtt ? c.xai_weights : [];
        }
        if (legendEl) {
            legendEl.style.display = hasAtt ? "flex" : "none";
        }
        resetDoctorChartZoom();
        _docChart.update();
    }
}

export async function submitDoctorDecision(decisionValue) {
    if (!_selectedCaseId) return;
    const note = document.getElementById("doc_notes").value.trim();
    try {
        await apiFetch("/api/doctor/respond", {
            method: "POST",
            body: { case_id: _selectedCaseId, decision: decisionValue, note },
        });
        showToast(
            getLang() === "ar" ? "تم حفظ القرار السريري بنجاح" : "Clinical decision saved",
            "success"
        );
        _selectedCaseId = null;
        document.getElementById("doc_notes").value = "";
        ["btnActionNoisy", "btnActionHealthy", "btnActionInfected"].forEach((id) => {
            document.getElementById(id).disabled = true;
        });
        if (_docChart) {
            _docChart.data.datasets[0].data = [];
            if (_docChart.data.datasets[1]) _docChart.data.datasets[1].data = [];
            _docChart.update();
        }
        const legendEl = document.getElementById("docCaseLegend");
        if (legendEl) legendEl.style.display = "none";
        await Promise.all([fetchDoctorKpis(), fetchDoctorCases(), fetchDoctorPatients()]);
    } catch (err) {
        showToast(err.message, "error");
    }
}

export async function fetchDoctorPatients() {
    try {
        const search = document.getElementById("docPatientSearch")?.value.trim() || "";
        const params = new URLSearchParams();
        if (search) params.set("search", search);
        const patients = await apiFetch(`/api/doctor/patients?${params.toString()}`);

        _sparklineCharts.forEach((ch) => ch.destroy());
        _sparklineCharts = [];

        const tbody = document.getElementById("docPatientsTableBody");
        if (!patients.length) {
            tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; color:var(--white-muted);">${escapeHtml(
                t("noData")
            )}</td></tr>`;
            return;
        }

        tbody.innerHTML = patients
            .map((p) => {
                const genderLabel = p.gender === "M" ? t("optMale") : t("optFemale");
                const statusBadge = p.last_prediction
                    ? `<span class="badge ${
                          p.last_prediction.includes("AFIB") || p.last_status === "ESCALATED"
                              ? "badge-danger"
                              : p.last_status === "REJECTED"
                              ? "badge-warn"
                              : "badge-success"
                      }">${escapeHtml(p.last_prediction)}</span>`
                    : `<span class="badge badge-muted">${escapeHtml(t("noData"))}</span>`;
                return `<tr>
                    <td><strong>${escapeHtml(p.full_name)}</strong><div style="font-size:11px; color:var(--white-muted);">@${escapeHtml(
                    p.username
                )}</div></td>
                    <td>${escapeHtml(p.age)} / ${escapeHtml(genderLabel)}</td>
                    <td>${escapeHtml(formatDateTime(p.last_test_at))}</td>
                    <td>${statusBadge}${
                    p.pending_cases_count > 0
                        ? ` <span class="badge badge-urgent">${p.pending_cases_count}</span>`
                        : ""
                }</td>
                    <td>
                        <div style="display:flex; align-items:center; gap:8px;">
                            <strong>${escapeHtml(p.last_bpm ?? "--")}</strong>
                            <div class="sparkline-box"><canvas id="spark_${p.id}"></canvas></div>
                        </div>
                    </td>
                    <td>
                        <div style="display:flex; gap:6px; flex-wrap:wrap;">
                            <button class="btn btn-secondary btn-sm btn-edit-pat" data-id="${p.id}">${escapeHtml(
                    t("btnEditClinical")
                )}</button>
                            <button class="btn btn-secondary btn-sm btn-hist-pat" data-id="${p.id}" data-name="${escapeHtml(
                    p.full_name
                )}">${escapeHtml(t("btnViewHistory"))}</button>
                        </div>
                    </td>
                </tr>`;
            })
            .join("");

        tbody.querySelectorAll(".btn-edit-pat").forEach((btn) => {
            btn.addEventListener("click", () => openEditClinicalModal(Number(btn.dataset.id)));
        });
        tbody.querySelectorAll(".btn-hist-pat").forEach((btn) => {
            btn.addEventListener("click", () =>
                openPatientHistoryModal(Number(btn.dataset.id), btn.dataset.name)
            );
        });

        // Render mini BPM trend sparklines
        patients.forEach((p) => {
            const canvas = document.getElementById(`spark_${p.id}`);
            if (!canvas || !p.bpm_trend || p.bpm_trend.length < 1) return;
            const ch = new Chart(canvas.getContext("2d"), {
                type: "line",
                data: {
                    labels: p.bpm_trend.map((_, i) => i),
                    datasets: [
                        {
                            data: p.bpm_trend.map((x) => x.bpm),
                            borderColor: "#06b6d4",
                            borderWidth: 2,
                            pointRadius: 2,
                            tension: 0.3,
                            fill: false,
                        },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: false,
                    plugins: { legend: { display: false }, tooltip: { enabled: false } },
                    scales: { x: { display: false }, y: { display: false } },
                },
            });
            _sparklineCharts.push(ch);
        });
    } catch (err) {
        showToast(err.message, "error");
    }
}

export function openAddPatientModal() {
    document.getElementById("addPatientForm").reset();
    document.getElementById("addPatientModal").classList.add("open");
}

export function closeAddPatientModal() {
    document.getElementById("addPatientModal").classList.remove("open");
}

export async function submitAddPatient(event) {
    event.preventDefault();
    const chronic = [];
    document.querySelectorAll("#addPatientForm .chronic-chk:checked").forEach((c) => chronic.push(c.value));

    try {
        await apiFetch("/api/doctor/patients", {
            method: "POST",
            body: {
                username: document.getElementById("newPatUsername").value.trim(),
                full_name: document.getElementById("newPatFullName").value.trim(),
                password: document.getElementById("newPatPassword").value,
                age: Number(document.getElementById("newPatAge").value),
                gender: document.getElementById("newPatGender").value,
                phone: document.getElementById("newPatPhone").value.trim(),
                emergency_contact_name: document.getElementById("newPatEmergName").value.trim(),
                emergency_contact_phone: document.getElementById("newPatEmergPhone").value.trim(),
                chronic_conditions: chronic,
                clinical_notes: document.getElementById("newPatNotes").value.trim(),
            },
        });
        closeAddPatientModal();
        showToast(
            getLang() === "ar" ? "تم إنشاء حساب المريض بنجاح" : "Patient account created",
            "success"
        );
        await Promise.all([fetchDoctorKpis(), fetchDoctorPatients()]);
    } catch (err) {
        showToast(err.message, "error");
    }
}

export async function openEditClinicalModal(patientId) {
    try {
        const data = await apiFetch(`/api/doctor/patients/${patientId}`);
        const p = data.profile || {};
        const u = data.user || {};
        document.getElementById("editClinPatientId").value = patientId;
        document.getElementById("editClinFullName").value = u.full_name || "";
        document.getElementById("editClinAge").value = p.age ?? "";
        document.getElementById("editClinGender").value = p.gender || "M";
        document.getElementById("editClinNotes").value = p.clinical_notes || "";

        const set = new Set(p.chronic_conditions || []);
        document.querySelectorAll("#editClinicalForm .edit-chronic-chk").forEach((chk) => {
            chk.checked = set.has(chk.value);
        });
        document.getElementById("editClinicalModal").classList.add("open");
    } catch (err) {
        showToast(err.message, "error");
    }
}

export function closeEditClinicalModal() {
    document.getElementById("editClinicalModal").classList.remove("open");
}

export async function submitEditClinical(event) {
    event.preventDefault();
    const patientId = document.getElementById("editClinPatientId").value;
    const chronic = [];
    document.querySelectorAll("#editClinicalForm .edit-chronic-chk:checked").forEach((c) => chronic.push(c.value));

    try {
        await apiFetch(`/api/doctor/patients/${patientId}`, {
            method: "PATCH",
            body: {
                full_name: document.getElementById("editClinFullName").value.trim(),
                age: Number(document.getElementById("editClinAge").value),
                gender: document.getElementById("editClinGender").value,
                chronic_conditions: chronic,
                clinical_notes: document.getElementById("editClinNotes").value.trim(),
            },
        });
        closeEditClinicalModal();
        showToast(
            getLang() === "ar" ? "تم تحديث الملف السريري" : "Clinical profile updated",
            "success"
        );
        await fetchDoctorPatients();
        if (_selectedCaseId && _selectedCasePatientId === Number(patientId)) {
            await selectCase(_selectedCaseId, false);
        }
    } catch (err) {
        showToast(err.message, "error");
    }
}

export async function openPatientHistoryModal(patientId, patientName) {
    try {
        const rows = await apiFetch(`/api/doctor/patients/${patientId}/history`);
        document.getElementById("histModalTitle").textContent = `${t("lblHistoryTitle")} — ${patientName || ""}`;
        const body = document.getElementById("histModalBody");
        if (!rows.length) {
            body.innerHTML = `<p style="color:var(--white-muted);">${escapeHtml(t("noData"))}</p>`;
        } else {
            body.innerHTML = rows
                .map((r, idx) => {
                    const wfAction = r.has_waveform
                        ? `<button class="btn btn-secondary btn-sm btn-doc-view-ecg" data-idx="${idx}">${escapeHtml(
                              t("btnViewEcg")
                          )}</button>`
                        : `<span style="font-size:11px; color:var(--white-muted);">${escapeHtml(
                              t("noWaveformStored")
                          )}</span>`;
                    return `<div class="message-item">
                    <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:10px; flex-wrap:wrap;">
                        <div>
                            <div style="font-size:11px; color:var(--white-muted);">${escapeHtml(formatDateTime(r.ts))}</div>
                            <div><strong>${escapeHtml(r.status)}</strong> • ${escapeHtml(r.prediction)} • ${escapeHtml(
                                r.bpm
                            )} BPM (${escapeHtml(r.rate_status)})</div>
                        </div>
                        <div>${wfAction}</div>
                    </div>
                    ${r.doctor_note ? `<div style="font-size:12px; color:var(--scai-accent); margin-top:4px;">💬 ${escapeHtml(r.doctor_note)}</div>` : ""}
                </div>`;
                })
                .join("");

            body.querySelectorAll(".btn-doc-view-ecg").forEach((btn) => {
                btn.addEventListener("click", () => {
                    const item = rows[Number(btn.dataset.idx)];
                    if (item) openWaveformModal(item, { isClinical: true });
                });
            });
        }
        document.getElementById("patientHistoryModal").classList.add("open");
    } catch (err) {
        showToast(err.message, "error");
    }
}

export function closePatientHistoryModal() {
    document.getElementById("patientHistoryModal").classList.remove("open");
}
