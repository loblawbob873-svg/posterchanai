/**
 * CSRF Protection Utilities - DISABLED
 * CSRF protection has been completely disabled.
 * This is now just a simple fetch wrapper that ensures credentials are included.
 */

/**
 * Simple fetch wrapper (CSRF disabled)
 * Just ensures credentials are included for authenticated requests
 *
 * @param {string} url - The URL to fetch
 * @param {Object} options - Fetch options
 * @returns {Promise<Response>}
 */
async function csrfFetch(url, options = {}) {
    // Ensure headers object exists
    if (!options.headers) {
        options.headers = {};
    }
    
    // Ensure credentials are included for authenticated requests
    if (!options.credentials) {
        options.credentials = 'include';
    }

    // No CSRF token handling - CSRF is disabled
    return fetch(url, options);
}

/**
 * Get CSRF token (compatibility function - always returns null since CSRF is disabled)
 */
function getCSRFToken() {
    return null;
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
// Also export as getCSRFToken for compatibility
window.getCSRFToken = getCSRFToken;
