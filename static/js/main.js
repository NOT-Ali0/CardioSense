/**
 * main.js - Application entry point, theme/language initialization, and event wiring.
 */
import { registerAuthHandlers } from "./api.js";
import { applyLanguage } from "./i18n.js";
import { getEcgChart, updateBleStatusLabel } from "./ble.js";
import {
    restoreSession,
    handleLogin,
    handleLogout,
    handleChangePassword,
    handleAcceptConsent,
    openSettingsView,
    closeSettingsView,
    openForcePasswordModal,
    showView,
} from "./auth.js";
import {
    switchPatientTab,
    savePatientContact,
    triggerAIDiagnosis,
    submitCaseToDoctor,
    resetPatientDashboard,
    handleConnectBle,
    handleDisconnectBle,
    generatePatientPDF,
    stopPatientPolling,
} from "./patient.js";
import {
    getDoctorChart,
    switchDoctorTab,
    fetchDoctorCases,
    fetchDoctorPatients,
    fetchDoctorKpis,
    onDoctorFilterChange,
    resetDoctorChartZoom,
    submitDoctorDecision,
    openAddPatientModal,
    closeAddPatientModal,
    submitAddPatient,
    closeEditClinicalModal,
    submitEditClinical,
    closePatientHistoryModal,
    stopDoctorPolling,
} from "./doctor.js";
import {
    getWaveformModalChart,
    closeWaveformModal,
    resetWaveformModalZoom,
} from "./waveform.js";
import {
    switchAdminTab,
    fetchAdminStats,
    fetchAdminUsers,
    renderAdminUsers,
    fetchAuditLogs,
    downloadMLDataset,
    openAddDoctorModal,
    closeAddDoctorModal,
    submitAddDoctor,
    openAdminAddPatientModal,
    closeAdminAddPatientModal,
    submitAdminAddPatient,
    closeReassignModal,
    submitReassign,
} from "./admin.js";

function applyTheme(theme) {
    const isLight = theme === "light";
    document.body.className = isLight ? "light-theme" : "dark-theme";
    localStorage.setItem("scai_theme", isLight ? "light" : "dark");

    const logoPath = isLight ? "/api/static/logo2.png" : "/api/static/logo.png";
    document.querySelectorAll(".scai-logo, #splashLogo").forEach((img) => {
        img.src = logoPath;
    });

    const sigColor = isLight ? "#0891b2" : "#06b6d4";
    const ecgChart = getEcgChart();
    if (ecgChart) {
        ecgChart.data.datasets[0].borderColor = sigColor;
        ecgChart.update("none");
    }
    const docChart = getDoctorChart();
    if (docChart) {
        docChart.data.datasets[0].borderColor = sigColor;
        docChart.update("none");
    }
    const wfChart = getWaveformModalChart();
    if (wfChart) {
        wfChart.data.datasets[0].borderColor = sigColor;
        wfChart.update("none");
    }
}

document.addEventListener("DOMContentLoaded", async () => {
    // Clean up any legacy insecure localStorage keys from previous versions
    ["user_admin", "active_session"].forEach((k) => localStorage.removeItem(k));

    const savedTheme = localStorage.getItem("scai_theme") || "dark";
    const savedLang = localStorage.getItem("scai_lang") || "ar";

    const themeSel = document.getElementById("settingTheme");
    const langSel = document.getElementById("settingLang");
    if (themeSel) themeSel.value = savedTheme;
    if (langSel) langSel.value = savedLang;

    applyTheme(savedTheme);
    applyLanguage(savedLang);
    updateBleStatusLabel();

    registerAuthHandlers({
        onUnauthorized: () => {
            stopPatientPolling();
            stopDoctorPolling();
            showView("loginView");
        },
        onPasswordChangeRequired: () => openForcePasswordModal(),
    });

    // Settings
    themeSel?.addEventListener("change", (e) => applyTheme(e.target.value));
    langSel?.addEventListener("change", (e) => {
        applyLanguage(e.target.value);
        updateBleStatusLabel();
    });
    document.querySelectorAll(".btn-open-settings").forEach((btn) =>
        btn.addEventListener("click", openSettingsView)
    );
    document.getElementById("btnSaveSettings")?.addEventListener("click", closeSettingsView);
    document.querySelectorAll(".btn-logout-action").forEach((btn) =>
        btn.addEventListener("click", handleLogout)
    );

    // Auth & Consent
    document.getElementById("loginForm")?.addEventListener("submit", handleLogin);
    document.getElementById("forcePasswordForm")?.addEventListener("submit", handleChangePassword);
    document.getElementById("consentCheck")?.addEventListener("change", (e) => {
        document.getElementById("consentAcceptBtn").disabled = !e.target.checked;
    });
    document.getElementById("consentAcceptBtn")?.addEventListener("click", handleAcceptConsent);

    // Patient Portal
    document.querySelectorAll("#patientView .tab-btn").forEach((btn) =>
        btn.addEventListener("click", () => switchPatientTab(btn.dataset.tab))
    );
    document.getElementById("connectBtn")?.addEventListener("click", handleConnectBle);
    document.getElementById("disconnectBtn")?.addEventListener("click", handleDisconnectBle);
    document.getElementById("diagnoseBtn")?.addEventListener("click", triggerAIDiagnosis);
    document.getElementById("btnSendToDoctor")?.addEventListener("click", submitCaseToDoctor);
    document.getElementById("resetBtn")?.addEventListener("click", resetPatientDashboard);
    document.getElementById("patientContactForm")?.addEventListener("submit", savePatientContact);
    document.getElementById("btnDownloadPDF")?.addEventListener("click", generatePatientPDF);

    // Doctor Dashboard
    document.querySelectorAll("#doctorView .tab-btn").forEach((btn) =>
        btn.addEventListener("click", () => switchDoctorTab(btn.dataset.tab))
    );
    document.getElementById("btnDocRefresh")?.addEventListener("click", () => {
        fetchDoctorKpis();
        fetchDoctorCases();
        fetchDoctorPatients();
    });
    document.getElementById("docQueueType")?.addEventListener("change", fetchDoctorCases);
    document.getElementById("docCaseSearch")?.addEventListener("input", onDoctorFilterChange);
    document.getElementById("docPriorityFilter")?.addEventListener("change", fetchDoctorCases);
    document.getElementById("docPredFilter")?.addEventListener("change", fetchDoctorCases);
    document.getElementById("docPatientSearch")?.addEventListener("input", () => fetchDoctorPatients());
    document.getElementById("btnResetZoom")?.addEventListener("click", resetDoctorChartZoom);
    document.getElementById("btnActionNoisy")?.addEventListener("click", () => submitDoctorDecision("NOISY"));
    document.getElementById("btnActionHealthy")?.addEventListener("click", () => submitDoctorDecision("HEALTHY"));
    document.getElementById("btnActionInfected")?.addEventListener("click", () => submitDoctorDecision("INFECTED"));
    document.getElementById("btnOpenAddPatient")?.addEventListener("click", openAddPatientModal);
    document.getElementById("btnCloseAddPatient")?.addEventListener("click", closeAddPatientModal);
    document.getElementById("addPatientForm")?.addEventListener("submit", submitAddPatient);
    document.getElementById("btnCloseEditClinical")?.addEventListener("click", closeEditClinicalModal);
    document.getElementById("editClinicalForm")?.addEventListener("submit", submitEditClinical);
    document.getElementById("btnCloseHistModal")?.addEventListener("click", closePatientHistoryModal);
    document.getElementById("btnCloseWaveformModal")?.addEventListener("click", closeWaveformModal);
    document.getElementById("btnResetWaveformZoom")?.addEventListener("click", resetWaveformModalZoom);

    // Admin Portal
    document.querySelectorAll("#adminView .tab-btn").forEach((btn) =>
        btn.addEventListener("click", () => switchAdminTab(btn.dataset.tab))
    );
    document.getElementById("btnExportDataset")?.addEventListener("click", downloadMLDataset);
    document.getElementById("btnExportDatasetCard")?.addEventListener("click", downloadMLDataset);
    document.getElementById("btnAdminRefresh")?.addEventListener("click", () => {
        fetchAdminStats();
        fetchAdminUsers();
    });
    document.getElementById("adminUserSearch")?.addEventListener("input", renderAdminUsers);
    document.getElementById("adminUserStatusFilter")?.addEventListener("change", renderAdminUsers);
    document.getElementById("adminAuditActionFilter")?.addEventListener("change", fetchAuditLogs);
    document.getElementById("btnOpenAddDoctor")?.addEventListener("click", openAddDoctorModal);
    document.getElementById("btnCloseAddDoctor")?.addEventListener("click", closeAddDoctorModal);
    document.getElementById("addDoctorForm")?.addEventListener("submit", submitAddDoctor);
    document.getElementById("btnOpenAdminAddPatient")?.addEventListener("click", openAdminAddPatientModal);
    document.getElementById("btnCloseAdminAddPatient")?.addEventListener("click", closeAdminAddPatientModal);
    document.getElementById("adminAddPatientForm")?.addEventListener("submit", submitAdminAddPatient);
    document.getElementById("btnCloseReassign")?.addEventListener("click", closeReassignModal);
    document.getElementById("reassignForm")?.addEventListener("submit", submitReassign);

    // Restore server session and hide splash
    await restoreSession();
    setTimeout(() => {
        document.getElementById("welcomeScreen")?.classList.add("welcome-hidden");
    }, 500);
});
