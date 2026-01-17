// File Manager
class FileManager {
    constructor() {
        this.currentPath = '';
        this.currentView = 'grid';
        this.currentFiles = [];
        this.filteredFiles = []; // Filtered files based on search
        this.imageFiles = [];
        this.currentImageIndex = 0;
        this.selectedFiles = new Set(); // Track selected file paths
        this.currentTab = 'files'; // 'files' or 'shares'
        this.searchQuery = ''; // Current search query
        this.externalStorageMounts = []; // External storage mounts
        this.init();
    }
    
    init() {
        // Use a helper to attach event listeners with retry
        const attachButtonListener = (buttonId, handler, retries = 3) => {
            const button = document.getElementById(buttonId);
            if (button) {
                // Check if listener is already attached
                if (button.dataset.listenerAttached === 'true') {
                    console.log(`FileManager: Listener already attached to ${buttonId}`);
                    return true;
                }
                button.addEventListener('click', handler);
                button.dataset.listenerAttached = 'true';
                return true;
            } else if (retries > 0) {
                // Retry after a short delay if button doesn't exist yet
                setTimeout(() => attachButtonListener(buttonId, handler, retries - 1), 100);
            } else {
                console.warn(`FileManager: Button ${buttonId} not found after retries`);
            }
            return false;
        };
        
        // Open file manager button (from user settings)
        attachButtonListener('openFileManagerBtn', () => this.open());
        
        // Open file manager button (from chat UI)
        attachButtonListener('fileManagerBtn', (e) => {
            e.preventDefault();
            e.stopPropagation();
            console.log('FileManager: Button clicked, opening file manager...');
            this.open();
        });
        
        // Close button
        attachButtonListener('fileManagerCloseBtn', () => this.close());
        
        // Upload button
        attachButtonListener('fileManagerUploadBtn', () => this.showUploadDialog());
        
        // New folder button
        attachButtonListener('fileManagerNewFolderBtn', (e) => {
            e.preventDefault();
            e.stopPropagation();
            console.log('FileManager: New Folder button clicked (via listener)');
            this.createNewFolder();
        });
        
        // Selection controls
        attachButtonListener('fileManagerSelectAllBtn', () => this.selectAll());
        attachButtonListener('fileManagerSelectNoneBtn', () => this.selectNone());
        attachButtonListener('fileManagerDeleteBtn', () => this.deleteSelected());
        attachButtonListener('fileManagerMoveBtn', () => this.showMoveDialog());
        
        // Tab switching
        document.getElementById('fileManagerFilesTab')?.addEventListener('click', () => this.switchTab('files'));
        document.getElementById('fileManagerSharesTab')?.addEventListener('click', () => this.switchTab('shares'));
        
        // Refresh button
        attachButtonListener('fileManagerRefreshBtn', (e) => {
            e.preventDefault();
            e.stopPropagation();
            console.log('FileManager: Refresh button clicked');
            this.refresh();
        });
        
        // Email modal send button
        attachButtonListener('sendEmailBtn', () => {
            console.log('FileManager: Send Email button clicked');
            if (this.sendEmail) {
                this.sendEmail();
            } else {
                console.error('FileManager: sendEmail method not found');
                alert('Email functionality not available');
            }
        });
        
        // Email modal send button
        attachButtonListener('sendEmailBtn', () => {
            console.log('FileManager: Send Email button clicked');
            if (this.sendEmail) {
                this.sendEmail();
            } else {
                console.error('FileManager: sendEmail method not found');
                alert('Email functionality not available');
            }
        });
        
        // View toggle
        document.querySelectorAll('.view-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this.currentView = btn.dataset.view;
                this.renderFiles();
            });
        });
        
        // Search input
        const searchInput = document.getElementById('fileManagerSearchInput');
        const clearSearchBtn = document.getElementById('fileManagerClearSearchBtn');
        
        if (searchInput) {
            searchInput.addEventListener('input', (e) => {
                this.searchQuery = e.target.value.trim().toLowerCase();
                this.filterFiles();
                clearSearchBtn.style.display = this.searchQuery ? 'block' : 'none';
            });
            
            searchInput.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') {
                    this.clearSearch();
                }
            });
        }
        
        if (clearSearchBtn) {
            clearSearchBtn.addEventListener('click', () => this.clearSearch());
        }
        
        // Image viewer
        document.getElementById('imageViewerClose')?.addEventListener('click', () => this.closeImageViewer());
        document.getElementById('imageViewerPrev')?.addEventListener('click', () => this.prevImage());
        document.getElementById('imageViewerNext')?.addEventListener('click', () => this.nextImage());
    }
    
    switchTab(tab) {
        console.log('FileManager: Switching to tab:', tab);
        this.currentTab = tab;
        
        // Clear search when switching tabs
        this.clearSearch();
        
        // Update tab buttons - use specific IDs
        const filesTab = document.getElementById('fileManagerFilesTab');
        const sharesTab = document.getElementById('fileManagerSharesTab');
        
        if (filesTab) filesTab.classList.remove('active');
        if (sharesTab) sharesTab.classList.remove('active');
        
        if (tab === 'files') {
            if (filesTab) filesTab.classList.add('active');
        } else if (tab === 'shares') {
            if (sharesTab) {
                sharesTab.classList.add('active');
                console.log('FileManager: Shares tab activated');
            } else {
                console.error('FileManager: fileManagerSharesTab button not found!');
            }
        }
        
        // Show/hide content areas
        const grid = document.getElementById('fileManagerGrid');
        const shares = document.getElementById('fileManagerShares');
        const viewToggle = document.getElementById('fileViewToggle');
        const selectionControls = document.getElementById('fileSelectionControls');
        
        if (tab === 'files') {
            if (grid) grid.style.display = 'block';
            if (shares) shares.style.display = 'none';
            if (viewToggle) viewToggle.style.display = 'flex';
            if (selectionControls) selectionControls.style.display = this.selectedFiles.size > 0 ? 'flex' : 'none';
        } else if (tab === 'shares') {
            if (grid) grid.style.display = 'none';
            if (shares) {
                shares.style.display = 'block';
                console.log('FileManager: Shares content area displayed');
            } else {
                console.error('FileManager: fileManagerShares content area not found!');
            }
            if (viewToggle) viewToggle.style.display = 'none';
            if (selectionControls) selectionControls.style.display = 'none';
            this.loadSharedFiles();
        }
    }
    
    async loadSharedFiles() {
        const sharesDiv = document.getElementById('fileManagerShares');
        if (!sharesDiv) {
            console.error('FileManager: fileManagerShares element not found!');
            return;
        }
        
        console.log('FileManager: Loading shared files...');
        sharesDiv.innerHTML = '<div class="file-manager-loading">Loading shared files...</div>';
        
        try {
            const response = await csrfFetch('/api/files/shares');
            if (response.ok) {
                const data = await response.json();
                console.log('FileManager: Loaded shared files:', data.shares?.length || 0);
                this.renderSharedFiles(data.shares || []);
            } else {
                const error = await response.json();
                console.error('FileManager: Error loading shared files:', error);
                sharesDiv.innerHTML = `<div class="file-manager-error">Error: ${this.escapeHtml(error.detail || 'Failed to load shared files')}</div>`;
            }
        } catch (error) {
            console.error('Error loading shared files:', error);
            sharesDiv.innerHTML = '<div class="file-manager-error">Error loading shared files: ' + this.escapeHtml(error.message || 'Network error') + '</div>';
        }
    }
    
    renderSharedFiles(shares) {
        const sharesDiv = document.getElementById('fileManagerShares');
        if (!sharesDiv) return;
        
        if (shares.length === 0) {
            sharesDiv.innerHTML = '<div class="file-manager-empty">No shared files</div>';
            return;
        }
        
        const baseUrl = window.location.origin;
        
        sharesDiv.innerHTML = `
            <table class="shares-table">
                <thead>
                    <tr>
                        <th>File</th>
                        <th>Share URL</th>
                        <th>Created</th>
                        <th>Expires</th>
                        <th>Access Count</th>
                        <th>Status</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    ${shares.map(share => {
                        const createdDate = new Date(share.created_at);
                        const expiresDate = share.expires_at ? new Date(share.expires_at) : null;
                        const fullUrl = baseUrl + share.share_url;
                        const status = share.is_expired ? 'Expired' : (share.is_limit_reached ? 'Limit Reached' : 'Active');
                        const statusClass = share.is_expired || share.is_limit_reached ? 'share-status-inactive' : 'share-status-active';
                        
                        return `
                            <tr>
                                <td>
                                    <div class="share-file-info">
                                        <span class="share-file-icon">${this.getFileIcon(share.filename)}</span>
                                        <div>
                                            <div class="share-file-name">${this.escapeHtml(share.filename)}</div>
                                            <div class="share-file-path">${this.escapeHtml(share.file_path)}</div>
                                        </div>
                                    </div>
                                </td>
                                <td>
                                    <div class="share-url-container">
                                        <input type="text" class="share-url-input" value="${this.escapeHtml(fullUrl)}" readonly>
                                        <button class="btn-secondary btn-small" onclick="fileManager.copyShareUrl('${this.escapeHtml(fullUrl)}')" title="Copy URL">📋</button>
                                    </div>
                                </td>
                                <td>${createdDate.toLocaleString()}</td>
                                <td>${expiresDate ? expiresDate.toLocaleString() : 'Never'}</td>
                                <td>${share.access_count || 0}${share.max_accesses ? ` / ${share.max_accesses}` : ''}</td>
                                <td><span class="share-status ${statusClass}">${status}</span></td>
                                <td>
                                    <button class="btn-danger btn-small" onclick="fileManager.unshareFile(${share.id}, '${this.escapeJs(share.filename)}')" title="Unshare">🗑️ Unshare</button>
                                </td>
                            </tr>
                        `;
                    }).join('')}
                </tbody>
            </table>
        `;
    }
    
    copyShareUrl(url) {
        navigator.clipboard.writeText(url).then(() => {
            alert('Share URL copied to clipboard!');
        }).catch(() => {
            // Fallback
            const input = document.createElement('input');
            input.value = url;
            document.body.appendChild(input);
            input.select();
            document.execCommand('copy');
            document.body.removeChild(input);
            alert('Share URL copied to clipboard!');
        });
    }
    
    async unshareFile(shareId, filename) {
        if (!confirm(`Are you sure you want to unshare "${filename}"? This will make the share URL inaccessible.`)) {
            return;
        }
        
        try {
            const response = await csrfFetch(`/api/files/shares/${shareId}`, {
                method: 'DELETE'
            });
            
            if (response.ok) {
                alert('Share revoked successfully');
                await this.loadSharedFiles(); // Reload the list
            } else {
                const error = await response.json();
                alert('Error: ' + (error.detail || 'Failed to revoke share'));
            }
        } catch (error) {
            console.error('Error revoking share:', error);
            alert('Error revoking share. Please try again.');
        }
    }
    
    async open() {
        console.log('FileManager: open() called');
        const overlay = document.getElementById('fileManagerOverlay');
        if (overlay) {
            console.log('FileManager: Overlay found, displaying...');
            overlay.style.display = 'flex'; // Use flex for modal
            
            // Ensure we're on the files tab by default
            this.switchTab('files');
            
            await this.loadFiles('');
            console.log('FileManager: Overlay displayed and files loaded');
        } else {
            console.error('FileManager: fileManagerOverlay not found!');
            console.error('FileManager: Available elements with "file" in id:', Array.from(document.querySelectorAll('[id*="file"]')).map(el => el.id));
        }
    }
    
    close() {
        const overlay = document.getElementById('fileManagerOverlay');
        if (overlay) {
            overlay.style.display = 'none';
        }
    }
    
    async loadExternalStorageMounts() {
        try {
            const response = await fetch('/api/files/external-storage');
            if (response.ok) {
                const data = await response.json();
                this.externalStorageMounts = data.mounts || [];
                console.log('FileManager: Loaded external storage mounts:', this.externalStorageMounts.length);
            } else {
                console.warn('FileManager: Failed to load external storage mounts:', response.status);
                this.externalStorageMounts = [];
            }
        } catch (error) {
            console.error('Error loading external storage mounts:', error);
            this.externalStorageMounts = [];
        }
    }
    
    async loadFiles(path) {
        // Load external storage mounts first if at root
        if (!path) {
            await this.loadExternalStorageMounts();
        }
        this.currentPath = path;
        // Clear selection when navigating
        this.selectedFiles.clear();
        const grid = document.getElementById('fileManagerGrid');
        if (grid) {
            grid.innerHTML = '<div class="file-manager-loading">Loading files...</div>';
        }
        
        try {
            const url = `/api/files/list${path ? '?path=' + encodeURIComponent(path) : ''}`;
            const response = await fetch(url);
            if (response.ok) {
                const data = await response.json();
                this.currentFiles = data.items;
                
                // Prepend external storage mounts if at root
                if (!path && this.externalStorageMounts && this.externalStorageMounts.length > 0) {
                    console.log('FileManager: Adding external storage mounts to file list:', this.externalStorageMounts.length);
                    const externalItems = this.externalStorageMounts.map(mount => ({
                        name: mount.name,
                        path: mount.mount_point,
                        is_directory: true,
                        size: 0,
                        modified: 0,
                        is_external: true,
                        external_name: mount.name,
                        description: mount.description || ''
                    }));
                    this.currentFiles = [...externalItems, ...this.currentFiles];
                    console.log('FileManager: Total files after adding external mounts:', this.currentFiles.length);
                }
                
                this.filterFiles(); // Apply current search filter if any
                this.updateBreadcrumb(path);
                this.updateStorageInfo(data.storage);
                this.renderFiles();
            } else {
                const error = await response.json();
                if (grid) {
                    grid.innerHTML = `<div class="file-manager-error">Error: ${this.escapeHtml(error.detail || 'Failed to load files')}</div>`;
                }
            }
        } catch (error) {
            console.error('Error loading files:', error);
            if (grid) {
                grid.innerHTML = `<div class="file-manager-error">Error loading files</div>`;
            }
        }
    }
    
    async refresh() {
        console.log('FileManager: Refreshing current directory...');
        // Invalidate cache first if we're on the files tab
        if (this.currentTab === 'files') {
            try {
                // Invalidate cache for current path
                const cachePath = this.currentPath || '';
                await fetch(`/api/files/invalidate-cache${cachePath ? '?path=' + encodeURIComponent(cachePath) : ''}`, {
                    method: 'POST'
                });
            } catch (error) {
                console.warn('FileManager: Failed to invalidate cache:', error);
            }
            // Reload current directory
            await this.loadFiles(this.currentPath);
        } else if (this.currentTab === 'shares') {
            // Reload shared files
            await this.loadSharedFiles();
        }
    }
    
    updateBreadcrumb(path) {
        const breadcrumb = document.getElementById('fileManagerBreadcrumb');
        if (!breadcrumb) return;
        
        const parts = path ? path.split('/').filter(p => p) : [];
        let html = '<button class="breadcrumb-item" data-path="">Home</button>';
        
        // Check if first part is an external storage mount
        let isExternalPath = false;
        if (parts.length > 0 && this.externalStorageMounts) {
            const firstPart = parts[0];
            const mount = this.externalStorageMounts.find(m => m.mount_point === firstPart);
            if (mount) {
                isExternalPath = true;
                html += ` <span class="breadcrumb-separator">/</span> <button class="breadcrumb-item external-storage-breadcrumb" data-path="${firstPart}" title="${this.escapeHtml(mount.name)}">💾 ${this.escapeHtml(mount.name)}</button>`;
                
                // Add remaining parts
                let currentPath = firstPart;
                for (let i = 1; i < parts.length; i++) {
                    currentPath += '/' + parts[i];
                    html += ` <span class="breadcrumb-separator">/</span> <button class="breadcrumb-item" data-path="${currentPath}">${this.escapeHtml(parts[i])}</button>`;
                }
            }
        }
        
        if (!isExternalPath) {
            // Regular path
            let currentPath = '';
            parts.forEach(part => {
                currentPath += (currentPath ? '/' : '') + part;
                html += ` <span class="breadcrumb-separator">/</span> <button class="breadcrumb-item" data-path="${currentPath}">${this.escapeHtml(part)}</button>`;
            });
        }
        
        breadcrumb.innerHTML = html;
        
        // Add click handlers
        breadcrumb.querySelectorAll('.breadcrumb-item').forEach(btn => {
            btn.addEventListener('click', () => {
                this.loadFiles(btn.dataset.path);
            });
        });
    }
    
    updateStorageInfo(storage) {
        const storageDiv = document.getElementById('fileManagerStorage');
        if (!storageDiv) return;
        
        const used_mb = storage.used_mb.toFixed(1);
        const quota_mb = storage.quota_mb > 0 ? storage.quota_mb.toFixed(1) : '∞';
        const percent = storage.unlimited ? 0 : Math.min(100, (storage.used / storage.quota) * 100);
        
        storageDiv.innerHTML = `
            <div class="storage-info">
                <div class="storage-label">Storage Usage</div>
                <div class="storage-bar-container">
                    <div class="storage-bar">
                        <div class="storage-bar-fill" style="width: ${percent}%"></div>
                    </div>
                </div>
                <div class="storage-text">${used_mb} MB / ${quota_mb} MB</div>
            </div>
        `;
    }
    
    filterFiles() {
        if (!this.searchQuery) {
            this.filteredFiles = [...this.currentFiles]; // Copy array
        } else {
            const query = this.searchQuery.toLowerCase();
            this.filteredFiles = this.currentFiles.filter(item => {
                const name = item.name.toLowerCase();
                const path = item.path.toLowerCase();
                return name.includes(query) || path.includes(query);
            });
        }
        this.renderFiles();
    }
    
    clearSearch() {
        const searchInput = document.getElementById('fileManagerSearchInput');
        const clearSearchBtn = document.getElementById('fileManagerClearSearchBtn');
        if (searchInput) {
            searchInput.value = '';
            this.searchQuery = '';
            this.filterFiles();
        }
        if (clearSearchBtn) {
            clearSearchBtn.style.display = 'none';
        }
    }
    
    renderFiles() {
        const filesToRender = this.searchQuery ? this.filteredFiles : this.currentFiles;
        const grid = document.getElementById('fileManagerGrid');
        if (!grid) return;
        
        if (filesToRender.length === 0) {
            const message = this.searchQuery 
                ? `<div class="file-manager-empty">No files found matching "${this.escapeHtml(this.searchQuery)}"</div>`
                : '<div class="file-manager-empty">No files in this directory</div>';
            grid.innerHTML = message;
            this.updateSelectionUI();
            return;
        }
        
        if (this.currentView === 'grid') {
            grid.className = 'file-manager-grid';
            grid.innerHTML = filesToRender.map(item => {
                // Use special icon for external storage
                const icon = item.is_external ? '💾' : (item.is_directory ? '📂' : this.getFileIcon(item.name));
                const thumbnail = item.thumbnail ? `<img src="${item.thumbnail}" alt="" class="file-thumbnail">` : '';
                const isSelected = this.selectedFiles.has(item.path);
                const isExternal = item.is_external || false;
                const actions = !item.is_directory ? `
                    <div class="file-actions" onclick="event.stopPropagation();">
                        <button class="file-action-btn" title="Email" onclick="if(window.fileManager && window.fileManager.emailFile){window.fileManager.emailFile('${this.escapeJs(item.path)}', '${this.escapeJs(item.name)}');}else{alert('Email functionality not available');}">📧</button>
                        <button class="file-action-btn" title="Share" onclick="if(window.fileManager && window.fileManager.shareFile){window.fileManager.shareFile('${this.escapeJs(item.path)}', '${this.escapeJs(item.name)}');}else{alert('Share functionality not available');}">🔗</button>
                    </div>
                ` : '';
                return `
                    <div class="file-item ${item.is_directory ? 'directory' : 'file'} ${isSelected ? 'selected' : ''} ${isExternal ? 'external-storage' : ''}" 
                         data-path="${this.escapeHtml(item.path)}" 
                         data-is-dir="${item.is_directory}"
                         data-is-external="${isExternal}"
                         data-name="${this.escapeHtml(item.name)}"
                         oncontextmenu="event.preventDefault(); fileManager.showContextMenu(event, '${this.escapeJs(item.path)}', '${this.escapeJs(item.name)}', ${item.is_directory ? 'true' : 'false'});"
                         onclick="if(event.ctrlKey || event.metaKey) { fileManager.toggleSelection('${this.escapeJs(item.path)}', !fileManager.selectedFiles.has('${this.escapeJs(item.path)}')); } else if (!event.target.closest('.file-actions') && !event.target.closest('.file-checkbox')) { ${item.is_directory ? `fileManager.loadFiles('${this.escapeJs(item.path)}');` : `fileManager.openFile('${this.escapeJs(item.path)}');`} }">
                        <input type="checkbox" class="file-checkbox" ${isSelected ? 'checked' : ''} 
                               onchange="fileManager.toggleSelection('${this.escapeJs(item.path)}', this.checked)"
                               onclick="event.stopPropagation();">
                        <div class="file-icon">${thumbnail || icon}</div>
                        <div class="file-name" title="${this.escapeHtml(item.name)}${item.description ? ' - ' + this.escapeHtml(item.description) : ''}">
                            ${this.escapeHtml(item.name)}
                            ${isExternal ? '<span class="external-badge" title="External Storage">💾</span>' : ''}
                        </div>
                        ${!item.is_directory ? `<div class="file-size">${this.formatSize(item.size)}</div>` : ''}
                        ${actions}
                    </div>
                `;
            }).join('');
        } else {
            grid.className = 'file-manager-list';
            grid.innerHTML = `
                <table class="file-list-table">
                    <thead>
                        <tr>
                            <th style="width: 30px;"><input type="checkbox" id="selectAllCheckbox" onchange="fileManager.toggleSelectAll(this.checked)"></th>
                            <th>Name</th>
                            <th>Size</th>
                            <th>Modified</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${filesToRender.map(item => {
                            const date = new Date(item.modified * 1000);
                            const isSelected = this.selectedFiles.has(item.path);
                            const isExternal = item.is_external || false;
                            const icon = isExternal ? '💾' : (item.is_directory ? '📂' : this.getFileIcon(item.name));
                            const actions = !item.is_directory ? `
                                <td>
                                    <button class="file-action-btn" title="Email" onclick="if(window.fileManager && window.fileManager.emailFile){window.fileManager.emailFile('${this.escapeJs(item.path)}', '${this.escapeJs(item.name)}');}else{alert('Email functionality not available');}">📧</button>
                                    <button class="file-action-btn" title="Share" onclick="if(window.fileManager && window.fileManager.shareFile){window.fileManager.shareFile('${this.escapeJs(item.path)}', '${this.escapeJs(item.name)}');}else{alert('Share functionality not available');}">🔗</button>
                                </td>
                            ` : '<td></td>';
                            return `
                                <tr class="file-list-row ${item.is_directory ? 'directory' : 'file'} ${isSelected ? 'selected' : ''} ${isExternal ? 'external-storage' : ''}" 
                                    data-path="${this.escapeHtml(item.path)}" 
                                    data-is-dir="${item.is_directory}"
                                    data-is-external="${isExternal}"
                                    data-name="${this.escapeHtml(item.name)}"
                                    oncontextmenu="event.preventDefault(); fileManager.showContextMenu(event, '${this.escapeJs(item.path)}', '${this.escapeJs(item.name)}', ${item.is_directory ? 'true' : 'false'});"
                                    onclick="if(event.ctrlKey || event.metaKey) { fileManager.toggleSelection('${this.escapeJs(item.path)}', !fileManager.selectedFiles.has('${this.escapeJs(item.path)}')); } else if (!event.target.closest('td:last-child') && !event.target.closest('.file-checkbox')) { ${item.is_directory ? `fileManager.loadFiles('${this.escapeJs(item.path)}');` : `fileManager.openFile('${this.escapeJs(item.path)}');`} }">
                                    <td><input type="checkbox" class="file-checkbox" ${isSelected ? 'checked' : ''} 
                                               onchange="fileManager.toggleSelection('${this.escapeJs(item.path)}', this.checked)"
                                               onclick="event.stopPropagation();"></td>
                                    <td>${icon} ${this.escapeHtml(item.name)}${isExternal ? ' <span class="external-badge" title="External Storage">💾</span>' : ''}</td>
                                    <td>${item.is_directory ? '-' : this.formatSize(item.size)}</td>
                                    <td>${date.toLocaleString()}</td>
                                    ${actions}
                                </tr>
                            `;
                        }).join('')}
                    </tbody>
                </table>
            `;
        }
        
        // Add click handlers
        grid.querySelectorAll('.file-item, .file-list-row').forEach(item => {
            item.addEventListener('click', (e) => {
                // Don't trigger if clicking on checkboxes or action buttons
                if (e.target.closest('.file-checkbox, .file-actions, .file-action-btn')) {
                    return;
                }
                const path = item.dataset.path;
                const isDir = item.dataset.isDir === 'true';
                
                if (isDir) {
                    this.loadFiles(path);
                } else {
                    // Check if it's an image
                    const ext = path.split('.').pop().toLowerCase();
                    if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp'].includes(ext)) {
                        this.openImageViewer(path);
                    } else {
                        // Download file
                        window.open(`/api/files/view/${encodeURIComponent(path)}`, '_blank');
                    }
                }
            });
        });
        
        this.updateSelectionUI();
    }
    
    async emailFile(filePath, fileName) {
        console.log('FileManager: emailFile called', { filePath, fileName });
        try {
            // Show email modal
            const modal = document.getElementById('fileEmailModal');
            if (!modal) {
                console.error('FileManager: fileEmailModal not found!');
                alert('Email modal not found. Please refresh the page.');
                return;
            }
            
            const emailFilePathInput = document.getElementById('emailFilePath');
            const emailFileNameSpan = document.getElementById('emailFileName');
            const emailToInput = document.getElementById('emailTo');
            const emailSubjectInput = document.getElementById('emailSubject');
            const emailBodyInput = document.getElementById('emailBody');
            
            if (!emailFilePathInput || !emailFileNameSpan || !emailToInput || !emailSubjectInput || !emailBodyInput) {
                console.error('FileManager: Required email modal elements not found!');
                alert('Email form elements not found. Please refresh the page.');
                return;
            }
            
            emailFilePathInput.value = filePath || '';
            emailFilePathInput.dataset.apiUrl = ''; // Clear any previous API URL
            emailFileNameSpan.textContent = fileName || 'Unknown file';
            emailToInput.value = '';
            emailSubjectInput.value = `Shared file: ${fileName || 'file'}`;
            emailBodyInput.value = `Please find the attached file: ${fileName || 'file'}`;
            
            // Load contact emails for autocomplete
            await this.loadContactEmailsForAutocomplete();
            
            modal.style.display = 'flex'; // Use flex like other modals
            // Focus on email input
            setTimeout(() => {
                if (emailToInput) emailToInput.focus();
            }, 100);
        } catch (error) {
            console.error('FileManager: Error in emailFile:', error);
            alert('Error opening email dialog: ' + (error.message || 'Unknown error'));
        }
    }
    
    async loadContactEmailsForAutocomplete() {
        try {
            const response = await fetch('/api/contacts/emails');
            if (response.ok) {
                const contacts = await response.json();
                const datalist = document.getElementById('emailToAutocomplete');
                if (datalist && contacts && Array.isArray(contacts)) {
                    // Clear existing options
                    datalist.innerHTML = '';
                    
                    // Add contact emails to datalist
                    contacts.forEach(contact => {
                        const option = document.createElement('option');
                        // Store email as value (what gets inserted when selected)
                        option.value = contact.email;
                        // Show formatted name+email in dropdown (what user sees)
                        // This allows matching by name or email
                        if (contact.name && contact.name.toLowerCase() !== contact.email.split('@')[0].toLowerCase()) {
                            option.textContent = `${contact.name} <${contact.email}>`;
                        } else {
                            option.textContent = contact.email;
                        }
                        datalist.appendChild(option);
                    });
                    
                    console.log(`Loaded ${contacts.length} contacts for email autocomplete`);
                }
            }
        } catch (e) {
            console.debug('Could not load contact emails for autocomplete:', e);
        }
    }
    
    async shareFile(filePath, fileName) {
        // Show share modal
        const modal = document.getElementById('fileShareModal');
        if (modal) {
            document.getElementById('shareFilePath').value = filePath;
            document.getElementById('shareFileName').textContent = fileName;
            document.getElementById('shareExpiresHours').value = '';
            document.getElementById('shareMaxAccesses').value = '';
            document.getElementById('shareUrl').value = '';
            document.getElementById('shareUrlDisplay').style.display = 'none';
            modal.style.display = 'flex'; // Use flex like other modals
        }
    }
    
    async sendEmail() {
        const emailFilePathInput = document.getElementById('emailFilePath');
        const emailToInput = document.getElementById('emailTo');
        const emailSubjectInput = document.getElementById('emailSubject');
        const emailBodyInput = document.getElementById('emailBody');
        
        if (!emailFilePathInput || !emailToInput || !emailSubjectInput || !emailBodyInput) {
            console.error('FileManager: Required email form elements not found');
            alert('Email form error. Please refresh the page.');
            return;
        }
        
        const filePath = emailFilePathInput.value;
        const apiUrl = emailFilePathInput.dataset.apiUrl; // For note attachments
        let to = emailToInput.value.trim();
        const subject = emailSubjectInput.value.trim();
        const body = emailBodyInput.value.trim();
        
        if (!to) {
            alert('Please enter recipient email address');
            return;
        }
        
        // Extract email from "Name <email>" format if present
        const emailMatch = to.match(/<([^>]+)>/);
        if (emailMatch) {
            to = emailMatch[1];
        }
        
        // Basic email validation
        const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
        if (!emailRegex.test(to)) {
            alert('Please enter a valid email address');
            return;
        }
        
        try {
            // Check if this is a note attachment (has apiUrl in data attribute)
            const requestBody = {
                to: to,
                subject: subject || 'Shared file',
                body: body || 'Please find the attached file.'
            };
            
            if (apiUrl) {
                // For note attachments, send the API URL
                requestBody.file_urls = [apiUrl];
            } else {
                // For regular files, send file paths
                requestBody.file_paths = [filePath];
            }
            
            const response = await csrfFetch('/api/files/email', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(requestBody)
            });
            
            const data = await response.json();
            if (response.ok) {
                alert('Email sent successfully!');
                const emailModal = document.getElementById('fileEmailModal');
                if (emailModal) emailModal.style.display = 'none';
            } else {
                alert('Error: ' + (data.detail || 'Failed to send email'));
            }
        } catch (error) {
            console.error('Error sending email:', error);
            alert('Error sending email. Please try again.');
        }
    }
    
    async createShare() {
        const filePath = document.getElementById('shareFilePath').value;
        const expiresHours = document.getElementById('shareExpiresHours').value;
        const maxAccesses = document.getElementById('shareMaxAccesses').value;
        
        try {
            const response = await csrfFetch('/api/files/share', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    file_path: filePath,
                    expires_hours: expiresHours ? parseInt(expiresHours) : null,
                    max_accesses: maxAccesses ? parseInt(maxAccesses) : null
                })
            });
            
            const data = await response.json();
            if (response.ok) {
                // Construct full URL
                const baseUrl = window.location.origin;
                const fullUrl = baseUrl + data.share_url;
                document.getElementById('shareUrl').value = fullUrl;
                document.getElementById('shareUrlDisplay').style.display = 'block';
                
                // Copy to clipboard
                document.getElementById('shareUrl').select();
                document.execCommand('copy');
                alert('Share URL created and copied to clipboard!');
            } else {
                alert('Error: ' + (data.detail || 'Failed to create share'));
            }
        } catch (error) {
            console.error('Error creating share:', error);
            alert('Error creating share. Please try again.');
        }
    }
    
    openImageViewer(path) {
        // Find all images in current directory
        this.imageFiles = this.currentFiles
            .filter(f => !f.is_directory && ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp'].includes(f.name.split('.').pop().toLowerCase()))
            .map(f => f.path);
        
        this.currentImageIndex = this.imageFiles.indexOf(path);
        if (this.currentImageIndex === -1) this.currentImageIndex = 0;
        
        this.showImage();
    }
    
    showImage() {
        if (this.imageFiles.length === 0) return;
        
        const imagePath = this.imageFiles[this.currentImageIndex];
        const modal = document.getElementById('imageViewerModal');
        const img = document.getElementById('imageViewerImage');
        const info = document.getElementById('imageViewerInfo');
        
        if (modal && img) {
            img.src = `/api/files/view/${encodeURIComponent(imagePath)}`;
            if (info) {
                info.textContent = `${this.currentImageIndex + 1} / ${this.imageFiles.length} - ${imagePath.split('/').pop()}`;
            }
            modal.style.display = 'block';
            
            // Update nav buttons
            const prevBtn = document.getElementById('imageViewerPrev');
            const nextBtn = document.getElementById('imageViewerNext');
            if (prevBtn) prevBtn.style.display = this.currentImageIndex > 0 ? 'block' : 'none';
            if (nextBtn) nextBtn.style.display = this.currentImageIndex < this.imageFiles.length - 1 ? 'block' : 'none';
        }
    }
    
    prevImage() {
        if (this.currentImageIndex > 0) {
            this.currentImageIndex--;
            this.showImage();
        }
    }
    
    nextImage() {
        if (this.currentImageIndex < this.imageFiles.length - 1) {
            this.currentImageIndex++;
            this.showImage();
        }
    }
    
    closeImageViewer() {
        const modal = document.getElementById('imageViewerModal');
        if (modal) {
            modal.style.display = 'none';
        }
    }
    
    getFileIcon(filename) {
        const ext = filename.split('.').pop().toLowerCase();
        const icons = {
            'jpg': '🖼️', 'jpeg': '🖼️', 'png': '🖼️', 'gif': '🖼️', 'webp': '🖼️', 'bmp': '🖼️',
            'pdf': '📄', 'doc': '📄', 'docx': '📄',
            'mp3': '🎵', 'wav': '🎵', 'flac': '🎵',
            'mp4': '🎬', 'avi': '🎬', 'mkv': '🎬',
            'zip': '📦', 'rar': '📦', 'tar': '📦',
            'txt': '📝', 'md': '📝',
        };
        return icons[ext] || '📄';
    }
    
    formatSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    }
    
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
    
    /**
     * Escape JavaScript string delimiters for use in JavaScript string literals
     * Prevents XSS when inserting user input into onclick handlers
     */
    escapeJs(text) {
        if (text == null) return '';
        return String(text)
            .replace(/\\/g, '\\\\')  // Escape backslashes first
            .replace(/'/g, "\\'")    // Escape single quotes
            .replace(/"/g, '\\"')     // Escape double quotes
            .replace(/\n/g, '\\n')   // Escape newlines
            .replace(/\r/g, '\\r')   // Escape carriage returns
            .replace(/\t/g, '\\t');    // Escape tabs
    }
    
    showContextMenu(event, filePath, fileName, isDirectory) {
        const menu = document.getElementById('fileContextMenu');
        if (!menu) return;
        
        // Store context for menu actions
        this.contextMenuPath = filePath;
        this.contextMenuName = fileName;
        this.contextMenuIsDir = isDirectory;
        
        // Position menu at cursor
        menu.style.display = 'block';
        menu.style.left = event.pageX + 'px';
        menu.style.top = event.pageY + 'px';
        
        // Hide menu when clicking elsewhere
        const hideMenu = (e) => {
            if (!menu.contains(e.target)) {
                menu.style.display = 'none';
                document.removeEventListener('click', hideMenu);
            }
        };
        setTimeout(() => document.addEventListener('click', hideMenu), 10);
    }
    
    async contextMenuAction(action) {
        const menu = document.getElementById('fileContextMenu');
        if (menu) menu.style.display = 'none';
        
        const filePath = this.contextMenuPath;
        const fileName = this.contextMenuName;
        const isDirectory = this.contextMenuIsDir;
        
        if (!filePath) return;
        
        switch(action) {
            case 'delete':
                if (confirm(`Are you sure you want to delete "${fileName}"?`)) {
                    await this.deleteFile(filePath);
                }
                break;
            case 'email':
                if (!isDirectory) {
                    await this.emailFile(filePath, fileName);
                } else {
                    alert('Cannot email directories');
                }
                break;
            case 'share':
                if (!isDirectory) {
                    await this.shareFile(filePath, fileName);
                } else {
                    alert('Cannot share directories');
                }
                break;
            case 'preview':
                if (!isDirectory) {
                    await this.previewUrl(filePath, fileName);
                } else {
                    alert('Cannot preview directories');
                }
                break;
        }
    }
    
    async deleteFile(filePath) {
        try {
            // Use the same endpoint as deleteSelected but for a single file
            const response = await csrfFetch('/api/files/delete-bulk', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ file_paths: [filePath] })
            });
            
            const data = await response.json();
            if (response.ok) {
                if (data.errors && data.errors.length > 0) {
                    alert(`Error: ${data.errors.join(', ')}`);
                } else {
                    await this.loadFiles(this.currentPath);
                }
            } else {
                alert('Error: ' + (data.detail || 'Failed to delete file'));
            }
        } catch (error) {
            console.error('Error deleting file:', error);
            alert('Error deleting file. Please try again.');
        }
    }
    
    async previewUrl(filePath, fileName) {
        // Check if file already has a share link
        try {
            const sharesResponse = await csrfFetch('/api/files/shares');
            if (sharesResponse.ok) {
                const shares = await sharesResponse.json();
                const existingShare = shares.find(s => s.file_path === filePath);
                
                if (existingShare) {
                    // Show existing share URL
                    const baseUrl = window.location.origin;
                    const fullUrl = baseUrl + existingShare.share_url;
                    this.showUrlPreview(fullUrl, fileName, true);
                } else {
                    // Create a quick share link
                    const response = await csrfFetch('/api/files/share', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            file_path: filePath,
                            expires_hours: null,
                            max_accesses: null
                        })
                    });
                    
                    const data = await response.json();
                    if (response.ok) {
                        const baseUrl = window.location.origin;
                        const fullUrl = baseUrl + data.share_url;
                        this.showUrlPreview(fullUrl, fileName, false);
                    } else {
                        alert('Error: ' + (data.detail || 'Failed to create share link'));
                    }
                }
            }
        } catch (error) {
            console.error('Error getting preview URL:', error);
            alert('Error getting preview URL. Please try again.');
        }
    }
    
    showUrlPreview(url, fileName, isExisting) {
        const message = isExisting 
            ? `Public URL for "${fileName}":\n\n${url}\n\n(Copied to clipboard)`
            : `Public URL created for "${fileName}":\n\n${url}\n\n(Copied to clipboard)`;
        
        // Copy to clipboard
        navigator.clipboard.writeText(url).then(() => {
            alert(message);
        }).catch(() => {
            // Fallback for older browsers
            const textarea = document.createElement('textarea');
            textarea.value = url;
            document.body.appendChild(textarea);
            textarea.select();
            document.execCommand('copy');
            document.body.removeChild(textarea);
            alert(message);
        });
    }
    
    openFile(filePath) {
        // Check if it's an image file
        const ext = filePath.split('.').pop().toLowerCase();
        const imageExts = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp'];
        
        if (imageExts.includes(ext)) {
            // Use image viewer for images
            this.openImageViewer(filePath);
        } else {
            // Open other files in new tab
            const url = `/api/files/view/${encodeURIComponent(filePath)}`;
            window.open(url, '_blank');
        }
    }
    
    showUploadDialog() {
        // Create a hidden file input
        const input = document.createElement('input');
        input.type = 'file';
        input.multiple = true;
        input.style.display = 'none';
        
        input.addEventListener('change', async (e) => {
            const files = Array.from(e.target.files);
            if (files.length === 0) return;
            
            // Show loading indicator
            const grid = document.getElementById('fileManagerGrid');
            const originalContent = grid ? grid.innerHTML : '';
            if (grid) {
                grid.innerHTML = '<div class="file-manager-loading">Uploading files...</div>';
            }
            
            try {
                // Upload each file
                for (const file of files) {
                    const formData = new FormData();
                    formData.append('file', file);
                    formData.append('path', this.currentPath);
                    
                    const response = await csrfFetch('/api/files/upload', {
                        method: 'POST',
                        body: formData
                    });
                    
                    if (!response.ok) {
                        const error = await response.json();
                        throw new Error(error.detail || 'Upload failed');
                    }
                }
                
                // Reload files after upload
                await this.loadFiles(this.currentPath);
            } catch (error) {
                console.error('Error uploading file:', error);
                alert(`Error uploading file: ${error.message || 'Unknown error'}`);
                if (grid) {
                    grid.innerHTML = originalContent;
                }
            } finally {
                // Remove the input element
                document.body.removeChild(input);
            }
        });
        
        // Trigger file picker
        document.body.appendChild(input);
        input.click();
    }
    
    async createNewFolder() {
        console.log('FileManager: createNewFolder() called');
        
        // Prevent multiple simultaneous prompts
        if (this._creatingFolder) {
            console.log('FileManager: Folder creation already in progress');
            return;
        }
        
        this._creatingFolder = true;
        try {
            const folderName = prompt('Enter folder name:');
            if (!folderName || !folderName.trim()) {
                console.log('FileManager: Folder name cancelled or empty');
                return;
            }
            
            const safeName = folderName.trim();
            const targetPath = this.currentPath ? `${this.currentPath}/${safeName}` : safeName;
            console.log(`FileManager: Creating folder at path: ${targetPath}`);
            
            const formData = new FormData();
            formData.append('path', targetPath);
            
            console.log('FileManager: Sending mkdir request...');
            const response = await csrfFetch('/api/files/mkdir', {
                method: 'POST',
                body: formData
            });
            
            console.log(`FileManager: mkdir response status: ${response.status}`);
            
            if (response.ok) {
                const data = await response.json();
                console.log('FileManager: Folder created successfully:', data);
                // Reload files to show new folder
                await this.loadFiles(this.currentPath);
            } else {
                const error = await response.json();
                console.error('FileManager: mkdir error:', error);
                throw new Error(error.detail || 'Failed to create folder');
            }
        } catch (error) {
            console.error('Error creating folder:', error);
            alert(`Error creating folder: ${error.message || 'Unknown error'}`);
        } finally {
            this._creatingFolder = false;
        }
    }
    
    // Selection management
    toggleSelection(filePath, checked) {
        if (checked) {
            this.selectedFiles.add(filePath);
        } else {
            this.selectedFiles.delete(filePath);
        }
        this.updateSelectionUI();
    }
    
    selectAll() {
        this.currentFiles.forEach(item => {
            this.selectedFiles.add(item.path);
        });
        this.renderFiles(); // Re-render to update checkboxes
        this.updateSelectionUI();
    }
    
    selectNone() {
        this.selectedFiles.clear();
        this.renderFiles(); // Re-render to update checkboxes
        this.updateSelectionUI();
    }
    
    toggleSelectAll(checked) {
        if (checked) {
            this.selectAll();
        } else {
            this.selectNone();
        }
    }
    
    updateSelectionUI() {
        const controls = document.getElementById('fileSelectionControls');
        const count = document.getElementById('fileSelectionCount');
        const selectAllCheckbox = document.getElementById('selectAllCheckbox');
        
        const selectedCount = this.selectedFiles.size;
        
        if (controls) {
            controls.style.display = selectedCount > 0 ? 'flex' : 'none';
        }
        
        if (count) {
            count.textContent = `${selectedCount} selected`;
        }
        
        if (selectAllCheckbox) {
            selectAllCheckbox.checked = selectedCount > 0 && selectedCount === this.currentFiles.length;
        }
    }
    
    async deleteSelected() {
        const selected = Array.from(this.selectedFiles);
        if (selected.length === 0) {
            alert('No files selected');
            return;
        }
        
        const count = selected.length;
        const confirmMsg = `Are you sure you want to delete ${count} item(s)? This action cannot be undone.`;
        if (!confirm(confirmMsg)) {
            return;
        }
        
        try {
            const response = await csrfFetch('/api/files/delete-bulk', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ file_paths: selected })
            });
            
            const data = await response.json();
            if (response.ok) {
                if (data.errors && data.errors.length > 0) {
                    alert(`Deleted ${data.deleted.length} item(s). Errors: ${data.errors.join(', ')}`);
                } else {
                    alert(`Successfully deleted ${data.deleted.length} item(s)`);
                }
                this.selectedFiles.clear();
                await this.loadFiles(this.currentPath);
            } else {
                alert('Error: ' + (data.detail || 'Failed to delete files'));
            }
        } catch (error) {
            console.error('Error deleting files:', error);
            alert('Error deleting files. Please try again.');
        }
    }
    
    showMoveDialog() {
        const selected = Array.from(this.selectedFiles);
        if (selected.length === 0) {
            alert('No files selected');
            return;
        }
        
        const modal = document.getElementById('fileMoveModal');
        const countSpan = document.getElementById('moveFileCount');
        const destinationInput = document.getElementById('moveDestination');
        
        if (modal && countSpan && destinationInput) {
            countSpan.textContent = selected.length;
            destinationInput.value = '';
            modal.style.display = 'block';
            this.loadMoveDestinationFolders();
        }
    }
    
    async loadMoveDestinationFolders() {
        // Load all directories for browsing
        const foldersDiv = document.getElementById('moveDestinationFolders');
        if (!foldersDiv) return;
        
        try {
            const response = await fetch('/api/files/list?path=');
            if (response.ok) {
                const data = await response.json();
                const dirs = data.items.filter(item => item.is_directory);
                
                foldersDiv.innerHTML = dirs.map(dir => {
                    return `<button type="button" class="btn-secondary folder-btn" onclick="fileManager.selectMoveDestination('${this.escapeJs(dir.path)}')">📂 ${this.escapeHtml(dir.name)}</button>`;
                }).join('');
            }
        } catch (error) {
            console.error('Error loading folders:', error);
        }
    }
    
    browseMoveDestination(path) {
        const destinationInput = document.getElementById('moveDestination');
        if (destinationInput) {
            destinationInput.value = path || '';
        }
    }
    
    selectMoveDestination(path) {
        const destinationInput = document.getElementById('moveDestination');
        if (destinationInput) {
            destinationInput.value = path;
        }
    }
    
    async executeMove() {
        const selected = Array.from(this.selectedFiles);
        if (selected.length === 0) {
            alert('No files selected');
            return;
        }
        
        const destinationInput = document.getElementById('moveDestination');
        if (!destinationInput) return;
        
        const destination = destinationInput.value.trim();
        
        try {
            const response = await csrfFetch('/api/files/move', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    file_paths: selected,
                    destination: destination
                })
            });
            
            const data = await response.json();
            if (response.ok) {
                if (data.errors && data.errors.length > 0) {
                    alert(`Moved ${data.moved.length} item(s). Errors: ${data.errors.join(', ')}`);
                } else {
                    alert(`Successfully moved ${data.moved.length} item(s)`);
                }
                this.selectedFiles.clear();
                document.getElementById('fileMoveModal').style.display = 'none';
                await this.loadFiles(this.currentPath);
            } else {
                alert('Error: ' + (data.detail || 'Failed to move files'));
            }
        } catch (error) {
            console.error('Error moving files:', error);
            alert('Error moving files. Please try again.');
        }
    }
}

// Initialize file manager
let fileManager;

// Function to ensure button handler is attached (with retry logic)
function ensureFileManagerButtonHandler() {
    const fileManagerBtn = document.getElementById('fileManagerBtn');
    if (fileManagerBtn) {
        // Check if already attached
        if (fileManagerBtn.dataset.listenerAttached === 'true') {
            return; // Already attached
        }
        
        // Remove any existing listeners by cloning the node
        const newBtn = fileManagerBtn.cloneNode(true);
        fileManagerBtn.parentNode.replaceChild(newBtn, fileManagerBtn);
        newBtn.dataset.listenerAttached = 'true';
        
        newBtn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            console.log('FileManager: Button clicked (direct handler)');
            if (window.fileManager) {
                window.fileManager.open();
            } else {
                console.error('FileManager: window.fileManager is not available, initializing...');
                fileManager = new FileManager();
                window.fileManager = fileManager;
                window.fileManager.open();
            }
        });
        
        console.log('FileManager: Direct button handler attached');
        return true;
    } else {
        console.warn('FileManager: fileManagerBtn not found');
        return false;
    }
}

// Initialize immediately if DOM is ready, otherwise wait for DOMContentLoaded
function initializeFileManager() {
    if (!window.fileManager) {
        fileManager = new FileManager();
        window.fileManager = fileManager;
        console.log('FileManager: Initialized');
        
        // Verify methods are available
        if (typeof window.fileManager.refresh === 'function') {
            console.log('FileManager: refresh method available');
        } else {
            console.error('FileManager: refresh method NOT available');
        }
        if (typeof window.fileManager.createNewFolder === 'function') {
            console.log('FileManager: createNewFolder method available');
        } else {
            console.error('FileManager: createNewFolder method NOT available');
        }
    }
    ensureFileManagerButtonHandler();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        initializeFileManager();
    });
} else {
    // DOM is already ready
    initializeFileManager();
}

// Also try to attach handler after delays as fallbacks (in case DOM loads late)
setTimeout(() => {
    if (!window.fileManager) {
        initializeFileManager();
    }
    if (!ensureFileManagerButtonHandler()) {
        setTimeout(() => {
            if (!window.fileManager) {
                initializeFileManager();
            }
            ensureFileManagerButtonHandler();
        }, 1000);
    }
}, 500);

// Global function for copying addresses
function copyAddress(inputId) {
    const input = document.getElementById(inputId);
    if (!input || !input.value) {
        alert('No address to copy');
        return;
    }
    
    input.select();
    input.setSelectionRange(0, 99999); // For mobile devices
    
    try {
        document.execCommand('copy');
        alert('Address copied to clipboard!');
    } catch (err) {
        // Fallback: use Clipboard API
        navigator.clipboard.writeText(input.value).then(() => {
            alert('Address copied to clipboard!');
        }).catch(() => {
            alert('Failed to copy. Please select and copy manually.');
        });
    }
}
