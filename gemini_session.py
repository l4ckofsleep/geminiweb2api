GEMINI_SESSION_DOMAINS = {"google.com", "gemini.google.com"}
GEMINI_SESSION_COOKIES = {
    "SID", "HSID", "SSID", "APISID", "SAPISID", "SIDCC",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
    "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PSIDTS", "__Secure-3PSIDTS",
    "__Secure-1PSIDCC", "__Secure-3PSIDCC",
    "OSID", "__Secure-OSID", "COMPASS",
}
REQUIRED_SESSION_COOKIES = {"__Secure-1PSID", "SAPISID"}


def get_gemini_session_cookies(cookies):
    """Keep the cookie fields used by the API from browser exports and saved states."""
    if not isinstance(cookies, list):
        return []

    result = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue

        name = cookie.get("name")
        value = cookie.get("value")
        domain = cookie.get("domain")
        path = cookie.get("path", "/")
        if (
            isinstance(name, str) and name in GEMINI_SESSION_COOKIES
            and isinstance(value, str) and value
            and isinstance(domain, str) and domain.lower().lstrip(".") in GEMINI_SESSION_DOMAINS
            and isinstance(path, str) and path.startswith("/")
        ):
            result.append({"name": name, "value": value, "domain": domain.lower(), "path": path})
    return result
