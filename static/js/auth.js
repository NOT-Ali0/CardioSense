/**
 * auth.js - Server-backed session boot (/api/auth/me), login, logout,
 * forced password change modal, and DB-backed consent modal.
 */
import { apiFetch, setAuthState, getCurrentUser, showToast } from "./api.js";
import { t } from "./i18n.js";
import { initPatientPortal, stopPatientPolling, resetPatientDashboard } from "./patient.js";
import { initDoctorDashboard, stopDoctorPolling } from "./doctor.js";
import { initAdminPortal } from "./admin.js";

let _pendingConsentUser = null;
let _previousViewId = "loginView";

export function showView(viewId) {
    document.querySelectorAll(".view-panel").forEach((p) => p.classList.remove("active-view"));
    const target = document.getElementById(viewId);
    if (target) target.classList.add("active-view");
}

export function openSettingsView() {
    const active = document.querySelector(".view-panel.active-view");
    if (active && active.id !== "settingsView") {
        _previousViewId = active.id;
    }
    showView("settingsView");
}

export function closeSettingsView() {
    showView(_previousViewId || "loginView");
}

export function openForcePasswordModal() {
    document.getElementById("forcePasswordForm").reset();
    document.getElementById("forcePasswordModal").classList.add("open");
}

export function closeForcePasswordModal() {
    document.getElementById("forcePasswordModal").classList.remove("open");
}

async function enterRolePortal(user, consentAccepted) {
    if (user.must_change_password) {
        openForcePasswordModal();
        return;
    }
    if (user.role === "patient" && !consentAccepted) {
        _pendingConsentUser = user;
        document.getElementById("consentCheck").checked = false;
        document.getElementById("consentAcceptBtn").disabled = true;
        document.getElementById("consentModal").classList.add("open");
        return;
    }

    if (user.role === "patient") {
        showView("patientView");
        await initPatientPortal();
    } else if (user.role === "doctor") {
        showView("doctorView");
        await initDoctorDashboard();
    } else if (user.role === "admin") {
        showView("adminView");
        await initAdminPortal();
    }
}

export async function restoreSession() {
    try {
        const data = await apiFetch("/api/auth/me", { skipAuthRedirect: true });
        setAuthState(data.user, data.csrf_token);
        await enterRolePortal(data.user, data.consent_accepted);
    } catch {
        setAuthState(null, "");
        showView("loginView");
    }
}

export async function handleLogin(event) {
    event.preventDefault();
    const username = document.getElementById("username").value.trim();
    const password = document.getElementById("password").value;
    if (!username || !password) return;

    try {
        const data = await apiFetch("/api/auth/login", {
            method: "POST",
            body: { username, password },
            skipAuthRedirect: true,
        });
        document.getElementById("password").value = "";
        setAuthState(data.user, data.csrf_token);
        await enterRolePortal(data.user, data.consent_accepted);
    } catch (err) {
        showToast(err.message, "error");
    }
}

export async function handleChangePassword(event) {
    event.preventDefault();
    const current_password = document.getElementById("chgCurrentPw").value;
    const new_password = document.getElementById("chgNewPw").value;
    const confirm_password = document.getElementById("chgConfirmPw").value;

    if (new_password !== confirm_password) {
        showToast(t("pwMismatch"), "error");
        return;
    }

    try {
        const data = await apiFetch("/api/auth/change-password", {
            method: "POST",
            body: { current_password, new_password },
        });
        setAuthState(data.user, data.csrf_token);
        closeForcePasswordModal();
        showToast(t("pwChanged"), "success");
        await enterRolePortal(data.user, data.consent_accepted);
    } catch (err) {
        showToast(err.message, "error");
    }
}

export async function handleAcceptConsent() {
    try {
        await apiFetch("/api/consent", { method: "POST", body: { policy_version: "1.0" } });
        document.getElementById("consentModal").classList.remove("open");
        const user = _pendingConsentUser || getCurrentUser();
        _pendingConsentUser = null;
        if (user) {
            await enterRolePortal(user, true);
        }
    } catch (err) {
        showToast(err.message, "error");
    }
}

export async function handleLogout() {
    stopPatientPolling();
    stopDoctorPolling();
    try {
        await apiFetch("/api/auth/logout", { method: "POST", skipAuthRedirect: true });
    } catch {
        // ignore if already expired
    }
    setAuthState(null, "");
    closeForcePasswordModal();
    document.getElementById("consentModal").classList.remove("open");
    resetPatientDashboard();
    showView("loginView");
}
