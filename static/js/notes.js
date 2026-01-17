// Notes Management
class NotesManager {
    constructor() {
        this.notes = [];
        this.folders = [];
        this.currentFolderId = 0; // 0 = all notes
        this.currentNoteId = null;
        this.currentNote = null; // Store current note for username access
        this.searchQuery = '';
        
        this.init();
    }
    
    init() {
        this.loadFolders();
        this.loadNotes();
        
        // Event listeners
        document.getElementById('newNoteBtn')?.addEventListener('click', () => this.createNote());
        document.getElementById('newFolderBtn')?.addEventListener('click', () => this.createFolder());
        document.getElementById('saveNoteBtn')?.addEventListener('click', () => this.saveNote());
        document.getElementById('cancelNoteBtn')?.addEventListener('click', () => this.cancelEdit());
        document.getElementById('deleteNoteBtn')?.addEventListener('click', () => this.deleteNote());
        document.getElementById('pinNoteBtn')?.addEventListener('click', () => this.togglePin());
        document.getElementById('moveNoteBtn')?.addEventListener('click', () => this.showMoveNoteDialog());
        // Attach file button - use once flag to prevent duplicate listeners
        const attachBtn = document.getElementById('attachFileBtn');
        if (attachBtn && !attachBtn.dataset.listenerAttached) {
            attachBtn.addEventListener('click', () => this.showAttachmentDialog());
            attachBtn.dataset.listenerAttached = 'true';
        }
        document.getElementById('noteFileInput')?.addEventListener('change', (e) => this.handleFileSelect(e));
        document.getElementById('notesSearchInput')?.addEventListener('input', (e) => {
            this.searchQuery = e.target.value;
            this.loadNotes();
        });
        
        // Edit/Preview mode toggle buttons - attach in separate method for re-initialization
        this.attachModeButtons();
        
        // Auto-save on content change (debounced)
        let saveTimeout;
        const contentInput = document.getElementById('noteContentInput');
        const titleInput = document.getElementById('noteTitleInput');
        if (contentInput) {
            contentInput.addEventListener('input', () => {
                clearTimeout(saveTimeout);
                saveTimeout = setTimeout(() => {
                    if (this.currentNoteId) {
                        this.saveNote(true); // Auto-save
                    }
                }, 2000);
            });
            
            // Handle paste events for images
            contentInput.addEventListener('paste', (e) => {
                this.handlePaste(e);
            });
        }
        if (titleInput) {
            titleInput.addEventListener('input', () => {
                clearTimeout(saveTimeout);
                saveTimeout = setTimeout(() => {
                    if (this.currentNoteId) {
                        this.saveNote(true); // Auto-save
                    }
                }, 2000);
            });
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
        const notesList = document.getElementById('notesList');
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
        const foldersList = document.getElementById('notesFoldersList');
        if (!foldersList) return;
        
        // Keep "All Notes" item
        let html = '<div class="notes-folder-item active" data-folder-id="0"><span class="folder-icon">📁</span><span class="folder-name">All Notes</span></div>';
        
        // Render folders (simple flat list for now)
        this.folders.forEach(folder => {
            html += `
                <div class="notes-folder-item" data-folder-id="${folder.id}">
                    <span class="folder-icon">📂</span>
                    <span class="folder-name">${this.escapeHtml(folder.name)}</span>
                    ${folder.notes_count > 0 ? `<span class="folder-count">${folder.notes_count}</span>` : ''}
                </div>
            `;
        });
        
        foldersList.innerHTML = html;
        
        // Add click handlers
        foldersList.querySelectorAll('.notes-folder-item').forEach(item => {
            item.addEventListener('click', () => {
                // Close note editor if open
                const notesEditor = document.getElementById('notesEditor');
                const notesList = document.getElementById('notesList');
                if (notesEditor && notesList) {
                    notesEditor.style.display = 'none';
                    notesList.style.display = 'grid';
                }
                
                // Clear current note
                this.currentNoteId = null;
                this.currentNote = null;
                
                // Update active folder
                foldersList.querySelectorAll('.notes-folder-item').forEach(i => i.classList.remove('active'));
                item.classList.add('active');
                this.currentFolderId = parseInt(item.dataset.folderId) || 0;
                this.loadNotes();
            });
        });
    }
    
    renderNotes() {
        const notesList = document.getElementById('notesList');
        if (!notesList) return;
        
        if (this.notes.length === 0) {
            notesList.innerHTML = `
                <div class="notes-empty">
                    <div style="font-size: 18px; margin-bottom: 12px; font-weight: 600;">No notes found</div>
                    <div style="opacity: 0.7;">Click "✨ New Note" to create your first note</div>
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
            const dateStr = date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: date.getFullYear() !== new Date().getFullYear() ? 'numeric' : undefined });
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
        
        // Add click handlers
        notesList.querySelectorAll('.notes-item').forEach(item => {
            item.addEventListener('click', () => {
                const noteId = parseInt(item.dataset.noteId);
                this.openNote(noteId);
            });
        });
    }
    
    async openNote(noteId) {
        try {
            const response = await fetch(`/api/notes/${noteId}`);
            if (response.ok) {
                const note = await response.json();
                this.currentNoteId = note.id;
                this.currentNote = note; // Store for username access in renderMarkdown
                
                document.getElementById('noteTitleInput').value = note.title;
                document.getElementById('noteContentInput').value = note.content;
                document.getElementById('noteTagsInput').value = note.tags || '';
                document.getElementById('pinNoteBtn').textContent = note.is_pinned ? '📌' : '📍';
                document.getElementById('pinNoteBtn').dataset.pinned = note.is_pinned;
                
                // Render attachments if any
                this.renderAttachments(note);
                
                // Update preview if in preview mode
                this.updatePreview();
                
                // Show editor, hide list
                document.getElementById('notesList').style.display = 'none';
                document.getElementById('notesEditor').style.display = 'block';
                
                // Re-attach event listeners in case they weren't attached
                const contentInput = document.getElementById('noteContentInput');
                if (contentInput && !contentInput.dataset.pasteListenerAttached) {
                    contentInput.addEventListener('paste', (e) => {
                        this.handlePaste(e);
                    });
                    contentInput.dataset.pasteListenerAttached = 'true';
                }
                
                // Re-attach attach button listener
                const attachBtn = document.getElementById('attachFileBtn');
                if (attachBtn && !attachBtn.dataset.listenerAttached) {
                    attachBtn.addEventListener('click', () => this.showAttachmentDialog());
                    attachBtn.dataset.listenerAttached = 'true';
                }
                
                // Start in preview mode (default action)
                this.setEditorMode('preview');
            }
        } catch (error) {
            console.error('Error loading note:', error);
        }
    }
    
    setEditorMode(mode) {
        const contentInput = document.getElementById('noteContentInput');
        const contentPreview = document.getElementById('noteContentPreview');
        const editBtn = document.getElementById('noteEditModeBtn');
        const previewBtn = document.getElementById('notePreviewModeBtn');
        
        // Null checks to prevent runtime errors
        if (!contentInput || !contentPreview || !editBtn || !previewBtn) {
            console.warn('Editor elements not found, cannot set mode');
            return;
        }
        
        if (mode === 'edit') {
            contentInput.style.display = 'block';
            contentPreview.style.display = 'none';
            editBtn.classList.add('active');
            previewBtn.classList.remove('active');
            contentInput.focus();
        } else {
            contentInput.style.display = 'none';
            contentPreview.style.display = 'block';
            editBtn.classList.remove('active');
            previewBtn.classList.add('active');
            this.updatePreview();
        }
    }
    
    updatePreview() {
        const contentInput = document.getElementById('noteContentInput');
        const contentPreview = document.getElementById('noteContentPreview');
        if (!contentInput || !contentPreview) return;
        
        const markdown = contentInput.value;
        let rendered = this.renderMarkdown(markdown);
        
        // Fix old Joplin resource URLs in the HTML string BEFORE setting innerHTML
        // This prevents the browser from trying to load them
        const placeholder = 'data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMjAwIiBoZWlnaHQ9IjIwMCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48cmVjdCB3aWR0aD0iMjAwIiBoZWlnaHQ9IjIwMCIgZmlsbD0iI2VlZSIvPjx0ZXh0IHg9IjUwJSIgeT0iNTAlIiBmb250LWZhbWlseT0iQXJpYWwiIGZvbnQtc2l6ZT0iMTQiIGZpbGw9IiM5OTkiIHRleHQtYW5jaG9yPSJtaWRkbGUiIGR5PSIuM2VtIj5JbWFnZSBub3QgZm91bmQ8L3RleHQ+PC9zdmc+';
        
        // Replace old Joplin resource URLs in img src attributes in the HTML string
        rendered = rendered.replace(/<img([^>]*)\ssrc=["']([^"']+)["']([^>]*)>/gi, (match, before, src, after) => {
            // Check for old Joplin resource format (:/[32-char-hex] or bare [32-char-hex])
            const isOldFormat = src.startsWith(':/') && /^:\/[a-f0-9]{32}$/.test(src);
            const isBareFormat = /^[a-f0-9]{32}$/.test(src);
            
            if (isOldFormat || isBareFormat) {
                return `<img${before} src="${placeholder}"${after}>`;
            }
            return match;
        });
        
        // Also replace any remaining :/[resource-id] patterns that might be in the HTML
        rendered = rendered.replace(/:\/([a-f0-9]{32})/g, () => placeholder);
        
        // Replace bare resource IDs that might appear as standalone URLs or in href attributes
        // This catches cases where resource IDs appear without the :/ prefix
        rendered = rendered.replace(/(src|href)=["']([a-f0-9]{32})["']/gi, (match, attr, id) => {
            // Only replace if it's exactly 32 hex characters (Joplin resource ID format)
            if (/^[a-f0-9]{32}$/i.test(id)) {
                return `${attr}="${placeholder}"`;
            }
            return match;
        });
        
        // Also replace bare resource IDs that might be in URLs (like /api/files/view/[resource-id])
        rendered = rendered.replace(/(\/api\/[^"'\s]*\/)([a-f0-9]{32})([^"'\s]*)/gi, (match, prefix, id, suffix) => {
            // If the ID is exactly 32 hex chars and appears to be a Joplin resource ID
            if (/^[a-f0-9]{32}$/i.test(id)) {
                return prefix + placeholder + suffix;
            }
            return match;
        });
        
        contentPreview.innerHTML = rendered;
        
        // Double-check after rendering to catch any that slipped through
        const imgs = contentPreview.querySelectorAll('img');
        imgs.forEach(img => {
            const src = img.getAttribute('src') || img.src;
            if (!src) return;
            
            const isOldFormat = src.startsWith(':/') && /^:\/[a-f0-9]{32}$/.test(src);
            const isBareFormat = /^[a-f0-9]{32}$/.test(src);
            // Also check if src is a bare resource ID in a URL path
            const isResourceIdInPath = /\/[a-f0-9]{32}(?:\/|$|:)/.test(src) && /[a-f0-9]{32}/.test(src);
            
            if (isOldFormat || isBareFormat || isResourceIdInPath) {
                // Extract just the resource ID part if it's in a path
                const resourceIdMatch = src.match(/([a-f0-9]{32})/);
                if (resourceIdMatch && /^[a-f0-9]{32}$/i.test(resourceIdMatch[1])) {
                    img.src = placeholder;
                    img.setAttribute('src', placeholder);
                    img.setAttribute('alt', 'Image not found (old Joplin resource)');
                    img.onerror = null;
                    // Prevent the browser from trying to load it
                    img.removeAttribute('srcset');
                }
            }
        });
        
        // Also check all links for resource IDs
        const links = contentPreview.querySelectorAll('a[href]');
        links.forEach(link => {
            const href = link.getAttribute('href') || link.href;
            if (!href) return;
            
            // Check if href contains a bare resource ID
            const resourceIdMatch = href.match(/([a-f0-9]{32})/);
            if (resourceIdMatch && /^[a-f0-9]{32}$/i.test(resourceIdMatch[1]) && !href.startsWith('http')) {
                // Replace with placeholder or remove the link
                link.href = '#';
                link.onclick = (e) => {
                    e.preventDefault();
                    alert('This link references an old Joplin resource that is no longer available.');
                };
            }
        });
    }
    
    renderMarkdown(text) {
        if (!text) return '';
        
        // Get username and note ID for image URLs
        // Try multiple methods for reliability
        let username = 'user';
        const sidebarUser = document.querySelector('.user-name');
        if (sidebarUser && sidebarUser.textContent) {
            username = sidebarUser.textContent.trim();
        } else {
            // Fallback: try to get from current note if available
            // This will be set when note is loaded
            if (this.currentNote && this.currentNote.username) {
                username = this.currentNote.username;
            }
        }
        
        // Validate username (basic check - fallback to 'user' if not found)
        
        const noteId = this.currentNoteId;
        
        // Extract code blocks first
        const codeBlocks = [];
        let processed = text.replace(/```(\w*)\n?([\s\S]*?)```/g, (match, lang, code) => {
            const index = codeBlocks.length;
            codeBlocks.push({ lang: lang || '', code: code.trimEnd() });
            return `\x00CODEBLOCK${index}\x00`;
        });
        
        // Extract and preserve markdown images/videos - convert relative paths to note attachment URLs
        const images = [];
        const videos = [];
        processed = processed.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (match, alt, src) => {
            let mediaSrc = src;
            
            // If it's already an /api/ URL, use it as-is (might be from migration)
            if (src.startsWith('/api/notes/files/')) {
                // Already a proper URL, use it
                mediaSrc = src;
            } else if (src.startsWith('http://') || src.startsWith('https://')) {
                // External URL, use as-is
                mediaSrc = src;
            } else if ((src.startsWith(':/') && /^:\/[a-f0-9]{32}$/.test(src)) || /^[a-f0-9]{32}$/.test(src)) {
                // Old Joplin resource format - return placeholder
                mediaSrc = 'data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMjAwIiBoZWlnaHQ9IjIwMCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48cmVjdCB3aWR0aD0iMjAwIiBoZWlnaHQ9IjIwMCIgZmlsbD0iI2VlZSIvPjx0ZXh0IHg9IjUwJSIgeT0iNTAlIiBmb250LWZhbWlseT0iQXJpYWwiIGZvbnQtc2l6ZT0iMTQiIGZpbGw9IiM5OTkiIHRleHQtYW5jaG9yPSJtaWRkbGUiIGR5PSIuM2VtIj5JbWFnZSBub3QgZm91bmQ8L3RleHQ+PC9zdmc+';
            } else {
                // Relative path or filename - convert to note attachment URL
                const filename = src.replace(/^\.\//, '').split('/').pop();
                if (noteId) {
                    mediaSrc = `/api/notes/files/${username}/${noteId}/${encodeURIComponent(filename)}`;
                } else {
                    // Keep original, might work if it's already a valid path
                    mediaSrc = src;
                }
            }
            
            // Check if this is a video file
            const isVideo = /\.(mp4|mpeg|mov|avi|webm|mkv|flv|wmv|3gp|ogv)$/i.test(mediaSrc);
            if (isVideo) {
                const index = videos.length;
                videos.push({ alt: alt || '', src: mediaSrc });
                return `\x00VIDEO${index}\x00`;
            } else {
                // It's an image (including GIFs)
                const index = images.length;
                images.push({ alt: alt || '', src: mediaSrc });
                return `\x00IMAGE${index}\x00`;
            }
        });
        
        // Process markdown links (but not images, which we already extracted)
        // Also handle old Joplin resource format: ](:/[resource-id])
        const links = [];
        processed = processed.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, text, url) => {
            const index = links.length;
            // Check if this is an old Joplin resource URL (:/[resource-id] or bare [resource-id])
            if ((url.startsWith(':/') && /^:\/[a-f0-9]{32}$/.test(url)) || /^[a-f0-9]{32}$/.test(url)) {
                // Old Joplin resource format - replace with placeholder link
                links.push({ text, url: '#', external: false, isOldResource: true });
            } else {
                const isExternal = url.startsWith('http') || url.startsWith('//');
                links.push({ text, url, external: isExternal });
            }
            return `\x00LINK${index}\x00`;
        });
        
        // Process markdown formatting BEFORE escaping HTML
        // Headers (process before other formatting)
        let html = processed.replace(/^### (.*$)/gim, '<h3>$1</h3>');
        html = html.replace(/^## (.*$)/gim, '<h2>$1</h2>');
        html = html.replace(/^# (.*$)/gim, '<h1>$1</h1>');
        
        // Horizontal rules
        html = html.replace(/^---$/gim, '<hr>');
        html = html.replace(/^\*\*\*$/gim, '<hr>');
        
        // Lists - wrap consecutive list items
        html = html.replace(/(?:^[\*\-\+] .+$(?:\n|$))+/gm, (match) => {
            const items = match.trim().split('\n').map(line => {
                const text = line.replace(/^[\*\-\+]\s+/, '').trim();
                if (!text) return '';
                return `<li>${text}</li>`;
            }).filter(item => item).join('');
            return items ? `<ul>${items}</ul>` : '';
        });
        
        // Numbered lists
        html = html.replace(/(?:^\d+\. .+$(?:\n|$))+/gm, (match) => {
            const items = match.trim().split('\n').map(line => {
                const text = line.replace(/^\d+\.\s+/, '').trim();
                if (!text) return '';
                return `<li>${text}</li>`;
            }).filter(item => item).join('');
            return items ? `<ol>${items}</ol>` : '';
        });
        
        // Bold **text** (after headers to avoid conflicts)
        html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        
        // Italic *text* (after bold to avoid conflicts)
        html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
        
        // Inline code `text` (but not inside code blocks)
        html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
        
        // Plain URLs (not already in a link)
        html = html.replace(/(https?:\/\/[^\s<]+)(?![^<]*<\/a>)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
        
        // Newlines (convert double newlines to paragraphs, single to <br>)
        // But preserve existing HTML structure
        const lines = html.split('\n');
        let result = [];
        let currentPara = [];
        
        for (let i = 0; i < lines.length; i++) {
            const line = lines[i].trim();
            if (!line) {
                if (currentPara.length > 0) {
                    result.push(`<p>${currentPara.join(' ')}</p>`);
                    currentPara = [];
                }
                result.push('');
            } else if (line.startsWith('<') || line.startsWith('</')) {
                // HTML tag - flush current para and add as-is
                if (currentPara.length > 0) {
                    result.push(`<p>${currentPara.join(' ')}</p>`);
                    currentPara = [];
                }
                result.push(line);
            } else {
                currentPara.push(line);
            }
        }
        if (currentPara.length > 0) {
            result.push(`<p>${currentPara.join(' ')}</p>`);
        }
        html = result.join('\n');
        
        // Convert remaining single newlines to <br> (but not inside HTML tags)
        html = html.replace(/\n/g, '<br>');
        
        // NOW escape HTML for text content (but preserve placeholders and already-inserted HTML tags)
        // We need to escape text but not the HTML tags we've already inserted
        // Strategy: temporarily replace HTML tags, escape, then restore
        // IMPORTANT: Image placeholders (\x00IMAGE...) should NOT be affected by HTML escaping
        // because \x00 is a null byte and won't match HTML patterns
        const htmlTagPlaceholders = [];
        let htmlTagIndex = 0;
        
        // Replace HTML tags with placeholders (this won't affect \x00IMAGE or \x00VIDEO placeholders)
        html = html.replace(/<[^>]+>/g, (match) => {
            // Skip if this is an image or video placeholder (shouldn't happen, but be safe)
            if (match.includes('\x00IMAGE') || match.includes('\x00VIDEO')) {
                return match;
            }
            const placeholder = `\x00HTMLTAG${htmlTagIndex}\x00`;
            htmlTagPlaceholders.push(match);
            htmlTagIndex++;
            return placeholder;
        });
        
        // Escape HTML in text content (this won't affect \x00IMAGE or \x00VIDEO placeholders since they use null bytes)
        html = html
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
        
        // Restore HTML tags
        html = html.replace(/\x00HTMLTAG(\d+)\x00/g, (match, index) => {
            return htmlTagPlaceholders[parseInt(index)];
        });
        
        // Restore videos as HTML5 video tags (before images, so they're processed first)
        // Videos must be restored AFTER HTML escaping but BEFORE images
        html = html.replace(/\x00VIDEO(\d+)\x00/g, (match, index) => {
            const vidIndex = parseInt(index);
            if (!videos || !videos[vidIndex]) {
                return `[Video ${vidIndex} not found]`;
            }
            const vid = videos[vidIndex];
            if (!vid || !vid.src) {
                return `[Video ${vidIndex} invalid]`;
            }
            const safeAlt = this.escapeHtml(vid.alt || '');
            const safeSrc = vid.src; // Don't escape URL - it's already a valid URL string
            
            // Add cache busting
            const cacheBust = safeSrc.includes('?') ? '&' : '?';
            const vidSrc = `${safeSrc}${cacheBust}t=${Date.now()}`;
            
            // Create HTML5 video player with controls
            return `<video src="${vidSrc}" controls style="max-width: 100%; height: auto; border-radius: 8px; margin: 16px 0; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);">Your browser does not support the video tag.</video>`;
        });
        
        // Restore images as HTML img tags (after escaping, so they're not escaped)
        // This must happen AFTER HTML tag restoration to ensure proper order
        html = html.replace(/\x00IMAGE(\d+)\x00/g, (match, index) => {
            const imgIndex = parseInt(index);
            if (!images || !images[imgIndex]) {
                return `[Image ${imgIndex} not found]`;
            }
            const img = images[imgIndex];
            if (!img || !img.src) {
                return `[Image ${imgIndex} invalid]`;
            }
            const safeAlt = this.escapeHtml(img.alt || '');
            const safeSrc = img.src; // Don't escape URL - it's already a valid URL string
            
            // Add cache busting and error handling
            const cacheBust = safeSrc.includes('?') ? '&' : '?';
            const imgSrc = `${safeSrc}${cacheBust}t=${Date.now()}`;
            
            // Check if it's a GIF (GIFs should animate)
            const isGif = /\.gif$/i.test(safeSrc);
            
            // Use proper error handling - show placeholder if image fails to load
            // For GIFs, ensure they can animate properly and don't get optimized away
            // Add loading="lazy" for better performance, but ensure GIFs animate
            const gifStyle = isGif ? ' image-rendering: auto;' : '';
            return `<img src="${imgSrc}" alt="${safeAlt}" loading="lazy" style="max-width: 100%; height: auto; border-radius: 8px; margin: 16px 0; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);${gifStyle}" onerror="this.onerror=null; this.src='data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMjAwIiBoZWlnaHQ9IjIwMCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48cmVjdCB3aWR0aD0iMjAwIiBoZWlnaHQ9IjIwMCIgZmlsbD0iIzJhMmEzZSIvPjx0ZXh0IHg9IjUwJSIgeT0iNTAlIiBmb250LWZhbWlseT0iQXJpYWwiIGZvbnQtc2l6ZT0iMTQiIGZpbGw9IiM4ODg4YWEiIHRleHQtYW5jaG9yPSJtaWRkbGUiIGR5PSIuM2VtIj5JbWFnZSBub3QgZm91bmQ8L3RleHQ+PC9zdmc+';">`;
        });
        
        // Restore links (after escaping, so they're not escaped)
        html = html.replace(/\x00LINK(\d+)\x00/g, (match, index) => {
            const link = links[parseInt(index)];
            // Handle old Joplin resource links specially
            if (link && link.isOldResource) {
                return `<a href="#" onclick="alert('This link references an old Joplin resource that is no longer available.'); return false;" class="old-resource-link" style="color: #888; text-decoration: line-through;">${this.escapeHtml(link.text)}</a>`;
            }
            const target = link.external ? ' target="_blank" rel="noopener"' : '';
            return `<a href="${this.escapeHtml(link.url)}"${target}>${this.escapeHtml(link.text)}</a>`;
        });
        
        // Restore code blocks with syntax highlighting
        html = html.replace(/\x00CODEBLOCK(\d+)\x00/g, (match, index) => {
            const block = codeBlocks[parseInt(index)];
            const lang = block.lang ? ` class="language-${block.lang}"` : '';
            return `<pre><code${lang}>${this.escapeHtml(block.code)}</code></pre>`;
        });
        
        return html;
    }
    
    renderAttachments(note) {
        // Remove existing attachments display
        const existing = document.getElementById('noteAttachments');
        if (existing) existing.remove();
        
        if (!note.attachments) return;
        
        let attachments;
        try {
            attachments = typeof note.attachments === 'string' 
                ? JSON.parse(note.attachments) 
                : note.attachments;
        } catch (e) {
            return; // Invalid JSON
        }
        
        if (!attachments || attachments.length === 0) return;
        
        const editor = document.getElementById('notesEditor');
        const attachmentsDiv = document.createElement('div');
        attachmentsDiv.id = 'noteAttachments';
        attachmentsDiv.className = 'notes-attachments';
        
        // Get username from sidebar (it's rendered in the template)
        // Try multiple methods for reliability
        let username = 'user';
        const sidebarUser = document.querySelector('.user-name');
        if (sidebarUser && sidebarUser.textContent) {
            username = sidebarUser.textContent.trim();
        } else if (this.currentNote && this.currentNote.username) {
            username = this.currentNote.username;
        }
        
        attachmentsDiv.innerHTML = `
            <div class="notes-attachments-header">
                <span>Attachments (${attachments.length})</span>
            </div>
            <div class="notes-attachments-list">
                ${attachments.map((filename, index) => {
                    const ext = filename.split('.').pop().toLowerCase();
                    const isImage = /^(png|jpg|jpeg|gif|webp|svg|bmp|tiff|ico)$/i.test(ext);
                    const isPdf = ext === 'pdf';
                    const isVideo = /^(mp4|mpeg|mov|avi|webm|mkv)$/i.test(ext);
                    const isAudio = /^(mp3|wav|ogg|m4a|webm)$/i.test(ext);
                    const isDocument = /^(doc|docx|xls|xlsx|ppt|pptx|odt|ods|odp)$/i.test(ext);
                    const isArchive = /^(zip|rar|tar|gz|7z)$/i.test(ext);
                    const isCode = /^(py|java|c|cpp|cs|js|html|css|json|xml|sh)$/i.test(ext);
                    const fileUrl = `/api/notes/files/${username}/${note.id}/${encodeURIComponent(filename)}`;
                    const shortName = filename.length > 20 ? filename.substring(0, 17) + '...' : filename;
                    
                    // Choose icon based on file type
                    let icon = '📎'; // Default
                    if (isImage) {
                        return `<div class="attachment-item" data-filename="${this.escapeHtml(filename)}">
                            <img src="${fileUrl}?t=${Date.now()}" alt="${this.escapeHtml(filename)}" class="attachment-preview" loading="lazy" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
                            <div class="attachment-icon" style="display: none;">🖼️</div>
                            <a href="${fileUrl}" target="_blank" class="attachment-link" onclick="event.stopPropagation()">${this.escapeHtml(shortName)}</a>
                            <button class="attachment-remove-btn" onclick="event.stopPropagation(); window.notesManager.removeAttachment('${this.escapeHtml(filename)}');" title="Remove attachment">🗑️</button>
                        </div>`;
                    } else if (isPdf) {
                        icon = '📄';
                    } else if (isVideo) {
                        icon = '🎬';
                    } else if (isAudio) {
                        icon = '🎵';
                    } else if (isDocument) {
                        icon = '📝';
                    } else if (isArchive) {
                        icon = '📦';
                    } else if (isCode) {
                        icon = '💻';
                    }
                    
                    return `<div class="attachment-item" data-filename="${this.escapeHtml(filename)}">
                        <div class="attachment-icon">${icon}</div>
                        <a href="${fileUrl}" target="_blank" class="attachment-link" onclick="event.stopPropagation()">${this.escapeHtml(shortName)}</a>
                        <button class="attachment-remove-btn" onclick="event.stopPropagation(); window.notesManager.removeAttachment('${this.escapeHtml(filename)}');" title="Remove attachment">🗑️</button>
                    </div>`;
                }).join('')}
            </div>
        `;
        
        // Insert before editor footer
        const footer = editor.querySelector('.notes-editor-footer');
        editor.insertBefore(attachmentsDiv, footer);
    }
    
    cancelEdit() {
        this.currentNoteId = null;
        this.currentNote = null; // Clear stored note
        document.getElementById('noteTitleInput').value = '';
        document.getElementById('noteContentInput').value = '';
        document.getElementById('noteTagsInput').value = '';
        document.getElementById('noteContentPreview').innerHTML = '';
        this.setEditorMode('edit');
        document.getElementById('notesList').style.display = 'block';
        document.getElementById('notesEditor').style.display = 'none';
    }
    
    createNote() {
        this.currentNoteId = null;
        this.currentNote = null; // Clear stored note for new note
        document.getElementById('noteTitleInput').value = '';
        document.getElementById('noteContentInput').value = '';
        document.getElementById('noteTagsInput').value = '';
        document.getElementById('noteContentPreview').innerHTML = '';
        document.getElementById('pinNoteBtn').textContent = '📍';
        document.getElementById('pinNoteBtn').dataset.pinned = 'false';
        
        this.setEditorMode('edit');
        document.getElementById('notesList').style.display = 'none';
        document.getElementById('notesEditor').style.display = 'block';
        
        // Re-attach event listeners in case they weren't attached
        const attachBtn = document.getElementById('attachFileBtn');
        if (attachBtn && !attachBtn.dataset.listenerAttached) {
            attachBtn.addEventListener('click', () => this.showAttachmentDialog());
            attachBtn.dataset.listenerAttached = 'true';
        }
        
        document.getElementById('noteTitleInput').focus();
    }
    
    async saveNote(autoSave = false) {
        const title = document.getElementById('noteTitleInput').value.trim();
        const content = document.getElementById('noteContentInput').value;
        const tags = document.getElementById('noteTagsInput').value.trim();
        const isPinned = document.getElementById('pinNoteBtn').dataset.pinned === 'true';
        
        if (!title && !autoSave) {
            alert('Please enter a note title');
            return;
        }
        
        if (!title && autoSave) {
            return; // Don't save empty notes on auto-save
        }
        
        try {
            const url = this.currentNoteId ? `/api/notes/${this.currentNoteId}` : '/api/notes';
            const method = this.currentNoteId ? 'PUT' : 'POST';
            
            const body = {
                title: title || 'Untitled',
                content: content,
                tags: tags || null,
                folder_id: this.currentFolderId !== 0 ? this.currentFolderId : null,
                is_pinned: isPinned
            };
            
            const response = await csrfFetch(url, {
                method: method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });
            
            if (response.ok) {
                const note = await response.json();
                this.currentNoteId = note.id;
                
                if (!autoSave) {
                    this.loadNotes();
                    this.cancelEdit();
                }
            } else {
                // Try to parse as JSON, fallback to text
                let errorDetail = 'Unknown error';
                try {
                    const errorText = await response.text();
                    try {
                        const errorJson = JSON.parse(errorText);
                        errorDetail = errorJson.detail || errorJson.message || errorText;
                    } catch {
                        errorDetail = errorText || `HTTP ${response.status}`;
                    }
                } catch (e) {
                    errorDetail = `HTTP ${response.status}: ${response.statusText}`;
                }
                console.error('Error saving note:', errorDetail);
                if (!autoSave) {
                    alert(`Error saving note: ${errorDetail}`);
                }
            }
        } catch (error) {
            console.error('Error saving note:', error);
            if (!autoSave) {
                alert(`Error saving note: ${error.message || 'Network error'}`);
            }
        }
    }
    
    async deleteNote() {
        if (!this.currentNoteId) return;
        
        if (!confirm('Are you sure you want to delete this note?')) {
            return;
        }
        
        try {
            const response = await csrfFetch(`/api/notes/${this.currentNoteId}`, {
                method: 'DELETE'
            });
            
            if (response.ok) {
                this.cancelEdit();
                this.loadNotes();
            } else {
                alert('Error deleting note');
            }
        } catch (error) {
            console.error('Error deleting note:', error);
            alert('Error deleting note');
        }
    }
    
    showMoveNoteDialog() {
        if (!this.currentNoteId) {
            this.showToast('Please open a note first', 'error');
            return;
        }
        
        const modal = document.getElementById('moveNoteModal');
        const titleSpan = document.getElementById('moveNoteTitle');
        const folderSelect = document.getElementById('moveNoteFolder');
        
        if (!modal || !titleSpan || !folderSelect) {
            this.showToast('Move dialog elements not found', 'error');
            return;
        }
        
        // Set note title
        const titleInput = document.getElementById('noteTitleInput');
        titleSpan.textContent = titleInput ? titleInput.value || 'Untitled Note' : 'Note';
        
        // Populate folder dropdown
        folderSelect.innerHTML = '<option value="0">All Notes (Root)</option>';
        this.folders.forEach(folder => {
            const option = document.createElement('option');
            option.value = folder.id;
            option.textContent = folder.name;
            // Select current folder if note is in one
            if (this.currentNote && this.currentNote.folder_id === folder.id) {
                option.selected = true;
            }
            folderSelect.appendChild(option);
        });
        
        // If note has no folder, select "All Notes"
        if (!this.currentNote || !this.currentNote.folder_id) {
            folderSelect.value = '0';
        }
        
        modal.style.display = 'flex';
    }
    
    async executeMoveNote() {
        if (!this.currentNoteId) {
            this.showToast('No note selected', 'error');
            return;
        }
        
        const folderSelect = document.getElementById('moveNoteFolder');
        if (!folderSelect) {
            this.showToast('Folder select not found', 'error');
            return;
        }
        
        const folderId = parseInt(folderSelect.value);
        const targetFolderId = folderId === 0 ? null : folderId;
        
        // Check if already in target folder
        const currentFolderId = this.currentNote?.folder_id || null;
        if (currentFolderId === targetFolderId) {
            document.getElementById('moveNoteModal').style.display = 'none';
            this.showToast('Note is already in this folder', 'info');
            return;
        }
        
        try {
            // Get current note data
            const titleInput = document.getElementById('noteTitleInput');
            const contentInput = document.getElementById('noteContentInput');
            const tagsInput = document.getElementById('noteTagsInput');
            const pinBtn = document.getElementById('pinNoteBtn');
            
            const updateData = {
                folder_id: targetFolderId,
                title: titleInput?.value || this.currentNote?.title,
                content: contentInput?.value || this.currentNote?.content,
                tags: tagsInput?.value || this.currentNote?.tags,
                is_pinned: pinBtn?.dataset.pinned === 'true'
            };
            
            const response = await csrfFetch(`/api/notes/${this.currentNoteId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(updateData)
            });
            
            if (response.ok) {
                const updatedNote = await response.json();
                this.currentNote = updatedNote;
                
                // Update current folder view if needed
                const newFolderId = updatedNote.folder_id || 0;
                if (this.currentFolderId !== newFolderId) {
                    // Note moved to different folder - switch to that folder view
                    this.currentFolderId = newFolderId;
                    // Update folder selection in sidebar
                    document.querySelectorAll('.notes-folder-item').forEach(item => {
                        item.classList.remove('active');
                        if (parseInt(item.dataset.folderId) === newFolderId) {
                            item.classList.add('active');
                        }
                    });
                }
                
                // Reload notes list
                await this.loadNotes();
                
                // Close modal
                document.getElementById('moveNoteModal').style.display = 'none';
                
                const folderName = updatedNote.folder_name || 'All Notes';
                this.showToast(`Note moved to "${folderName}"`);
            } else {
                const error = await response.json();
                this.showToast(error.detail || 'Failed to move note', 'error');
            }
        } catch (error) {
            console.error('Error moving note:', error);
            this.showToast('Failed to move note', 'error');
        }
    }
    
    async togglePin() {
        if (!this.currentNoteId) return;
        
        const isPinned = document.getElementById('pinNoteBtn').dataset.pinned === 'true';
        
        try {
            const response = await csrfFetch(`/api/notes/${this.currentNoteId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ is_pinned: !isPinned })
            });
            
            if (response.ok) {
                document.getElementById('pinNoteBtn').dataset.pinned = (!isPinned).toString();
                document.getElementById('pinNoteBtn').textContent = !isPinned ? '📌' : '📍';
                this.loadNotes();
            }
        } catch (error) {
            console.error('Error toggling pin:', error);
        }
    }
    
    async createFolder() {
        const name = prompt('Enter folder name:');
        if (!name) return;
        
        // Ensure csrfFetch is available
        if (typeof csrfFetch === 'undefined') {
            // csrfFetch not available, fallback to fetch
            alert('Error: CSRF protection not loaded. Please refresh the page.');
            return;
        }
        
        try {
            const response = await csrfFetch('/api/notes/folders', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: name.trim() })
            });
            
            
            if (response.ok) {
                this.loadFolders();
            } else {
                let errorDetail = 'Unknown error';
                try {
                    const errorText = await response.text();
                    try {
                        const errorJson = JSON.parse(errorText);
                        errorDetail = errorJson.detail || errorJson.message || errorText;
                    } catch {
                        errorDetail = errorText || `HTTP ${response.status}`;
                    }
                } catch (e) {
                    errorDetail = `HTTP ${response.status}: ${response.statusText}`;
                }
                console.error('Error creating folder:', errorDetail);
                alert(`Error creating folder: ${errorDetail}`);
            }
        } catch (error) {
            console.error('Error creating folder:', error);
            alert(`Error creating folder: ${error.message || 'Network error'}`);
        }
    }
    
    attachModeButtons() {
        // Attach event listeners to Edit/Preview mode buttons
        // This is called separately because buttons might not be available during init()
        const editBtn = document.getElementById('noteEditModeBtn');
        const previewBtn = document.getElementById('notePreviewModeBtn');
        
        if (editBtn && !editBtn.dataset.listenerAttached) {
            editBtn.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
                this.setEditorMode('edit');
            });
            editBtn.dataset.listenerAttached = 'true';
        }
        
        if (previewBtn && !previewBtn.dataset.listenerAttached) {
            previewBtn.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
                this.setEditorMode('preview');
            });
            previewBtn.dataset.listenerAttached = 'true';
        }
        
        // Don't re-attach attach button listener - it's already attached in init()
        // The listener persists and doesn't need to be re-attached
    }
    
    async handlePaste(e) {
        const items = e.clipboardData?.items;
        if (!items) {
            return;
        }
        
        // Check if we're in the note content input
        const contentInput = document.getElementById('noteContentInput');
        if (!contentInput || document.activeElement !== contentInput) {
            return; // Not pasting into note editor
        }
        
        // Check if there's an image in the clipboard
        let hasImage = false;
        for (let i = 0; i < items.length; i++) {
            if (items[i].type.indexOf('image') !== -1) {
                hasImage = true;
                break;
            }
        }
        
        if (!hasImage) {
            return; // Not an image, allow normal paste
        }
        
        // Prevent default paste behavior for images
        e.preventDefault();
        
        // Check if we have a note open
        if (!this.currentNoteId) {
            // If no note is open, create a new one in the database first
            try {
                // Create a new note with a default title
                const title = 'Untitled Note';
                const content = '';
                
                const response = await csrfFetch('/api/notes', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        title: title,
                        content: content,
                        folder_id: this.currentFolderId !== 0 ? this.currentFolderId : null,
                        is_pinned: false
                    })
                });
                
                if (response.ok) {
                    const note = await response.json();
                    this.currentNoteId = note.id;
                    this.currentNote = note;
                    
                    // Update the UI
                    document.getElementById('noteTitleInput').value = note.title;
                    document.getElementById('noteContentInput').value = note.content;
                    document.getElementById('noteTagsInput').value = note.tags || '';
                    document.getElementById('pinNoteBtn').textContent = note.is_pinned ? '📌' : '📍';
                    document.getElementById('pinNoteBtn').dataset.pinned = note.is_pinned;
                    
                    // Show editor, hide list
                    document.getElementById('notesList').style.display = 'none';
                    document.getElementById('notesEditor').style.display = 'block';
                    this.setEditorMode('edit');
                    
                    // Now process the paste
                    await this.processPastedImage(items);
                } else {
                    const error = await response.json();
                    this.showToast(error.detail || 'Failed to create note', 'error');
                }
            } catch (error) {
                console.error('Error creating note for paste:', error);
                this.showToast('Failed to create note', 'error');
            }
        } else {
            // Note exists, process paste normally
            await this.processPastedImage(items);
        }
    }
    
    async processPastedImage(items) {
        for (let i = 0; i < items.length; i++) {
            const item = items[i];
            if (item.type.indexOf('image') !== -1) {
                const file = item.getAsFile();
                if (file) {
                    // Generate a filename if none exists
                    if (!file.name) {
                        const ext = file.type.split('/')[1] || 'png';
                        file.name = `pasted-image-${Date.now()}.${ext}`;
                    }
                    await this.uploadAttachment(file);
                }
            }
        }
    }
    
    async uploadAttachment(file) {
        if (!this.currentNoteId) {
            this.showToast('Please create or open a note first', 'error');
            return;
        }
        
        // Get username for the file URL
        let username = 'user';
        const sidebarUser = document.querySelector('.user-name');
        if (sidebarUser && sidebarUser.textContent) {
            username = sidebarUser.textContent.trim();
        } else if (this.currentNote && this.currentNote.username) {
            username = this.currentNote.username;
        }
        
        const formData = new FormData();
        formData.append('file', file);
        
        try {
            const response = await csrfFetch(`/api/notes/${this.currentNoteId}/attachments`, {
                method: 'POST',
                body: formData
            });
            
            if (response.ok) {
                const data = await response.json();
                this.showToast(`Attachment "${data.filename}" uploaded successfully`);
                
                // Insert file reference at cursor position with proper API URL
                const contentInput = document.getElementById('noteContentInput');
                if (contentInput && data.filename) {
                    const cursorPos = contentInput.selectionStart || contentInput.value.length;
                    const textBefore = contentInput.value.substring(0, cursorPos);
                    const textAfter = contentInput.value.substring(cursorPos);
                    
                    // Use the full API path for the file
                    const fileUrl = `/api/notes/files/${username}/${this.currentNoteId}/${encodeURIComponent(data.filename)}`;
                    
                    // Determine if it's an image or other file type
                    const isImage = /\.(jpg|jpeg|png|gif|webp|svg|bmp|tiff|ico)$/i.test(data.filename);
                    
                    let fileRef;
                    if (isImage) {
                        // For images, use markdown image syntax
                        fileRef = `![${data.filename}](${fileUrl})`;
                    } else {
                        // For other files, use markdown link syntax
                        fileRef = `[${data.filename}](${fileUrl})`;
                    }
                    
                    contentInput.value = textBefore + fileRef + textAfter;
                    
                    // Set cursor position after the inserted file reference
                    const newCursorPos = cursorPos + fileRef.length;
                    contentInput.selectionStart = contentInput.selectionEnd = newCursorPos;
                    contentInput.focus();
                    
                    // Update preview if in preview mode
                    this.updatePreview();
                    
                    // Reload note to get updated attachments list
                    await this.openNote(this.currentNoteId);
                    
                    // Auto-save
                    this.saveNote(true);
                } else {
                    // Reload note to get updated attachments if we couldn't insert
                    await this.openNote(this.currentNoteId);
                }
            } else {
                const error = await response.json();
                this.showToast(error.detail || 'Failed to upload attachment', 'error');
            }
        } catch (error) {
            console.error('Error uploading attachment:', error);
            this.showToast('Failed to upload attachment', 'error');
        }
    }
    
    async removeAttachment(filename) {
        if (!this.currentNoteId) return;
        
        if (!confirm(`Remove attachment "${filename}"?`)) {
            return;
        }
        
        try {
            const response = await csrfFetch(`/api/notes/${this.currentNoteId}/attachments/${encodeURIComponent(filename)}`, {
                method: 'DELETE'
            });
            
            if (response.ok) {
                this.showToast('Attachment removed');
                // Reload note to refresh attachments
                await this.openNote(this.currentNoteId);
            } else {
                const error = await response.json();
                this.showToast(error.detail || 'Failed to remove attachment', 'error');
            }
        } catch (error) {
            console.error('Error removing attachment:', error);
            this.showToast('Failed to remove attachment', 'error');
        }
    }
    
    showToast(message, type = 'success') {
        // Simple toast notification
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.textContent = message;
        toast.style.cssText = 'position: fixed; top: 20px; right: 20px; padding: 12px 20px; background: #2a2a3e; border: 1px solid #3a3a4e; border-radius: 8px; color: #e0e0e0; z-index: 10000; box-shadow: 0 4px 12px rgba(0,0,0,0.3);';
        document.body.appendChild(toast);
        setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transition = 'opacity 0.3s';
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    }
    
    showAttachmentDialog() {
        // Prevent multiple simultaneous calls
        if (this._showingAttachmentDialog) {
            return;
        }
        this._showingAttachmentDialog = true;
        
        const fileInput = document.getElementById('noteFileInput');
        if (!fileInput) {
            this.showToast('File input not found', 'error');
            this._showingAttachmentDialog = false;
            return;
        }
        
        // Check if we have a note open
        if (!this.currentNoteId) {
            // Create a new note first (sets up UI)
            this.createNote();
            // Save the note to get an ID, then show file picker
            this.saveNote().then(() => {
                // Wait a moment for the note to be fully set up
                setTimeout(() => {
                    if (this.currentNoteId) {
                        fileInput.click();
                    } else {
                        this.showToast('Please create or open a note first', 'error');
                    }
                    this._showingAttachmentDialog = false;
                }, 100);
            }).catch((error) => {
                console.error('Error creating note for attachment:', error);
                this.showToast('Failed to create note', 'error');
                this._showingAttachmentDialog = false;
            });
        } else {
            // Note exists, show file picker
            fileInput.click();
            // Reset flag after a short delay (file dialog opens asynchronously)
            setTimeout(() => {
                this._showingAttachmentDialog = false;
            }, 100);
        }
    }
    
    async handleFileSelect(e) {
        // Prevent duplicate processing
        if (this._processingFileSelect) {
            return;
        }
        this._processingFileSelect = true;
        
        const files = e.target.files;
        if (!files || files.length === 0) {
            // Reset the file input so the same file can be selected again
            e.target.value = '';
            this._processingFileSelect = false;
            return;
        }
        
        if (!this.currentNoteId) {
            this.showToast('Please create or open a note first', 'error');
            e.target.value = '';
            this._processingFileSelect = false;
            return;
        }
        
        try {
            // Upload each file
            for (let i = 0; i < files.length; i++) {
                const file = files[i];
                await this.uploadAttachment(file);
            }
        } finally {
            // Clear the input so the same file can be selected again
            e.target.value = '';
            this._processingFileSelect = false;
        }
    }
    
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// Expose notesManager globally for notes browser
window.notesManager = null;

// Initialize notes manager (called when needed)
function ensureNotesManager() {
    if (!window.notesManager) {
        window.notesManager = new NotesManager();
    }
    return window.notesManager;
}

// Initialize notes modal
function initNotesModal() {
    const notesModal = document.getElementById('notesModal');
    const closeBtn = document.getElementById('closeNotesModal');
    
    if (!notesModal) {
        return;
    }
    
    if (closeBtn) {
        closeBtn.addEventListener('click', () => {
            notesModal.style.display = 'none';
        });
    }
    
    notesModal.addEventListener('click', (e) => {
        if (e.target === notesModal) {
            notesModal.style.display = 'none';
        }
    });
    
    // Initialize notes manager when modal opens
    const observer = new MutationObserver((mutations) => {
        mutations.forEach((mutation) => {
            if (mutation.type === 'attributes' && mutation.attributeName === 'style') {
                if (notesModal.style.display === 'flex' || notesModal.style.display === 'block') {
                    const manager = ensureNotesManager();
                    manager.loadFolders();
                    manager.loadNotes();
                    // Ensure Edit/Preview buttons are wired up (in case they weren't available during init)
                    manager.attachModeButtons();
                }
            }
        });
    });
    observer.observe(notesModal, { attributes: true, attributeFilter: ['style'] });
    
    window.openNotesModal = function() {
        notesModal.style.display = 'flex';
        // Ensure manager is ready immediately
        const manager = ensureNotesManager();
        manager.attachModeButtons();
    };
}

// Initialize on page load
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initNotesModal);
} else {
    initNotesModal();
}
