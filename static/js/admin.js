/**
 * admin.js - Admin telemetry, ML dataset export, user management
 * (create/deactivate doctors, create/reassign patients), and audit logs.
 */
import { apiFetch, escapeHtml, formatDateTime, showToast, getCurrentUser } from "./api.js";
import { t, getLang } from "./i18n.js";

let _adminChart = null;
let _cachedDoctors = [];

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

export async function fetchAdminStats() {
    try {
        const data = await apiFetch("/api/admin/stats");
        const s = data.stats || {};
        document.getElementById("stat_total").textContent = s.total_tests ?? 0;
        document.getElementById("stat_normal").textContent = s.normal_tests ?? 0;
        document.getElementById("stat_afib").textContent = s.afib_tests ?? 0;
        document.getElementById("stat_rejected").textContent = s.rejected_tests ?? 0;
        document.getElementById("stat_escalated").textContent = s.escalated_tests ?? 0;
        document.getElementById("stat_dataset").textContent = data.dataset_size ?? 0;

        const ctx = document.getElementById("adminStatsChart")?.getContext("2d");
        if (!ctx) return;
        const chartVals = [s.normal_tests, s.afib_tests, s.escalated_tests, s.rejected_tests];
        if (_adminChart) {
            _adminChart.data.datasets[0].data = chartVals;
            _adminChart.update();
        } else {
            _adminChart = new Chart(ctx, {
                type: "doughnut",
                data: {
                    labels: ["NORMAL", "AFIB", "ESCALATED", "REJECTED"],
                    datasets: [
                        {
                            data: chartVals,
                            backgroundColor: ["#10b981", "#ef4444", "#06b6d4", "#eab308"],
                            borderWidth: 0,
                        },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { position: "right" } },
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
        const me = getCurrentUser();
        const doctors = users.filter((u) => u.role === "doctor");
        const patients = users.filter((u) => u.role === "patient");
        _cachedDoctors = doctors.filter((d) => d.is_active);

        const docTbody = document.getElementById("adminDoctorsTableBody");
        docTbody.innerHTML = doctors.length
            ? doctors
                  .map(
                      (d) => `<tr>
                <td><strong>${escapeHtml(d.full_name)}</strong></td>
                <td>@${escapeHtml(d.username)}</td>
                <td>${escapeHtml(d.active_patients_count ?? 0)}</td>
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
            : `<tr><td colspan="5" style="text-align:center; color:var(--white-muted);">${escapeHtml(
                  t("noData")
              )}</td></tr>`;

        const patTbody = document.getElementById("adminPatientsTableBody");
        patTbody.innerHTML = patients.length
            ? patients
                  .map(
                      (p) => `<tr>
                <td><strong>${escapeHtml(p.full_name)}</strong><div style="font-size:11px; color:var(--white-muted);">@${escapeHtml(
                          p.username
                      )}</div></td>
                <td>${escapeHtml(p.age ?? "--")} / ${escapeHtml(p.gender || "--")}</td>
                <td>${escapeHtml(p.doctor_name || "--")}</td>
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
            </tr>`
                  )
                  .join("")
            : `<tr><td colspan="5" style="text-align:center; color:var(--white-muted);">${escapeHtml(
                  t("noData")
              )}</td></tr>`;

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
    } catch (err) {
        showToast(err.message, "error");
    }
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
        await fetchAdminUsers();
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

export async function fetchAuditLogs() {
    try {
        const logs = await apiFetch("/api/admin/audit-logs?limit=100");
        const tbody = document.getElementById("adminAuditTableBody");
        tbody.innerHTML = logs.length
            ? logs
                  .map(
                      (l) => `<tr>
                <td>${escapeHtml(formatDateTime(l.created_at))}</td>
                <td><span class="badge badge-info">${escapeHtml(l.action)}</span></td>
                <td>${escapeHtml(l.actor_username || "--")}</td>
                <td>${escapeHtml(l.target_type || "")} ${escapeHtml(l.target_id || "")}</td>
                <td>${escapeHtml(l.ip_address || "--")}</td>
            </tr>`
                  )
                  .join("")
            : `<tr><td colspan="5" style="text-align:center; color:var(--white-muted);">${escapeHtml(
                  t("noData")
              )}</td></tr>`;
    } catch (err) {
        showToast(err.message, "error");
    }
}
