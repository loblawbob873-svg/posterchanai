/**
 * RAG Modal functionality
 * Handles RAG collection management and settings
 */
document.addEventListener('DOMContentLoaded', function() {
    const ragBtn = document.getElementById('ragBtn');
    const ragModal = document.getElementById('ragModal');
    const closeRagModal = document.getElementById('closeRagModal');
    const ragContextEnabled = document.getElementById('ragContextEnabled');
    const ragUserCollections = document.getElementById('ragUserCollections');
    const ragStatusModel = document.getElementById('ragStatusModel');
    const ragStatusDocs = document.getElementById('ragStatusDocs');
    const saveRagSettings = document.getElementById('saveRagSettings');
    const ragUploadPatterns = document.getElementById('ragUploadPatterns');

    // Load RAG state from localStorage (default to disabled)
    let ragEnabled = localStorage.getItem('ragEnabled') === 'true';
    let selectedCollections = JSON.parse(localStorage.getItem('ragSelectedCollections') || '[]');

    // Load file patterns from localStorage
    const savedPatterns = localStorage.getItem('ragFilePatterns');
    if (savedPatterns && ragUploadPatterns) {
        ragUploadPatterns.value = savedPatterns;
    }

    // Save file patterns when changed
    if (ragUploadPatterns) {
        ragUploadPatterns.addEventListener('blur', () => {
            localStorage.setItem('ragFilePatterns', ragUploadPatterns.value);
        });
    }

    // Update button state - only active when enabled AND has collections
    function updateRagButton() {
        if (ragEnabled && selectedCollections.length > 0) {
            ragBtn.classList.add('active');
            ragBtn.title = `RAG: ${selectedCollections.length} collection(s) active`;
        } else {
            ragBtn.classList.remove('active');
            ragBtn.title = ragEnabled ? 'RAG: No collections selected' : 'RAG Context';
        }
    }

    // Open modal
    ragBtn.addEventListener('click', async () => {
        ragModal.style.display = 'flex';
        ragContextEnabled.checked = ragEnabled;
        await loadRagCollections();
        await loadRagStatus();
    });

    // Close modal
    closeRagModal.addEventListener('click', () => {
        ragModal.style.display = 'none';
    });

    ragModal.addEventListener('click', (e) => {
        if (e.target === ragModal) {
            ragModal.style.display = 'none';
        }
    });

    // Helper function to escape HTML
    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // Load collections
    async function loadRagCollections() {
        try {
            const response = await fetch('/api/rag/collections');
            if (response.ok) {
                const collections = await response.json();

                if (collections.length === 0) {
                    ragUserCollections.innerHTML = '<div class="rag-empty-msg">No collections yet. Upload a zip file below to get started.</div>';
                    return;
                }

                ragUserCollections.innerHTML = collections.map(c => {
                    const lastIndexed = c.last_indexed_at ? new Date(c.last_indexed_at).toLocaleString() : 'Never';
                    return `
                    <label class="rag-collection-checkbox">
                        <input type="checkbox" value="${c.id}" ${selectedCollections.includes(c.id) ? 'checked' : ''}>
                        <span class="collection-name">${escapeHtml(c.name)}</span>
                        <span class="collection-stats">${c.document_count || 0} docs</span>
                        <span class="collection-indexed" title="Last indexed: ${lastIndexed}">📅 ${c.last_indexed_at ? new Date(c.last_indexed_at).toLocaleDateString() : '-'}</span>
                        ${c.collection_type === 'git' ? `<button type="button" class="btn-pull-collection" data-id="${c.id}" title="Pull & Re-index">↻</button>` : ''}
                        <button type="button" class="btn-delete-collection" data-id="${c.id}" title="Delete collection">×</button>
                    </label>
                `}).join('');

                // Add delete handlers
                ragUserCollections.querySelectorAll('.btn-delete-collection').forEach(btn => {
                    btn.addEventListener('click', async (e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        const id = btn.dataset.id;
                        if (confirm('Delete this collection? This cannot be undone.')) {
                            try {
                                const resp = await fetch(`/api/rag/collections/${id}`, { method: 'DELETE' });
                                if (resp.ok) {
                                    // Remove from selected collections
                                    selectedCollections = selectedCollections.filter(cid => cid !== parseInt(id));
                                    localStorage.setItem('ragSelectedCollections', JSON.stringify(selectedCollections));
                                    await loadRagCollections();
                                    updateRagButton();
                                } else {
                                    alert('Failed to delete collection');
                                }
                            } catch (err) {
                                alert('Error deleting collection');
                            }
                        }
                    });
                });

                // Add pull handlers for git collections
                ragUserCollections.querySelectorAll('.btn-pull-collection').forEach(btn => {
                    btn.addEventListener('click', async (e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        const id = btn.dataset.id;

                        // Get current last_indexed_at before starting
                        let originalIndexTime = null;
                        try {
                            const preResp = await fetch('/api/rag/collections');
                            if (preResp.ok) {
                                const preCols = await preResp.json();
                                const preCol = preCols.find(c => c.id === parseInt(id));
                                if (preCol && preCol.last_indexed_at) {
                                    originalIndexTime = new Date(preCol.last_indexed_at).getTime();
                                }
                            }
                        } catch (e) {}

                        try {
                            btn.disabled = true;
                            btn.textContent = '⏳';
                            btn.title = 'Pulling & indexing...';
                            btn.classList.add('pulling');
                            const resp = await fetch(`/api/rag/collections/${id}/pull`, { method: 'POST' });
                            if (resp.ok) {
                                // Keep showing spinner while polling for completion
                                let attempts = 0;
                                const checkInterval = setInterval(async () => {
                                    attempts++;
                                    try {
                                        const checkResp = await fetch('/api/rag/collections');
                                        if (checkResp.ok) {
                                            const cols = await checkResp.json();
                                            const col = cols.find(c => c.id === parseInt(id));
                                            if (col && col.last_indexed_at) {
                                                const indexedTime = new Date(col.last_indexed_at).getTime();
                                                // Check if last_indexed_at changed from before
                                                if (originalIndexTime === null || indexedTime > originalIndexTime) {
                                                    clearInterval(checkInterval);
                                                    btn.textContent = '✓';
                                                    btn.title = 'Done! Indexed ' + new Date(col.last_indexed_at).toLocaleString();
                                                    btn.classList.remove('pulling');
                                                    btn.classList.add('done');
                                                    // Show success briefly then refresh
                                                    setTimeout(async () => {
                                                        await loadRagCollections();
                                                    }, 1500);
                                                    return;
                                                }
                                            }
                                        }
                                    } catch (e) {}
                                    // Update spinner to show progress
                                    btn.title = `Indexing... (${attempts * 3}s)`;
                                    // Stop polling after 5 minutes
                                    if (attempts >= 100) {
                                        clearInterval(checkInterval);
                                        btn.textContent = '?';
                                        btn.title = 'Timeout - check logs';
                                        btn.classList.remove('pulling');
                                        setTimeout(() => loadRagCollections(), 2000);
                                    }
                                }, 3000);
                            } else {
                                const data = await resp.json();
                                alert(data.detail || 'Failed to pull');
                                btn.disabled = false;
                                btn.textContent = '↻';
                                btn.title = 'Pull & Re-index';
                                btn.classList.remove('pulling');
                            }
                        } catch (err) {
                            alert('Error pulling repository');
                            btn.disabled = false;
                            btn.textContent = '↻';
                            btn.title = 'Pull & Re-index';
                            btn.classList.remove('pulling');
                        }
                    });
                });
            }
        } catch (err) {
            console.error('Failed to load RAG collections:', err);
            ragUserCollections.innerHTML = '<div class="rag-empty-msg">Failed to load collections</div>';
        }
    }

    // Load RAG status
    async function loadRagStatus() {
        try {
            const response = await fetch('/api/rag/status');
            if (response.ok) {
                const data = await response.json();
                ragStatusModel.textContent = data.model_name || 'Not loaded';
                ragStatusDocs.textContent = data.total_documents || 0;
            }
        } catch (err) {
            console.error('Failed to load RAG status:', err);
        }
    }

    // Save settings
    saveRagSettings.addEventListener('click', () => {
        ragEnabled = ragContextEnabled.checked;
        localStorage.setItem('ragEnabled', ragEnabled);

        // Get selected collections
        const checkboxes = ragUserCollections.querySelectorAll('input[type="checkbox"]:checked');
        selectedCollections = Array.from(checkboxes).map(cb => parseInt(cb.value));
        localStorage.setItem('ragSelectedCollections', JSON.stringify(selectedCollections));

        updateRagButton();
        ragModal.style.display = 'none';

        // Update global state for chat.js to use
        window.ragEnabled = ragEnabled;
        window.ragCollections = selectedCollections;
    });

    // Upload handling
    const ragDropzone = document.getElementById('ragDropzone');
    const ragUploadFile = document.getElementById('ragUploadFile');
    const ragFileName = document.getElementById('ragFileName');
    const ragUploadBtn = document.getElementById('ragUploadBtn');
    const ragUploadName = document.getElementById('ragUploadName');
    const ragUploadStatus = document.getElementById('ragUploadStatus');

    if (ragDropzone && ragUploadFile) {
        // Note: ragDropzone is a <label> wrapping the input, so clicks automatically trigger file dialog
        ragDropzone.addEventListener('dragover', (e) => {
            e.preventDefault();
            ragDropzone.classList.add('dragover');
        });
        ragDropzone.addEventListener('dragleave', () => ragDropzone.classList.remove('dragover'));
        ragDropzone.addEventListener('drop', (e) => {
            e.preventDefault();
            ragDropzone.classList.remove('dragover');
            if (e.dataTransfer.files.length > 0) {
                ragUploadFile.files = e.dataTransfer.files;
                ragFileName.textContent = e.dataTransfer.files[0].name;
                ragUploadBtn.disabled = !ragUploadName.value.trim();
            }
        });
        ragUploadFile.addEventListener('change', () => {
            if (ragUploadFile.files.length > 0) {
                ragFileName.textContent = ragUploadFile.files[0].name;
                ragUploadBtn.disabled = !ragUploadName.value.trim();
            }
        });
        ragUploadName.addEventListener('input', () => {
            ragUploadBtn.disabled = !ragUploadName.value.trim() || !ragUploadFile.files.length;
        });
        ragUploadBtn.addEventListener('click', async () => {
            if (!ragUploadFile.files.length || !ragUploadName.value.trim()) return;

            ragUploadStatus.textContent = 'Uploading...';
            ragUploadStatus.className = 'settings-status';
            ragUploadBtn.disabled = true;

            const formData = new FormData();
            formData.append('name', ragUploadName.value.trim());
            formData.append('file_patterns', ragUploadPatterns.value.trim());
            formData.append('file', ragUploadFile.files[0]);

            try {
                const response = await fetch('/api/rag/collections/upload', {
                    method: 'POST',
                    body: formData
                });
                if (response.ok) {
                    ragUploadStatus.textContent = 'Uploaded! Indexing in background...';
                    ragUploadStatus.className = 'settings-status success';
                    ragUploadName.value = '';
                    ragFileName.textContent = '';
                    ragUploadFile.value = '';
                    await loadRagCollections();
                } else {
                    const err = await response.json();
                    ragUploadStatus.textContent = err.detail || 'Upload failed';
                    ragUploadStatus.className = 'settings-status error';
                }
            } catch (e) {
                ragUploadStatus.textContent = 'Upload failed';
                ragUploadStatus.className = 'settings-status error';
            }
            ragUploadBtn.disabled = false;
        });
    }

    // Initialize
    updateRagButton();
    window.ragEnabled = ragEnabled;
    window.ragCollections = selectedCollections;
});
