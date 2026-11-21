/**
 * api.js - Secure same-origin fetch wrapper (CSRF, JSON, 401/403 handling),
 * HTML escaping (XSS prevention), toast notifications, and date formatting.
 */

let _csrfToken = "";
let _currentUser = null;
let _onUnauthorized = null;
let _onPasswordChangeRequired = null;

export function setAuthState(user, csrfToken) {
    _currentUser = user || null;
    if (csrfToken !== undefined) {
        _csrfToken = csrfToken || "";
    }
}

export function getCurrentUser() {
    return _currentUser;
}

export function getCsrfToken() {
    return _csrfToken;
}

export function registerAuthHandlers({ onUnauthorized, onPasswordChangeRequired }) {
    _onUnauthorized = onUnauthorized || null;
    _onPasswordChangeRequired = onPasswordChangeRequired || null;
}

export function escapeHtml(value) {
    if (value === null || value === undefined) return "";
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

export function formatDateTime(iso) {
    if (!iso) return "--";
    try {
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return iso;
        const lang = localStorage.getItem("scai_lang") || "ar";
        return d.toLocaleString(lang === "ar" ? "ar-EG" : "en-US", {
            year: "numeric",
            month: "short",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
        });
    } catch {
        return iso;
    }
}

export function showToast(message, type = "info") {
    const container = document.getElementById("toastContainer");
    if (!container) return;
    const toast = document.createElement("div");
    toast.className = "haptic-toast";
    toast.textContent = message;
    if (type === "success") toast.style.borderInlineStartColor = "var(--medical-green)";
    if (type === "error") toast.style.borderInlineStartColor = "var(--medical-red)";
    if (type === "warn") toast.style.borderInlineStartColor = "var(--medical-yellow)";
    container.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = "0";
        toast.style.transition = "opacity 0.3s ease";
        setTimeout(() => toast.remove(), 320);
    }, 4000);
}

export async function apiFetch(url, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const headers = { ...(options.headers || {}) };

    if (options.body !== undefined && !(options.body instanceof FormData)) {
        headers["Content-Type"] = "application/json";
    }
    if (["POST", "PUT", "PATCH", "DELETE"].includes(method) && _csrfToken) {
        headers["X-CSRF-Token"] = _csrfToken;
    }

    const response = await fetch(url, {
        ...options,
        method,
        headers,
        credentials: "same-origin",
        body:
            options.body !== undefined && typeof options.body !== "string"
                ? JSON.stringify(options.body)
                : options.body,
    });

    let data = null;
    const ct = response.headers.get("Content-Type") || "";
    if (ct.includes("application/json")) {
        try {
            data = await response.json();
        } catch {
            data = null;
        }
    }

    if (response.status === 401 && !options.skipAuthRedirect) {
        setAuthState(null, "");
        if (_onUnauthorized) _onUnauthorized();
    } else if (
        response.status === 403 &&
        data &&
        data.code === "PASSWORD_CHANGE_REQUIRED" &&
        _onPasswordChangeRequired
    ) {
        _onPasswordChangeRequired();
    }

    if (!response.ok) {
        const err = new Error((data && data.error) || `Request failed (${response.status})`);
        err.status = response.status;
        err.data = data;
        throw err;
    }

    return data;
}
