/**
 * CSRF Protection Utilities
 * Automatically includes CSRF token in state-changing requests
 */

const CSRF_COOKIE_NAME = 'csrf_token';
const CSRF_HEADER_NAME = 'X-CSRF-Token';

/**
 * Get CSRF token from cookies
 */
function getCSRFToken() {
    const cookies = document.cookie.split(';');
    for (const cookie of cookies) {
        const [name, value] = cookie.trim().split('=');
        if (name === CSRF_COOKIE_NAME) {
            return decodeURIComponent(value);
        }
    }
    return null;
}

/**
 * CSRF-protected fetch wrapper
 * Automatically adds CSRF token header for POST, PUT, DELETE, PATCH requests
 *
 * @param {string} url - The URL to fetch
 * @param {Object} options - Fetch options
 * @returns {Promise<Response>}
 */
async function csrfFetch(url, options = {}) {
    const method = (options.method || 'GET').toUpperCase();

    // Add CSRF token for all authenticated requests
    // (Even GET requests may need it if the middleware expects it)
    const token = getCSRFToken();
    if (token) {
        // Ensure headers object exists
        if (!options.headers) {
            options.headers = {};
        }
        // Always include CSRF token for state-changing methods
        // For GET requests, include it if available (some endpoints may require it)
        if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(method) || token) {
            options.headers[CSRF_HEADER_NAME] = token;
        }
    } else {
        // Only warn for state-changing methods
        if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(method)) {
            console.warn(`CSRF token not found in cookies for ${method} request to ${url}`);
            console.warn('Available cookies:', document.cookie);
            // Still make the request - let the server handle the error
            // This allows for better error messages from the server
        }
    }

    return fetch(url, options);
}

/**
 * POST request with CSRF protection
 */
async function csrfPost(url, data, options = {}) {
    return csrfFetch(url, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            ...options.headers
        },
        body: JSON.stringify(data),
        ...options
    });
}

/**
 * PUT request with CSRF protection
 */
async function csrfPut(url, data, options = {}) {
    return csrfFetch(url, {
        method: 'PUT',
        headers: {
            'Content-Type': 'application/json',
            ...options.headers
        },
        body: JSON.stringify(data),
        ...options
    });
}

/**
 * DELETE request with CSRF protection
 */
async function csrfDelete(url, options = {}) {
    return csrfFetch(url, {
        method: 'DELETE',
        ...options
    });
}

// Export for module usage
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { getCSRFToken, csrfFetch, csrfPost, csrfPut, csrfDelete };
}

// Make available globally
window.csrfFetch = csrfFetch;
window.csrfPost = csrfPost;
window.csrfPut = csrfPut;
window.csrfDelete = csrfDelete;
window.getCSRFToken = getCSRFToken;
