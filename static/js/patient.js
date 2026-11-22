/**
 * patient.js - Patient portal: profile view & contact editing, ECG prediction
 * using server-side profile, escalation symptoms, status polling, DB history & PDF.
 */
import { apiFetch, escapeHtml, formatDateTime, showToast, getCurrentUser } from "./api.js";
import { t, getLang } from "./i18n.js";
import { openWaveformModal } from "./waveform.js";
import {
    initPatientEcgChart,
    getRawSignalBuffer,
    resetAcquisitionState,
    connectBluetooth,
    disconnectBluetooth,
} from "./ble.js";

let _doctorStatusInterval = null;
let _patientHistoryInterval = null;
let _activeEscalatedCaseId = null;
let _cachedProfileData = null;

export function stopPatientPolling() {
    if (_doctorStatusInterval) {
        clearInterval(_doctorStatusInterval);
        _doctorStatusInterval = null;
    }
    if (_patientHistoryInterval) {
        clearInterval(_patientHistoryInterval);
        _patientHistoryInterval = null;
    }
}

export async function initPatientPortal() {
    initPatientEcgChart();
    await Promise.all([loadPatientProfile(), loadPatientHistory()]);
    stopPatientPolling();
    _patientHistoryInterval = setInterval(loadPatientHistory, 10000);
}

export function switchPatientTab(tabId) {
    document.querySelectorAll("#patientView .tab-btn").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.tab === tabId);
    });
    document.querySelectorAll("#patientView .tab-content").forEach((panel) => {
        panel.classList.toggle("active", panel.id === tabId);
    });
    if (tabId === "patientProfileTab") {
        loadPatientProfile();
        loadPatientHistory();
    }
}

export async function loadPatientProfile() {
    try {
        const data = await apiFetch("/api/patient/profile");
        _cachedProfileData = data;
        const u = data.user || {};
        const p = data.profile || {};
        const doc = data.doctor || {};

        const setText = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.textContent = val;
        };
        const setVal = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.value = val || "";
        };

        const genderLabel = p.gender === "M" ? t("optMale") : p.gender === "F" ? t("optFemale") : "--";
        setText("patSummaryName", u.full_name || u.username || "--");
        setText("patSummaryDoctor", doc.full_name || "--");
        setText("patSummaryAgeGender", `${p.age ?? "--"} / ${genderLabel}`);

        setText("profFullName", u.full_name || "--");
        setText("profAssignedDoctor", doc.full_name || "--");
        setText("profAge", p.age ?? "--");
        setText("profGender", genderLabel);
        setText(
            "profChronic",
            p.chronic_conditions && p.chronic_conditions.length
                ? p.chronic_conditions.join("، ")
                : t("noData")
        );
        setText("profClinicalNotes", p.clinical_notes || t("noData"));

        setVal("editPatPhone", p.phone || "");
        setVal("editPatEmergencyName", p.emergency_contact_name || "");
        setVal("editPatEmergencyPhone", p.emergency_contact_phone || "");
    } catch (err) {
        showToast(err.message, "error");
    }
}

export async function savePatientContact(event) {
    if (event) event.preventDefault();
    try {
        await apiFetch("/api/patient/profile", {
            method: "PATCH",
            body: {
                phone: document.getElementById("editPatPhone").value.trim(),
                emergency_contact_name: document.getElementById("editPatEmergencyName").value.trim(),
                emergency_contact_phone: document.getElementById("editPatEmergencyPhone").value.trim(),
            },
        });
        showToast(
            getLang() === "ar" ? "تم حفظ بيانات التواصل بنجاح" : "Contact details updated",
            "success"
        );
        await loadPatientProfile();
    } catch (err) {
        showToast(err.message, "error");
    }
}

export function resetPatientDashboard() {
    resetAcquisitionState();
    _activeEscalatedCaseId = null;
    if (_doctorStatusInterval) {
        clearInterval(_doctorStatusInterval);
        _doctorStatusInterval = null;
    }

    const hide = (id) => {
        const el = document.getElementById(id);
        if (el) el.style.display = "none";
    };
    hide("diagnosticHeaderBox");
    hide("patientResultComfortCard");
    hide("diagnoseBtn");
    hide("btnSendToDoctor");
    hide("resetBtn");
    hide("pnlComplaint");

    const bar = document.getElementById("signalProgressBar");
    if (bar) bar.style.width = "0%";

    ["sym_palp", "sym_sob", "sym_diz", "sym_chest"].forEach((id) => {
        const chk = document.getElementById(id);
        if (chk) chk.checked = false;
    });

    const bpmEl = document.getElementById("metric_bpm");
    if (bpmEl) bpmEl.textContent = "000 BPM";
    const rateEl = document.getElementById("metric_rate_status");
    if (rateEl) rateEl.textContent = t("awaitingSignal");
    const aiEl = document.getElementById("aiDiagnosisLabel");
    if (aiEl) aiEl.textContent = t("coreIdle");
}

export function handleConnectBle() {
    connectBluetooth(resetPatientDashboard);
}

export function handleDisconnectBle() {
    disconnectBluetooth(resetPatientDashboard);
}

export async function triggerAIDiagnosis() {
    const signal = getRawSignalBuffer();
    if (!signal || !signal.length) {
        showToast(getLang() === "ar" ? "لا توجد إشارة ECG للتحليل" : "No ECG signal captured", "error");
        return;
    }

    const diagBtn = document.getElementById("diagnoseBtn");
    const aiLabel = document.getElementById("aiDiagnosisLabel");
    if (diagBtn) diagBtn.style.display = "none";
    if (aiLabel) aiLabel.textContent = "...";

    try {
        const result = await apiFetch("/predict", {
            method: "POST",
            body: { signal },
        });

        const bpmVal = result.bpm !== undefined && result.bpm !== null ? result.bpm : "--";
        const rateStatusVal = result.rate_status ? String(result.rate_status).toUpperCase() : "UNKNOWN";

        document.getElementById("metric_bpm").textContent = `${bpmVal} BPM`;
        document.getElementById("metric_rate_status").textContent = rateStatusVal;

        const diagHeader = document.getElementById("diagnosticHeaderBox");
        const topLabel = document.getElementById("topInferenceLabel");
        const msgText = document.getElementById("diagMessageText");
        const comfortCard = document.getElementById("patientResultComfortCard");
        const comfortIcon = document.getElementById("comfortCardIcon");
        const comfortTitle = document.getElementById("comfortCardTitle");
        const comfortDesc = document.getElementById("comfortCardDescription");

        diagHeader.style.display = "block";
        comfortCard.style.display = "block";
        const isNormal = (result.prediction || "").toUpperCase() === "NORMAL";

        if (result.status === "SUCCESS") {
            topLabel.textContent = isNormal ? t("aiResultNegative") : t("aiResultPositive");
            topLabel.style.color = isNormal ? "var(--medical-green)" : "var(--medical-red)";
            diagHeader.style.borderInlineStartColor = isNormal ? "var(--medical-green)" : "var(--medical-red)";
            msgText.textContent =
                getLang() === "ar" ? "اكتمل التحليل التلقائي الموحد" : "AUTONOMOUS ANALYSIS COMPLETE";
            comfortCard.style.borderInlineStartColor = isNormal ? "var(--medical-green)" : "var(--medical-red)";
            comfortIcon.textContent = isNormal ? "🟢" : "🔴";
            comfortTitle.textContent = isNormal ? t("comfortTitleNormal") : t("comfortTitleAfib");
            comfortDesc.textContent = isNormal ? t("comfortDescNormal") : t("comfortDescAfib");
        } else if (result.status === "REJECTED") {
            topLabel.textContent =
                getLang() === "ar" ? "تم رفض الإشارة (تشويش حاد)" : "ACQUISITION REJECTED";
            topLabel.style.color = "var(--medical-yellow)";
            diagHeader.style.borderInlineStartColor = "var(--medical-yellow)";
            msgText.textContent = result.message || "ARTIFACT DETECTED";
            comfortCard.style.borderInlineStartColor = "var(--medical-yellow)";
            comfortIcon.textContent = "🟡";
            comfortTitle.textContent = t("comfortTitleRejected");
            comfortDesc.textContent = t("comfortDescRejected");
        } else if (result.status === "ESCALATED") {
            _activeEscalatedCaseId = result.case_id;
            topLabel.textContent = getLang() === "ar" ? "قيد المراجعة الاستشارية" : "ESCALATED FOR REVIEW";
            topLabel.style.color = "var(--scai-accent)";
            diagHeader.style.borderInlineStartColor = "var(--scai-accent)";
            msgText.textContent =
                getLang() === "ar"
                    ? "تم التحويل لطبيبك المتابع. يرجى تحديد الأعراض الحالية وإرسالها."
                    : "Routed to your assigned physician. Please select current symptoms and send.";
            document.getElementById("pnlComplaint").style.display = "block";
            document.getElementById("btnSendToDoctor").style.display = "inline-flex";
            comfortCard.style.borderInlineStartColor = "var(--scai-accent)";
            comfortIcon.textContent = "⚪";
            comfortTitle.textContent = t("comfortTitleEscalated");
            comfortDesc.textContent = t("comfortDescEscalated");
        }
        if (aiLabel) aiLabel.textContent = `CORE: ${result.status}`;
        await loadPatientHistory();
    } catch (err) {
        showToast(err.message, "error");
        if (aiLabel) aiLabel.textContent = "SERVER ERROR";
    }
}

export async function submitCaseToDoctor() {
    if (!_activeEscalatedCaseId) return;
    const symptoms = [];
    if (document.getElementById("sym_palp").checked) symptoms.push("خفقان/تسارع النبض");
    if (document.getElementById("sym_sob").checked) symptoms.push("ضيق بالتنفس");
    if (document.getElementById("sym_diz").checked) symptoms.push("دوار/إرهاق");
    if (document.getElementById("sym_chest").checked) symptoms.push("ألم بالصدر");

    try {
        await apiFetch("/api/patient/update_case", {
            method: "POST",
            body: { case_id: _activeEscalatedCaseId, symptoms },
        });
        document.getElementById("btnSendToDoctor").style.display = "none";
        document.getElementById("pnlComplaint").style.display = "none";
        document.getElementById("diagMessageText").textContent =
            getLang() === "ar" ? "تم الإرسال... بانتظار قرار الطبيب" : "SENT... AWAITING PHYSICIAN";
        startDoctorDecisionAutoPolling();
    } catch (err) {
        showToast(err.message, "error");
    }
}

function startDoctorDecisionAutoPolling() {
    if (_doctorStatusInterval) clearInterval(_doctorStatusInterval);
    _doctorStatusInterval = setInterval(async () => {
        try {
            const res = await apiFetch("/api/patient/status");
            const txt = res.status_text || "IDLE";
            if (!txt.includes("Awaiting") && txt !== "IDLE") {
                clearInterval(_doctorStatusInterval);
                _doctorStatusInterval = null;
                const lang = getLang();
                const topLabel = document.getElementById("topInferenceLabel");
                const msgEl = document.getElementById("diagMessageText");
                topLabel.textContent = lang === "ar" ? "وصل قرار الطبيب" : "CLINICAL REVIEW RECEIVED";
                topLabel.style.color = "var(--scai-accent)";

                let summary = txt;
                if (txt.includes("NORMAL")) {
                    summary = lang === "ar" ? "القرار: سليم (لا توجد مؤشرات AFib)" : "DECISION: Normal Rhythm";
                } else if (txt.includes("AFIB")) {
                    summary = lang === "ar" ? "القرار: مؤشرات AFib مؤكدة" : "DECISION: AFib Confirmed";
                } else if (txt.includes("REJECTED")) {
                    summary = lang === "ar" ? "القرار: تم رفض الإشارة بسبب التشويش" : "DECISION: Signal Rejected (Noise)";
                }

                msgEl.innerHTML = escapeHtml(summary) +
                    (res.doctor_note
                        ? `<br><strong style="color:var(--scai-accent);">${escapeHtml(
                              lang === "ar" ? "ملاحظة الطبيب:" : "Doctor Note:"
                          )}</strong> ${escapeHtml(res.doctor_note)}`
                        : "");

                showToast(
                    lang === "ar" ? "تم استلام رد الطبيب بنجاح." : "Physician review received.",
                    "success"
                );
                await loadPatientHistory();
            }
        } catch {
            // ignore transient poll errors
        }
    }, 3000);
}

export async function loadPatientHistory() {
    try {
        const rows = await apiFetch("/api/patient/history");
        const box = document.getElementById("patientHistoryContainer");
        const msgBox = document.getElementById("patientMessagesContainer");

        if (box) {
            if (!rows || !rows.length) {
                box.innerHTML = `<p style="color:var(--white-muted); font-size:13px;">${escapeHtml(
                    getLang() === "ar" ? "لا توجد تسجيلات سابقة." : "No previous recordings."
                )}</p>`;
            } else {
                box.innerHTML = rows
                    .map((x, idx) => {
                        const pred = x.prediction || "";
                        const isBad = pred.toUpperCase().includes("AFIB") || x.status === "ESCALATED";
                        const isWarn = x.status === "REJECTED" || pred.toUpperCase().includes("REJECTED");
                        const color = isWarn
                            ? "var(--medical-yellow)"
                            : isBad
                            ? "var(--medical-red)"
                            : "var(--medical-green)";
                        const wfAction = x.has_waveform
                            ? `<button class="btn btn-secondary btn-sm btn-pat-view-ecg" data-idx="${idx}">${escapeHtml(
                                  t("btnViewEcg")
                              )}</button>`
                            : `<span style="font-size:11px; color:var(--white-muted);">${escapeHtml(
                                  t("noWaveformStored")
                              )}</span>`;
                        return `<div class="message-item" style="border-inline-start-color:${color};">
                            <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:10px; flex-wrap:wrap;">
                                <div>
                                    <div style="font-size:11px; color:var(--white-muted);">⏱ ${escapeHtml(formatDateTime(x.ts))}</div>
                                    <div style="font-weight:700; margin-top:4px;">${escapeHtml(x.status)}${
                            pred && pred !== "N/A" ? " • " + escapeHtml(pred) : ""
                        }</div>
                                    <div style="font-size:12px; color:var(--white-muted);">${escapeHtml(
                                        x.bpm ?? "--"
                                    )} BPM (${escapeHtml(x.rate_status || "--")})</div>
                                </div>
                                <div>${wfAction}</div>
                            </div>
                            ${
                                x.doctor_note
                                    ? `<div style="margin-top:6px; font-size:12px; color:var(--scai-accent);">💬 ${escapeHtml(
                                          x.doctor_note
                                      )}</div>`
                                    : ""
                            }
                        </div>`;
                    })
                    .join("");

                box.querySelectorAll(".btn-pat-view-ecg").forEach((btn) => {
                    btn.addEventListener("click", () => {
                        const item = rows[Number(btn.dataset.idx)];
                        if (item) openWaveformModal(item, { isClinical: false });
                    });
                });
            }
        }

        // DB-backed doctor feedback messages (replaces localStorage messages_<user>)
        if (msgBox) {
            const reviewed = (rows || []).filter((r) => r.status === "REVIEWED" || r.doctor_note);
            if (!reviewed.length) {
                msgBox.innerHTML = `<p style="color:var(--white-muted); font-size:13px;">${escapeHtml(
                    t("noMessages")
                )}</p>`;
            } else {
                msgBox.innerHTML = reviewed
                    .map((r) => {
                        const isBad = (r.prediction || "").toUpperCase().includes("AFIB");
                        const color = isBad ? "var(--medical-red)" : "var(--medical-green)";
                        return `<div class="message-item" style="border-inline-start-color:${color};">
                            <div style="font-size:11px; color:var(--white-muted);">⏱ ${escapeHtml(formatDateTime(r.ts))}</div>
                            <div style="font-weight:700;">${escapeHtml(r.prediction)}</div>
                            ${r.doctor_note ? `<div style="margin-top:4px;">${escapeHtml(r.doctor_note)}</div>` : ""}
                        </div>`;
                    })
                    .join("");
            }
        }
    } catch {
        // handled by apiFetch
    }
}

export async function generatePatientPDF() {
    try {
        const [profData, rows] = await Promise.all([
            _cachedProfileData ? Promise.resolve(_cachedProfileData) : apiFetch("/api/patient/profile"),
            apiFetch("/api/patient/history"),
        ]);
        if (!rows || !rows.length) {
            showToast(getLang() === "ar" ? "لا توجد سجلات لتحميلها" : "No records to export", "error");
            return;
        }
        const isAr = getLang() === "ar";
        const u = profData.user || getCurrentUser() || {};
        const p = profData.profile || {};
        const doc = profData.doctor || {};
        const genderText = p.gender === "M" ? t("optMale") : t("optFemale");
        const chronicText =
            p.chronic_conditions && p.chronic_conditions.length ? p.chronic_conditions.join("، ") : t("noData");

        const win = window.open("", "", "height=800,width=820");
        const dir = isAr ? "rtl" : "ltr";
        const itemsHtml = rows
            .map(
                (r) => `<div class="item">
                <div class="ts">${escapeHtml(formatDateTime(r.ts))}</div>
                <div class="status">${escapeHtml(r.status)} - ${escapeHtml(r.prediction)}</div>
                <div>BPM: ${escapeHtml(r.bpm)} (${escapeHtml(r.rate_status)})</div>
                ${r.doctor_note ? `<div><strong>${isAr ? "ملاحظة الطبيب:" : "Doctor Note:"}</strong> ${escapeHtml(r.doctor_note)}</div>` : ""}
            </div>`
            )
            .join("");

        win.document.write(`<!DOCTYPE html><html dir="${dir}"><head><meta charset="UTF-8"><title>SCAI Clinical Report</title>
            <style>
                body { font-family: system-ui, sans-serif; padding: 32px; color: #1e293b; direction: ${dir}; }
                .header { text-align: center; border-bottom: 2px solid #06b6d4; padding-bottom: 14px; margin-bottom: 20px; }
                .info { background: #f1f5f9; padding: 14px 18px; border-radius: 8px; margin-bottom: 20px; line-height: 1.8; }
                .item { border: 1px solid #cbd5e1; border-radius: 8px; padding: 12px; margin-bottom: 10px; }
                .ts { font-size: 12px; color: #64748b; }
                .status { font-weight: 700; margin: 4px 0; }
            </style></head><body>
            <div class="header"><h1>${isAr ? "التقرير السريري الشامل" : "Comprehensive Clinical Report"}</h1><div>Safwa Cardio-AI Unit</div></div>
            <div class="info">
                <div><strong>${isAr ? "المريض:" : "Patient:"}</strong> ${escapeHtml(u.full_name || u.username)}</div>
                <div><strong>${isAr ? "العمر / الجنس:" : "Age / Gender:"}</strong> ${escapeHtml(p.age)} / ${escapeHtml(genderText)}</div>
                <div><strong>${isAr ? "الطبيب المتابع:" : "Assigned Physician:"}</strong> ${escapeHtml(doc.full_name || "--")}</div>
                <div><strong>${isAr ? "الأمراض المزمنة:" : "Chronic Conditions:"}</strong> ${escapeHtml(chronicText)}</div>
            </div>
            ${itemsHtml}
            </body></html>`);
        win.document.close();
        win.focus();
        setTimeout(() => win.print(), 400);
    } catch (err) {
        showToast(err.message, "error");
    }
}
