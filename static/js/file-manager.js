// File Manager
class FileManager {
    constructor() {
        this.currentPath = '';
        this.currentView = 'grid';
        this.currentFiles = [];
        this.imageFiles = [];
        this.currentImageIndex = 0;
        this.init();
    }
    
    init() {
        // Open file manager button
        document.getElementById('openFileManagerBtn')?.addEventListener('click', () => this.open());
        
        // Close button
        document.getElementById('fileManagerCloseBtn')?.addEventListener('click', () => this.close());
        
        // View toggle
        document.querySelectorAll('.view-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this.currentView = btn.dataset.view;
                this.renderFiles();
            });
        });
        
        // Image viewer
        document.getElementById('imageViewerClose')?.addEventListener('click', () => this.closeImageViewer());
        document.getElementById('imageViewerPrev')?.addEventListener('click', () => this.prevImage());
        document.getElementById('imageViewerNext')?.addEventListener('click', () => this.nextImage());
    }
    
    async open() {
        const overlay = document.getElementById('fileManagerOverlay');
        if (overlay) {
            overlay.style.display = 'block';
            await this.loadFiles('');
        }
    }
    
    close() {
        const overlay = document.getElementById('fileManagerOverlay');
        if (overlay) {
            overlay.style.display = 'none';
        }
    }
    
    async loadFiles(path) {
        this.currentPath = path;
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
    
    updateBreadcrumb(path) {
        const breadcrumb = document.getElementById('fileManagerBreadcrumb');
        if (!breadcrumb) return;
        
        const parts = path ? path.split('/').filter(p => p) : [];
        let html = '<button class="breadcrumb-item" data-path="">Home</button>';
        
        let currentPath = '';
        parts.forEach(part => {
            currentPath += '/' + part;
            html += ` <span class="breadcrumb-separator">/</span> <button class="breadcrumb-item" data-path="${currentPath}">${this.escapeHtml(part)}</button>`;
        });
        
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
    
    renderFiles() {
        const grid = document.getElementById('fileManagerGrid');
        if (!grid) return;
        
        if (this.currentFiles.length === 0) {
            grid.innerHTML = '<div class="file-manager-empty">No files in this directory</div>';
            return;
        }
        
        if (this.currentView === 'grid') {
            grid.className = 'file-manager-grid';
            grid.innerHTML = this.currentFiles.map(item => {
                const icon = item.is_directory ? '📂' : this.getFileIcon(item.name);
                const thumbnail = item.thumbnail ? `<img src="${item.thumbnail}" alt="" class="file-thumbnail">` : '';
                const actions = !item.is_directory ? `
                    <div class="file-actions" onclick="event.stopPropagation();">
                        <button class="file-action-btn" title="Email" onclick="fileManager.emailFile('${this.escapeHtml(item.path)}', '${this.escapeHtml(item.name)}')">📧</button>
                        <button class="file-action-btn" title="Share" onclick="fileManager.shareFile('${this.escapeHtml(item.path)}', '${this.escapeHtml(item.name)}')">🔗</button>
                    </div>
                ` : '';
                return `
                    <div class="file-item ${item.is_directory ? 'directory' : 'file'}" 
                         data-path="${this.escapeHtml(item.path)}" 
                         data-is-dir="${item.is_directory}">
                        <div class="file-icon">${thumbnail || icon}</div>
                        <div class="file-name" title="${this.escapeHtml(item.name)}">${this.escapeHtml(item.name)}</div>
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
                            <th>Name</th>
                            <th>Size</th>
                            <th>Modified</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${this.currentFiles.map(item => {
                            const date = new Date(item.modified * 1000);
                            const actions = !item.is_directory ? `
                                <td>
                                    <button class="file-action-btn" title="Email" onclick="fileManager.emailFile('${this.escapeHtml(item.path)}', '${this.escapeHtml(item.name)}')">📧</button>
                                    <button class="file-action-btn" title="Share" onclick="fileManager.shareFile('${this.escapeHtml(item.path)}', '${this.escapeHtml(item.name)}')">🔗</button>
                                </td>
                            ` : '<td></td>';
                            return `
                                <tr class="file-list-row ${item.is_directory ? 'directory' : 'file'}" 
                                    data-path="${this.escapeHtml(item.path)}" 
                                    data-is-dir="${item.is_directory}">
                                    <td>${item.is_directory ? '📂' : this.getFileIcon(item.name)} ${this.escapeHtml(item.name)}</td>
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
                // Don't trigger if clicking on action buttons
                if (e.target.closest('.file-actions, .file-action-btn')) {
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
    }
    
    async emailFile(filePath, fileName) {
        // Show email modal
        const modal = document.getElementById('fileEmailModal');
        if (modal) {
            document.getElementById('emailFilePath').value = filePath;
            document.getElementById('emailFileName').textContent = fileName;
            document.getElementById('emailTo').value = '';
            document.getElementById('emailSubject').value = `Shared file: ${fileName}`;
            document.getElementById('emailBody').value = `Please find the attached file: ${fileName}`;
            modal.style.display = 'block';
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
            modal.style.display = 'block';
        }
    }
    
    async sendEmail() {
        const filePath = document.getElementById('emailFilePath').value;
        const to = document.getElementById('emailTo').value.trim();
        const subject = document.getElementById('emailSubject').value.trim();
        const body = document.getElementById('emailBody').value.trim();
        
        if (!to) {
            alert('Please enter recipient email address');
            return;
        }
        
        try {
            const response = await fetch('/api/files/email', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    file_paths: [filePath],
                    to: to,
                    subject: subject || 'Shared file',
                    body: body || 'Please find the attached file.'
                })
            });
            
            const data = await response.json();
            if (response.ok) {
                alert('Email sent successfully!');
                document.getElementById('fileEmailModal').style.display = 'none';
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
            const response = await fetch('/api/files/share', {
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
}

// Initialize file manager
let fileManager;
document.addEventListener('DOMContentLoaded', () => {
    fileManager = new FileManager();
    window.fileManager = fileManager;
});

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
