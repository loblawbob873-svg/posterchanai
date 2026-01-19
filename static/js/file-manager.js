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
        this.uploadQueue = []; // Upload queue
        this.uploadMonitorVisible = false;
        this.uploadMonitorMinimized = false;
        this.pictureViewerOpen = false;
        this.fullscreenViewerOpen = false;
        this.allImages = [];
        this.currentImageIndex = 0;
        this.imageLoadOffset = 0;
        this.imageLoadLimit = 50;
        this.hasMoreImages = true;
        this.loadingImages = false; // Track if images are currently loading
        this.scrollObserver = null; // Intersection observer for infinite scroll
        this.currentAudioPlayer = null; // Track currently playing audio
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
        
        // Upload directory button
        attachButtonListener('fileManagerUploadDirBtn', () => this.showUploadDirectoryDialog());
        
        // Mobile upload buttons
        attachButtonListener('mobileUploadFileBtn', () => this.showMobileFileUpload());
        attachButtonListener('mobileUploadCameraBtn', () => this.showMobileCameraUpload());
        
        // Mobile upload area tap handler
        const mobileUploadArea = document.getElementById('mobileUploadArea');
        if (mobileUploadArea) {
            mobileUploadArea.addEventListener('click', (e) => {
                // Only trigger if clicking on the area itself, not buttons
                if (e.target === mobileUploadArea || e.target.classList.contains('mobile-upload-content')) {
                    this.showMobileFileUpload();
                }
            });
        }
        
        // Mobile file input handlers
        const mobileFileInput = document.getElementById('mobileFileInput');
        if (mobileFileInput) {
            mobileFileInput.addEventListener('change', (e) => this.handleMobileFileUpload(e));
        }
        
        const mobileCameraInput = document.getElementById('mobileCameraInput');
        if (mobileCameraInput) {
            mobileCameraInput.addEventListener('change', (e) => this.handleMobileFileUpload(e));
        }
        
        // Detect mobile and show mobile upload area
        this.detectMobile();
        
        // New folder button
        attachButtonListener('fileManagerNewFolderBtn', (e) => {
            e.preventDefault();
            e.stopPropagation();
            console.log('FileManager: New Folder button clicked (via listener)');
            this.createNewFolder();
        });
        
        // Scan storage button
        attachButtonListener('fileManagerScanBtn', () => {
            this.scanStorage();
        });
        
        // Picture viewer button
        attachButtonListener('fileManagerPictureViewerBtn', () => {
            this.openPictureViewer();
        });
        
        // Selection controls
        attachButtonListener('fileManagerSelectAllBtn', () => this.selectAll());
        attachButtonListener('fileManagerSelectNoneBtn', () => this.selectNone());
        attachButtonListener('fileManagerDeleteBtn', () => this.deleteSelected());
        attachButtonListener('fileManagerMoveBtn', () => this.showMoveDialog());
        
        // Tab switching
        document.getElementById('fileManagerFilesTab')?.addEventListener('click', () => this.switchTab('files'));
        document.getElementById('fileManagerSharesTab')?.addEventListener('click', () => this.switchTab('shares'));
        
        // Picture viewer buttons
        attachButtonListener('pictureViewerCloseBtn', () => this.closePictureViewer());
        attachButtonListener('pictureViewerRefreshBtn', () => this.loadAllImages());
        attachButtonListener('pictureViewerLoadMoreBtn', () => this.loadMoreImages());
        attachButtonListener('cyberpunkFullscreenClose', () => this.closeFullscreenViewer());
        attachButtonListener('cyberpunkFullscreenPrev', () => this.prevFullscreenImage());
        attachButtonListener('cyberpunkFullscreenNext', () => this.nextFullscreenImage());
        
        // Keyboard shortcuts for picture viewer
        document.addEventListener('keydown', (e) => {
            if (this.pictureViewerOpen) {
                if (e.key === 'Escape') {
                    if (this.fullscreenViewerOpen) {
                        this.closeFullscreenViewer();
                    } else {
                        this.closePictureViewer();
                    }
                } else if (e.key === 'ArrowLeft' && this.fullscreenViewerOpen) {
                    this.prevFullscreenImage();
                } else if (e.key === 'ArrowRight' && this.fullscreenViewerOpen) {
                    this.nextFullscreenImage();
                }
            }
        });
        
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
        
        // Upload monitor
        document.getElementById('uploadMonitorMinimizeBtn')?.addEventListener('click', () => this.toggleUploadMonitorMinimize());
        document.getElementById('uploadMonitorCloseBtn')?.addEventListener('click', () => this.hideUploadMonitor());
        document.getElementById('uploadMonitorClearBtn')?.addEventListener('click', () => this.clearCompletedUploads());
        document.getElementById('uploadMonitorCancelAllBtn')?.addEventListener('click', () => this.cancelAllUploads());
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
            // Keep selection controls always visible (user requested)
            if (selectionControls) selectionControls.style.display = 'flex';
        } else if (tab === 'shares') {
            if (grid) grid.style.display = 'none';
            if (shares) {
                shares.style.display = 'block';
                console.log('FileManager: Shares content area displayed');
            } else {
                console.error('FileManager: fileManagerShares content area not found!');
            }
            if (viewToggle) viewToggle.style.display = 'none';
            // Keep selection controls always visible (user requested)
            if (selectionControls) selectionControls.style.display = 'flex';
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
            
            // Detect mobile and show/hide mobile upload area
            this.detectMobile();
            
            // Add resize listener for mobile detection on orientation change
            if (!this._resizeListener) {
                this._resizeListener = () => {
                    this.detectMobile();
                };
                window.addEventListener('resize', this._resizeListener);
                window.addEventListener('orientationchange', this._resizeListener);
            }
            
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
        
        // Remove resize and orientation listeners when closing
        if (this._resizeListener) {
            window.removeEventListener('resize', this._resizeListener);
            window.removeEventListener('orientationchange', this._resizeListener);
            this._resizeListener = null;
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
                const isAudio = !item.is_directory && this.isAudioFile(item.path);
                const isVideo = !item.is_directory && this.isVideoFile(item.path);
                const actions = !item.is_directory ? `
                    <div class="file-actions" onclick="event.stopPropagation();">
                        ${isAudio ? `<button class="file-action-btn file-play-btn" title="Play Audio" onclick="if(window.fileManager && window.fileManager.openAudioPlayer){window.fileManager.openAudioPlayer('${this.escapeJs(item.path)}', '${this.escapeJs(item.name)}');}else{alert('Audio player not available');}">▶</button>` : ''}
                        ${isVideo ? `<button class="file-action-btn file-play-btn" title="Play Video" onclick="if(window.fileManager && window.fileManager.openVideoPlayer){window.fileManager.openVideoPlayer('${this.escapeJs(item.path)}', '${this.escapeJs(item.name)}');}else{alert('Video player not available');}">▶</button>` : ''}
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
                            const isAudio = !item.is_directory && this.isAudioFile(item.path);
                            const isVideo = !item.is_directory && this.isVideoFile(item.path);
                            const actions = !item.is_directory ? `
                                <td>
                                    ${isAudio ? `<button class="file-action-btn file-play-btn" title="Play Audio" onclick="if(window.fileManager && window.fileManager.openAudioPlayer){window.fileManager.openAudioPlayer('${this.escapeJs(item.path)}', '${this.escapeJs(item.name)}');}else{alert('Audio player not available');}">▶</button>` : ''}
                                    ${isVideo ? `<button class="file-action-btn file-play-btn" title="Play Video" onclick="if(window.fileManager && window.fileManager.openVideoPlayer){window.fileManager.openVideoPlayer('${this.escapeJs(item.path)}', '${this.escapeJs(item.name)}');}else{alert('Video player not available');}">▶</button>` : ''}
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
                    // Check file type and handle accordingly
                    const ext = path.split('.').pop().toLowerCase();
                    if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp'].includes(ext)) {
                        this.openImageViewer(path);
                    } else if (this.isAudioFile(path)) {
                        this.openAudioPlayer(path, item.dataset.name);
                    } else if (this.isVideoFile(path)) {
                        this.openVideoPlayer(path, item.dataset.name);
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
    
    isAudioFile(filePath) {
        const ext = filePath.split('.').pop().toLowerCase();
        const audioExtensions = ['mp3', 'flac', 'ogg', 'wav', 'm4a', 'aac', 'opus', 'wma'];
        return audioExtensions.includes(ext);
    }
    
    isVideoFile(filePath) {
        const ext = filePath.split('.').pop().toLowerCase();
        const videoExtensions = ['mp4', 'avi', 'mov', 'mkv', 'webm', 'flv', 'wmv', 'm4v', '3gp', 'ogv'];
        return videoExtensions.includes(ext);
    }
    
    getFileIcon(filename) {
        const ext = filename.split('.').pop().toLowerCase();
        const icons = {
            'jpg': '🖼️', 'jpeg': '🖼️', 'png': '🖼️', 'gif': '🖼️', 'webp': '🖼️', 'bmp': '🖼️',
            'pdf': '📄', 'doc': '📄', 'docx': '📄',
            'mp3': '🎵', 'wav': '🎵', 'flac': '🎵', 'ogg': '🎵', 'm4a': '🎵', 'aac': '🎵', 'opus': '🎵', 'wma': '🎵',
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
        // Check file type and handle accordingly
        const ext = filePath.split('.').pop().toLowerCase();
        const imageExts = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp'];
        
        if (imageExts.includes(ext)) {
            // Use image viewer for images
            this.openImageViewer(filePath);
        } else if (this.isAudioFile(filePath)) {
            // Extract filename from path for display
            const fileName = filePath.split('/').pop();
            this.openAudioPlayer(filePath, fileName);
        } else if (this.isVideoFile(filePath)) {
            // Extract filename from path for display
            const fileName = filePath.split('/').pop();
            this.openVideoPlayer(filePath, fileName);
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
        
        input.addEventListener('change', (e) => {
            const files = Array.from(e.target.files);
            if (files.length === 0) return;
            
            // Add files to upload queue
            this.addToUploadQueue(files, this.currentPath);
            
            // Remove the input element
            document.body.removeChild(input);
        });
        
        // Trigger file picker
        document.body.appendChild(input);
        input.click();
    }
    
    showUploadDirectoryDialog() {
        // Create a hidden file input with webkitdirectory attribute
        const input = document.createElement('input');
        input.type = 'file';
        input.webkitdirectory = true;
        input.directory = true; // Fallback for Firefox
        input.multiple = true;
        input.style.display = 'none';
        
        input.addEventListener('change', async (e) => {
            const files = Array.from(e.target.files);
            if (files.length === 0) return;
            
            // Group files by their relative path to preserve directory structure
            const fileMap = new Map();
            const basePath = this.currentPath || '';
            
            files.forEach(file => {
                // Get the relative path from the file's webkitRelativePath
                // webkitRelativePath format: "folder/subfolder/file.txt"
                const relativePath = file.webkitRelativePath || file.name;
                const pathParts = relativePath.split('/');
                const fileName = pathParts.pop();
                const dirPath = pathParts.join('/');
                
                // Build the full target path
                const targetDir = basePath ? `${basePath}/${dirPath}` : dirPath;
                
                if (!fileMap.has(targetDir)) {
                    fileMap.set(targetDir, []);
                }
                fileMap.get(targetDir).push({ file, fileName });
            });
            
            // Create directories first
            for (const dirPath of fileMap.keys()) {
                if (dirPath) {
                    try {
                        const formData = new FormData();
                        formData.append('path', dirPath);
                        
                        const mkdirResponse = await csrfFetch('/api/files/mkdir', {
                            method: 'POST',
                            body: formData
                        });
                        
                        if (!mkdirResponse.ok && mkdirResponse.status !== 400) {
                            // 400 means directory already exists, which is fine
                            const error = await mkdirResponse.json();
                            console.warn(`Warning creating directory ${dirPath}:`, error.detail || 'Directory creation failed');
                        }
                    } catch (dirError) {
                        console.warn(`Warning creating directory ${dirPath}:`, dirError);
                        // Continue anyway - directory might already exist
                    }
                }
            }
            
            // Add all files to upload queue with their respective paths
            for (const [dirPath, fileList] of fileMap.entries()) {
                const filesToUpload = fileList.map(item => item.file);
                this.addToUploadQueue(filesToUpload, dirPath);
            }
            
            // Remove the input element
            document.body.removeChild(input);
        });
        
        // Trigger file picker
        document.body.appendChild(input);
        input.click();
    }
    
    detectMobile() {
        // Check if device is mobile
        const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent) ||
                         (window.innerWidth <= 768 && 'ontouchstart' in window);
        
        // Mobile upload area is now hidden - use header buttons (📤 Upload Files, 📁 Upload Directory) instead
        const mobileUploadArea = document.getElementById('mobileUploadArea');
        if (mobileUploadArea) {
            mobileUploadArea.style.display = 'none';
        }
    }
    
    showMobileFileUpload() {
        const input = document.getElementById('mobileFileInput');
        if (input) {
            input.click();
        }
    }
    
    showMobileCameraUpload() {
        const input = document.getElementById('mobileCameraInput');
        if (input) {
            input.click();
        }
    }
    
    async handleMobileFileUpload(e) {
        const files = Array.from(e.target.files);
        if (files.length === 0) return;
        
        // Add files to upload queue
        this.addToUploadQueue(files, this.currentPath);
        
        // Reset inputs
        const mobileFileInput = document.getElementById('mobileFileInput');
        const mobileCameraInput = document.getElementById('mobileCameraInput');
        if (mobileFileInput) mobileFileInput.value = '';
        if (mobileCameraInput) mobileCameraInput.value = '';
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
    
    // Scan storage for EXIF timestamps and thumbnails
    async scanStorage() {
        console.log('FileManager: scanStorage() called');
        
        if (this._scanning) {
            console.log('FileManager: Scan already in progress');
            return;
        }
        
        if (!confirm('Scan storage to restore EXIF timestamps and generate thumbnails?\n\nThis may take several minutes for large photo collections.')) {
            return;
        }
        
        this._scanning = true;
        const scanBtn = document.getElementById('fileManagerScanBtn');
        const originalTitle = scanBtn?.getAttribute('title') || '';
        
        try {
            if (scanBtn) {
                scanBtn.innerHTML = '⏳';
                scanBtn.setAttribute('title', 'Scanning...');
                scanBtn.disabled = true;
            }
            
            const response = await csrfFetch('/api/admin/storage/rescan', {
                method: 'POST'
            });
            
            if (response.ok) {
                const data = await response.json();
                let summary = 'Storage scan completed!\n\n';
                if (data.results && data.results.length > 0) {
                    const result = data.results[0];
                    summary += `Files scanned: ${result.files || 0}\n`;
                    summary += `EXIF restored: ${result.exif_restored || 0}\n`;
                    summary += `Thumbnails generated: ${result.thumbnails_generated || 0}`;
                }
                alert(summary);
                await this.loadFiles(this.currentPath);
            } else {
                const error = await response.json();
                throw new Error(error.detail || 'Scan failed');
            }
        } catch (error) {
            console.error('Error scanning storage:', error);
            alert(`Scan error: ${error.message || 'Unknown error'}`);
        } finally {
            this._scanning = false;
            if (scanBtn) {
                scanBtn.innerHTML = '🔄';
                scanBtn.setAttribute('title', originalTitle);
                scanBtn.disabled = false;
            }
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
            // Keep controls always visible (user requested)
            controls.style.display = 'flex';
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
    
    // Upload Monitoring Methods
    addToUploadQueue(files, path = '') {
        const uploadItems = files.map(file => ({
            id: Date.now() + Math.random(),
            file: file,
            name: file.name,
            size: file.size,
            path: path,
            status: 'pending', // pending, uploading, completed, failed, cancelled
            progress: 0,
            uploaded: 0,
            speed: 0,
            xhr: null,
            startTime: null,
            error: null
        }));
        
        this.uploadQueue.push(...uploadItems);
        this.showUploadMonitor();
        this.updateUploadMonitor();
        this.processUploadQueue();
    }
    
    processUploadQueue() {
        // Process up to 3 uploads concurrently
        const maxConcurrent = 3;
        const active = this.uploadQueue.filter(u => u.status === 'uploading').length;
        const pending = this.uploadQueue.filter(u => u.status === 'pending');
        
        for (let i = 0; i < Math.min(maxConcurrent - active, pending.length); i++) {
            this.startUpload(pending[i]);
        }
    }
    
    startUpload(uploadItem) {
        if (uploadItem.status !== 'pending') return;
        
        uploadItem.status = 'uploading';
        uploadItem.startTime = Date.now();
        uploadItem.uploaded = 0;
        
        const formData = new FormData();
        formData.append('file', uploadItem.file);
        formData.append('path', uploadItem.path);
        
        const xhr = new XMLHttpRequest();
        uploadItem.xhr = xhr;
        
        xhr.upload.addEventListener('progress', (e) => {
            if (e.lengthComputable) {
                uploadItem.uploaded = e.loaded;
                uploadItem.progress = (e.loaded / e.total) * 100;
                
                // Calculate speed
                const elapsed = (Date.now() - uploadItem.startTime) / 1000; // seconds
                if (elapsed > 0) {
                    uploadItem.speed = uploadItem.uploaded / elapsed; // bytes per second
                }
                
                this.updateUploadMonitor();
            }
        });
        
        xhr.addEventListener('load', () => {
            if (xhr.status >= 200 && xhr.status < 300) {
                uploadItem.status = 'completed';
                uploadItem.progress = 100;
                this.updateUploadMonitor();
                this.processUploadQueue();
                
                // Reload files if this was the last upload
                setTimeout(() => {
                    if (this.uploadQueue.filter(u => u.status === 'uploading').length === 0) {
                        this.loadFiles(this.currentPath);
                    }
                }, 500);
            } else {
                uploadItem.status = 'failed';
                uploadItem.error = `HTTP ${xhr.status}: ${xhr.statusText}`;
                this.updateUploadMonitor();
                this.processUploadQueue();
            }
        });
        
        xhr.addEventListener('error', () => {
            uploadItem.status = 'failed';
            uploadItem.error = 'Network error';
            this.updateUploadMonitor();
            this.processUploadQueue();
        });
        
        xhr.addEventListener('abort', () => {
            uploadItem.status = 'cancelled';
            this.updateUploadMonitor();
            this.processUploadQueue();
        });
        
        xhr.open('POST', '/api/files/upload');
        xhr.send(formData);
    }
    
    cancelUpload(uploadId) {
        const uploadItem = this.uploadQueue.find(u => u.id === uploadId);
        if (uploadItem && uploadItem.xhr) {
            uploadItem.xhr.abort();
            uploadItem.status = 'cancelled';
            this.updateUploadMonitor();
            this.processUploadQueue();
        }
    }
    
    cancelAllUploads() {
        this.uploadQueue.forEach(uploadItem => {
            if (uploadItem.status === 'pending' || uploadItem.status === 'uploading') {
                if (uploadItem.xhr) {
                    uploadItem.xhr.abort();
                }
                uploadItem.status = 'cancelled';
            }
        });
        this.updateUploadMonitor();
    }
    
    clearCompletedUploads() {
        this.uploadQueue = this.uploadQueue.filter(u => 
            u.status !== 'completed' && u.status !== 'failed' && u.status !== 'cancelled'
        );
        this.updateUploadMonitor();
        
        if (this.uploadQueue.length === 0) {
            this.hideUploadMonitor();
        }
    }
    
    showUploadMonitor() {
        this.uploadMonitorVisible = true;
        const panel = document.getElementById('uploadMonitorPanel');
        if (panel) {
            panel.style.display = 'block';
            if (this.uploadMonitorMinimized) {
                panel.classList.add('minimized');
            } else {
                panel.classList.remove('minimized');
            }
        }
    }
    
    hideUploadMonitor() {
        this.uploadMonitorVisible = false;
        const panel = document.getElementById('uploadMonitorPanel');
        if (panel) {
            panel.style.display = 'none';
        }
    }
    
    toggleUploadMonitorMinimize() {
        this.uploadMonitorMinimized = !this.uploadMonitorMinimized;
        const panel = document.getElementById('uploadMonitorPanel');
        if (panel) {
            if (this.uploadMonitorMinimized) {
                panel.classList.add('minimized');
            } else {
                panel.classList.remove('minimized');
            }
        }
    }
    
    updateUploadMonitor() {
        const total = this.uploadQueue.length;
        const active = this.uploadQueue.filter(u => u.status === 'uploading').length;
        const completed = this.uploadQueue.filter(u => u.status === 'completed').length;
        const failed = this.uploadQueue.filter(u => u.status === 'failed').length;
        
        // Calculate average speed
        const uploading = this.uploadQueue.filter(u => u.status === 'uploading');
        const avgSpeed = uploading.length > 0
            ? uploading.reduce((sum, u) => sum + u.speed, 0) / uploading.length
            : 0;
        
        // Update summary
        document.getElementById('uploadTotalCount').textContent = total;
        document.getElementById('uploadActiveCount').textContent = active;
        document.getElementById('uploadCompletedCount').textContent = completed;
        document.getElementById('uploadFailedCount').textContent = failed;
        document.getElementById('uploadSpeed').textContent = this.formatSpeed(avgSpeed);
        
        // Update list
        const list = document.getElementById('uploadMonitorList');
        if (!list) return;
        
        if (this.uploadQueue.length === 0) {
            list.innerHTML = '<div class="upload-monitor-empty">No uploads in progress</div>';
            return;
        }
        
        list.innerHTML = this.uploadQueue.map(uploadItem => {
            const statusIcon = {
                'pending': '⏳',
                'uploading': '📤',
                'completed': '✅',
                'failed': '❌',
                'cancelled': '🚫'
            }[uploadItem.status] || '❓';
            
            const statusClass = uploadItem.status;
            const progressBar = uploadItem.status === 'uploading' || uploadItem.status === 'completed'
                ? `<div class="upload-progress-bar">
                    <div class="upload-progress-fill" style="width: ${uploadItem.progress}%"></div>
                </div>`
                : '';
            
            const speedInfo = uploadItem.status === 'uploading' && uploadItem.speed > 0
                ? `<span class="upload-speed">${this.formatSpeed(uploadItem.speed)}</span>`
                : '';
            
            const cancelBtn = uploadItem.status === 'pending' || uploadItem.status === 'uploading'
                ? `<button class="btn-icon btn-small" onclick="fileManager.cancelUpload(${uploadItem.id})" title="Cancel">×</button>`
                : '';
            
            const errorInfo = uploadItem.error
                ? `<div class="upload-error">${this.escapeHtml(uploadItem.error)}</div>`
                : '';
            
            return `
                <div class="upload-item ${statusClass}">
                    <div class="upload-item-header">
                        <span class="upload-status-icon">${statusIcon}</span>
                        <span class="upload-file-name" title="${this.escapeHtml(uploadItem.name)}">${this.escapeHtml(uploadItem.name)}</span>
                        <span class="upload-file-size">${this.formatSize(uploadItem.size)}</span>
                        ${speedInfo}
                        ${cancelBtn}
                    </div>
                    ${progressBar}
                    ${errorInfo}
                </div>
            `;
        }).join('');
        
        // Update footer buttons visibility
        const hasCompleted = completed > 0 || failed > 0 || this.uploadQueue.some(u => u.status === 'cancelled');
        const hasActive = active > 0 || this.uploadQueue.some(u => u.status === 'pending');
        
        document.getElementById('uploadMonitorClearBtn').style.display = hasCompleted ? 'inline-block' : 'none';
        document.getElementById('uploadMonitorCancelAllBtn').style.display = hasActive ? 'inline-block' : 'none';
    }
    
    formatSpeed(bytesPerSecond) {
        if (bytesPerSecond < 1024) {
            return `${Math.round(bytesPerSecond)} B/s`;
        } else if (bytesPerSecond < 1024 * 1024) {
            return `${(bytesPerSecond / 1024).toFixed(1)} KB/s`;
        } else {
            return `${(bytesPerSecond / (1024 * 1024)).toFixed(1)} MB/s`;
        }
    }
    
    showToast(message, type = 'info') {
        // Create toast notification
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.textContent = message;
        toast.style.cssText = `
            position: fixed;
            bottom: 20px;
            right: 20px;
            background: ${type === 'error' ? '#ff3366' : type === 'success' ? '#4ade80' : '#3b82f6'};
            color: white;
            padding: 12px 24px;
            border-radius: 8px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
            z-index: 10000;
            font-size: 14px;
            font-weight: 500;
            animation: slideIn 0.3s ease;
        `;
        
        // Add animation
        const style = document.createElement('style');
        if (!document.getElementById('toast-animations')) {
            style.id = 'toast-animations';
            style.textContent = `
                @keyframes slideIn {
                    from {
                        transform: translateX(100%);
                        opacity: 0;
                    }
                    to {
                        transform: translateX(0);
                        opacity: 1;
                    }
                }
                @keyframes slideOut {
                    from {
                        transform: translateX(0);
                        opacity: 1;
                    }
                    to {
                        transform: translateX(100%);
                        opacity: 0;
                    }
                }
            `;
            document.head.appendChild(style);
        }
        
        document.body.appendChild(toast);
        
        // Auto-remove after 3 seconds
        setTimeout(() => {
            toast.style.animation = 'slideOut 0.3s ease';
            setTimeout(() => {
                if (toast.parentNode) {
                    toast.parentNode.removeChild(toast);
                }
            }, 300);
        }, 3000);
    }
    
    // ============================================
    // CYBERPUNK PICTURE VIEWER
    // ============================================
    
    openPictureViewer() {
        console.log('FileManager: openPictureViewer called');
        this.pictureViewerOpen = true;
        const viewer = document.getElementById('cyberpunkPictureViewer');
        if (viewer) {
            console.log('FileManager: Found picture viewer element, showing...');
            
            // Close file manager first
            this.close();
            
            // Move to body if not already there (fixes display issue)
            if (viewer.parentElement !== document.body) {
                document.body.appendChild(viewer);
            }
            
            // Show viewer
            viewer.style.display = 'block';
            
            // Setup close button
            const closeBtn = document.getElementById('pictureViewerCloseBtn');
            if (closeBtn) {
                closeBtn.onclick = () => {
                    viewer.style.display = 'none';
                    this.pictureViewerOpen = false;
                };
            }
            
            this.loadAllImages();
            this.setupInfiniteScroll();
        } else {
            console.error('FileManager: cyberpunkPictureViewer element NOT found in DOM!');
            alert('Photo Gallery viewer not found. Please refresh the page.');
        }
    }
    
    setupInfiniteScroll() {
        // Remove existing observer if any
        if (this.scrollObserver) {
            this.scrollObserver.disconnect();
        }
        
        const grid = document.getElementById('cyberpunkGalleryGrid');
        if (!grid) return;
        
        // Create intersection observer for infinite scroll
        this.scrollObserver = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting && this.hasMoreImages && !this.loadingImages) {
                    // Load more images when user scrolls near bottom
                    console.log('Infinite scroll triggered - loading more images...');
                    this.loadMoreImages();
                }
            });
        }, {
            root: null,
            rootMargin: '300px', // Start loading 300px before reaching bottom
            threshold: 0.01
        });
        
        // Observe sentinel element (will be added in renderImageGrid)
        const sentinel = document.getElementById('galleryScrollSentinel');
        if (sentinel) {
            this.scrollObserver.observe(sentinel);
        }
    }
    
    closePictureViewer() {
        this.pictureViewerOpen = false;
        this.fullscreenViewerOpen = false;
        const viewer = document.getElementById('cyberpunkPictureViewer');
        const fullscreen = document.getElementById('cyberpunkFullscreenViewer');
        if (viewer) viewer.style.display = 'none';
        if (fullscreen) fullscreen.style.display = 'none';
        this.allImages = [];
        this.currentImageIndex = 0;
        this.imageLoadOffset = 0;
        this.hasMoreImages = true;
    }
    
    // Helper function to validate and clean a single image
    _validateImage(img) {
        if (!img) {
            return null;
        }
        
        const pathValue = img.path;
        const hasPath = pathValue != null && 
                       pathValue !== 'undefined' && 
                       String(pathValue).trim() !== '';
        
        const nameValue = img.name;
        const hasName = nameValue != null && 
                       nameValue !== 'undefined' && 
                       String(nameValue).trim() !== '';
        
        // If no path and no name, skip
        if (!hasPath && !hasName) {
            return null;
        }
        
        // If we have name but no path, we can't display it - skip
        if (!hasPath && hasName) {
            return null;
        }
        
        // Create a clean copy
        const cleanImg = {...img};
        
        // Ensure path is a string
        cleanImg.path = String(pathValue);
        
        // If we have path but no name, extract name from path
        if (hasPath && !hasName) {
            cleanImg.name = cleanImg.path.split('/').pop() || cleanImg.path;
        } else {
            cleanImg.name = String(nameValue);
        }
        
        return cleanImg;
    }
    
    async loadAllImages(reset = true) {
        if (reset) {
            this.allImages = [];
            this.imageLoadOffset = 0;
            this.hasMoreImages = true;
        }
        
        const grid = document.getElementById('cyberpunkGalleryGrid');
        const countEl = document.getElementById('pictureViewerCount');
        const infoEl = document.getElementById('pictureViewerInfo');
        const loadMoreBtn = document.getElementById('pictureViewerLoadMoreBtn');
        
        if (!grid) return;
        
        if (reset) {
            grid.innerHTML = '<div class="cyberpunk-loading"><div class="cyberpunk-scanline"></div><div class="cyberpunk-loading-text">[SCANNING_STORAGE...]</div></div>';
            if (countEl) countEl.textContent = 'Loading...';
            if (infoEl) infoEl.textContent = '[SCANNING...]';
        }
        
        try {
            const response = await fetch(`/api/files/all-images?limit=${this.imageLoadLimit}&offset=${this.imageLoadOffset}`);
            if (!response.ok) throw new Error('Failed to load images');
            
            const data = await response.json();
            
            // Debug: log first image structure to verify data format
            if (data.images && data.images.length > 0) {
                console.log('Photo Gallery - First image data structure:', data.images[0]);
            }
            
            // Filter and validate images
            const totalReceived = (data.images || []).length;
            console.log(`Photo Gallery - Received ${totalReceived} images from API`);
            
            const validImages = (data.images || [])
                .map(img => this._validateImage(img))
                .filter(img => img !== null);
            
            console.log(`Photo Gallery - After filtering: ${validImages.length} valid images out of ${totalReceived} total`);
            
            if (validImages.length === 0 && totalReceived > 0) {
                console.error('Photo Gallery - All images were filtered out! First few images:', (data.images || []).slice(0, 3));
            }
            
            if (reset) {
                this.allImages = validImages;
            } else {
                // When loading more, we need to merge while maintaining sort order
                // Backend returns sorted chunks, but we need to merge them correctly
                // Since backend sorts newest first, we should merge maintaining that order
                // Combine and re-sort to ensure global order (in case of pagination edge cases)
                this.allImages = [...this.allImages, ...validImages];
                // Re-sort to ensure correct order across all loaded images
                // This is necessary because pagination might have edge cases
                this.allImages.sort((a, b) => {
                    const timeA = Number(a.modified) || 0;
                    const timeB = Number(b.modified) || 0;
                    if (timeB !== timeA) {
                        return timeB - timeA; // Descending: newer (higher) timestamps first
                    }
                    // Tie-breaker: sort by path for stability
                    const pathA = (a.path || '').toLowerCase();
                    const pathB = (b.path || '').toLowerCase();
                    return pathA.localeCompare(pathB);
                });
                // Remove duplicates based on path
                const seen = new Set();
                this.allImages = this.allImages.filter(img => {
                    const path = img.path || '';
                    if (seen.has(path)) return false;
                    seen.add(path);
                    return true;
                });
            }
            
            // ALWAYS re-sort after loading to ensure correct order (newest first)
            // This is critical because backend might have issues or pagination might mix things up
            this.allImages.sort((a, b) => {
                const timeA = Number(a.modified) || 0;
                const timeB = Number(b.modified) || 0;
                if (timeB !== timeA) {
                    return timeB - timeA; // Descending: newer (higher) timestamps first
                }
                // Tie-breaker: sort by path for stability
                const pathA = (a.path || '').toLowerCase();
                const pathB = (b.path || '').toLowerCase();
                return pathA.localeCompare(pathB);
            });
            
            this.hasMoreImages = data.has_more || false;
            this.imageLoadOffset += data.images?.length || 0;
            
            // Update count displays
            const countEl = document.querySelector('.photo-gallery-count');
            const infoEl = document.querySelector('.photo-gallery-info');
            const loadMoreBtn = document.getElementById('loadMoreImages');
            
            // Show total from server in main count
            if (countEl && data.total !== undefined) {
                countEl.textContent = `[${data.total} IMAGES FOUND]`;
            }
            
            // Show loaded vs total in info display
            if (infoEl) {
                infoEl.textContent = `[LOADED: ${this.allImages.length}/${data.total || 0}]`;
            }
            
            if (loadMoreBtn) {
                loadMoreBtn.style.display = this.hasMoreImages ? 'block' : 'none';
            }
            
            // Log summary for debugging
            console.log(`Photo Gallery - Load complete: ${this.allImages.length} images loaded, hasMore: ${this.hasMoreImages}, total from API: ${data.total || 0}`);
            
            // Debug: log first few images to verify sorting
            if (this.allImages.length > 0) {
                console.log('Photo Gallery - Sorting verification:');
                console.log(`  Total images: ${this.allImages.length}`);
                console.log('  First 10 images (should be newest first):');
                this.allImages.slice(0, 10).forEach((img, idx) => {
                    const timestamp = Number(img.modified) || 0;
                    const date = timestamp > 0 ? new Date(timestamp * 1000).toLocaleString() : 'N/A';
                    const name = img.name || (img.path ? img.path.split('/').pop() : 'Unknown');
                    console.log(`    ${idx + 1}. ${name} - ${date} (timestamp: ${timestamp})`);
                    // Debug: log if name is missing
                    if (!img.name) {
                        console.warn(`      ⚠️ Image at index ${idx} missing "name" field, using path fallback: ${img.path}`);
                    }
                });
                
                // Verify sorting is correct
                let isSorted = true;
                let firstError = null;
                for (let i = 1; i < Math.min(this.allImages.length, 100); i++) {
                    const prev = Number(this.allImages[i - 1].modified) || 0;
                    const curr = Number(this.allImages[i].modified) || 0;
                    if (curr > prev) {
                        isSorted = false;
                        if (!firstError) {
                            firstError = i;
                            console.error(`❌ Sorting error at index ${i}:`);
                            const prevName = this.allImages[i - 1].name || (this.allImages[i - 1].path ? this.allImages[i - 1].path.split('/').pop() : 'Unknown');
                            const currName = this.allImages[i].name || (this.allImages[i].path ? this.allImages[i].path.split('/').pop() : 'Unknown');
                            console.error(`  Previous: ${prevName} (${prev})`);
                            console.error(`  Current: ${currName} (${curr})`);
                        }
                    }
                }
                if (isSorted) {
                    console.log('  ✓ Sorting verified: All images are in correct order (newest first)');
                } else {
                    console.error(`  ❌ Sorting failed: Found ${firstError ? 'at least one' : 'multiple'} out-of-order images`);
                }
            }
            
            this.renderImageGrid();
        } catch (error) {
            console.error('Error loading images:', error);
            grid.innerHTML = `<div class="cyberpunk-loading"><div class="cyberpunk-loading-text" style="color: #ff0066;">[ERROR: ${error.message}]</div></div>`;
            if (infoEl) infoEl.textContent = '[ERROR LOADING IMAGES]';
        } finally {
            this.loadingImages = false;
        }
    }
    
    async loadMoreImages() {
        await this.loadAllImages(false);
    }
    
    renderImageGrid() {
        const grid = document.getElementById('cyberpunkGalleryGrid');
        if (!grid) return;
        
        if (this.allImages.length === 0) {
            grid.innerHTML = '<div class="cyberpunk-loading"><div class="cyberpunk-loading-text">[NO IMAGES FOUND]</div></div>';
            return;
        }
        
        // CRITICAL: Sort by modified time (newest first) before rendering
        // Convert to numbers explicitly to ensure numeric comparison (not string)
        // This MUST happen every time before rendering to ensure correct order
        this.allImages.sort((a, b) => {
            const timeA = Number(a.modified) || 0;
            const timeB = Number(b.modified) || 0;
            // Descending order (newest first) - higher timestamp comes first
            // timeB - timeA: if B is newer (timeB > timeA), returns positive, B comes before A
            if (timeB !== timeA) {
                return timeB - timeA; // Positive if B is newer, negative if A is newer
            }
            // Timestamps are equal, sort by path for stability
            const pathA = (a.path || '').toLowerCase();
            const pathB = (b.path || '').toLowerCase();
            return pathA.localeCompare(pathB);
        });
        
        // Debug: log first few to verify sorting
        if (this.allImages.length > 0) {
            console.log(`Photo Gallery - Sorted ${this.allImages.length} images. First 10 (should be newest first):`);
            this.allImages.slice(0, 10).forEach((img, idx) => {
                const ts = Number(img.modified) || 0;
                const date = ts > 0 ? new Date(ts * 1000).toLocaleString() : 'N/A';
                console.log(`  ${idx + 1}. ${img.name}: timestamp=${ts}, date=${date}`);
            });
            
            // Verify sorting
            let prevTs = null;
            let errors = 0;
            for (let i = 0; i < Math.min(this.allImages.length, 20); i++) {
                const currTs = Number(this.allImages[i].modified) || 0;
                if (prevTs !== null && currTs > prevTs) {
                    errors++;
                    if (errors === 1) {
                        console.error(`❌ Sorting error at index ${i}: ${this.allImages[i].name} (${currTs}) is NEWER than previous (${prevTs})`);
                    }
                }
                prevTs = currTs;
            }
            if (errors === 0) {
                console.log('✓ Sorting verified: All images in correct order (newest first)');
            } else {
                console.error(`❌ Found ${errors} sorting errors!`);
            }
        }
        
        grid.innerHTML = '';
        
        this.allImages.forEach((image, index) => {
            // Validate image (should already be validated, but double-check)
            const validated = this._validateImage(image);
            if (!validated) {
                console.error('Photo Gallery - Invalid image object at index', index, ':', image);
                return; // Skip invalid images
            }
            
            const imagePath = validated.path;
            const imageName = validated.name;
            
            const item = document.createElement('div');
            item.className = 'cyberpunk-gallery-item';
            item.dataset.index = index;
            item.dataset.path = imagePath;
            // Store timestamp in dataset for debugging
            item.dataset.modified = image.modified || '0';
            
            const img = document.createElement('img');
            
            // Validate that we have a valid path before proceeding
            if (!imagePath || imagePath === 'undefined' || imagePath.trim() === '') {
                console.error('Photo Gallery - Invalid image path:', image);
                // Show error placeholder
                img.src = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="200" height="200"%3E%3Crect fill="%23333" width="200" height="200"/%3E%3Ctext fill="%23ff0066" font-family="monospace" font-size="12" x="50%25" y="50%25" text-anchor="middle" dy=".3em"%3EINVALID%3C/text%3E%3C/svg%3E';
            } else if (image.thumbnail) {
                // Use thumbnail from API response (base64 data URL)
                img.src = image.thumbnail;
            } else {
                // Fallback: load thumbnail asynchronously
                img.src = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="200" height="200"%3E%3Crect fill="%23111" width="200" height="200"/%3E%3Ctext fill="%2300ffff" font-family="monospace" font-size="12" x="50%25" y="50%25" text-anchor="middle" dy=".3em"%3ELOADING...%3C/text%3E%3C/svg%3E';
                
                // Try to load thumbnail
                fetch(`/api/files/thumbnail/${encodeURIComponent(imagePath)}?size=300`)
                    .then(response => {
                        if (response.ok) {
                            return response.json();
                        }
                        throw new Error('Thumbnail not available');
                    })
                    .then(data => {
                        if (data && data.thumbnail) {
                            img.src = data.thumbnail;
                        } else {
                            // Fallback to full image
                            img.src = `/api/files/view/${encodeURIComponent(imagePath)}`;
                        }
                    })
                    .catch(() => {
                        // Fallback to full image if thumbnail fails
                        img.src = `/api/files/view/${encodeURIComponent(imagePath)}`;
                    });
            }
            img.alt = imageName;
            img.loading = 'lazy';
            
            // Add error handler to try full image if thumbnail fails
            img.onerror = () => {
                if (imagePath && imagePath !== 'undefined' && imagePath.trim() !== '') {
                    if (img.src.includes('data:image/svg+xml') || img.src.includes('/thumbnail/')) {
                        // If thumbnail failed, try full image
                        img.src = `/api/files/view/${encodeURIComponent(imagePath)}`;
                    } else if (!img.src.includes('/view/')) {
                        // If full image also failed, show placeholder
                        img.src = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="200" height="200"%3E%3Crect fill="%23333" width="200" height="200"/%3E%3Ctext fill="%23ff0066" font-family="monospace" font-size="12" x="50%25" y="50%25" text-anchor="middle" dy=".3em"%3EERROR%3C/text%3E%3C/svg%3E';
                    }
                } else {
                    // Invalid path - show error placeholder
                    img.src = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="200" height="200"%3E%3Crect fill="%23333" width="200" height="200"/%3E%3Ctext fill="%23ff0066" font-family="monospace" font-size="12" x="50%25" y="50%25" text-anchor="middle" dy=".3em"%3EINVALID%3C/text%3E%3C/svg%3E';
                }
            };
            
            const overlay = document.createElement('div');
            overlay.className = 'cyberpunk-gallery-item-overlay';
            overlay.textContent = imageName;
            
            item.appendChild(img);
            item.appendChild(overlay);
            
            // Add play button overlay for videos
            const isVideo = image.type === 'video' || /\.(mp4|avi|mov|mkv|webm|flv|wmv|m4v|3gp|ogv)$/i.test(imageName);
            if (isVideo) {
                item.dataset.type = 'video';
                const playButton = document.createElement('div');
                playButton.className = 'cyberpunk-video-play-button';
                playButton.innerHTML = '▶';
                playButton.title = 'Play video';
                item.appendChild(playButton);
            }
            
            item.addEventListener('click', () => {
                this.openFullscreenViewer(index);
            });
            
            grid.appendChild(item);
        });
        
        // Add/update scroll sentinel for infinite scroll (at the end of grid)
        let sentinel = document.getElementById('galleryScrollSentinel');
        if (!sentinel) {
            sentinel = document.createElement('div');
            sentinel.id = 'galleryScrollSentinel';
            sentinel.style.height = '1px';
            sentinel.style.width = '100%';
            grid.appendChild(sentinel);
        }
        
        // Re-observe sentinel if observer exists
        if (this.scrollObserver && sentinel) {
            this.scrollObserver.observe(sentinel);
        }
    }
    
    openFullscreenViewer(index) {
        this.fullscreenViewerOpen = true;
        this.currentImageIndex = index;
        const fullscreen = document.getElementById('cyberpunkFullscreenViewer');
        const img = document.getElementById('cyberpunkFullscreenImage');
        const video = document.getElementById('cyberpunkFullscreenVideo');
        const info = document.getElementById('cyberpunkFullscreenInfo');
        const prevBtn = document.getElementById('cyberpunkFullscreenPrev');
        const nextBtn = document.getElementById('cyberpunkFullscreenNext');
        
        if (!fullscreen) return;
        
        const media = this.allImages[index];
        if (!media) {
            console.error('Photo Gallery - openFullscreenViewer: No media at index', index);
            return;
        }
        
        // Validate media (should already be validated, but double-check)
        const validated = this._validateImage(media);
        if (!validated) {
            console.error('Photo Gallery - openFullscreenViewer: Invalid media at index', index, ':', media);
            return;
        }
        
        const mediaPath = validated.path;
        const mediaName = validated.name;
        
        const isVideo = media.type === 'video' || /\.(mp4|avi|mov|mkv|webm|flv|wmv|m4v|3gp|ogv)$/i.test(mediaName);
        
        // Hide/show image or video element
        if (img) {
            img.style.display = isVideo ? 'none' : 'block';
        }
        if (video) {
            video.style.display = isVideo ? 'block' : 'none';
            if (isVideo && mediaPath) {
                video.src = `/api/files/view/${encodeURIComponent(mediaPath)}`;
                video.load(); // Reload video
            } else {
                video.pause();
                video.src = '';
            }
        } else if (isVideo && mediaPath) {
            // Create video element if it doesn't exist
            const videoContainer = fullscreen.querySelector('.cyberpunk-fullscreen-content');
            if (videoContainer) {
                const newVideo = document.createElement('video');
                newVideo.id = 'cyberpunkFullscreenVideo';
                newVideo.className = 'cyberpunk-fullscreen-media';
                newVideo.controls = true;
                newVideo.src = `/api/files/view/${encodeURIComponent(mediaPath)}`;
                videoContainer.appendChild(newVideo);
            }
        }
        
        if (!isVideo && img && mediaPath) {
            img.src = `/api/files/view/${encodeURIComponent(mediaPath)}`;
        }
        
        if (info) {
            // Format date safely - handle various formats
            let dateStr = 'N/A';
            if (media.modified) {
                try {
                    // Try to parse as number (Unix timestamp in seconds)
                    let timestamp = Number(media.modified);
                    if (isNaN(timestamp) || timestamp <= 0) {
                        // Try parsing as ISO string or other format
                        const dateObj = new Date(media.modified);
                        if (!isNaN(dateObj.getTime())) {
                            dateStr = dateObj.toLocaleString();
                        }
                    } else {
                        // Convert seconds to milliseconds if needed
                        if (timestamp < 10000000000) {
                            timestamp = timestamp * 1000;
                        }
                        const dateObj = new Date(timestamp);
                        if (!isNaN(dateObj.getTime())) {
                            dateStr = dateObj.toLocaleString();
                        }
                    }
                } catch (e) {
                    console.warn('Photo Gallery - Error formatting date:', e, 'for media:', media);
                }
            }
            
            const typeLabel = isVideo ? 'VIDEO' : 'IMAGE';
            info.textContent = `${index + 1} / ${this.allImages.length} - ${mediaName} [${typeLabel}] [${dateStr}]`;
        }
        
        if (prevBtn) prevBtn.style.display = index > 0 ? 'flex' : 'none';
        if (nextBtn) nextBtn.style.display = index < this.allImages.length - 1 ? 'flex' : 'none';
        
        // Update download button to download original file
        const downloadBtn = document.getElementById('cyberpunkFullscreenDownload');
        if (downloadBtn) {
            downloadBtn.onclick = () => {
                // Create a temporary link and trigger download
                const link = document.createElement('a');
                link.href = `/api/files/view/${encodeURIComponent(mediaPath)}`;
                link.download = mediaName; // Suggest filename for download
                document.body.appendChild(link);
                link.click();
                document.body.removeChild(link);
                console.log(`Photo Gallery - Downloading original: ${mediaName}`);
            };
        }
        
        fullscreen.style.display = 'flex';
    }
    
    closeFullscreenViewer() {
        this.fullscreenViewerOpen = false;
        const fullscreen = document.getElementById('cyberpunkFullscreenViewer');
        if (fullscreen) fullscreen.style.display = 'none';
    }
    
    openAudioPlayer(filePath, fileName) {
        const modal = document.getElementById('audioPlayerModal');
        if (!modal) {
            console.error('Audio player modal not found');
            // Fallback: open in new tab
            window.open(`/api/files/view/${encodeURIComponent(filePath)}`, '_blank');
            return;
        }
        
        // Stop any currently playing audio
        if (this.currentAudioPlayer) {
            this.currentAudioPlayer.pause();
            this.currentAudioPlayer = null;
        }
        
        const audio = document.getElementById('audioPlayer');
        const audioTitle = document.getElementById('audioPlayerTitle');
        
        if (audio) {
            audio.src = `/api/files/view/${encodeURIComponent(filePath)}`;
            audio.load();
            this.currentAudioPlayer = audio;
        }
        
        if (audioTitle) {
            audioTitle.textContent = fileName;
        }
        
        modal.style.display = 'flex';
        
        // Auto-play (optional - browsers may block this)
        if (audio) {
            audio.play().catch(err => {
                console.log('Auto-play blocked:', err);
                // User will need to click play manually
            });
        }
    }
    
    closeAudioPlayer() {
        const modal = document.getElementById('audioPlayerModal');
        if (modal) modal.style.display = 'none';
        
        if (this.currentAudioPlayer) {
            this.currentAudioPlayer.pause();
            this.currentAudioPlayer = null;
        }
    }
    
    openVideoPlayer(filePath, fileName) {
        const modal = document.getElementById('videoPlayerModal');
        if (!modal) {
            console.error('Video player modal not found');
            // Fallback: open in new tab
            window.open(`/api/files/view/${encodeURIComponent(filePath)}`, '_blank');
            return;
        }
        
        // Stop any currently playing video
        if (this.currentAudioPlayer) {
            this.currentAudioPlayer.pause();
            this.currentAudioPlayer = null;
        }
        
        const video = document.getElementById('videoPlayer');
        const videoTitle = document.getElementById('videoPlayerTitle');
        
        if (video) {
            video.src = `/api/files/view/${encodeURIComponent(filePath)}`;
            video.load();
            this.currentAudioPlayer = video; // Reuse the same tracking variable
        }
        
        if (videoTitle) {
            videoTitle.textContent = fileName;
        }
        
        modal.style.display = 'flex';
        
        // Auto-play (optional - browsers may block this)
        if (video) {
            video.play().catch(err => {
                console.log('Auto-play blocked:', err);
                // User will need to click play manually
            });
        }
    }
    
    closeVideoPlayer() {
        const modal = document.getElementById('videoPlayerModal');
        if (modal) modal.style.display = 'none';
        
        if (this.currentAudioPlayer) {
            this.currentAudioPlayer.pause();
            this.currentAudioPlayer = null;
        }
    }
    
    prevFullscreenImage() {
        if (this.currentImageIndex > 0) {
            this.currentImageIndex--;
            this.openFullscreenViewer(this.currentImageIndex);
        }
    }
    
    nextFullscreenImage() {
        if (this.currentImageIndex < this.allImages.length - 1) {
            this.currentImageIndex++;
            this.openFullscreenViewer(this.currentImageIndex);
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
        if (typeof window.fileManager.openPictureViewer === 'function') {
            console.log('FileManager: openPictureViewer method available');
        } else {
            console.error('FileManager: openPictureViewer method NOT available');
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

// Global function to open photo gallery - can be called directly from onclick
window.openPhotoGallery = function() {
    console.log('openPhotoGallery global function called');
    
    // Ensure fileManager exists
    if (!window.fileManager) {
        console.log('FileManager not initialized, creating now...');
        initializeFileManager();
    }
    
    // Try to open picture viewer
    if (window.fileManager && typeof window.fileManager.openPictureViewer === 'function') {
        console.log('Calling fileManager.openPictureViewer()');
        window.fileManager.openPictureViewer();
    } else {
        console.error('FileManager.openPictureViewer not available');
        // Direct fallback - show the viewer element
        const viewer = document.getElementById('cyberpunkPictureViewer');
        if (viewer) {
            console.log('Direct fallback: showing viewer element');
            viewer.style.display = 'block';
            // Try to load images if fileManager exists
            if (window.fileManager && window.fileManager.loadAllImages) {
                window.fileManager.loadAllImages();
            }
        } else {
            alert('Photo Gallery not found. Please refresh the page.');
        }
    }
};
