#!/bin/bash

BACKUP_DIR="/opt/sspl-erp/image-backups"

function list_backups() {
    echo "=============================="
    echo " Available Backups"
    echo "=============================="
    
    if [ ! -d "$BACKUP_DIR" ]; then
        echo "No backup directory found."
        return
    fi
    
    BACKUPS=$(ls -t "$BACKUP_DIR"/backup_*.tar 2>/dev/null)
    
    if [ -z "$BACKUPS" ]; then
        echo "No backups found."
        return
    fi
    
    LATEST=$(cat "$BACKUP_DIR/latest_backup.txt" 2>/dev/null)
    
    echo "$BACKUPS" | while read backup; do
        SIZE=$(du -h "$backup" | cut -f1)
        DATE=$(stat -c %y "$backup" | cut -d' ' -f1,2 | cut -d'.' -f1)
        FILENAME=$(basename "$backup")
        
        if [ "$backup" = "$LATEST" ]; then
            echo "→ $FILENAME (LATEST)"
        else
            echo "  $FILENAME"
        fi
        echo "    Size: $SIZE | Created: $DATE"
    done
    
    echo ""
    TOTAL_SIZE=$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1)
    COUNT=$(ls "$BACKUP_DIR"/backup_*.tar 2>/dev/null | wc -l)
    echo "Total: $COUNT backup(s) | Total size: $TOTAL_SIZE"
}

function clean_old_backups() {
    KEEP=${1:-3}
    
    echo "=============================="
    echo " Cleaning Old Backups"
    echo "=============================="
    echo "Keeping the $KEEP most recent backups..."
    
    if [ ! -d "$BACKUP_DIR" ]; then
        echo "No backup directory found."
        return
    fi
    
    BACKUPS=$(ls -t "$BACKUP_DIR"/backup_*.tar 2>/dev/null)
    TOTAL=$(echo "$BACKUPS" | wc -l)
    
    if [ -z "$BACKUPS" ] || [ "$TOTAL" -le "$KEEP" ]; then
        echo "No backups to clean (found $TOTAL, keeping $KEEP)."
        return
    fi
    
    TO_DELETE=$(echo "$BACKUPS" | tail -n +$((KEEP + 1)))
    DELETE_COUNT=$(echo "$TO_DELETE" | wc -l)
    
    echo "Found $TOTAL backups, will delete $DELETE_COUNT old backup(s):"
    echo ""
    
    echo "$TO_DELETE" | while read backup; do
        SIZE=$(du -h "$backup" | cut -f1)
        DATE=$(stat -c %y "$backup" | cut -d' ' -f1,2 | cut -d'.' -f1)
        echo "  $(basename "$backup") - $SIZE (created: $DATE)"
    done
    
    echo ""
    read -p "Continue with deletion? (yes/no): " CONFIRM
    
    if [ "$CONFIRM" = "yes" ]; then
        echo "$TO_DELETE" | while read backup; do
            rm -f "$backup"
            echo "  ✓ Deleted: $(basename "$backup")"
        done
        echo ""
        echo "✅ Cleanup complete!"
    else
        echo "Cleanup cancelled."
    fi
}

function delete_all_backups() {
    echo "=============================="
    echo " Delete All Backups"
    echo "=============================="
    
    if [ ! -d "$BACKUP_DIR" ]; then
        echo "No backup directory found."
        return
    fi
    
    COUNT=$(ls "$BACKUP_DIR"/backup_*.tar 2>/dev/null | wc -l)
    TOTAL_SIZE=$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1)
    
    if [ "$COUNT" -eq 0 ]; then
        echo "No backups found."
        return
    fi
    
    echo "⚠️  WARNING: This will delete ALL $COUNT backup(s) ($TOTAL_SIZE)"
    echo ""
    read -p "Are you sure? (yes/no): " CONFIRM
    
    if [ "$CONFIRM" = "yes" ]; then
        rm -f "$BACKUP_DIR"/backup_*.tar
        rm -f "$BACKUP_DIR/latest_backup.txt"
        echo "✅ All backups deleted!"
    else
        echo "Deletion cancelled."
    fi
}

# Main menu
case "$1" in
    list|ls)
        list_backups
        ;;
    clean)
        KEEP=${2:-3}
        clean_old_backups "$KEEP"
        ;;
    delete-all)
        delete_all_backups
        ;;
    *)
        echo "SSPL ERP Backup Manager"
        echo ""
        echo "Usage: $0 {list|clean|delete-all}"
        echo ""
        echo "Commands:"
        echo "  list         - List all backups"
        echo "  clean [N]    - Keep N most recent backups, delete older ones (default: 3)"
        echo "  delete-all   - Delete all backups (with confirmation)"
        echo ""
        echo "Examples:"
        echo "  $0 list"
        echo "  $0 clean 5"
        echo "  $0 delete-all"
        exit 1
        ;;
esac
