// Notes Browser - Full-screen note browsing UI
class NotesBrowser {
    constructor() {
        this.notes = [];
        this.folders = [];
        this.currentFolderId = 0; // 0 = all notes
        this.searchQuery = '';
        this.isOpen = false;
        
        this.init();
    }
    
    init() {
        // Event listeners
        document.getElementById('notesBrowseBtn')?.addEventListener('click', () => this.open());
        document.getElementById('closeNotesBrowser')?.addEventListener('click', () => this.close());
        document.getElementById('notesBrowserNewNoteBtn')?.addEventListener('click', () => this.createNote());
        document.getElementById('notesBrowserNewFolderBtn')?.addEventListener('click', () => this.createFolder());
        document.getElementById('notesBrowserSearchInput')?.addEventListener('input', (e) => {
            this.searchQuery = e.target.value;
            this.loadNotes();
        });
        
        // Load initial data
        this.loadFolders();
        this.loadNotes();
    }
    
    open() {
        const browser = document.getElementById('notesBrowser');
        const chatMain = document.querySelector('.chat-main');
        
        if (browser && chatMain) {
            browser.style.display = 'flex';
            chatMain.style.display = 'none';
            this.isOpen = true;
            
            // Refresh data when opening
            this.loadFolders();
            this.loadNotes();
        }
    }
    
    close() {
        const browser = document.getElementById('notesBrowser');
        const chatMain = document.querySelector('.chat-main');
        
        if (browser && chatMain) {
            browser.style.display = 'none';
            chatMain.style.display = 'flex';
            this.isOpen = false;
        }
    }
    
    async loadFolders() {
        try {
            const response = await fetch('/api/notes/folders');
            if (response.ok) {
                this.folders = await response.json();
                this.renderFolders();
            }
        } catch (error) {
            console.error('Error loading folders:', error);
        }
    }
    
    async loadNotes() {
        const notesList = document.getElementById('notesBrowserList');
        if (notesList) {
            notesList.innerHTML = '<div class="notes-loading">Loading notes...</div>';
        }
        
        try {
            let url = '/api/notes?';
            if (this.currentFolderId !== 0) {
                url += `folder_id=${this.currentFolderId}&`;
            }
            if (this.searchQuery) {
                url += `search=${encodeURIComponent(this.searchQuery)}&`;
            }
            
            const response = await fetch(url);
            if (response.ok) {
                this.notes = await response.json();
                this.renderNotes();
            } else {
                const errorText = await response.text();
                let errorDetail = 'Unknown error';
                try {
                    const errorJson = JSON.parse(errorText);
                    errorDetail = errorJson.detail || errorText;
                } catch {
                    errorDetail = errorText || `HTTP ${response.status}`;
                }
                console.error('Error loading notes:', errorDetail);
                if (notesList) {
                    notesList.innerHTML = `<div class="notes-empty">Error loading notes: ${this.escapeHtml(errorDetail)}</div>`;
                }
            }
        } catch (error) {
            console.error('Error loading notes:', error);
            if (notesList) {
                notesList.innerHTML = `<div class="notes-empty">Error loading notes: ${this.escapeHtml(error.message || 'Network error')}</div>`;
            }
        }
    }
    
    renderFolders() {
        const foldersList = document.getElementById('notesBrowserFoldersList');
        if (!foldersList) return;
        
        // Keep "All Notes" item
        let html = '<div class="notes-browser-folder-item active" data-folder-id="0"><span class="folder-icon">📁</span><span class="folder-name">All Notes</span></div>';
        
        // Render folders
        this.folders.forEach(folder => {
            html += `
                <div class="notes-browser-folder-item" data-folder-id="${folder.id}">
                    <span class="folder-icon">📂</span>
                    <span class="folder-name">${this.escapeHtml(folder.name)}</span>
                    ${folder.notes_count > 0 ? `<span class="folder-count">${folder.notes_count}</span>` : ''}
                    <button class="folder-delete-btn" onclick="event.stopPropagation(); notesBrowserManager.deleteFolder(${folder.id}, '${this.escapeJs(folder.name)}')" title="Delete folder">🗑️</button>
                </div>
            `;
        });
        
        foldersList.innerHTML = html;
        
        // Add click handlers
        foldersList.querySelectorAll('.notes-browser-folder-item').forEach(item => {
            item.addEventListener('click', () => {
                foldersList.querySelectorAll('.notes-browser-folder-item').forEach(i => i.classList.remove('active'));
                item.classList.add('active');
                this.currentFolderId = parseInt(item.dataset.folderId) || 0;
                this.loadNotes();
            });
        });
    }
    
    renderNotes() {
        const notesList = document.getElementById('notesBrowserList');
        if (!notesList) return;
        
        if (this.notes.length === 0) {
            notesList.innerHTML = `
                <div class="notes-empty">
                    <div style="font-size: 18px; margin-bottom: 12px; font-weight: 600;">No notes found</div>
                    <div style="opacity: 0.7;">${this.searchQuery ? 'Try a different search term' : 'Click "✨ New Note" to create your first note'}</div>
                </div>
            `;
            return;
        }
        
        let html = '';
        this.notes.forEach(note => {
            // Clean markdown for preview
            let preview = note.content
                .replace(/^#+\s+/gm, '') // Remove headers
                .replace(/!\[.*?\]\(.*?\)/g, '[Image]') // Replace images
                .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1') // Replace links
                .replace(/\*\*([^*]+)\*\*/g, '$1') // Remove bold
                .replace(/\*([^*]+)\*/g, '$1') // Remove italic
                .trim();
            
            preview = this.escapeHtml(preview.substring(0, 120));
            const date = new Date(note.updated_at);
            const dateStr = date.toLocaleDateString('en-US', { 
                month: 'short', 
                day: 'numeric', 
                year: date.getFullYear() !== new Date().getFullYear() ? 'numeric' : undefined 
            });
            const hasAttachments = note.attachments && (typeof note.attachments === 'string' ? JSON.parse(note.attachments) : note.attachments).length > 0;
            
            html += `
                <div class="notes-item ${note.is_pinned ? 'pinned' : ''}" data-note-id="${note.id}">
                    <div class="notes-item-header">
                        <h3 class="notes-item-title">${this.escapeHtml(note.title)}</h3>
                        <div style="display: flex; gap: 4px; align-items: center;">
                            ${note.is_pinned ? '<span class="pin-badge" title="Pinned">📌</span>' : ''}
                            ${hasAttachments ? '<span class="attachment-badge" title="Has attachments">📎</span>' : ''}
                        </div>
                    </div>
                    <div class="notes-item-preview">${preview}${note.content.length > 120 ? '...' : ''}</div>
                    <div class="notes-item-meta">
                        ${note.folder_name ? `<span class="notes-folder-badge">📂 ${this.escapeHtml(note.folder_name)}</span>` : ''}
                        ${note.tags ? `<span class="notes-tags">🏷️ ${this.escapeHtml(note.tags)}</span>` : ''}
                        <span class="notes-date">${dateStr}</span>
                    </div>
                </div>
            `;
        });
        
        notesList.innerHTML = html;
        
        // Add click handlers - open note in modal
        notesList.querySelectorAll('.notes-item').forEach(item => {
            item.addEventListener('click', () => {
                const noteId = parseInt(item.dataset.noteId);
                // Close browser and open note in modal
                this.close();
                if (window.openNotesModal) {
                    // Open modal first
                    window.openNotesModal();
                    // Wait for modal to open and manager to initialize
                    setTimeout(() => {
                        // Ensure manager exists
                        if (typeof ensureNotesManager === 'function') {
                            const manager = ensureNotesManager();
                            manager.openNote(noteId);
                        } else if (window.notesManager) {
                            window.notesManager.openNote(noteId);
                        } else {
                            // Fallback: wait a bit more
                            setTimeout(() => {
                                if (window.notesManager) {
                                    window.notesManager.openNote(noteId);
                                } else {
                                    console.error('NotesManager not initialized');
                                    alert('Error: Notes manager not ready. Please try again.');
                                }
                            }, 200);
                        }
                    }, 150);
                }
            });
        });
    }
    
    async createNote() {
        // Close browser and open note modal in create mode
        this.close();
        if (window.openNotesModal) {
            // Open modal first
            window.openNotesModal();
            // Wait a bit for modal to open and manager to initialize
            setTimeout(() => {
                // Ensure manager exists, create it if needed
                if (typeof ensureNotesManager === 'function') {
                    const manager = ensureNotesManager();
                    manager.createNote();
                } else if (window.notesManager) {
                    window.notesManager.createNote();
                } else {
                    // Fallback: wait a bit more and try again
                    setTimeout(() => {
                        if (window.notesManager) {
                            window.notesManager.createNote();
                        } else {
                            console.error('NotesManager not initialized');
                            alert('Error: Notes manager not ready. Please try again.');
                        }
                    }, 200);
                }
            }, 150);
        }
    }
    
    async createFolder() {
        const name = prompt('Enter folder name:');
        if (!name) return;
        
        try {
            const response = await fetch('/api/notes/folders', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: name.trim() })
            });
            
            if (response.ok) {
                this.loadFolders();
            } else {
                const error = await response.json();
                alert(`Error creating folder: ${error.detail || 'Unknown error'}`);
            }
        } catch (error) {
            console.error('Error creating folder:', error);
            alert('Error creating folder');
        }
    }
    
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
    
    escapeJs(text) {
        return text.replace(/'/g, "\\'").replace(/"/g, '\\"');
    }
    
    async deleteFolder(folderId, folderName) {
        if (!confirm(`Are you sure you want to delete folder "${folderName}"?\n\nAll notes in this folder will be moved to "All Notes" (root).`)) {
            return;
        }
        
        try {
            const response = await fetch(`/api/notes/folders/${folderId}`, {
                method: 'DELETE',
                credentials: 'include'
            });
            
            if (response.ok) {
                const data = await response.json();
                this.showToast(data.message || 'Folder deleted successfully', 'success');
                
                // If we were viewing this folder, switch to "All Notes"
                if (this.currentFolderId === folderId) {
                    this.currentFolderId = 0;
                }
                
                // Reload folders and notes
                await this.loadFolders();
                await this.loadNotes();
            } else {
                const error = await response.json();
                this.showToast(error.detail || 'Failed to delete folder', 'error');
            }
        } catch (error) {
            console.error('Error deleting folder:', error);
            this.showToast('Network error while deleting folder', 'error');
        }
    }
    
    showToast(message, type = 'info') {
        // Simple toast notification
        const toast = document.createElement('div');
        toast.textContent = message;
        toast.style.cssText = `
            position: fixed;
            bottom: 20px;
            right: 20px;
            padding: 12px 20px;
            background: ${type === 'success' ? '#4CAF50' : type === 'error' ? '#f44336' : '#2196F3'};
            color: white;
            border-radius: 4px;
            z-index: 10000;
            animation: slideIn 0.3s ease;
        `;
        document.body.appendChild(toast);
        setTimeout(() => {
            toast.style.animation = 'slideOut 0.3s ease';
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    }
}

// Initialize notes browser
let notesBrowser = null;
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        notesBrowser = new NotesBrowser();
        window.notesBrowser = notesBrowser;
    });
} else {
    notesBrowser = new NotesBrowser();
    window.notesBrowser = notesBrowser;
}
