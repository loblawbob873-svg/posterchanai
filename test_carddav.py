#!/usr/bin/env python3
"""
Test CardDAV functionality
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from app.database import SessionLocal
from app.models import User, UserSetting, Setting
from app.services.caldav_service import (
    get_user_contacts_config,
    add_user_contact,
    get_user_contacts,
    delete_user_contact,
    edit_user_contact
)
from app.services.cardav_server import get_user_cardav_path
from pathlib import Path

def test_carddav():
    """Test CardDAV functionality"""
    db = SessionLocal()
    
    try:
        # Get test user (or use first user)
        user = db.query(User).first()
        if not user:
            print("❌ No users found in database")
            return
        
        print(f"🧪 Testing CardDAV for user: {user.username}")
        print("=" * 60)
        
        # Check CardDAV configuration
        print("\n1. Checking CardDAV configuration...")
        config = get_user_contacts_config(user.id, db)
        if not config:
            print("❌ No CardDAV configuration found")
            print("   → Go to User Settings → Calendar & Contacts")
            print("   → Set Contacts Server to 'Built-in CardDAV Server'")
            return
        
        print(f"   ✅ Configuration found:")
        print(f"      URL: {config.get('url')}")
        print(f"      Username: {config.get('username')}")
        print(f"      Built-in: {config.get('builtin', False)}")
        
        # Check storage path
        print("\n2. Checking storage path...")
        cardav_path = get_user_cardav_path(user, db)
        print(f"   Path: {cardav_path}")
        print(f"   Exists: {cardav_path.exists()}")
        
        # List existing contacts
        print("\n3. Listing existing contacts...")
        contacts = get_user_contacts(user.id, "", db)
        print(f"   Found {len(contacts)} contacts")
        for i, contact in enumerate(contacts[:5], 1):
            print(f"   {i}. {contact.name}")
            if contact.phone:
                print(f"      Phone: {contact.phone}")
            if contact.emails:
                print(f"      Email: {', '.join(contact.emails)}")
        
        # Test adding a contact
        print("\n4. Testing add contact...")
        test_name = "Test User CardDAV"
        test_phone = "555-TEST-123"
        test_email = "test@carddav.example.com"
        
        success = add_user_contact(
            user.id, 
            db, 
            test_name, 
            phone=test_phone,
            email=test_email
        )
        
        if success:
            print(f"   ✅ Contact added: {test_name}")
            
            # Verify file was created
            vcf_files = list(cardav_path.glob("*.vcf"))
            print(f"   ✅ Found {len(vcf_files)} .vcf files in storage")
            
            # List contacts again
            contacts = get_user_contacts(user.id, "", db)
            test_contact = next((c for c in contacts if c.name == test_name), None)
            if test_contact:
                print(f"   ✅ Contact found in list: {test_contact.name}")
                print(f"      UID: {test_contact.uid}")
                
                # Test search
                print("\n5. Testing search...")
                search_results = get_user_contacts(user.id, "Test", db)
                print(f"   ✅ Search 'Test' found {len(search_results)} contacts")
                
                # Test edit
                print("\n6. Testing edit contact...")
                updates = {"phone": "555-UPDATED"}
                edit_success = edit_user_contact(user.id, db, test_contact.uid, updates)
                if edit_success:
                    print(f"   ✅ Contact updated successfully")
                    
                    # Verify update
                    updated_contacts = get_user_contacts(user.id, test_name, db)
                    if updated_contacts:
                        updated = updated_contacts[0]
                        print(f"   ✅ Verified: Phone is now {updated.phone}")
                
                # Test delete
                print("\n7. Testing delete contact...")
                delete_success = delete_user_contact(user.id, db, test_contact.uid)
                if delete_success:
                    print(f"   ✅ Contact deleted successfully")
                    
                    # Verify deletion
                    remaining = get_user_contacts(user.id, test_name, db)
                    if not remaining:
                        print(f"   ✅ Verified: Contact no longer in list")
                    else:
                        print(f"   ⚠️  Warning: Contact still found after deletion")
            else:
                print(f"   ⚠️  Contact not found in list after adding")
        else:
            print(f"   ❌ Failed to add contact")
            print(f"   → Check CardDAV server is running (port 8082)")
            print(f"   → Check User Settings → Calendar & Contacts")
        
        # Check CardDAV server status
        print("\n8. Checking CardDAV server status...")
        cardav_enabled = db.query(Setting).filter(Setting.key == "cardav_enabled").first()
        if cardav_enabled and cardav_enabled.value == "true":
            print(f"   ✅ CardDAV server is enabled")
            cardav_port = db.query(Setting).filter(Setting.key == "cardav_port").first()
            port = cardav_port.value if cardav_port else "8082"
            print(f"   ✅ CardDAV server port: {port}")
        else:
            print(f"   ⚠️  CardDAV server may not be enabled")
            print(f"   → Go to Admin → Site Settings → Services")
            print(f"   → Enable 'CardDAV Server'")
        
        print("\n" + "=" * 60)
        print("✅ CardDAV test complete!")
        
    except Exception as e:
        print(f"\n❌ Error during test: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()

if __name__ == "__main__":
    test_carddav()
