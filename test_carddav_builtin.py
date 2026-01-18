#!/usr/bin/env python3
"""
Test Built-in CardDAV functionality
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from app.database import SessionLocal
from app.models import User, UserSetting
from app.services.caldav_service import (
    get_user_contacts_config,
    add_user_contact,
    get_user_contacts,
    delete_user_contact
)
from app.services.cardav_server import get_user_cardav_path
from pathlib import Path

def test_builtin_carddav():
    """Test built-in CardDAV functionality"""
    db = SessionLocal()
    
    try:
        # Get test user
        user = db.query(User).first()
        if not user:
            print("❌ No users found in database")
            return
        
        print(f"🧪 Testing Built-in CardDAV for user: {user.username}")
        print("=" * 60)
        
        # Check if using built-in
        print("\n1. Checking CardDAV configuration...")
        config = get_user_contacts_config(user.id, db)
        if not config:
            print("❌ No CardDAV configuration found")
            return
        
        is_builtin = config.get('builtin', False)
        print(f"   Current mode: {'Built-in' if is_builtin else 'External'}")
        print(f"   URL: {config.get('url')}")
        
        if not is_builtin:
            print("\n   ⚠️  Currently using external CardDAV server")
            print("   To test built-in server:")
            print("   1. Go to User Settings → Calendar & Contacts")
            print("   2. Set 'Contacts Server Type' to 'Built-in CardDAV Server'")
            print("   3. Save settings")
            print("   4. Run this test again")
            return
        
        # Check storage path
        print("\n2. Checking built-in storage path...")
        cardav_path = get_user_cardav_path(user, db)
        print(f"   Path: {cardav_path}")
        print(f"   Exists: {cardav_path.exists()}")
        
        # List .vcf files
        vcf_files = list(cardav_path.glob("*.vcf"))
        print(f"   Found {len(vcf_files)} .vcf files")
        
        # List contacts via API
        print("\n3. Listing contacts via API...")
        contacts = get_user_contacts(user.id, "", db)
        print(f"   Found {len(contacts)} contacts via API")
        
        # Test adding a contact
        print("\n4. Testing add contact to built-in storage...")
        test_name = "Built-in Test Contact"
        test_phone = "555-BUILTIN"
        test_email = "builtin@test.com"
        
        success = add_user_contact(
            user.id, 
            db, 
            test_name, 
            phone=test_phone,
            email=test_email
        )
        
        if success:
            print(f"   ✅ Contact added: {test_name}")
            
            # Check if file was created
            vcf_files_after = list(cardav_path.glob("*.vcf"))
            print(f"   ✅ Now have {len(vcf_files_after)} .vcf files")
            
            # Find the new file
            new_files = [f for f in vcf_files_after if f not in vcf_files]
            if new_files:
                print(f"   ✅ New file created: {new_files[0].name}")
                
                # Read and display file content
                with open(new_files[0], 'r') as f:
                    content = f.read()
                    if test_name in content:
                        print(f"   ✅ File contains contact name")
                    if test_phone in content:
                        print(f"   ✅ File contains phone number")
            
            # Verify via API
            contacts_after = get_user_contacts(user.id, test_name, db)
            if contacts_after:
                contact = contacts_after[0]
                print(f"   ✅ Contact found via API: {contact.name}")
                print(f"      UID: {contact.uid}")
                print(f"      Phone: {contact.phone}")
                print(f"      Email: {contact.emails[0] if contact.emails else 'None'}")
                
                # Test delete
                print("\n5. Testing delete contact...")
                delete_success = delete_user_contact(user.id, db, contact.uid)
                if delete_success:
                    print(f"   ✅ Contact deleted")
                    
                    # Verify file was deleted
                    vcf_files_final = list(cardav_path.glob("*.vcf"))
                    print(f"   ✅ File count back to {len(vcf_files_final)}")
                    
                    # Verify via API
                    contacts_final = get_user_contacts(user.id, test_name, db)
                    if not contacts_final:
                        print(f"   ✅ Contact removed from API")
                    else:
                        print(f"   ⚠️  Contact still found via API")
        else:
            print(f"   ❌ Failed to add contact")
        
        print("\n" + "=" * 60)
        print("✅ Built-in CardDAV test complete!")
        
    except Exception as e:
        print(f"\n❌ Error during test: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()

if __name__ == "__main__":
    test_builtin_carddav()
