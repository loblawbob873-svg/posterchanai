#!/bin/bash
# Restore Original Timestamps from EXIF Data
# This fixes the issue where rsync overwrites original file dates

USER_DIR="/var/lib/posterchanai/verita84@poster.place/Pictures"

echo "=========================================="
echo "Restoring Original Timestamps from EXIF"
echo "=========================================="
echo "Target directory: $USER_DIR"
echo ""

# Check if exiftool is installed
if ! command -v exiftool &> /dev/null; then
    echo "ERROR: exiftool is not installed!"
    echo "Install with: sudo pacman -S perl-image-exiftool"
    exit 1
fi

# Check if directory exists
if [ ! -d "$USER_DIR" ]; then
    echo "ERROR: Directory does not exist: $USER_DIR"
    exit 1
fi

echo "Starting timestamp restoration..."
echo "This may take several minutes for large collections..."
echo ""

# Counter for statistics
total_files=0
updated_files=0
failed_files=0

# Process images (JPEG, PNG, HEIC, etc.)
echo "Processing images..."
while IFS= read -r -d '' file; do
    ((total_files++))
    
    # Get original date from EXIF
    date_original=$(exiftool -s -s -s -DateTimeOriginal "$file" 2>/dev/null)
    
    if [ -n "$date_original" ]; then
        # Convert EXIF date to Unix timestamp and set file modification time
        if touch -d "$date_original" "$file" 2>/dev/null; then
            ((updated_files++))
            if [ $((updated_files % 100)) -eq 0 ]; then
                echo "  Processed $updated_files files..."
            fi
        else
            ((failed_files++))
        fi
    else
        ((failed_files++))
    fi
done < <(find "$USER_DIR" -type f \( -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" -o -iname "*.heic" -o -iname "*.heif" \) -print0)

# Process videos (MOV, MP4, etc.)
echo "Processing videos..."
while IFS= read -r -d '' file; do
    ((total_files++))
    
    # Try multiple date tags for videos
    date_original=$(exiftool -s -s -s -CreationDate "$file" 2>/dev/null)
    if [ -z "$date_original" ]; then
        date_original=$(exiftool -s -s -s -CreateDate "$file" 2>/dev/null)
    fi
    if [ -z "$date_original" ]; then
        date_original=$(exiftool -s -s -s -DateTimeOriginal "$file" 2>/dev/null)
    fi
    
    if [ -n "$date_original" ]; then
        # Convert to format touch understands
        formatted_date=$(echo "$date_original" | sed 's/:/-/; s/:/-/')
        if touch -d "$formatted_date" "$file" 2>/dev/null; then
            ((updated_files++))
            if [ $((updated_files % 100)) -eq 0 ]; then
                echo "  Processed $updated_files files..."
            fi
        else
            ((failed_files++))
        fi
    else
        ((failed_files++))
    fi
done < <(find "$USER_DIR" -type f \( -iname "*.mov" -o -iname "*.mp4" -o -iname "*.m4v" -o -iname "*.avi" \) -print0)

echo ""
echo "=========================================="
echo "Timestamp Restoration Complete!"
echo "=========================================="
echo "Total files processed: $total_files"
echo "Successfully updated: $updated_files"
echo "Failed/No EXIF data: $failed_files"
echo ""
echo "Next steps:"
echo "1. Restart posterchanai to refresh the file cache"
echo "2. Hard refresh your browser (Ctrl+Shift+R)"
echo "3. Your photos/videos should now be sorted by original capture date!"
