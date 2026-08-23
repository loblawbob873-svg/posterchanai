package place.poster.app.sms;

import com.android.mms.transaction.PushReceiver;

/**
 * PICTURE-MESSAGE NOTIFICATIONS ARRIVE HERE and are downloaded through the carrier MMS service.
 *
 * Android will not let an app hold the SMS role at all unless it declares a WAP_PUSH_DELIVER
 * receiver, so this class has to exist. What it does NOT do is pretend.
 *
 * PushReceiver parses M-Notification.ind, persists the pending provider row, asks the subscription's
 * SmsManager to download over the MMS APN, and routes completion to MmsDownloadedReceiver. Keeping
 * this class as the manifest-facing name means upgrades do not temporarily leave Android pointing
 * at a removed role component.
 */
public class MmsDeliverReceiver extends PushReceiver { }
