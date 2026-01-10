/**
 * FileUploader - Handles file uploads, paste, and preview functionality
 * Can be used standalone or integrated into ChatHandler
 */
class FileUploader {
    constructor(options = {}) {
        this.fileInput = options.fileInput || document.getElementById('fileInput');
        this.cameraInput = options.cameraInput || document.getElementById('cameraInput');
        this.uploadPreview = options.uploadPreview || document.getElementById('uploadPreview');
        this.imagePreview = options.imagePreview || document.getElementById('imagePreview');
        this.filePreview = options.filePreview || document.getElementById('filePreview');
        this.removeUpload = options.removeUpload || document.getElementById('removeUpload');

        // Callbacks
        this.onUpload = options.onUpload || (() => {});
        this.onClear = options.onClear || (() => {});

        // Upload data
        this.uploadedImage = null;
        this.uploadedFile = null;
        this.uploadedPDF = null;
        this.uploadedDocument = null;

        this.init();
    }

    init() {
        if (this.fileInput) {
            this.fileInput.addEventListener('change', (e) => this.handleFileSelect(e));
        }
        if (this.cameraInput) {
            this.cameraInput.addEventListener('change', (e) => this.handleFileSelect(e));
        }
        if (this.removeUpload) {
            this.removeUpload.addEventListener('click', () => this.clear());
        }
    }

    /**
     * Handle paste event for images
     */
    handlePaste(e) {
        const items = e.clipboardData?.items;
        if (!items) return false;

        for (const item of items) {
            if (item.type.startsWith('image/')) {
                e.preventDefault();
                const blob = item.getAsFile();
                this.processImageBlob(blob);
                return true;
            }
        }
        return false;
    }

    /**
     * Handle file selection from input
     */
    handleFileSelect(e) {
        const file = e.target.files[0];
        if (!file) return;

        const fileName = file.name.toLowerCase();

        // Image files
        if (file.type.startsWith('image/') || fileName.endsWith('.heic') || fileName.endsWith('.heif')) {
            this.processImageFile(file);
        }
        // PDF files
        else if (file.type === 'application/pdf' || fileName.endsWith('.pdf')) {
            this.processPDF(file);
        }
        // Office documents
        else if (this.isOfficeFile(fileName)) {
            this.processOfficeDocument(file);
        }
        // Text/code files
        else {
            this.processTextFile(file);
        }
    }

    /**
     * Process image blob (from paste)
     */
    processImageBlob(blob) {
        const reader = new FileReader();
        reader.onload = (e) => {
            this.uploadedImage = e.target.result;
            this.showImagePreview(e.target.result, blob.name || 'Pasted image');
            this.onUpload({ type: 'image', data: this.uploadedImage });
        };
        reader.readAsDataURL(blob);
    }

    /**
     * Process image file
     */
    processImageFile(file) {
        const reader = new FileReader();
        reader.onload = (e) => {
            this.uploadedImage = e.target.result;
            this.showImagePreview(e.target.result, file.name);
            this.onUpload({ type: 'image', data: this.uploadedImage, name: file.name });
        };
        reader.readAsDataURL(file);
    }

    /**
     * Process PDF file
     */
    processPDF(file) {
        const reader = new FileReader();
        reader.onload = (e) => {
            this.uploadedPDF = e.target.result;
            this.showFilePreview(`📄 ${file.name} (${this.formatFileSize(file.size)})`);
            this.onUpload({ type: 'pdf', data: this.uploadedPDF, name: file.name });
        };
        reader.readAsDataURL(file);
    }

    /**
     * Process Office document
     */
    processOfficeDocument(file) {
        const reader = new FileReader();
        reader.onload = (e) => {
            this.uploadedDocument = e.target.result;
            const icon = this.getDocumentIcon(file.name);
            this.showFilePreview(`${icon} ${file.name} (${this.formatFileSize(file.size)})`);
            this.onUpload({ type: 'document', data: this.uploadedDocument, name: file.name });
        };
        reader.readAsDataURL(file);
    }

    /**
     * Process text/code file
     */
    processTextFile(file) {
        const reader = new FileReader();
        reader.onload = (e) => {
            this.uploadedFile = e.target.result;
            this.showFilePreview(`📎 ${file.name} (${this.formatFileSize(file.size)})`);
            this.onUpload({ type: 'text', data: this.uploadedFile, name: file.name });
        };
        reader.readAsText(file);
    }

    /**
     * Show image preview
     */
    showImagePreview(dataUrl, name) {
        if (this.imagePreview) {
            this.imagePreview.src = dataUrl;
            this.imagePreview.style.display = 'block';
        }
        if (this.filePreview) {
            this.filePreview.textContent = name;
            this.filePreview.style.display = 'block';
        }
        if (this.uploadPreview) {
            this.uploadPreview.style.display = 'flex';
        }
    }

    /**
     * Show file preview (non-image)
     */
    showFilePreview(text) {
        if (this.imagePreview) {
            this.imagePreview.style.display = 'none';
        }
        if (this.filePreview) {
            this.filePreview.textContent = text;
            this.filePreview.style.display = 'block';
        }
        if (this.uploadPreview) {
            this.uploadPreview.style.display = 'flex';
        }
    }

    /**
     * Clear all uploaded data
     */
    clear() {
        this.uploadedImage = null;
        this.uploadedFile = null;
        this.uploadedPDF = null;
        this.uploadedDocument = null;

        if (this.uploadPreview) {
            this.uploadPreview.style.display = 'none';
        }
        if (this.imagePreview) {
            this.imagePreview.src = '';
            this.imagePreview.style.display = 'none';
        }
        if (this.filePreview) {
            this.filePreview.textContent = '';
        }
        if (this.fileInput) {
            this.fileInput.value = '';
        }
        if (this.cameraInput) {
            this.cameraInput.value = '';
        }

        this.onClear();
    }

    /**
     * Get current upload data
     */
    getData() {
        return {
            image: this.uploadedImage,
            file: this.uploadedFile,
            pdf: this.uploadedPDF,
            document: this.uploadedDocument
        };
    }

    /**
     * Check if any file is uploaded
     */
    hasUpload() {
        return !!(this.uploadedImage || this.uploadedFile || this.uploadedPDF || this.uploadedDocument);
    }

    /**
     * Format file size for display
     */
    formatFileSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    }

    /**
     * Check if file is an Office document
     */
    isOfficeFile(filename) {
        const officeExtensions = ['.docx', '.xlsx', '.pptx', '.doc', '.xls', '.ppt'];
        return officeExtensions.some(ext => filename.toLowerCase().endsWith(ext));
    }

    /**
     * Get appropriate icon for document type
     */
    getDocumentIcon(filename) {
        const lower = filename.toLowerCase();
        if (lower.endsWith('.docx') || lower.endsWith('.doc')) return '📝';
        if (lower.endsWith('.xlsx') || lower.endsWith('.xls')) return '📊';
        if (lower.endsWith('.pptx') || lower.endsWith('.ppt')) return '📽️';
        return '📄';
    }
}

// Export for module systems or make available globally
if (typeof module !== 'undefined' && module.exports) {
    module.exports = FileUploader;
} else {
    window.FileUploader = FileUploader;
}
