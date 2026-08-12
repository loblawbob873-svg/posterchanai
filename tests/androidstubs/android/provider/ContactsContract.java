package android.provider;

import android.net.Uri;

/**
 * Only the columns and constants the contacts sync actually reads and writes. Column NAMES are the
 * real ones — a stub that renamed one would hide the exact mistake this is here to catch.
 */
public final class ContactsContract {
  public static final String AUTHORITY = "com.android.contacts";
  public static final String CALLER_IS_SYNCADAPTER = "caller_is_syncadapter";

  public static final class RawContacts {
    public static final Uri CONTENT_URI = null;
    public static final String _ID = "_id";
    public static final String CONTACT_ID = "contact_id";
    public static final String ACCOUNT_NAME = "account_name";
    public static final String ACCOUNT_TYPE = "account_type";
    public static final String SOURCE_ID = "sourceid";
    public static final String DIRTY = "dirty";
    public static final String DELETED = "deleted";
    public static final String VERSION = "version";
  }

  public static final class Contacts {
    public static final Uri CONTENT_URI = null;
    public static final String _ID = "_id";
    public static final String CONTACT_LAST_UPDATED_TIMESTAMP = "contact_last_updated_timestamp";
  }

  public static final class Data {
    public static final Uri CONTENT_URI = null;
    public static final String RAW_CONTACT_ID = "raw_contact_id";
    public static final String MIMETYPE = "mimetype";
    public static final String DATA1 = "data1";
    public static final String DATA2 = "data2";
    public static final String DATA3 = "data3";
    public static final String DATA4 = "data4";
    public static final String DATA5 = "data5";
    public static final String DATA6 = "data6";
    public static final String DATA7 = "data7";
    public static final String DATA8 = "data8";
    public static final String DATA9 = "data9";
    public static final String DATA10 = "data10";
  }

  public static final class CommonDataKinds {
    public static final class StructuredName {
      public static final String CONTENT_ITEM_TYPE = "vnd.android.cursor.item/name";
      public static final String DISPLAY_NAME = "data1";
      public static final String GIVEN_NAME = "data2";
      public static final String FAMILY_NAME = "data3";
      public static final String PREFIX = "data4";
      public static final String MIDDLE_NAME = "data5";
      public static final String SUFFIX = "data6";
    }

    public static final class Phone {
      public static final String CONTENT_ITEM_TYPE = "vnd.android.cursor.item/phone_v2";
      public static final String NUMBER = "data1";
      public static final String TYPE = "data2";
      public static final String LABEL = "data3";
      public static final int TYPE_HOME = 1;
      public static final int TYPE_MOBILE = 2;
      public static final int TYPE_WORK = 3;
      public static final int TYPE_FAX_WORK = 4;
      public static final int TYPE_FAX_HOME = 5;
      public static final int TYPE_PAGER = 6;
      public static final int TYPE_OTHER = 7;
      public static final int TYPE_CUSTOM = 0;
    }

    public static final class Email {
      public static final String CONTENT_ITEM_TYPE = "vnd.android.cursor.item/email_v2";
      public static final String ADDRESS = "data1";
      public static final String TYPE = "data2";
      public static final String LABEL = "data3";
      public static final int TYPE_HOME = 1;
      public static final int TYPE_WORK = 2;
      public static final int TYPE_OTHER = 3;
      public static final int TYPE_MOBILE = 4;
      public static final int TYPE_CUSTOM = 0;
    }

    public static final class Organization {
      public static final String CONTENT_ITEM_TYPE = "vnd.android.cursor.item/organization";
      public static final String COMPANY = "data1";
      public static final String TYPE = "data2";
      public static final String TITLE = "data4";
      public static final int TYPE_WORK = 1;
    }

    public static final class StructuredPostal {
      public static final String CONTENT_ITEM_TYPE = "vnd.android.cursor.item/postal-address_v2";
      public static final String FORMATTED_ADDRESS = "data1";
      public static final String TYPE = "data2";
      public static final String STREET = "data4";
      public static final String CITY = "data7";
      public static final String REGION = "data8";
      public static final String POSTCODE = "data9";
      public static final String COUNTRY = "data10";
      public static final int TYPE_HOME = 1;
      public static final int TYPE_WORK = 2;
      public static final int TYPE_OTHER = 3;
    }

    public static final class Note {
      public static final String CONTENT_ITEM_TYPE = "vnd.android.cursor.item/note";
      public static final String NOTE = "data1";
    }

    public static final class Event {
      public static final String CONTENT_ITEM_TYPE = "vnd.android.cursor.item/contact_event";
      public static final String START_DATE = "data1";
      public static final String TYPE = "data2";
      public static final int TYPE_ANNIVERSARY = 1;
      public static final int TYPE_OTHER = 2;
      public static final int TYPE_BIRTHDAY = 3;
    }

    public static final class Photo {
      public static final String CONTENT_ITEM_TYPE = "vnd.android.cursor.item/photo";
      public static final String PHOTO = "data15";
    }
  }
}
