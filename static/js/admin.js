/**
 * admin.js - Admin telemetry, ML dataset export, user management
 * (create/deactivate doctors, create/reassign patients, search/filter), and audit logs.
 */
import { apiFetch, escapeHtml, formatDateTime, showToast, getCurrentUser } from "./api.js";
import { t, getLang } from "./i18n.js";

let _adminChart = null;
let _cachedDoctors = [];
let _allAdminUsers = [];

export async function initAdminPortal() {
    await Promise.all([fetchAdminStats(), fetchAdminUsers()]);
}

export function switchAdminTab(tabId) {
    document.querySelectorAll("#adminView .tab-btn").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.tab === tabId);
    });
    document.querySelectorAll("#adminView .tab-content").forEach((panel) => {
        panel.classList.toggle("active", panel.id === tabId);
    });
    if (tabId === "adminUsersTab") fetchAdminUsers();
    if (tabId === "adminAuditTab") fetchAuditLogs();
    if (tabId === "adminTelemetryTab") fetchAdminStats();
}

function _fmtPct(part, total) {
    if (!total) return "0%";
    return `${Math.round((part / total) * 100)}%`;
}

function _initials(name) {
    const clean = (name || "").trim();
    if (!clean) return "?";
    const parts = clean.split(/\s+/);
    if (parts.length >= 2) {
        return (parts[0][0] + parts[1][0]).toUpperCase();
    }
    return clean.slice(0, 2).toUpperCase();
}

export async function fetchAdminStats() {
    try {
        const data = await apiFetch("/api/admin/stats");
        const s = data.stats || {};
        const u = data.users || {};

        const total = s.total_tests ?? 0;
        const normal = s.normal_tests ?? 0;
        const afib = s.afib_tests ?? 0;
        const escalated = s.escalated_tests ?? 0;
        const rejected = s.rejected_tests ?? 0;
        const dsSize = data.dataset_size ?? 0;

        const setText = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.textContent = val;
        };

        setText("stat_total", total);
        setText("stat_normal", normal);
        setText("stat_afib", afib);
        setText("stat_rejected", rejected);
        setText("stat_escalated", escalated);
        setText("stat_dataset", dsSize);
        setText("stat_dataset_badge", dsSize);

        setText("stat_normal_pct", _fmtPct(normal, total));
        setText("stat_afib_pct", _fmtPct(afib, total));
        setText("stat_escalated_pct", _fmtPct(escalated, total));
        setText("stat_rejected_pct", _fmtPct(rejected, total));
        setText("adminChartCenterTotal", total);

        setText("ops_active_doctors", u.active_doctors ?? 0);
        setText("ops_active_patients", u.active_patients ?? 0);
        setText("ops_pending_cases", u.pending_cases ?? 0);

        const segments = [
            { label: t("kpiNormalTests"), value: normal, color: "#10b981" },
            { label: t("kpiAfibTests"), value: afib, color: "#ef4444" },
            { label: t("kpiEscalatedTests"), value: escalated, color: "#06b6d4" },
            { label: t("kpiRejectedTests"), value: rejected, color: "#eab308" },
        ];

        const breakdownEl = document.getElementById("adminDistributionBreakdown");
        if (breakdownEl) {
            breakdownEl.innerHTML = segments
                .map((seg) => {
                    const pct = total ? Math.round((seg.value / total) * 100) : 0;
                    return `<div class="admin-breakdown-row">
                        <div class="admin-breakdown-head">
                            <span class="admin-breakdown-name">
                                <span class="admin-breakdown-swatch" style="background:${seg.color};"></span>
                                ${escapeHtml(seg.label)}
                            </span>
                            <span class="admin-breakdown-nums"><strong>${seg.value}</strong> (${pct}%)</span>
                        </div>
                        <div class="admin-breakdown-track">
                            <div class="admin-breakdown-fill" style="width:${pct}%; background:${seg.color};"></div>
                        </div>
                    </div>`;
                })
                .join("");
        }

        const ctx = document.getElementById("adminStatsChart")?.getContext("2d");
        if (!ctx) return;
        const chartVals = segments.map((seg) => seg.value);
        const hasAny = chartVals.some((v) => v > 0);
        const displayVals = hasAny ? chartVals : [1];
        const displayColors = hasAny ? segments.map((seg) => seg.color) : ["rgba(148,163,184,0.22)"];
        const displayLabels = hasAny ? segments.map((seg) => seg.label) : [t("noData")];

        if (_adminChart) {
            _adminChart.data.labels = displayLabels;
            _adminChart.data.datasets[0].data = displayVals;
            _adminChart.data.datasets[0].backgroundColor = displayColors;
            _adminChart.update();
        } else {
            _adminChart = new Chart(ctx, {
                type: "doughnut",
                data: {
                    labels: displayLabels,
                    datasets: [
                        {
                            data: displayVals,
                            backgroundColor: displayColors,
                            borderWidth: 0,
                            hoverOffset: 5,
                        },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    cutout: "72%",
                    plugins: {
                        legend: { display: false },
                        tooltip: { enabled: hasAny },
                    },
                },
            });
        }
    } catch (err) {
        showToast(err.message, "error");
    }
}

export function downloadMLDataset() {
    window.location.href = "/api/admin/export_dataset";
}

export async function fetchAdminUsers() {
    try {
        const users = await apiFetch("/api/admin/users");
        _allAdminUsers = Array.isArray(users) ? users : [];
        _cachedDoctors = _allAdminUsers.filter((u) => u.role === "doctor" && u.is_active);
        renderAdminUsers();
    } catch (err) {
        showToast(err.message, "error");
    }
}

export function renderAdminUsers() {
    const me = getCurrentUser();
    const search = (document.getElementById("adminUserSearch")?.value || "").trim().toLowerCase();
    const statusFilter = document.getElementById("adminUserStatusFilter")?.value || "all";

    const allDoctors = _allAdminUsers.filter((u) => u.role === "doctor");
    const allPatients = _allAdminUsers.filter((u) => u.role === "patient");
    const activeDoctorsCount = allDoctors.filter((u) => u.is_active).length;
    const activePatientsCount = allPatients.filter((u) => u.is_active).length;
    const inactiveCount = _allAdminUsers.filter((u) => u.role !== "admin" && !u.is_active).length;

    const setText = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.textContent = val;
    };
    setText("user_sum_doctors", activeDoctorsCount);
    setText("user_sum_patients", activePatientsCount);
    setText("user_sum_inactive", inactiveCount);

    const matches = (u) => {
        if (statusFilter === "active" && !u.is_active) return false;
        if (statusFilter === "inactive" && u.is_active) return false;
        if (search) {
            const hay = `${u.full_name || ""} ${u.username || ""} ${u.doctor_name || ""}`.toLowerCase();
            if (!hay.includes(search)) return false;
        }
        return true;
    };

    const doctors = allDoctors.filter(matches);
    const patients = allPatients.filter(matches);

    setText("adminDoctorsCount", doctors.length);
    setText("adminPatientsCount", patients.length);

    const docTbody = document.getElementById("adminDoctorsTableBody");
    if (docTbody) {
        docTbody.innerHTML = doctors.length
            ? doctors
                  .map(
                      (d) => `<tr>
                <td>
                    <div class="user-cell">
                        <span class="user-avatar">${escapeHtml(_initials(d.full_name))}</span>
                        <div>
                            <strong>${escapeHtml(d.full_name)}</strong>
                            <div class="user-meta-sub">@${escapeHtml(d.username)}</div>
                        </div>
                    </div>
                </td>
                <td><span class="count-pill">${escapeHtml(d.active_patients_count ?? 0)}</span></td>
                <td><span class="badge ${d.is_active ? "badge-success" : "badge-muted"}">${escapeHtml(
                          d.is_active ? t("activeBadge") : t("inactiveBadge")
                      )}</span></td>
                <td>
                    <button class="btn btn-sm ${
                        d.is_active ? "btn-danger" : "btn-success"
                    } btn-toggle-status" data-id="${d.id}" data-active="${!d.is_active}">
                        ${escapeHtml(d.is_active ? t("btnDeactivate") : t("btnActivate"))}
                    </button>
                </td>
            </tr>`
                  )
                  .join("")
            : `<tr><td colspan="4" style="text-align:center; padding:24px; color:var(--white-muted);">${escapeHtml(
                  t("noData")
              )}</td></tr>`;
    }

    const patTbody = document.getElementById("adminPatientsTableBody");
    if (patTbody) {
        patTbody.innerHTML = patients.length
            ? patients
                  .map((p) => {
                      const genderLabel =
                          p.gender === "M" ? t("optMale") : p.gender === "F" ? t("optFemale") : "--";
                      return `<tr>
                <td>
                    <div class="user-cell">
                        <span class="user-avatar user-avatar-patient">${escapeHtml(_initials(p.full_name))}</span>
                        <div>
                            <strong>${escapeHtml(p.full_name)}</strong>
                            <div class="user-meta-sub">@${escapeHtml(p.username)}</div>
                        </div>
                    </div>
                </td>
                <td>${escapeHtml(p.age ?? "--")} <span style="color:var(--white-muted);">/</span> ${escapeHtml(
                          genderLabel
                      )}</td>
                <td><span class="badge badge-info">${escapeHtml(p.doctor_name || "--")}</span></td>
                <td><span class="badge ${p.is_active ? "badge-success" : "badge-muted"}">${escapeHtml(
                          p.is_active ? t("activeBadge") : t("inactiveBadge")
                      )}</span></td>
                <td>
                    <div style="display:flex; gap:6px; flex-wrap:wrap;">
                        <button class="btn btn-secondary btn-sm btn-reassign-pat" data-id="${p.id}" data-name="${escapeHtml(
                          p.full_name
                      )}" data-doc="${p.doctor_id || ""}">${escapeHtml(t("btnReassign"))}</button>
                        ${
                            !me || me.id !== p.id
                                ? `<button class="btn btn-sm ${
                                      p.is_active ? "btn-danger" : "btn-success"
                                  } btn-toggle-status" data-id="${p.id}" data-active="${!p.is_active}">
                                    ${escapeHtml(p.is_active ? t("btnDeactivate") : t("btnActivate"))}
                                  </button>`
                                : ""
                        }
                    </div>
                </td>
            </tr>`;
                  })
                  .join("")
            : `<tr><td colspan="5" style="text-align:center; padding:24px; color:var(--white-muted);">${escapeHtml(
                  t("noData")
              )}</td></tr>`;
    }

    document.querySelectorAll("#adminUsersTab .btn-toggle-status").forEach((btn) => {
        btn.addEventListener("click", () =>
            toggleUserActive(Number(btn.dataset.id), btn.dataset.active === "true")
        );
    });
    document.querySelectorAll("#adminUsersTab .btn-reassign-pat").forEach((btn) => {
        btn.addEventListener("click", () =>
            openReassignModal(Number(btn.dataset.id), btn.dataset.name, Number(btn.dataset.doc))
        );
    });
}

async function toggleUserActive(userId, isActive) {
    try {
        await apiFetch(`/api/admin/users/${userId}/status`, {
            method: "PATCH",
            body: { is_active: isActive },
        });
        showToast(
            getLang() === "ar" ? "تم تحديث حالة الحساب" : "Account status updated",
            "success"
        );
        await Promise.all([fetchAdminUsers(), fetchAdminStats()]);
    } catch (err) {
        showToast(err.message, "error");
    }
}

export function openAddDoctorModal() {
    document.getElementById("addDoctorForm").reset();
    document.getElementById("addDoctorModal").classList.add("open");
}

export function closeAddDoctorModal() {
    document.getElementById("addDoctorModal").classList.remove("open");
}

export async function submitAddDoctor(event) {
    event.preventDefault();
    try {
        await apiFetch("/api/admin/doctors", {
            method: "POST",
            body: {
                username: document.getElementById("newDocUsername").value.trim(),
                full_name: document.getElementById("newDocFullName").value.trim(),
                password: document.getElementById("newDocPassword").value,
            },
        });
        closeAddDoctorModal();
        showToast(getLang() === "ar" ? "تم إنشاء حساب الطبيب" : "Doctor account created", "success");
        await Promise.all([fetchAdminUsers(), fetchAdminStats()]);
    } catch (err) {
        showToast(err.message, "error");
    }
}

export function openAdminAddPatientModal() {
    document.getElementById("adminAddPatientForm").reset();
    const sel = document.getElementById("adminNewPatDoctor");
    sel.innerHTML = _cachedDoctors
        .map((d) => `<option value="${d.id}">${escapeHtml(d.full_name)} (@${escapeHtml(d.username)})</option>`)
        .join("");
    if (!_cachedDoctors.length) {
        showToast(
            getLang() === "ar" ? "يجب إنشاء طبيب نشط أولاً" : "Create an active doctor first",
            "warn"
        );
        return;
    }
    document.getElementById("adminAddPatientModal").classList.add("open");
}

export function closeAdminAddPatientModal() {
    document.getElementById("adminAddPatientModal").classList.remove("open");
}

export async function submitAdminAddPatient(event) {
    event.preventDefault();
    try {
        await apiFetch("/api/admin/patients", {
            method: "POST",
            body: {
                username: document.getElementById("adminNewPatUsername").value.trim(),
                full_name: document.getElementById("adminNewPatFullName").value.trim(),
                password: document.getElementById("adminNewPatPassword").value,
                doctor_id: Number(document.getElementById("adminNewPatDoctor").value),
                age: Number(document.getElementById("adminNewPatAge").value),
                gender: document.getElementById("adminNewPatGender").value,
                phone: document.getElementById("adminNewPatPhone").value.trim(),
            },
        });
        closeAdminAddPatientModal();
        showToast(getLang() === "ar" ? "تم إنشاء حساب المريض" : "Patient created", "success");
        await Promise.all([fetchAdminUsers(), fetchAdminStats()]);
    } catch (err) {
        showToast(err.message, "error");
    }
}

export function openReassignModal(patientId, patientName, currentDoctorId) {
    document.getElementById("reassignPatientId").value = patientId;
    document.getElementById("reassignPatientLabel").textContent = patientName || "";
    const sel = document.getElementById("reassignDoctorSelect");
    sel.innerHTML = _cachedDoctors
        .map(
            (d) =>
                `<option value="${d.id}" ${d.id === currentDoctorId ? "selected" : ""}>${escapeHtml(
                    d.full_name
                )} (@${escapeHtml(d.username)})</option>`
        )
        .join("");
    document.getElementById("reassignModal").classList.add("open");
}

export function closeReassignModal() {
    document.getElementById("reassignModal").classList.remove("open");
}

export async function submitReassign(event) {
    event.preventDefault();
    const patientId = document.getElementById("reassignPatientId").value;
    const doctorId = Number(document.getElementById("reassignDoctorSelect").value);
    try {
        const res = await apiFetch(`/api/admin/patients/${patientId}/reassign`, {
            method: "PATCH",
            body: { doctor_id: doctorId },
        });
        closeReassignModal();
        showToast(
            getLang() === "ar"
                ? `تمت إعادة التعيين (تم نقل ${res.transferred_cases} حالة مفتوحة)`
                : `Patient reassigned (${res.transferred_cases} open case(s) transferred)`,
            "success"
        );
        await fetchAdminUsers();
    } catch (err) {
        showToast(err.message, "error");
    }
}

function _auditBadgeClass(action) {
    if (!action) return "badge-muted";
    if (action.includes("FAIL")) return "badge-danger";
    if (action === "LOGIN_OK" || action === "USER_CREATE") return "badge-success";
    if (action === "USER_STATUS_CHANGE" || action === "PATIENT_REASSIGN") return "badge-warn";
    return "badge-info";
}

export async function fetchAuditLogs() {
    try {
        const action = document.getElementById("adminAuditActionFilter")?.value || "";
        const params = new URLSearchParams({ limit: "100" });
        if (action) params.set("action", action);

        const logs = await apiFetch(`/api/admin/audit-logs?${params.toString()}`);
        const tbody = document.getElementById("adminAuditTableBody");
        tbody.innerHTML = logs.length
            ? logs
                  .map(
                      (l) => `<tr>
                <td class="user-meta-sub">${escapeHtml(formatDateTime(l.created_at))}</td>
                <td><span class="badge ${_auditBadgeClass(l.action)}">${escapeHtml(l.action)}</span></td>
                <td><strong>${escapeHtml(l.actor_username || "--")}</strong></td>
                <td>${escapeHtml(l.target_type || "")} ${escapeHtml(l.target_id || "")}</td>
                <td class="user-meta-sub">${escapeHtml(l.ip_address || "--")}</td>
            </tr>`
                  )
                  .join("")
            : `<tr><td colspan="5" style="text-align:center; padding:24px; color:var(--white-muted);">${escapeHtml(
                  t("noData")
              )}</td></tr>`;
    } catch (err) {
        showToast(err.message, "error");
    }
}
