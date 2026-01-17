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
            // Handle URL-encoded values
            try {
                return decodeURIComponent(value);
            } catch (e) {
                return value;
            }
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

    // Ensure headers object exists
    if (!options.headers) {
        options.headers = {};
    }
    
    // Ensure credentials are included
    if (!options.credentials) {
        options.credentials = 'include';
    }

    // Add CSRF token for all authenticated requests
    const token = getCSRFToken();
    if (token) {
        // Always include CSRF token for state-changing methods
        if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(method)) {
            options.headers[CSRF_HEADER_NAME] = token;
        }
    } else {
        // For state-changing methods, try to get token from a GET request first
        if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(method)) {
            console.warn(`CSRF token not found in cookies for ${method} request to ${url}`);
            console.warn('Available cookies:', document.cookie);
            
            // Try to fetch the token by making a GET request to trigger cookie setting
            try {
                await fetch('/', { method: 'GET', credentials: 'include' });
                // Wait for cookie to be set
                await new Promise(resolve => setTimeout(resolve, 200));
                const retryToken = getCSRFToken();
                if (retryToken) {
                    options.headers[CSRF_HEADER_NAME] = retryToken;
                    console.log('CSRF token retrieved after retry');
                } else {
                    console.error('CSRF token still not available after retry');
                    console.error('Cookies after retry:', document.cookie);
                }
            } catch (e) {
                console.error('Failed to retrieve CSRF token:', e);
            }
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
// Also export as getCSRFToken for compatibility
window.getCSRFToken = getCSRFToken;
